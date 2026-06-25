"""
MIRA HTTP routes (Flask Blueprint) - /api/mira/*.

Each route:
  1. receives filters (querystring or JSON body),
  2. calls the relevant KPI / query function (reusing dashboard logic),
  3. passes the output through privacyGuardService,
  4. sends the privacy-approved output to the active provider (mock by default),
  5. returns a clean response to the frontend.

Registered once in app.py via `app.register_blueprint(mira_bp)`. No existing
dashboard route is modified.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import time

from flask import Blueprint, current_app, jsonify, request

from . import config
from .core import context as ctx
from .modules.maintenance import assistant_service
from .modules.maintenance import chat_service
from .modules.maintenance import risk_service
from .privacy import privacy_guard_service as guard
from .providers import get_provider, get_provider_status, generate_structured_summary
from .reports import report_draft_service
from .services import kpi_query_service as kpi
from .services import presentation_service as presentation
from .services import asset_report_service as asset_report

mira_bp = Blueprint("mira", __name__, url_prefix="/api/mira")

MIRA_BACKEND_VERSION = "2026.06.08-overview-direct-2"
MIRA_BACKEND_STARTED_AT = datetime.now(timezone.utc).isoformat()
MIRA_SUMMARY_CACHE_TTL_SECONDS = 120
MIRA_OVERVIEW_SUMMARY_TIMEOUT_SECONDS = 10
MIRA_AI_SUMMARY_TIMEOUT_SECONDS = 8
_SUMMARY_CACHE: dict[str, dict] = {}
_SUMMARY_WARMING: set[str] = set()
_PREDICTIVE_WORDING_CACHE: dict[str, dict] = {}
_MIRA_DESCRIPTION_TAGS_PATH = Path(__file__).resolve().parents[2] / "data" / "mira_description_tags.json"

print(f"[MIRA] Backend build {MIRA_BACKEND_VERSION} loaded at {MIRA_BACKEND_STARTED_AT}. Restart Flask after Python API/schema changes.")


def _fast_provider_status(message: str = "AI wording status loads after KPI cards.") -> dict:
    return {"provider": "mira", "model": None, "status": message, "llm": False}


def _provider_status_from_summary(structured: dict | None) -> dict:
    structured = structured or {}
    if structured.get("provider") == "ollama":
        model = structured.get("model")
        return {
            "provider": "ollama",
            "model": model,
            "status": f"Ollama connected ({model})" if model else "Ollama connected",
            "llm": True,
        }
    return {"provider": "mock", "model": None, "status": "Rule-based summary active", "llm": False}


def _coalesce(*values):
    for value in values:
        if value not in (None, "", "all"):
            return value
    return None


def _copy_filter_aliases(source: dict | None, target: dict) -> None:
    """Accept both dashboard snake_case filters and chatbot camelCase fields."""
    if not isinstance(source, dict):
        return

    aliases = {
        "selectedStage": "stage",
        "selectedYear": "year",
        "selectedMonth": "month",
        "selectedDate": "start",
        "selectedStart": "start",
        "selectedEnd": "end",
        "periodMode": "period_mode",
        "selectedPeriodMode": "period_mode",
        "selectedPeriod": "period_mode",
        "selectedAssetId": "assetId",
        "selectedAssetName": "assetName",
        "selectedStatus": "status",
    }
    for key in ctx.FILTER_KEYS:
        if key in source and source[key] not in (None, ""):
            target[key] = source[key]
    for src_key, dest_key in aliases.items():
        if src_key in source and source[src_key] not in (None, ""):
            target[dest_key] = source[src_key]

    selected_period = str(source.get("selectedPeriod") or "").strip().lower()
    if selected_period:
        period_map = {
            "month": "monthly",
            "monthly summary": "monthly",
            "monthly": "monthly",
            "ytd": "ytd",
            "year to date": "ytd",
            "all years": "full_year",
            "all year": "full_year",
            "full year": "full_year",
            "full_year": "full_year",
            "fy": "financial_year",
            "financial year": "financial_year",
            "financial_year": "financial_year",
        }
        if selected_period in period_map:
            target["period_mode"] = period_map[selected_period]
    if source.get("selectedDate"):
        target.setdefault("period_mode", "custom")
        target.setdefault("end", source.get("selectedDate"))


def _read_filters() -> dict:
    """Merge JSON body + querystring into a raw filter dict (body wins)."""
    raw = {}
    for key in ctx.FILTER_KEYS:
        if request.args.get(key) is not None:
            raw[key] = request.args.get(key)
    if request.is_json:
        body = request.get_json(silent=True) or {}
        _copy_filter_aliases(body, raw)
        if "filters" in body and isinstance(body["filters"], dict):
            _copy_filter_aliases(body["filters"], raw)
        if "dashboardContext" in body and isinstance(body["dashboardContext"], dict):
            _copy_filter_aliases(body["dashboardContext"], raw)
            _copy_filter_aliases(body["dashboardContext"].get("filters"), raw)
    return raw


def _normalised_cache_key(filters: dict, include_spare_parts: bool) -> str:
    normalised = ctx.normalize_filters(filters)
    serialisable = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in normalised.items()
    }
    return json.dumps(
        {"include_spare_parts": include_spare_parts, "filters": serialisable},
        sort_keys=True,
        default=str,
    )


def _fallback_dashboard_summary(filters: dict, warning: str) -> dict:
    f = ctx.normalize_filters(filters)
    period = ctx.month_label(f)
    empty_wo = {
        "total": None,
        "open": None,
        "closed": None,
        "rejected": None,
        "closure_rate_pct": None,
        "open_rate_pct": None,
    }
    empty_pm = {
        "total_scheduled": None,
        "completed": None,
        "due_this_month": None,
        "due_soon": None,
        "overdue": None,
        "compliance_pct": None,
        "backlog": None,
        "missing_mapping": None,
        "needs_review": None,
        "completed_manual_only": True,
    }
    empty_downtime = {
        "total_work_orders": None,
        "open_work_orders": None,
        "closed_work_orders": None,
        "rejected_work_orders": None,
        "in_progress_count": None,
        "new_count": None,
        "finished_count": None,
        "confirm_count": None,
        "closure_rate_pct": None,
        "open_rate_pct": None,
        "mttr_hours": None,
        "mtbf_hours": None,
        "carry_over_open_mr": None,
        "opening_backlog_count": None,
        "total_active_workload": None,
        "total_with_backlog_count": None,
        "preventive_count": None,
        "corrective_count": None,
        "top_recorded_asset_name": None,
        "top_actual_machine_asset_name": None,
        "top_functional_location_name": None,
        "severity_mix": [],
        "data_quality_issue_count": None,
        "missing_asset_count": None,
        "general_area_asset_count": None,
        "missing_functional_location_count": None,
        "unknown_status_count": None,
        "selected_work_order_rows_count": None,
    }
    return {
        "window": period,
        "stage": f["stage"],
        "filters": f,
        "period_mode": ctx.resolved_window(f)["mode"],
        "work_orders": empty_wo,
        "mttr_hours": None,
        "mtbf_hours": None,
        "preventive_count": None,
        "corrective_count": None,
        "performance_status": "Data unavailable",
        "data_reliability_issue_count": None,
        "opening_backlog_count": None,
        "total_with_backlog_count": None,
        "pm_schedule": empty_pm,
        "asset_groups": [],
        "stage_breakdown": [],
        "downtime_summary": empty_downtime,
        "data_reliability": {"requires_attention_count": None, "issues": [], "data_notes": [warning]},
        "spare_parts": {"data_notes": [warning]},
        "data_availability": {"complete": False, "warnings": [warning]},
    }


def _safe_int(value):
    try:
        if value in (None, "", "--"):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value):
    try:
        if value in (None, "", "--"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _unique_strings(items) -> list[str]:
    out = []
    seen = set()
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _stage_label(stage: str | None) -> str:
    return {"all": "All stages", "stage1": "Stage 1", "stage2": "Stage 2"}.get(str(stage or "").strip().lower(), stage or "All stages")


def _row_request_id(row: dict) -> str | None:
    text = str(
        row.get("request_id")
        or row.get("maintenance_order_id")
        or row.get("request_no")
        or row.get("mr_id")
        or ""
    ).strip()
    return text or None


def _row_work_order_id(row: dict) -> str | None:
    text = str(row.get("work_order_id") or row.get("wo_id") or "").strip()
    if not text or text == "--":
        return None
    return text


def _row_description_text(row: dict) -> str:
    return str(
        row.get("translated_description")
        or row.get("description")
        or row.get("description_original")
        or row.get("remarks")
        or ""
    ).strip()


def _row_actual_start_dt(row: dict):
    for key in ("actual_start_time", "actual_start", "maintenance_start_time", "start_time"):
        parsed = kpi._parse_mix_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _row_actual_end_dt(row: dict):
    for key in ("actual_end_time", "actual_end", "maintenance_end_time", "end_time"):
        parsed = kpi._parse_mix_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _row_created_dt(row: dict):
    for key in ("request_created_time", "created_date", "start_time"):
        parsed = kpi._parse_mix_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _row_reference_dt(row: dict):
    return _row_actual_start_dt(row) or _row_created_dt(row)


def _row_reference_date(row: dict) -> date | None:
    dt = _row_reference_dt(row)
    return dt.date() if dt is not None else None


def _row_duration_hours(row: dict):
    duration = _safe_float(row.get("duration_hours"))
    if duration is not None and duration > 0:
        return round(duration, 3)
    start_dt = _row_actual_start_dt(row)
    end_dt = _row_actual_end_dt(row)
    if start_dt and end_dt and end_dt > start_dt:
        return round((end_dt - start_dt).total_seconds() / 3600.0, 3)
    return None


def _row_asset_name(row: dict) -> str:
    return kpi._asset_identity(row)[1]


def _row_asset_id(row: dict) -> str | None:
    return kpi._asset_identity(row)[0]


def _row_functional_location(row: dict) -> str:
    return kpi._functional_location_value(row)


def _row_is_open(row: dict) -> bool:
    return kpi._mr_status_bucket(row) == "open"


def _row_is_closed(row: dict) -> bool:
    return kpi._mr_status_bucket(row) == "closed"


def _rank_counts(counter: Counter, *, limit: int = 5, label_key: str = "name", value_key: str = "count") -> list[dict]:
    return [
        {label_key: name, value_key: int(count)}
        for name, count in counter.most_common(limit)
        if str(name or "").strip()
    ]


def _resolve_selected_daily_date(filters: dict, dates: list[date]) -> date | None:
    if filters.get("start") and filters.get("end") and filters.get("start") == filters.get("end"):
        return filters["start"]
    return max(dates) if dates else None


def _metric_available(data: dict) -> bool:
    work_orders = data.get("work_orders") or {}
    downtime = data.get("downtime_summary") or {}
    pm = data.get("pm_schedule") or {}
    spare = data.get("spare_parts") or {}
    return any(
        value is not None
        for value in (
            work_orders.get("total"),
            downtime.get("total_work_orders"),
            pm.get("total_scheduled"),
            spare.get("current_in_stock_items"),
            spare.get("current_in_stock_value"),
        )
    )


def _apply_data_availability(data: dict, warnings: list[str] | None = None) -> dict:
    availability = dict((data.get("data_availability") or {}))
    merged_warnings = _unique_strings((availability.get("warnings") or []) + list(warnings or []))
    availability["warnings"] = merged_warnings
    availability["complete"] = bool(_metric_available(data) or availability.get("complete"))
    data["data_availability"] = availability
    return data


def _log_mira_event(event: str, **fields) -> None:
    payload = {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in (fields or {}).items()
    }
    try:
        current_app.logger.info("[MIRA][%s] %s", event, json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        print(f"[MIRA][{event}] {json.dumps(payload, ensure_ascii=False, default=str)}")


def _collect_mira_debug_snapshot(filters: dict) -> tuple[list[dict], list[dict], dict, dict]:
    normalized = ctx.normalize_filters(filters)
    filtered_rows = kpi._filtered_work_order_rows(normalized)
    selected_rows = kpi._selected_period_work_order_rows(normalized, filtered_rows)
    recent_context = build_recent_description_context(normalized, filtered_rows, selected_rows)
    period_mode = str(normalized.get("period_mode") or "ytd").strip().lower()
    year = str(normalized.get("year") or date.today().year)
    selected_period = {
        "monthly": ctx.month_label(normalized),
        "full_year": f"Full Year {year}",
        "financial_year": f"FY{year}",
        "ytd": f"YTD {year}",
    }.get(period_mode, period_mode or year)
    debug = {
        "selectedPeriod": selected_period,
        "recordCountLoaded": len(filtered_rows),
        "latestMrDateFound": recent_context.get("latestAvailableDate"),
        "selectedPeriodMrCount": len(selected_rows),
        "rolling7DayMrCount": recent_context.get("mrRaised"),
        "descriptionsIncluded": recent_context.get("descriptionsIncluded"),
    }
    return filtered_rows, selected_rows, recent_context, debug


def _build_partial_dashboard_summary(filters: dict, *, include_spare_parts: bool) -> tuple[dict, list[str]]:
    f = ctx.normalize_filters(filters)
    warnings: list[str] = []

    def capture(label: str, builder):
        try:
            return builder()
        except Exception as exc:
            warnings.append(f"{label} unavailable: {exc}")
            return {}

    mr_activity = capture("MR / WO summary", lambda: kpi.get_mr_activity_summary(f))
    mttr = capture("MTTR summary", lambda: kpi.get_mttr(f))
    mtbf = capture("MTBF summary", lambda: kpi.get_mtbf(f))
    dq = capture("Data reliability summary", lambda: kpi.get_data_reliability_issues(f))
    pm = capture("PM schedule summary", lambda: kpi.get_pm_schedule_status(f))
    spare = capture("Spare-parts summary", lambda: kpi.get_spare_parts_summary(f)) if include_spare_parts else {}

    opening_backlog_count = mr_activity.get("carry_over_open_mr")
    total_with_backlog_count = mr_activity.get("total_active_workload")
    data = {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "filters": f,
        "period_mode": ctx.resolved_window(f)["mode"],
        "work_orders": {
            "total": mr_activity.get("mr_raised"),
            "open": mr_activity.get("open_count"),
            "closed": mr_activity.get("closed_count"),
            "rejected": mr_activity.get("rejected_count"),
            "closure_rate_pct": mr_activity.get("closure_rate_pct"),
            "open_rate_pct": mr_activity.get("open_rate_pct"),
        },
        "mttr_hours": mttr.get("overall_mttr_hours"),
        "mtbf_hours": mtbf.get("overall_average_mtbf_hours"),
        "preventive_count": mr_activity.get("preventive_count"),
        "corrective_count": mr_activity.get("corrective_count"),
        "performance_status": mr_activity.get("performance_status"),
        "data_reliability_issue_count": mr_activity.get("data_quality_issue_count"),
        "opening_backlog_count": opening_backlog_count,
        "total_with_backlog_count": total_with_backlog_count,
        "pm_schedule": {
            "total_scheduled": pm.get("total_scheduled"),
            "completed": pm.get("completed"),
            "due_this_month": pm.get("due_this_month"),
            "due_soon": pm.get("due_soon"),
            "overdue": pm.get("overdue"),
            "compliance_pct": pm.get("compliance_pct"),
            "backlog": pm.get("backlog"),
            "missing_mapping": pm.get("missing_mapping"),
            "needs_review": pm.get("needs_review"),
            "completed_manual_only": True,
        },
        "asset_groups": pm.get("by_main_group"),
        "stage_breakdown": pm.get("by_stage"),
        "downtime_summary": {
            "total_work_orders": mr_activity.get("mr_raised"),
            "open_work_orders": mr_activity.get("open_count"),
            "closed_work_orders": mr_activity.get("closed_count"),
            "rejected_work_orders": mr_activity.get("rejected_count"),
            "in_progress_count": mr_activity.get("in_progress_count"),
            "new_count": mr_activity.get("new_count"),
            "finished_count": mr_activity.get("finished_count"),
            "confirm_count": mr_activity.get("confirm_count"),
            "closure_rate_pct": mr_activity.get("closure_rate_pct"),
            "open_rate_pct": mr_activity.get("open_rate_pct"),
            "mttr_hours": mttr.get("overall_mttr_hours"),
            "mtbf_hours": mtbf.get("overall_average_mtbf_hours"),
            "carry_over_open_mr": opening_backlog_count,
            "carry_over_in_progress": mr_activity.get("carry_over_in_progress"),
            "carry_over_new": mr_activity.get("carry_over_new"),
            "opening_backlog_count": opening_backlog_count,
            "total_active_workload": total_with_backlog_count,
            "total_with_backlog_count": total_with_backlog_count,
            "top_recorded_asset_name": mr_activity.get("top_recorded_asset_name"),
            "top_recorded_asset_id": mr_activity.get("top_recorded_asset_id"),
            "top_recorded_asset_count": mr_activity.get("top_recorded_asset_count"),
            "top_recorded_asset_is_placeholder": mr_activity.get("top_recorded_asset_is_placeholder"),
            "top_recorded_asset_reason": mr_activity.get("top_recorded_asset_reason"),
            "top_actual_machine_asset_name": mr_activity.get("top_actual_machine_asset_name"),
            "top_actual_machine_asset_id": mr_activity.get("top_actual_machine_asset_id"),
            "top_actual_machine_asset_count": mr_activity.get("top_actual_machine_asset_count"),
            "top_actual_machine_asset_reason": mr_activity.get("top_actual_machine_asset_reason"),
            "top_functional_location_name": mr_activity.get("top_functional_location_name"),
            "top_functional_location_count": mr_activity.get("top_functional_location_count"),
            "top_functional_location_reason": mr_activity.get("top_functional_location_reason"),
            "focus_asset_name": mr_activity.get("top_recorded_asset_name"),
            "focus_asset_id": mr_activity.get("top_recorded_asset_id"),
            "focus_asset_reason": mr_activity.get("top_recorded_asset_reason"),
            "focus_asset_kind": "area/placeholder" if mr_activity.get("top_recorded_asset_is_placeholder") else "asset",
            "focus_asset_count": mr_activity.get("top_recorded_asset_count"),
            "top_asset_by_mr_count_name": mr_activity.get("top_recorded_asset_name"),
            "top_asset_by_mr_count_id": mr_activity.get("top_recorded_asset_id"),
            "top_asset_by_mr_count": mr_activity.get("top_recorded_asset_count"),
            "top_work_order_machine_group": mr_activity.get("top_functional_location_name"),
            "top_work_order_machine_group_count": mr_activity.get("top_functional_location_count"),
            "worst_asset_name": mr_activity.get("top_actual_machine_asset_name") or mr_activity.get("top_recorded_asset_name"),
            "worst_asset_id": mr_activity.get("top_actual_machine_asset_id") or mr_activity.get("top_recorded_asset_id"),
            "worst_asset_work_order_count": mr_activity.get("top_actual_machine_asset_count") or mr_activity.get("top_recorded_asset_count"),
            "worst_machine_group_name": mr_activity.get("top_functional_location_name"),
            "worst_machine_group_reason": mr_activity.get("top_functional_location_reason"),
            "top_mttr_machine_group": mttr.get("highest_mttr_machine_group"),
            "severity_mix": mr_activity.get("severity_breakdown") or [],
            "preventive_count": mr_activity.get("preventive_count"),
            "corrective_count": mr_activity.get("corrective_count"),
            "preventive_ratio_pct": mr_activity.get("preventive_ratio_pct"),
            "corrective_ratio_pct": mr_activity.get("corrective_ratio_pct"),
            "performance_status": mr_activity.get("performance_status"),
            "wo_created_count": mr_activity.get("work_orders_with_linked_wo"),
            "wo_created_pct": mr_activity.get("wo_created_pct"),
            "data_quality_issue_count": mr_activity.get("data_quality_issue_count"),
            "missing_asset_count": mr_activity.get("missing_asset_count"),
            "general_area_asset_count": mr_activity.get("general_area_asset_count"),
            "general_area_asset_breakdown": mr_activity.get("general_area_asset_breakdown"),
            "missing_functional_location_count": mr_activity.get("missing_functional_location_count"),
            "unknown_status_count": mr_activity.get("unknown_status_count"),
            "selected_work_order_rows_count": mr_activity.get("selected_work_order_rows_count"),
        },
        "data_reliability": dq or {"requires_attention_count": None, "issues": [], "data_notes": []},
        "spare_parts": spare or {"data_notes": []},
    }
    _apply_data_availability(data, warnings)
    return data, warnings


def _build_dashboard_summary_with_timeout(filters: dict, *, include_spare_parts: bool) -> dict:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mira-summary")
    future = executor.submit(
        lambda: kpi.get_dashboard_kpi_summary(filters, include_spare_parts=include_spare_parts)
    )
    try:
        return future.result(timeout=MIRA_OVERVIEW_SUMMARY_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        future.cancel()
        raise TimeoutError(
            f"Verified KPI summary exceeded {MIRA_OVERVIEW_SUMMARY_TIMEOUT_SECONDS}s"
        ) from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _start_summary_warmup(cache_key: str, filters: dict, *, include_spare_parts: bool, delay_seconds: float = 0.75) -> None:
    if cache_key in _SUMMARY_WARMING:
        return
    _SUMMARY_WARMING.add(cache_key)

    def warm() -> None:
        try:
            data = kpi.get_dashboard_kpi_summary(filters, include_spare_parts=include_spare_parts)
            _apply_data_availability(data)
            _SUMMARY_CACHE[cache_key] = {"created_at": time.time(), "data": data, "warnings": []}
        except Exception:
            pass
        finally:
            _SUMMARY_WARMING.discard(cache_key)

    timer = threading.Timer(delay_seconds, warm)
    timer.name = "mira-summary-warmup"
    timer.daemon = True
    timer.start()


def _get_dashboard_summary(
    filters: dict,
    *,
    include_spare_parts: bool = True,
    allow_blocking_build: bool = True,
    start_warmup: bool = True,
) -> tuple[dict, list[str], bool]:
    """Return verified summary with a short cache and graceful fallback."""
    f = ctx.normalize_filters(filters)
    key = _normalised_cache_key(f, include_spare_parts)
    cached = _SUMMARY_CACHE.get(key)
    now = time.time()
    if cached and now - cached.get("created_at", 0) <= MIRA_SUMMARY_CACHE_TTL_SECONDS:
        return cached["data"], list(cached.get("warnings") or []), True

    warnings: list[str] = []
    if not allow_blocking_build:
        warning = "Full verified KPI summary is warming in the background; fast overview fallback is active."
        if start_warmup:
            _start_summary_warmup(key, f, include_spare_parts=include_spare_parts)
        return _fallback_dashboard_summary(f, warning), [warning], False

    try:
        data = _build_dashboard_summary_with_timeout(f, include_spare_parts=include_spare_parts)
        _apply_data_availability(data)
    except Exception as exc:
        warning = f"Verified KPI summary could not be fully loaded: {exc}"
        warnings.append(warning)
        data, partial_warnings = _build_partial_dashboard_summary(f, include_spare_parts=include_spare_parts)
        warnings.extend(partial_warnings)
        _apply_data_availability(data, warnings)

    _SUMMARY_CACHE[key] = {"created_at": now, "data": data, "warnings": warnings}
    return data, warnings, False


def _core_payloads_warm() -> bool:
    """True when the FAST payloads (PM schedule + downtime) are cached.

    Spare parts is deliberately excluded — its cold build (the all-years project-
    transactions parse) can take minutes, so we must NOT make the whole Overview
    wait for it. The page shows PM + downtime KPIs as soon as these two are ready
    (~60s on a cold start) and spare fills in separately once warm."""
    try:
        import pm_schedule_service as _pm
        import downtime_service as _dt
        if not getattr(_pm, "_PM_PAGE_PAYLOAD_CACHE", None):
            return False
        if not getattr(_dt, "_DOWNTIME_CACHE", None):
            return False
        return True
    except Exception:
        return True


def _spare_warm() -> bool:
    """True when the spare-parts payload is cached (separate, slower build)."""
    try:
        import spare_parts_service as _sp
        return bool(getattr(_sp, "_SPARE_PARTS_CACHE", None))
    except Exception:
        return True


def _dashboard_payloads_warm() -> bool:
    """All three payloads warm (PM + downtime + spare). Kept for any caller that
    needs the strict check; the Overview uses _core_payloads_warm instead."""
    return _core_payloads_warm() and _spare_warm()


def _health_routes() -> list[str]:
    try:
        return sorted(
            str(rule.rule)
            for rule in current_app.url_map.iter_rules()
            if str(rule.rule).startswith("/api/mira")
        )
    except Exception:
        return []


def _normalise_chat_mode(value) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"kpi", "kpi_analysis", "kpi analysis"}:
        return "kpi_analysis"
    if text in {"chat", "qa", "q_a"}:
        return "chat"
    return text or None


def _read_selected_kpi_items(body: dict, *names: str) -> list[str]:
    for name in names:
        value = body.get(name)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
    return []


def _read_description_tag_rows(limit: int = 600) -> list[dict]:
    """Best-effort read of persisted MR/WO description tags for issue focus."""
    try:
        raw = json.loads(_MIRA_DESCRIPTION_TAGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    values = raw.values() if isinstance(raw, dict) else raw
    rows = [row for row in values if isinstance(row, dict)]

    def sort_key(row: dict) -> str:
        return str(row.get("classified_at") or "")

    return sorted(rows, key=sort_key, reverse=True)[:limit]


def build_mr_wo_context(filters: dict, rows: list[dict] | None = None, selected_rows: list[dict] | None = None) -> dict:
    normalized = ctx.normalize_filters(filters)
    filtered_rows = rows if rows is not None else kpi._filtered_work_order_rows(normalized)
    scoped_rows = selected_rows if selected_rows is not None else kpi._selected_period_work_order_rows(normalized, filtered_rows)
    activity = kpi.get_mr_activity_summary(normalized)
    carry_over_rows = kpi._opening_backlog_rows(normalized, filtered_rows)
    active_open_rows = [row for row in scoped_rows if _row_is_open(row)] + carry_over_rows
    open_area_counter: Counter[str] = Counter()
    for row in active_open_rows:
        label = _row_asset_name(row) if not kpi._is_general_area_asset(_row_asset_name(row)) else _row_functional_location(row)
        if label:
            open_area_counter[label] += 1
    oldest_open_date = min(
        (kpi._mr_raised_date(row) for row in active_open_rows if kpi._mr_raised_date(row) is not None),
        default=None,
    )
    end_date = ctx.resolved_window(normalized)["end_date"]
    top_open_name, top_open_count = (open_area_counter.most_common(1)[0] if open_area_counter else (None, 0))
    return {
        "raised": activity.get("mr_raised"),
        "new": activity.get("new_count"),
        "inProgress": activity.get("in_progress_count"),
        "finished": activity.get("finished_count"),
        "confirmed": activity.get("confirm_count"),
        "open": activity.get("open_count"),
        "closedConfirmed": activity.get("closed_count"),
        "closureRate": activity.get("closure_rate_pct"),
        "workOrdersCreatedCount": activity.get("work_orders_with_linked_wo"),
        "woCreatedPct": activity.get("wo_created_pct"),
        "carryOverOpen": activity.get("carry_over_open_mr"),
        "totalActiveWorkload": activity.get("total_active_workload"),
        "topRecordedAsset": {
            "name": activity.get("top_recorded_asset_name"),
            "assetId": activity.get("top_recorded_asset_id"),
            "count": activity.get("top_recorded_asset_count"),
            "isPlaceholder": bool(activity.get("top_recorded_asset_is_placeholder")),
        },
        "topActualMachineAsset": {
            "name": activity.get("top_actual_machine_asset_name"),
            "assetId": activity.get("top_actual_machine_asset_id"),
            "count": activity.get("top_actual_machine_asset_count"),
        },
        "topFunctionalLocation": {
            "name": activity.get("top_functional_location_name"),
            "count": activity.get("top_functional_location_count"),
        },
        "topOpenAssetArea": {"name": top_open_name, "count": int(top_open_count or 0)},
        "oldestOpenMrAgeDays": (end_date - oldest_open_date).days if oldest_open_date else None,
        "selectedPeriodRows": len(scoped_rows),
        "totalRowsLoaded": len(filtered_rows),
        "carryOverRows": len(carry_over_rows),
    }


def build_recent_description_context(filters: dict, rows: list[dict] | None = None, selected_rows: list[dict] | None = None) -> dict:
    normalized = ctx.normalize_filters(filters)
    filtered_rows = rows if rows is not None else kpi._filtered_work_order_rows(normalized)
    scoped_rows = selected_rows if selected_rows is not None else kpi._selected_period_work_order_rows(normalized, filtered_rows)
    base_rows = scoped_rows or filtered_rows
    dated_rows = [(row, _row_reference_date(row)) for row in base_rows]
    available_dates = [stamp for _row, stamp in dated_rows if stamp is not None]
    latest_available = max(available_dates) if available_dates else None
    selected_daily_date = _resolve_selected_daily_date(normalized, available_dates)
    if not selected_daily_date and latest_available:
        selected_daily_date = latest_available
    if not selected_daily_date:
        return {
            "selectedDate": None,
            "latestAvailableDate": None,
            "rollingWindowStart": None,
            "rollingWindowEnd": None,
            "days": 7,
            "mrRaised": 0,
            "open": 0,
            "latestDayMrCount": 0,
            "topAssets": [],
            "topFunctionalLocations": [],
            "descriptionExamples": [],
            "descriptionsIncluded": 0,
            "rowsLoaded": len(base_rows),
            "_rolling_rows": [],
            "_latest_rows": [],
        }

    rolling_start = selected_daily_date - timedelta(days=6)
    rolling_rows = [row for row, stamp in dated_rows if stamp is not None and rolling_start <= stamp <= selected_daily_date]
    latest_rows = [row for row, stamp in dated_rows if stamp == selected_daily_date]
    description_rows = [row for row in rolling_rows if _row_description_text(row)]
    asset_counts = Counter(_row_asset_name(row) for row in rolling_rows if _row_asset_name(row))
    location_counts = Counter(_row_functional_location(row) for row in rolling_rows if _row_functional_location(row))
    open_count = sum(1 for row in rolling_rows if _row_is_open(row))
    examples = _unique_strings(_row_description_text(row)[:120] for row in description_rows[:6])
    return {
        "selectedDate": selected_daily_date.isoformat(),
        "latestAvailableDate": latest_available.isoformat() if latest_available else None,
        "rollingWindowStart": rolling_start.isoformat(),
        "rollingWindowEnd": selected_daily_date.isoformat(),
        "days": 7,
        "mrRaised": len(rolling_rows),
        "open": open_count,
        "latestDayMrCount": len(latest_rows),
        "topAssets": _rank_counts(asset_counts, limit=5),
        "topFunctionalLocations": _rank_counts(location_counts, limit=5, label_key="functionalLocation", value_key="mrCount"),
        "descriptionExamples": examples[:4],
        "descriptionsIncluded": len(description_rows),
        "rowsLoaded": len(base_rows),
        "_rolling_rows": rolling_rows,
        "_latest_rows": latest_rows,
    }


def build_data_quality_context(filters: dict, rows: list[dict] | None = None, selected_rows: list[dict] | None = None) -> dict:
    normalized = ctx.normalize_filters(filters)
    filtered_rows = rows if rows is not None else kpi._filtered_work_order_rows(normalized)
    scoped_rows = selected_rows if selected_rows is not None else kpi._selected_period_work_order_rows(normalized, filtered_rows)
    request_counts: Counter[str] = Counter()
    missing_actual_start = 0
    missing_actual_end = 0
    end_before_start = 0
    missing_asset_or_name = 0
    missing_work_order_number = 0
    missing_description = 0
    rows_with_any_issue = 0
    for row in scoped_rows:
        has_issue = False
        request_id = _row_request_id(row)
        if request_id:
            request_counts[request_id] += 1
        start_dt = _row_actual_start_dt(row)
        end_dt = _row_actual_end_dt(row)
        if start_dt is None:
            missing_actual_start += 1
            has_issue = True
        if _row_is_closed(row) and end_dt is None:
            missing_actual_end += 1
            has_issue = True
        if start_dt is not None and end_dt is not None and end_dt < start_dt:
            end_before_start += 1
            has_issue = True
        if not (_row_asset_id(row) or _row_asset_name(row)):
            missing_asset_or_name += 1
            has_issue = True
        if not _row_work_order_id(row):
            missing_work_order_number += 1
            has_issue = True
        if len(_row_description_text(row)) < 6:
            missing_description += 1
            has_issue = True
        if has_issue:
            rows_with_any_issue += 1
    duplicate_request_numbers = sum(count - 1 for count in request_counts.values() if count > 1)
    return {
        "rowsWithIssues": rows_with_any_issue,
        "missingActualStart": missing_actual_start,
        "missingActualEndForClosed": missing_actual_end,
        "actualEndBeforeActualStart": end_before_start,
        "missingAssetOrName": missing_asset_or_name,
        "missingWorkOrderNumber": missing_work_order_number,
        "duplicateMrNumbers": duplicate_request_numbers,
        "missingOrShortDescriptions": missing_description,
        "rowsLoaded": len(scoped_rows),
    }


def build_mttr_context(filters: dict, rows: list[dict] | None = None, selected_rows: list[dict] | None = None, recent_context: dict | None = None) -> dict:
    normalized = ctx.normalize_filters(filters)
    filtered_rows = rows if rows is not None else kpi._filtered_work_order_rows(normalized)
    scoped_rows = selected_rows if selected_rows is not None else kpi._selected_period_work_order_rows(normalized, filtered_rows)
    rolling_rows = list((recent_context or {}).get("_rolling_rows") or [])

    def collect_duration_map(source_rows):
        durations = []
        by_asset = defaultdict(list)
        for row in source_rows:
            if not _row_is_closed(row):
                continue
            hours = _row_duration_hours(row)
            if hours is None or hours <= 0:
                continue
            durations.append(hours)
            by_asset[_row_asset_name(row)].append(hours)
        return durations, by_asset

    overall_durations, baseline_by_asset = collect_duration_map(scoped_rows)
    recent_durations, recent_by_asset = collect_duration_map(rolling_rows)
    increasing_assets = []
    for asset_name, recent_values in recent_by_asset.items():
        baseline_values = baseline_by_asset.get(asset_name) or []
        if len(recent_values) < 2 or len(baseline_values) < 2:
            continue
        recent_avg = round(sum(recent_values) / len(recent_values), 2)
        baseline_avg = round(sum(baseline_values) / len(baseline_values), 2)
        if baseline_avg > 0 and recent_avg >= baseline_avg * 1.15:
            confidence = "High" if len(recent_values) >= 3 and len(baseline_values) >= 3 else "Medium"
            increasing_assets.append({
                "asset": asset_name,
                "recentMttrHours": recent_avg,
                "baselineMttrHours": baseline_avg,
                "recentCount": len(recent_values),
                "baselineCount": len(baseline_values),
                "confidence": confidence,
            })
    increasing_assets.sort(key=lambda item: (-(item["recentMttrHours"] - item["baselineMttrHours"]), item["asset"]))
    return {
        "overallHours": round(sum(overall_durations) / len(overall_durations), 2) if overall_durations else None,
        "recentWindowHours": round(sum(recent_durations) / len(recent_durations), 2) if recent_durations else None,
        "validClosedRecords": len(overall_durations),
        "recentValidClosedRecords": len(recent_durations),
        "increasingAssets": increasing_assets[:5],
        "dataNotes": ["MTTR is calculated from valid closed MR with positive Actual End - Actual Start duration."] if overall_durations else ["MTTR could not be calculated from the selected MR rows."],
    }


def build_mtbf_context(filters: dict, rows: list[dict] | None = None, selected_rows: list[dict] | None = None, recent_context: dict | None = None) -> dict:
    normalized = ctx.normalize_filters(filters)
    filtered_rows = rows if rows is not None else kpi._filtered_work_order_rows(normalized)
    scoped_rows = selected_rows if selected_rows is not None else kpi._selected_period_work_order_rows(normalized, filtered_rows)
    analysis_rows = scoped_rows if len(scoped_rows) >= 4 else filtered_rows
    by_asset = defaultdict(list)
    for row in analysis_rows:
        if not _row_is_closed(row):
            continue
        start_dt = _row_actual_start_dt(row)
        end_dt = _row_actual_end_dt(row)
        asset_name = _row_asset_name(row)
        if not start_dt or not end_dt or end_dt <= start_dt or not asset_name:
            continue
        by_asset[asset_name].append((start_dt, end_dt))

    all_gaps = []
    decreasing_assets = []
    for asset_name, events in by_asset.items():
        events.sort(key=lambda item: item[0])
        gaps = []
        for previous, current in zip(events, events[1:]):
            gap_hours = (current[0] - previous[1]).total_seconds() / 3600.0
            if gap_hours > 0:
                gaps.append(round(gap_hours, 2))
        if gaps:
            all_gaps.extend(gaps)
        if len(gaps) >= 2:
            latest_gap = gaps[-1]
            baseline_gap = round(sum(gaps[:-1]) / len(gaps[:-1]), 2)
            if baseline_gap > 0 and latest_gap <= baseline_gap * 0.8:
                confidence = "High" if len(gaps) >= 3 else "Medium"
                decreasing_assets.append({
                    "asset": asset_name,
                    "latestMtbfHours": latest_gap,
                    "baselineMtbfHours": baseline_gap,
                    "gapCount": len(gaps),
                    "confidence": confidence,
                })
    decreasing_assets.sort(key=lambda item: (item["latestMtbfHours"] - item["baselineMtbfHours"], item["asset"]))
    return {
        "overallHours": round(sum(all_gaps) / len(all_gaps), 2) if all_gaps else None,
        "assetsWithValidMtbf": sum(1 for _asset, events in by_asset.items() if len(events) >= 2),
        "decreasingAssets": decreasing_assets[:5],
        "dataNotes": ["MTBF uses the gap between the previous closed MR Actual End and the next MR Actual Start for the same asset."] if all_gaps else ["MTBF could not be calculated because repeated closed MR with valid dates were limited."],
    }


def build_issue_theme_context(filters: dict, recent_context: dict | None, mttr_context: dict | None = None, mtbf_context: dict | None = None) -> dict:
    rolling_rows = list((recent_context or {}).get("_rolling_rows") or [])
    latest_rows = {(id(row)) for row in ((recent_context or {}).get("_latest_rows") or [])}
    unknown_theme = getattr(chat_service, "_UNKNOWN_THEME", "Unknown / Insufficient Information")
    if not rolling_rows:
        return {
            "top_issues": [],
            "issue_categories": [],
            "trending_patterns": [],
            "description_themes": [],
            "data_notes": ["No recent MR descriptions were available for rolling 7-day issue focus detection."],
            "source": "Live MR/WO descriptions",
            "rows_loaded": 0,
            "rows_used": 0,
        }

    mttr_assets = {str(item.get("asset") or "").strip() for item in (mttr_context or {}).get("increasingAssets") or []}
    mtbf_assets = {str(item.get("asset") or "").strip() for item in (mtbf_context or {}).get("decreasingAssets") or []}
    theme_buckets: dict[str, dict] = {}
    unknown_count = 0
    for row in rolling_rows:
        desc = _row_description_text(row)
        if not desc:
            continue
        theme = chat_service.classify_theme(desc)
        if theme == unknown_theme:
            unknown_count += 1
            continue
        bucket = theme_buckets.setdefault(theme, {
            "count": 0,
            "openCount": 0,
            "latestDayCount": 0,
            "missingWorkOrderCount": 0,
            "criticalCount": 0,
            "examples": [],
            "assetCounts": Counter(),
            "locationCounts": Counter(),
        })
        bucket["count"] += 1
        bucket["openCount"] += 1 if _row_is_open(row) else 0
        bucket["latestDayCount"] += 1 if id(row) in latest_rows else 0
        bucket["missingWorkOrderCount"] += 1 if not _row_work_order_id(row) else 0
        bucket["criticalCount"] += 1 if getattr(chat_service, "_is_existing_critical_asset", lambda _row: False)(row) else 0
        asset_name = _row_asset_name(row)
        location_name = _row_functional_location(row)
        if asset_name:
            bucket["assetCounts"][asset_name] += 1
        if location_name:
            bucket["locationCounts"][location_name] += 1
        snippet = " ".join(desc.split())[:110]
        if snippet and snippet not in bucket["examples"]:
            bucket["examples"].append(snippet)

    if not theme_buckets:
        return {
            "top_issues": [],
            "issue_categories": [],
            "trending_patterns": [],
            "description_themes": [],
            "data_notes": ["Recent MR descriptions were available, but no dominant recurring theme could be classified confidently."],
            "source": "Live MR/WO descriptions",
            "rows_loaded": len(rolling_rows),
            "rows_used": 0,
        }

    total = sum(bucket["count"] for bucket in theme_buckets.values())
    ranked = []
    for theme, bucket in theme_buckets.items():
        top_asset = bucket["assetCounts"].most_common(1)[0][0] if bucket["assetCounts"] else None
        top_location = bucket["locationCounts"].most_common(1)[0][0] if bucket["locationCounts"] else None
        score = (
            bucket["count"] * 3
            + bucket["openCount"] * 2
            + bucket["latestDayCount"] * 2
            + bucket["missingWorkOrderCount"]
            + bucket["criticalCount"]
            + (2 if top_asset in mttr_assets else 0)
            + (2 if top_asset in mtbf_assets else 0)
        )
        ranked.append((score, theme, bucket, top_asset, top_location))
    ranked.sort(key=lambda item: (-item[0], -item[2]["count"], item[1]))

    description_themes = []
    top_issues = []
    categories = []
    for score, theme, bucket, top_asset, top_location in ranked[:6]:
        percentage = round((bucket["count"] / total) * 100.0, 1) if total else 0.0
        description_themes.append({
            "theme": theme,
            "count": bucket["count"],
            "openCount": bucket["openCount"],
            "latestDayCount": bucket["latestDayCount"],
            "topAsset": top_asset,
            "topFunctionalLocation": top_location,
            "exampleDescriptions": bucket["examples"][:3],
        })
        categories.append({"category": theme, "count": bucket["count"], "percentage": percentage})
        evidence = [
            f"{bucket['count']} MR in the rolling 7-day window; {bucket['openCount']} still open / in progress.",
        ]
        if top_asset:
            evidence.append(f"Top related asset: {top_asset}.")
        if top_location:
            evidence.append(f"Top functional location: {top_location}.")
        evidence.extend(bucket["examples"][:2])
        why_it_matters = (
            f"Repeated {theme.lower()} descriptions appear in the latest maintenance activity and may create follow-up workload if the same equipment or area remains unresolved."
        )
        if top_asset in mtbf_assets:
            why_it_matters += f" MTBF is also tightening for {top_asset}."
        if top_asset in mttr_assets:
            why_it_matters += f" MTTR is also trending higher for {top_asset}."
        affected_areas = [value for value in (top_asset, top_location, "Critical asset" if bucket["criticalCount"] else None) if value]
        top_issues.append({
            "issue_focus_area": theme,
            "frequency": bucket["count"],
            "open_count": bucket["openCount"],
            "affected_areas": affected_areas[:4],
            "evidence": evidence[:4],
            "why_it_matters": why_it_matters,
            "follow_up_action": "Review open MR in the rolling 7-day window and confirm whether repeated descriptions link back to the same equipment, location, or unresolved root cause.",
            "score": score,
        })

    patterns = []
    if description_themes:
        first = description_themes[0]
        patterns.append(f"Most repeated recent theme: {first['theme']} ({first['count']} MR in the rolling 7-day window).")
        if first.get("topAsset"):
            patterns.append(f"Theme is most visible around {first['topAsset']}.")
        if len(description_themes) > 1:
            second = description_themes[1]
            patterns.append(f"Secondary repeated theme: {second['theme']} ({second['count']} MR).")
    if unknown_count:
        patterns.append(f"{unknown_count} recent descriptions were too short or unclear for reliable theme tagging.")

    return {
        "top_issues": top_issues[:4],
        "issue_categories": categories[:6],
        "trending_patterns": patterns[:4],
        "description_themes": description_themes[:6],
        "latest_available_date": (recent_context or {}).get("latestAvailableDate"),
        "lookback_label": f"Rolling {(recent_context or {}).get('days') or 7}-day MR/WO descriptions ending {(recent_context or {}).get('selectedDate') or 'latest available date'}",
        "data_notes": [
            "Issue focus is built from live MR/WO descriptions in the rolling 7-day window; engineering should confirm the true root cause before action."
        ],
        "source": "Live MR/WO descriptions",
        "rows_loaded": len(rolling_rows),
        "rows_used": sum(bucket["count"] for bucket in theme_buckets.values()),
    }


def build_predictive_context(filters: dict, data: dict, mr_context: dict, recent_context: dict, issue_context: dict, mttr_context: dict, mtbf_context: dict, data_quality_context: dict) -> dict:
    predictions = []
    forecast = []
    pm = data.get("pm_schedule") or {}
    downtime = data.get("downtime_summary") or {}
    top_theme = (issue_context.get("description_themes") or [None])[0]
    top_issue = (issue_context.get("top_issues") or [None])[0]
    mttr_asset = (mttr_context.get("increasingAssets") or [None])[0]
    mtbf_asset = (mtbf_context.get("decreasingAssets") or [None])[0]

    if mtbf_asset:
        predictions.append({
            "risk_area": "Decreasing MTBF",
            "evidence": f"{mtbf_asset['asset']} latest MTBF is {mtbf_asset['latestMtbfHours']:.2f} h versus a prior average of {mtbf_asset['baselineMtbfHours']:.2f} h.",
            "prediction": f"MTBF appears to be reducing for {mtbf_asset['asset']} because repeated MR are being raised closer together.",
            "confidence": mtbf_asset.get("confidence") or "Medium",
            "impact": "high",
            "follow_up_action": f"Review recent repeated MR on {mtbf_asset['asset']} and confirm whether the same failure mode is returning.",
        })

    if mttr_asset:
        predictions.append({
            "risk_area": "Increasing MTTR",
            "evidence": f"{mttr_asset['asset']} recent MTTR is {mttr_asset['recentMttrHours']:.2f} h versus a baseline of {mttr_asset['baselineMttrHours']:.2f} h.",
            "prediction": f"MTTR appears higher for {mttr_asset['asset']}, suggesting closure is taking longer than usual.",
            "confidence": mttr_asset.get("confidence") or "Medium",
            "impact": "medium",
            "follow_up_action": f"Check whether parts, access, or repeat troubleshooting are delaying closure for {mttr_asset['asset']}.",
        })

    if top_theme:
        predictions.append({
            "risk_area": top_theme.get("theme") or "Repeated issue theme",
            "evidence": f"{top_theme.get('count')} recent MR descriptions in the rolling 7-day window, with {top_theme.get('openCount')} still open.",
            "prediction": "Repeated descriptions may indicate a recurring issue cluster that could continue into the next operating period.",
            "confidence": "High" if (top_theme.get("count") or 0) >= 5 else "Medium",
            "impact": "high" if (top_theme.get("openCount") or 0) >= 5 else "medium",
            "follow_up_action": top_issue.get("follow_up_action") if isinstance(top_issue, dict) else "Review the repeated issue descriptions and check for a shared equipment or location root cause.",
        })

    if (mr_context.get("open") or 0) > 0 or (mr_context.get("carryOverOpen") or 0) > 0:
        predictions.append({
            "risk_area": "Open backlog risk",
            "evidence": f"{mr_context.get('open') or 0} selected-period MR remain open / in progress, with {mr_context.get('carryOverOpen') or 0} carry-over open MR.",
            "prediction": "Open MR backlog remains a follow-up risk if current open requests are not cleared before the next review cycle.",
            "confidence": "High" if (mr_context.get("open") or 0) >= 20 else "Medium",
            "impact": "high" if (mr_context.get("open") or 0) >= 50 else "medium",
            "follow_up_action": "Review open and carry-over MR together so repeat issues are not left unresolved across shifts.",
        })

    quality_issue_total = (data_quality_context.get("rowsWithIssues") or 0)
    if quality_issue_total > 0:
        predictions.append({
            "risk_area": "Data reliability risk",
            "evidence": f"{quality_issue_total} selected-period MR rows have missing dates, missing work-order fields, duplicate MR IDs, or weak descriptions.",
            "prediction": "Analysis confidence is reduced where Actual End, Asset, Work Order, or description data are incomplete.",
            "confidence": "Medium" if quality_issue_total < 15 else "High",
            "impact": "medium",
            "follow_up_action": "Correct key date and asset fields in the source workflow so future MTTR, MTBF, and issue-theme analysis stays reliable.",
        })

    if pm.get("compliance_pct") is not None and pm.get("compliance_pct") < 90:
        predictions.append({
            "risk_area": "PM compliance pressure",
            "evidence": f"PM compliance is {pm.get('compliance_pct')}% with {pm.get('overdue') or 0} overdue PM tasks and {pm.get('backlog') or 0} backlog items.",
            "prediction": "Lower PM completion can increase corrective maintenance pressure if overdue tasks continue to slip.",
            "confidence": "Medium",
            "impact": "medium",
            "follow_up_action": "Review overdue PM tasks together with corrective backlog before the next handover.",
        })

    if mr_context.get("open") is not None:
        forecast.append({
            "metric": "Open MR backlog",
            "current": mr_context.get("open"),
            "predicted": (mr_context.get("open") or 0) + (mr_context.get("carryOverOpen") or 0),
            "trend": "watch" if (mr_context.get("open") or 0) > 0 else "stable",
        })
    if mttr_context.get("overallHours") is not None:
        forecast.append({
            "metric": "Average MTTR (h)",
            "current": mttr_context.get("overallHours"),
            "predicted": mttr_context.get("recentWindowHours") if mttr_context.get("recentWindowHours") is not None else mttr_context.get("overallHours"),
            "trend": "up" if (mttr_context.get("recentWindowHours") or 0) > (mttr_context.get("overallHours") or 0) else "stable",
        })
    if downtime.get("corrective_ratio_pct") is not None:
        forecast.append({
            "metric": "Corrective MR share (%)",
            "current": downtime.get("corrective_ratio_pct"),
            "predicted": downtime.get("corrective_ratio_pct"),
            "trend": "high" if (downtime.get("corrective_ratio_pct") or 0) >= 70 else "stable",
        })

    if not predictions:
        predictions.append({
            "risk_area": "Limited prediction confidence",
            "evidence": "Recent repeated-issue, backlog, and reliability signals were limited for the selected filters.",
            "prediction": "Prediction confidence is limited because the selected window did not contain enough repeated MR history.",
            "confidence": "Low",
            "impact": "low",
            "follow_up_action": "Use the Downtime and PM detail views for deeper record-level follow-up.",
        })

    notes = [
        "Predictions are read-only risk indicators based on verified dashboard KPI and recent MR/WO description context; they are not severity recommendations."
    ]
    if recent_context.get("descriptionsIncluded") == 0:
        notes.append("No recent descriptions were available for theme-based prediction.")
    return {"predictions": predictions[:5], "forecast": forecast[:3], "data_notes": notes}


def build_mira_overview_context(
    filters: dict,
    data: dict | None = None,
    rows: list[dict] | None = None,
    selected_rows: list[dict] | None = None,
    recent_context: dict | None = None,
) -> dict:
    normalized = ctx.normalize_filters(filters)
    filtered_rows = rows if rows is not None else kpi._filtered_work_order_rows(normalized)
    scoped_rows = selected_rows if selected_rows is not None else kpi._selected_period_work_order_rows(normalized, filtered_rows)
    base_data = dict(data or kpi.get_dashboard_kpi_summary(normalized, include_spare_parts=True))
    _apply_data_availability(base_data)
    recent = recent_context if recent_context is not None else build_recent_description_context(normalized, filtered_rows, scoped_rows)
    mr_context = build_mr_wo_context(normalized, filtered_rows, scoped_rows)
    data_quality_context = build_data_quality_context(normalized, filtered_rows, scoped_rows)
    mttr_context = build_mttr_context(normalized, filtered_rows, scoped_rows, recent)
    mtbf_context = build_mtbf_context(normalized, filtered_rows, scoped_rows, recent)
    issue_context = build_issue_theme_context(normalized, recent, mttr_context, mtbf_context)
    predictive_context = build_predictive_context(normalized, base_data, mr_context, recent, issue_context, mttr_context, mtbf_context, data_quality_context)
    try:
        key_actions = presentation._build_priority_follow_up(base_data)  # intentional reuse of the existing overview follow-up rules
    except Exception:
        key_actions = []

    context = {
        **base_data,
        "selectedPeriod": base_data.get("window") or ctx.month_label(normalized),
        "selectedDate": recent.get("selectedDate") or recent.get("latestAvailableDate"),
        "selectedStage": _stage_label(normalized.get("stage")),
        "mrSummary": mr_context,
        "recentWindow": {
            "days": recent.get("days"),
            "selectedDate": recent.get("selectedDate"),
            "latestAvailableDate": recent.get("latestAvailableDate"),
            "windowStart": recent.get("rollingWindowStart"),
            "windowEnd": recent.get("rollingWindowEnd"),
            "mrRaised": recent.get("mrRaised"),
            "open": recent.get("open"),
            "latestDayMrCount": recent.get("latestDayMrCount"),
            "topAssets": recent.get("topAssets"),
            "topFunctionalLocations": recent.get("topFunctionalLocations"),
        },
        "descriptionThemes": issue_context.get("description_themes") or [],
        "mttrContext": mttr_context,
        "mtbfContext": mtbf_context,
        "dataQualityContext": data_quality_context,
        "issue_focus": issue_context,
        "predictive_analysis": predictive_context,
        "keyActionsToday": key_actions[:5],
        "debug": {
            "selectedPeriod": base_data.get("window") or ctx.month_label(normalized),
            "recordCountLoaded": len(filtered_rows),
            "latestMrDateFound": recent.get("latestAvailableDate"),
            "selectedPeriodMrCount": len(scoped_rows),
            "rolling7DayMrCount": recent.get("mrRaised"),
            "descriptionsIncluded": recent.get("descriptionsIncluded"),
        },
    }
    context.pop("_rolling_rows", None)
    context.pop("_latest_rows", None)
    return context


def call_ollama_for_mira_summary(overview_context: dict, filters: dict, warnings: list[str] | None = None) -> dict:
    structured = generate_structured_summary(
        overview_context,
        question=(
            "Produce a concise engineering daily-report overview. "
            "executive_summary must be 2 to 4 sentences only. "
            "key_observations must be 2 to 4 short bullets focused on the main issue focus and recent movement. "
            "recommended_follow_up must be 3 to 5 short actions required today. "
            "Use only the provided structured context and never invent values."
        ),
        filters=filters,
        warnings=warnings,
        timeout=MIRA_AI_SUMMARY_TIMEOUT_SECONDS,
    )
    structured["issue_focus"] = overview_context.get("issue_focus") or {}
    structured["predictive_analysis"] = overview_context.get("predictive_analysis") or {}
    return structured


def _row_year_matches(row: dict, filters: dict) -> bool:
    year = str((filters or {}).get("year") or "").strip()
    if not year:
        return True
    period = str(row.get("period") or "")
    classified_at = str(row.get("classified_at") or "")
    return year in period or classified_at.startswith(year)


def _description_issue_focus(filters: dict) -> dict:
    rows = [row for row in _read_description_tag_rows() if _row_year_matches(row, filters)]
    if not rows:
        return {
            "top_issues": [],
            "issue_categories": [],
            "trending_patterns": [],
            "data_notes": ["No persisted MR/WO description tags were available for issue-focus detection."],
            "source": "MIRA persisted MR/WO description tags",
        }

    latest_stamp = max(str(row.get("classified_at") or "")[:10] for row in rows if row.get("classified_at")) if any(row.get("classified_at") for row in rows) else None
    if latest_stamp:
        recent_rows = [row for row in rows if str(row.get("classified_at") or "").startswith(latest_stamp)]
    else:
        recent_rows = rows[:120]
    if len(recent_rows) < 8:
        recent_rows = rows[:120]

    theme_counts: Counter[str] = Counter()
    theme_rows: dict[str, list[dict]] = {}
    for row in recent_rows:
        theme = str(row.get("suggested_theme") or "Unknown / Insufficient Information").strip()
        if not theme or theme.lower().startswith("unknown"):
            continue
        theme_counts[theme] += 1
        theme_rows.setdefault(theme, []).append(row)

    total = sum(theme_counts.values())
    top_issues = []
    for theme, count in theme_counts.most_common(5):
        records = theme_rows.get(theme) or []
        asset_counts = Counter(str(r.get("asset_name") or r.get("asset_id") or "Unknown asset").strip() for r in records)
        location_counts = Counter(str(r.get("functional_location") or "Unspecified location").strip() for r in records)
        snippets = [
            str(r.get("description_snippet") or "").strip()
            for r in records
            if str(r.get("description_snippet") or "").strip()
        ][:3]
        affected = [name for name, _ in asset_counts.most_common(2) if name and name != "Unknown asset"]
        affected.extend(name for name, _ in location_counts.most_common(2) if name and name != "Unspecified location")
        top_issues.append({
            "issue_focus_area": theme,
            "frequency": count,
            "affected_areas": affected[:4],
            "evidence": snippets,
            "why_it_matters": f"{count} recent tagged MR/WO description(s) share this theme.",
            "follow_up_action": "Review the tagged work orders with engineering and confirm the actual root cause from source records.",
            "trend": "increasing" if count >= 5 else "stable",
        })

    categories = [
        {
            "category": theme,
            "count": count,
            "percentage": round((count / total) * 100, 1) if total else 0,
        }
        for theme, count in theme_counts.most_common(6)
    ]
    patterns = []
    if top_issues:
        patterns.append(f"Most repeated recent theme: {top_issues[0]['issue_focus_area']} ({top_issues[0]['frequency']} tagged description(s)).")
    if len(top_issues) > 1:
        patterns.append(f"Secondary repeated theme: {top_issues[1]['issue_focus_area']} ({top_issues[1]['frequency']} tagged description(s)).")
    if not patterns:
        patterns.append("No dominant repeated description theme was detected in the latest persisted tags.")

    return {
        "top_issues": top_issues,
        "issue_categories": categories,
        "trending_patterns": patterns,
        "latest_available_date": latest_stamp,
        "lookback_label": "Latest persisted MR/WO description tags",
        "data_notes": [
            "Issue focus uses existing MIRA keyword tags from MR/WO descriptions; engineering should confirm root cause before action."
        ],
        "source": "MIRA persisted MR/WO description tags",
        "rows_loaded": len(rows),
        "rows_used": len(recent_rows),
    }


def _predictive_indicators(data: dict, issue_focus: dict) -> dict:
    """Conservative risk indicators from verified KPI fields and recent issue tags."""
    predictions = []
    forecast = []
    wo = data.get("work_orders") or {}
    pm = data.get("pm_schedule") or {}
    spare = data.get("spare_parts") or {}

    open_count = wo.get("open")
    overdue = pm.get("overdue")
    backlog = pm.get("backlog")
    compliance = pm.get("compliance_pct")

    if isinstance(open_count, (int, float)) and open_count > 0:
        predictions.append({
            "risk_area": "Open MR / WO carry-over",
            "evidence": f"{open_count} open / in-progress MR in the selected period.",
            "prediction": "Open maintenance requests may carry into the next review period if not closed or confirmed.",
            "confidence": "Medium",
            "impact": "medium" if open_count < 60 else "high",
            "follow_up_action": "Prioritise open MR linked to critical assets and confirm closure status in the source workflow.",
        })
        forecast.append({"metric": "Open MR", "current": open_count, "predicted": open_count, "change": 0, "trend": "neutral"})

    if isinstance(overdue, (int, float)) and overdue > 0:
        evidence = f"{overdue} overdue PM task(s)"
        if isinstance(backlog, (int, float)):
            evidence += f" and {backlog} backlog PM task(s)"
        predictions.append({
            "risk_area": "PM schedule pressure",
            "evidence": evidence + ".",
            "prediction": "PM follow-up pressure may increase if overdue tasks are not cleared before the next shift review.",
            "confidence": "Medium",
            "impact": "high" if overdue > 50 else "medium",
            "follow_up_action": "Review overdue PM tasks with engineering and verify manual Done status only from source records.",
        })
        forecast.append({"metric": "Overdue PM", "current": overdue, "predicted": overdue, "change": 0, "trend": "neutral"})
    elif isinstance(compliance, (int, float)) and compliance < 80:
        predictions.append({
            "risk_area": "PM compliance",
            "evidence": f"PM compliance is {compliance}%.",
            "prediction": "Low compliance may increase corrective maintenance pressure if scheduled tasks continue to slip.",
            "confidence": "Low",
            "impact": "medium",
            "follow_up_action": "Check PM completion evidence and confirm whether pending tasks are genuinely still open.",
        })

    top_issue = (issue_focus.get("top_issues") or [None])[0]
    if top_issue:
        predictions.append({
            "risk_area": top_issue.get("issue_focus_area"),
            "evidence": f"{top_issue.get('frequency')} recent tagged description(s); examples: " + "; ".join((top_issue.get("evidence") or [])[:2]),
            "prediction": "Repeated descriptions may indicate a recurring issue cluster that could reappear in the next review period.",
            "confidence": "High" if (top_issue.get("frequency") or 0) >= 5 else "Medium",
            "impact": "high" if (top_issue.get("frequency") or 0) >= 5 else "medium",
            "follow_up_action": top_issue.get("follow_up_action"),
        })

    if spare.get("top_consumed_part"):
        predictions.append({
            "risk_area": "Spare-parts consumption",
            "evidence": f"Top consumed part: {spare.get('top_consumed_part')}.",
            "prediction": "High-consumption parts should be checked against current stock and pending purchases before the next maintenance review.",
            "confidence": "Low",
            "impact": "medium",
            "follow_up_action": "Validate high-consumption parts with store records and open PO status.",
        })

    if not predictions:
        predictions.append({
            "risk_area": "Limited prediction confidence",
            "evidence": "Verified trend, backlog, and issue-tag data are incomplete for the current selection.",
            "prediction": "Prediction confidence is limited because historical data is incomplete.",
            "confidence": "Low",
            "impact": "low",
            "follow_up_action": "Refresh verified data or use the Downtime and PM Schedule pages for detailed checks.",
        })

    return {
        "predictions": predictions[:5],
        "forecast": forecast[:3],
        "data_notes": ["Predictions are read-only risk indicators based on verified dashboard values and persisted issue tags; they are not severity recommendations."],
    }


def _enrich_summary_intelligence(structured: dict, data: dict, filters: dict) -> dict:
    issue_focus = _description_issue_focus(filters)
    predictive = _predictive_indicators(data, issue_focus)
    structured["issue_focus"] = issue_focus
    structured["predictive_analysis"] = predictive
    return structured


def _read_summary_type() -> str | None:
    summary_type = request.args.get("summaryType") or request.args.get("summary_type")
    if request.is_json:
        body = request.get_json(silent=True) or {}
        summary_type = body.get("summaryType") or body.get("summary_type") or summary_type
    return summary_type


def _summary_response(intent: str, data: dict, *, filters: dict | None = None, response_type: str | None = None):
    """Guard -> provider -> envelope, for the single-intent KPI routes."""
    provider = get_provider()
    guarded = guard.guard_summary(data, mode="kpi_summary")
    answer = guard.mark_draft(provider.generate(intent, data))
    presentation_model = presentation.build_presentation(
        intent,
        data,
        filters,
        mode="kpi_summary",
        provider_name=provider.name,
        response_type=response_type,
    )
    return jsonify({
        "ok": True,
        "intent": intent,
        "mode": "kpi_summary",
        "answer": answer,
        "data": guarded["data"],
        "presentation": guard._deep_redact(presentation_model),
        "provider": provider.name,
        "draft_label": config.DRAFT_LABEL,
        "disclaimer": config.MODEL_DISCLAIMER,
    })


@mira_bp.route("/fast-kpis", methods=["GET", "POST"])
def fast_kpis():
    """Lightweight KPI snapshot — returns in < 200 ms whether or not the heavy
    payloads are warm.

    Priority order:
    1. Read from the in-memory _SUMMARY_CACHE (sub-millisecond if present).
    2. Build a partial summary from the three service-level in-memory caches
       (pm_schedule_service, downtime_service, spare_parts_service) without
       triggering any Ollama call or heavy rebuild.
    3. Return the warming-placeholder values with warming=True so the frontend
       knows to keep polling /overview for the full numbers.

    The frontend calls this first to populate KPI chips quickly, then calls
    /overview in parallel for the full verified payload.
    """
    raw = _read_filters()
    filters = ctx.normalize_filters(raw)
    now = time.time()

    # ── Try _SUMMARY_CACHE first (zero-build path) ──────────────────────────
    for include_sp in (True, False):
        key = _normalised_cache_key(filters, include_sp)
        cached = _SUMMARY_CACHE.get(key)
        if cached and (now - cached.get("created_at", 0)) <= MIRA_SUMMARY_CACHE_TTL_SECONDS:
            data = cached["data"]
            return jsonify({
                "ok": True,
                "warming": False,
                "cache_hit": True,
                "kpis": _extract_flat_kpis(data),
                "sections": _extract_sections_for_kpi_chips(data),
                "filters": filters,
                "data_availability": data.get("data_availability") or {"complete": True},
            })

    # ── Try partial build from service-level in-memory caches ───────────────
    try:
        partial_kpis = _build_fast_kpis_from_service_caches(filters)
        if partial_kpis:
            return jsonify({
                "ok": True,
                "warming": False,
                "cache_hit": False,
                "partial": True,
                "kpis": partial_kpis,
                "sections": _fast_sections_from_partial(partial_kpis),
                "filters": filters,
                "data_availability": {"complete": True},
            })
    except Exception:
        pass

    # ── Nothing warm yet — return placeholder immediately ───────────────────
    _start_summary_warmup(
        _normalised_cache_key(filters, True), filters, include_spare_parts=True
    )
    return jsonify({
        "ok": True,
        "warming": True,
        "cache_hit": False,
        "kpis": {},
        "sections": {},
        "filters": filters,
        "data_availability": {"complete": False, "warming": True},
    })


def _extract_flat_kpis(data: dict) -> dict:
    """Pull a compact flat KPI dict from a full dashboard summary."""
    pm = data.get("pm_schedule") or {}
    wo = data.get("work_orders") or {}
    dt = data.get("downtime_summary") or {}
    sp = data.get("spare_parts") or {}
    return {
        "pm_total_scheduled": pm.get("total_scheduled"),
        "pm_completed": pm.get("completed"),
        "pm_overdue": pm.get("overdue"),
        "pm_compliance_pct": pm.get("compliance_pct"),
        "pm_backlog": pm.get("backlog"),
        "wo_total": wo.get("total"),
        "wo_open": wo.get("open"),
        "wo_closed": wo.get("closed"),
        "wo_closure_rate_pct": wo.get("closure_rate_pct"),
        "mttr_hours": data.get("mttr_hours"),
        "mtbf_hours": data.get("mtbf_hours"),
        "late_wo": dt.get("late_count"),
        "open_overdue_wo": dt.get("open_overdue_count"),
        "sla_pct": dt.get("sla_compliance_pct"),
        "data_completeness_pct": dt.get("data_completeness_pct"),
        "missing_data_count": dt.get("missing_data_count"),
        "spare_low_stock": sp.get("items_below_minimum"),
        "spare_high_usage": sp.get("high_usage_item_count"),
        "spare_pending_po": sp.get("pending_po_count"),
        "performance_status": data.get("performance_status"),
    }


def _extract_sections_for_kpi_chips(data: dict) -> dict:
    """Convert a full summary into the presentation.sections format the frontend
    already knows how to render."""
    try:
        pres = presentation.build_presentation("monthly_summary", data,
                                               data.get("filters") or {}, provider_name="mira")
        return pres.get("sections") or {}
    except Exception:
        return {}


def _build_fast_kpis_from_service_caches(filters: dict) -> dict | None:
    """Read directly from the three service-level in-memory caches.

    Returns a flat KPI dict when enough cached data is available, or None if
    the caches are empty. Never blocks on a heavy rebuild.
    """
    try:
        import pm_schedule_service as _pm
        import downtime_service as _dt
        pm_cache = getattr(_pm, "_PM_PAGE_PAYLOAD_CACHE", {})
        dt_cache = getattr(_dt, "_DOWNTIME_CACHE", {})
        if not pm_cache and not dt_cache:
            return None

        # Pull the first matching PM payload (any key, we only need top-level totals)
        pm_payload = {}
        for v in pm_cache.values():
            if isinstance(v, dict) and ("overview" in v or "meta" in v):
                pm_payload = v; break

        pm_ov = (pm_payload.get("overview") or {})
        pm_kpis = (pm_ov.get("kpis") or pm_ov.get("periodKpis") or {})

        # Pull the first matching downtime payload
        dt_payload = {}
        for v in dt_cache.values():
            if isinstance(v, dict) and ("management" in v or "management_summary" in v):
                dt_payload = v; break

        mgmt = dt_payload.get("management") or {}
        mgmt_sum = mgmt.get("summary") or {}

        kpis = {
            "pm_total_scheduled": pm_kpis.get("yearTaskCount"),
            "pm_completed": pm_kpis.get("completedInMonth"),
            "pm_overdue": pm_kpis.get("overdue") or pm_kpis.get("overdueInMonth"),
            "pm_compliance_pct": pm_kpis.get("compliancePct"),
            "pm_backlog": pm_kpis.get("backlogInMonth") or pm_kpis.get("backlog"),
            "wo_total": mgmt_sum.get("total_work_orders"),
            "wo_open": mgmt_sum.get("open_work_orders"),
            "wo_closed": mgmt_sum.get("closed_work_orders"),
            "wo_closure_rate_pct": mgmt_sum.get("closure_rate_pct"),
            "mttr_hours": (mgmt.get("mtbf") or {}).get("summary") and
                          mgmt.get("summary", {}).get("overall_mttr_hours"),
        }
        # Only return if we got something useful
        if any(v is not None for v in kpis.values()):
            return kpis
    except Exception:
        pass
    return None


def _fast_sections_from_partial(kpis: dict) -> dict:
    """Build minimal chip sections from the flat KPI dict."""
    def chip(label, value, tone="neutral", note=None):
        c = {"label": label, "value": str(value) if value is not None else "--", "tone": tone}
        if note: c["note"] = note
        return c

    def fmt(v, suffix=""):
        if v is None: return "--"
        if isinstance(v, float): return f"{round(v, 1)}{suffix}"
        return f"{v}{suffix}"

    pm = {"metrics": [
        chip("PM Scheduled", fmt(kpis.get("pm_total_scheduled"))),
        chip("PM Completed", fmt(kpis.get("pm_completed"))),
        chip("PM Overdue", fmt(kpis.get("pm_overdue")), tone="warn" if kpis.get("pm_overdue") else "good"),
        chip("Compliance", fmt(kpis.get("pm_compliance_pct"), "%")),
    ]}
    dt = {"metrics": [
        chip("Total MR", fmt(kpis.get("wo_total"))),
        chip("Open MR", fmt(kpis.get("wo_open"))),
        chip("Closure Rate", fmt(kpis.get("wo_closure_rate_pct"), "%")),
        chip("MTTR", fmt(kpis.get("mttr_hours"), " h") if kpis.get("mttr_hours") is not None else "--"),
    ]}
    sp = {"metrics": [chip("Data", "Loading…", "neutral")]}
    return {"pm_schedule_summary": pm, "downtime_work_order_summary": dt, "spare_parts_summary": sp}


@mira_bp.route("/overview", methods=["GET", "POST"])
def overview():
    """FAST verified-metrics overview (NO LLM) — renders KPI cards immediately.

    Numbers are deterministic backend KPIs. The AI wording is fetched separately
    via /ai-summary so the page never blocks on Ollama.
    """
    raw = _read_filters()
    filters = ctx.normalize_filters(raw)
    data, load_warnings, cache_hit = _get_dashboard_summary(
        filters,
        include_spare_parts=True,
        allow_blocking_build=True,
        start_warmup=False,
    )
    warming = False
    spare_warming = False
    status = _fast_provider_status("Verified KPI cards loaded. AI wording loads separately.")
    pres = presentation.build_presentation(
        "monthly_summary", data, filters, provider_name="mira",
    )
    if load_warnings:
        pres.setdefault("data_notes", []).extend(load_warnings)
        pres.setdefault("view_data_used", {}).setdefault("data_warnings", []).extend(load_warnings)
    debug = {
        "selectedPeriod": data.get("window") or ctx.month_label(filters),
        "recordCountLoaded": None,
        "latestMrDateFound": None,
        "selectedPeriodMrCount": None,
        "rolling7DayMrCount": None,
        "descriptionsIncluded": None,
    }
    try:
        _filtered_rows, _selected_rows, _recent_context, debug = _collect_mira_debug_snapshot(filters)
    except Exception as exc:
        snapshot_warning = f"MIRA overview debug snapshot failed: {exc}"
        load_warnings = _unique_strings(load_warnings + [snapshot_warning])
        pres.setdefault("data_notes", []).extend([snapshot_warning])
        pres.setdefault("view_data_used", {}).setdefault("data_warnings", []).extend([snapshot_warning])
        debug["debugError"] = str(exc)
    _log_mira_event(
        "overview",
        **debug,
        availabilityComplete=(data.get("data_availability") or {}).get("complete"),
        cacheHit=cache_hit,
        ollamaCalled=False,
        ollamaResponseStatus="not_called",
        fallbackReason="; ".join(load_warnings) if load_warnings else None,
    )
    guarded = guard.guard_summary(data, mode="kpi_summary")
    availability = data.get("data_availability") or {"complete": not warming, "warnings": load_warnings}
    if warming:
        availability = {**availability, "complete": False, "warming": True}
    freshness = data.get("data_freshness") or {}
    return jsonify({
        "ok": True,
        "filters": filters,
        "presentation": guard._deep_redact(pres),
        "data": guarded["data"],
        "provider_status": status,
        "backend_version": MIRA_BACKEND_VERSION,
        "cache_hit": cache_hit,
        "warming": warming,
        "spare_warming": spare_warming,
        "data_availability": availability,
        "last_updated": freshness.get("last_updated") or data.get("last_updated"),
        "latest_import_time": freshness.get("latest_import_time") or data.get("latest_import_time"),
        "source_files_used": freshness.get("source_files_used") or data.get("source_files_used") or [],
        "data_freshness": freshness,
        "draft_label": config.DRAFT_LABEL,
    })


@mira_bp.route("/risk", methods=["GET", "POST"])
def risk():
    """Backend-calculated maintenance risk insights (read-only; not a prediction)."""
    raw = _read_filters()
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body.get("filters"), dict):
            raw = {**raw, **{k: v for k, v in body["filters"].items() if k in ctx.FILTER_KEYS}}
    result = risk_service.get_asset_risk_insights(ctx.normalize_filters(raw))
    return jsonify(guard._deep_redact(result))


@mira_bp.route("/ai-summary", methods=["GET", "POST"])
def ai_summary():
    """Verified metrics -> Ollama (or rule-based) -> structured summary JSON.

    The numbers come ONLY from the verified backend KPI summary; the LLM just
    writes the wording. The frontend renders KPI cards from `data`/`view_data_used`
    immediately and shows this AI summary when ready.
    """
    raw = _read_filters()
    filters = ctx.normalize_filters(raw)
    data, load_warnings, cache_hit = _get_dashboard_summary(
        filters,
        include_spare_parts=True,
        allow_blocking_build=True,
        start_warmup=False,
    )
    pres = presentation.build_presentation(
        "monthly_summary", data, filters, provider_name="mira",
    )
    warnings = _unique_strings(list(pres.get("data_notes") or []) + load_warnings)
    debug = {
        "selectedPeriod": data.get("window") or ctx.month_label(filters),
        "recordCountLoaded": None,
        "latestMrDateFound": None,
        "selectedPeriodMrCount": None,
        "rolling7DayMrCount": None,
        "descriptionsIncluded": None,
    }
    try:
        filtered_rows, selected_rows, recent_context, debug = _collect_mira_debug_snapshot(filters)
        overview_context = build_mira_overview_context(
            filters,
            data,
            filtered_rows,
            selected_rows,
            recent_context,
        )
    except Exception as exc:
        warning = f"Structured MIRA overview context could not be fully built: {exc}"
        warnings = _unique_strings(warnings + [warning])
        debug["debugError"] = str(exc)
        overview_context = {
            **data,
            "selectedPeriod": data.get("window") or ctx.month_label(filters),
            "selectedDate": None,
            "selectedStage": _stage_label(filters.get("stage")),
            "issue_focus": {},
            "predictive_analysis": {},
            "keyActionsToday": (pres.get("priority_follow_up") or [])[:5],
            "debug": debug,
        }

    structured = call_ollama_for_mira_summary(overview_context, filters, warnings)
    status = _provider_status_from_summary(structured)
    fallback_reason = None
    if structured.get("provider") != "ollama":
        fallback_reason = (structured.get("data_notes") or warnings or ["ollama_unavailable"])[0]
    _log_mira_event(
        "ai-summary",
        **debug,
        availabilityComplete=(data.get("data_availability") or {}).get("complete"),
        cacheHit=cache_hit,
        ollamaCalled=True,
        ollamaResponseStatus=status["status"],
        provider=structured.get("provider"),
        fallbackReason=fallback_reason,
    )
    guarded = guard.guard_summary(data, mode="kpi_summary")
    freshness = data.get("data_freshness") or {}
    return jsonify({
        "ok": True,
        "filters": filters,
        "summary": guard._deep_redact(structured),
        "issue_focus": guard._deep_redact(structured.get("issue_focus")),
        "predictive_analysis": guard._deep_redact(structured.get("predictive_analysis")),
        "provider": structured.get("provider"),
        "provider_status": status["status"],
        "llm_active": structured.get("provider") == "ollama",
        "llm_model": structured.get("model"),
        "last_updated": freshness.get("last_updated") or data.get("last_updated"),
        "latest_import_time": freshness.get("latest_import_time") or data.get("latest_import_time"),
        "source_files_used": freshness.get("source_files_used") or data.get("source_files_used") or [],
        "data_freshness": freshness,
        "backend_version": MIRA_BACKEND_VERSION,
        "cache_hit": cache_hit,
        "fallback_active": structured.get("provider") != "ollama",
        "view_data_used": guard._deep_redact(pres.get("view_data_used")),
        "data": guarded["data"],
        "draft_label": config.DRAFT_LABEL,
        "disclaimer": config.MODEL_DISCLAIMER,
    })


@mira_bp.get("/health")
def health():
    status = get_provider_status()
    return jsonify({
        "ok": True,
        "status": "ok",
        "service": "MIRA",
        "version": MIRA_BACKEND_VERSION,
        "backend_version": MIRA_BACKEND_VERSION,
        "started_at": MIRA_BACKEND_STARTED_AT,
        "available_routes": _health_routes(),
        "provider": status["provider"],
        "provider_status": status["status"],
        "llm_active": status["llm"],
        "llm_model": status.get("model"),
        "local_llm_enabled": config.LOCAL_LLM_ENABLED,
        "row_cap_max": config.ROW_CAP_MAX,
        "draft_label": config.DRAFT_LABEL,
        "disclaimer": config.MODEL_DISCLAIMER,
    })


@mira_bp.route("/summary", methods=["GET", "POST"])
def summary():
    """Consolidated KPI snapshot for the selected window/stage."""
    filters = _read_filters()
    response_type = _read_summary_type()
    data = kpi.get_dashboard_kpi_summary(filters, include_spare_parts=True)
    return _summary_response("monthly_summary", data, filters=filters, response_type=response_type)


@mira_bp.route("/query", methods=["POST"])
def query():
    """Free-text question -> intent routing -> KPI summary (or limited rows)."""
    body = request.get_json(silent=True) or {}
    question = body.get("question") or body.get("q") or ""
    limit = body.get("limit")
    result = assistant_service.ask(question, _read_filters(), limit=limit)
    return jsonify(result)


@mira_bp.route("/chat", methods=["POST"])
def chat():
    """Intelligent read-only Q&A: intent + period extraction -> verified data -> wording.

    The question period (e.g. "April 2026") overrides the dashboard filter, so the
    answer never falls back to a generic YTD summary when a month was asked for.
    """
    body = request.get_json(silent=True) or {}
    question = _coalesce(body.get("question"), body.get("userQuestion"), body.get("q"), "")
    selected_kpis = _read_selected_kpi_items(body, "selected_kpis", "selectedKpis", "selectedKpiIds")
    selected_kpi_labels = _read_selected_kpi_items(body, "selected_kpi_labels", "selectedKpiLabels")
    mode = _normalise_chat_mode(body.get("mode"))
    base_filters = _read_filters()
    if isinstance(body.get("filters"), dict):
        body_filters = {}
        _copy_filter_aliases(body["filters"], body_filters)
        base_filters = {**base_filters, **body_filters}
    result = chat_service.answer(
        question,
        base_filters,
        mode=mode,
        selected_kpis=selected_kpis,
        selected_kpi_labels=selected_kpi_labels,
    )
    return jsonify(guard._deep_redact(result))


@mira_bp.route("/asset-report", methods=["POST"])
def asset_report_endpoint():
    """Asset breakdown + repair-cost report (deterministic calc + optional LLM wording).

    POST body:
        {
            "machine":                  "Combi Oven",
            "stage":                    "stage1",          # optional
            "period":                   "past 1 year",     # optional (default ytd)
            "include_cost":             true,
            "include_excluded_rows":    false,
            "format":                   "management_summary"
        }
    """
    body = request.get_json(silent=True) or {}
    base_filters = _read_filters()
    if isinstance(body.get("filters"), dict):
        body_filters = {}
        _copy_filter_aliases(body["filters"], body_filters)
        base_filters = {**base_filters, **body_filters}

    machine = str(body.get("machine") or "").strip()
    stage = str(body.get("stage") or body.get("selectedStage") or base_filters.get("stage") or "").strip() or None
    period_text = str(body.get("period") or body.get("period_text") or "ytd").strip()
    include_cost = bool(body.get("include_cost") or body.get("includeCost"))
    include_excluded = bool(body.get("include_excluded_rows") or body.get("includeExcludedRows"))
    fmt = str(body.get("format") or "standard").strip()

    if not machine:
        return jsonify({"ok": False, "error": "machine parameter is required."}), 400

    params = {
        "machine": machine,
        "stage": stage,
        "period_text": period_text,
        "include_cost": include_cost,
        "include_excluded_rows": include_excluded,
        "format": fmt,
        "group_by": "unit",
    }

    # Resolve machine family key from display name
    for key, fam in asset_report._MACHINE_FAMILIES.items():
        if fam["display"].lower() == machine.lower() or any(
            alias in machine.lower() for alias in fam["aliases"]
        ):
            params["machine_family_key"] = key
            params["machine"] = fam["display"]
            break

    if "machine_family_key" not in params:
        return jsonify({
            "ok": False,
            "error": f"Machine '{machine}' is not recognised. Supported machines: "
                     + ", ".join(fam["display"] for fam in asset_report._MACHINE_FAMILIES.values()),
        }), 400

    try:
        report = asset_report.build_asset_report(params, base_filters)
        report["answer"] = asset_report.generate_asset_report_wording(report)
        report["ok"] = True
        report["period_used"] = f"Period used: {report.get('period_label', period_text)}"
        report["read_only"] = True
        report["provider_mode_label"] = "Asset Report (deterministic)"
        return jsonify(guard._deep_redact(report))
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Asset report generation failed: {exc}"}), 500


@mira_bp.route("/data-quality", methods=["GET", "POST"])
def data_quality():
    filters = _read_filters()
    data = kpi.get_data_reliability_issues(filters)
    return _summary_response("data_quality", data, filters=filters)


@mira_bp.route("/pm-schedule", methods=["GET", "POST"])
def pm_schedule():
    filters = _read_filters()
    data = kpi.get_pm_schedule_status(filters)
    return _summary_response("pm_schedule", data, filters=filters)


@mira_bp.route("/mttr", methods=["GET", "POST"])
def mttr():
    filters = _read_filters()
    return _summary_response("mttr", kpi.get_mttr(filters), filters=filters)


@mira_bp.route("/mtbf", methods=["GET", "POST"])
def mtbf():
    filters = _read_filters()
    return _summary_response("mtbf", kpi.get_mtbf(filters), filters=filters)


@mira_bp.route("/stage-compare", methods=["GET", "POST"])
def stage_compare():
    filters = _read_filters()
    return _summary_response("stage_compare", kpi.get_stage_summary(filters), filters=filters)


@mira_bp.route("/work-orders", methods=["GET", "POST"])
def work_orders():
    """Limited Filtered Rows Mode - capped + scrubbed; never the full dataset."""
    limit = request.args.get("limit") or (request.get_json(silent=True) or {}).get("limit")
    filters = _read_filters()
    raw = kpi.get_work_orders(filters, limit=limit)
    guarded = guard.guard_work_orders(raw, requested_limit=limit)
    provider = get_provider()
    answer = guard.mark_draft(provider.generate("work_order_search", guarded))
    presentation_model = presentation.build_presentation(
        "work_order_search",
        guarded,
        filters,
        mode="limited_filtered_rows",
        provider_name=provider.name,
    )
    return jsonify({
        "ok": True,
        "intent": "work_order_search",
        "mode": "limited_filtered_rows",
        "answer": answer,
        "data": guarded,
        "presentation": guard._deep_redact(presentation_model),
        "provider": provider.name,
        "draft_label": config.DRAFT_LABEL,
        "disclaimer": config.MODEL_DISCLAIMER,
    })


@mira_bp.route("/report-draft", methods=["GET", "POST"])
def report_draft():
    """Monthly maintenance report draft (structured + markdown)."""
    return jsonify(report_draft_service.generate_monthly_maintenance_summary(_read_filters()))


# ── AI Tech Notes / Early-Warning Scanner ─────────────────────────────────────

_TECH_NOTE_SYSTEM_PROMPT = """You are MIRA, a local maintenance AI assistant for SATS Thailand.

Your task is to scan maintenance notes and identify early warning signs or possible equipment issues.

Rules:
- Only flag issues that are clearly mentioned or reasonably implied in the note.
- Do not exaggerate the issue.
- Do not assign official D365 severity levels.
- Do not create or close work orders.
- Do not recommend bypassing safety, food safety, machine guarding, alarms, or interlocks.
- Keep recommended actions practical and inspection-focused.
- If the note is too vague, mark risk_level as "low" and recommend verification.
- If no issues are found, return an empty items array.
- Return only valid JSON in the required schema — no extra text.

Required JSON schema:
{
  "items": [
    {
      "equipment_id": "string (asset ID if identifiable, else empty string)",
      "equipment_name": "string (equipment or asset name from the note)",
      "risk_level": "low | medium | high",
      "detected_issue": "string (brief description of the possible issue)",
      "recommended_action": "string (practical inspection or follow-up action)",
      "source_note": "string (exact or close excerpt from the note that triggered this flag)"
    }
  ]
}"""

_TECH_NOTE_SCHEMA_KEYS = {"equipment_id", "equipment_name", "risk_level", "detected_issue", "recommended_action", "source_note"}
_VALID_RISK_LEVELS = {"low", "medium", "high"}


def _parse_tech_note_response(raw_text: str) -> list[dict]:
    """Extract and validate the items array from the Ollama response."""
    text = raw_text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(l for l in lines if not l.strip().startswith("```"))
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Try to find the first JSON object in the text
        import re as _re
        match = _re.search(r"\{.*\}", text, _re.DOTALL)
        if not match:
            return []
        try:
            parsed = json.loads(match.group())
        except (json.JSONDecodeError, ValueError):
            return []
    items = parsed.get("items") if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])
    if not isinstance(items, list):
        return []
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        risk = str(item.get("risk_level") or "low").lower().strip()
        if risk not in _VALID_RISK_LEVELS:
            risk = "low"
        clean.append({
            "equipment_id": str(item.get("equipment_id") or "").strip(),
            "equipment_name": str(item.get("equipment_name") or "").strip(),
            "risk_level": risk,
            "detected_issue": str(item.get("detected_issue") or "").strip(),
            "recommended_action": str(item.get("recommended_action") or "").strip(),
            "source_note": str(item.get("source_note") or "").strip(),
        })
    return clean


@mira_bp.route("/scan-tech-notes", methods=["POST"])
def scan_tech_notes():
    """Scan raw technician / MR notes for early-warning maintenance signals.

    POST body: { "notes": "<freetext>" }
    Returns:   { "ok": true, "items": [...], "provider": "ollama|rule_based",
                 "disclaimer": "..." }

    Uses local Ollama when available; falls back to a simple keyword scanner so
    the feature remains useful even when Ollama is not running.
    """
    body = request.get_json(silent=True) or {}
    notes_text = str(body.get("notes") or "").strip()
    if not notes_text:
        return jsonify({"ok": False, "error": "No notes provided.", "items": []}), 400
    if len(notes_text) > 12_000:
        notes_text = notes_text[:12_000] + "\n[truncated]"

    # ── Try Ollama ────────────────────────────────────────────────────────────
    try:
        from .providers.ollama_provider import generate_with_ollama
        raw = generate_with_ollama(
            _TECH_NOTE_SYSTEM_PROMPT,
            f"Scan the following maintenance notes and return structured JSON:\n\n{notes_text}",
            format_json=True,
            timeout=45,
        )
        items = _parse_tech_note_response(raw)
        return jsonify({
            "ok": True,
            "items": items,
            "provider": "ollama",
            "note_count_chars": len(notes_text),
            "disclaimer": "AI-detected issues are for review only. Technician/Engineer verification is required before any action.",
        })
    except Exception as ollama_error:
        current_app.logger.debug("scan-tech-notes: Ollama unavailable (%s), using keyword fallback", ollama_error)

    # ── Keyword fallback ─────────────────────────────────────────────────────
    import re as _re
    EARLY_WARNING_PATTERNS = [
        (r"\bvibrat\w*\b", "Possible vibration issue", "Inspect mounting, bearings, and alignment.", "medium"),
        (r"\brunning\s+warm\b|\boverheating\b|\btemperature.*high\b|\bhigh.*temp\b", "Possible overheating", "Check cooling, filters, and load.", "medium"),
        (r"\babnormal\s+sound\b|\bunusual\s+sound\b|\bstrange\s+noise\b|\bnoisy\b", "Unusual sound detected", "Inspect for mechanical wear or looseness.", "medium"),
        (r"\bleak\w*\b|\bleaking\b|\bseepage\b", "Possible leakage", "Inspect seals, joints, and pipework.", "high"),
        (r"\bintermittent\s+trip\b|\btripping\b|\bnuisance\s+trip\b", "Intermittent tripping", "Check electrical connections and protection settings.", "medium"),
        (r"\btemperature\s+drift\w*\b|\bdrift\w*\s+temp\w*\b", "Temperature drifting", "Verify sensor calibration and control loop.", "medium"),
        (r"\bslow\s+start\b|\bdifficult\s+start\b|\bhard\s+start\b", "Slow or difficult start", "Check motor, capacitors, and starter.", "medium"),
        (r"\bweak\s+cooling\b|\bcooling.*not.*sufficient\b|\bpoor\s+cooling\b", "Weak cooling performance", "Check refrigerant, condenser, and airflow.", "medium"),
        (r"\bunusual\s+smell\b|\bburning\s+smell\b|\bsmell\s+burn\w*\b", "Unusual or burning smell", "Inspect electrical components and insulation.", "high"),
        (r"\bpressure\s+unstable\b|\bpressure.*fluctuat\w*\b|\bfluctuat\w*.*pressure\b", "Pressure instability", "Inspect pressure controls, valves, and leaks.", "medium"),
        (r"\bslightly\s+(off|wrong|unusual)\b|\bminor\s+issue\b", "Minor anomaly noted", "Monitor and schedule inspection.", "low"),
    ]
    items = []
    for pattern, issue, action, risk in EARLY_WARNING_PATTERNS:
        matches = _re.findall(pattern, notes_text, _re.IGNORECASE)
        if matches:
            excerpt = ""
            for line in notes_text.split("\n"):
                if _re.search(pattern, line, _re.IGNORECASE):
                    excerpt = line.strip()[:120]
                    break
            items.append({
                "equipment_id": "",
                "equipment_name": "See note",
                "risk_level": risk,
                "detected_issue": issue,
                "recommended_action": action,
                "source_note": excerpt or str(matches[0]),
            })
    return jsonify({
        "ok": True,
        "items": items,
        "provider": "keyword_fallback",
        "note_count_chars": len(notes_text),
        "disclaimer": "AI-detected issues are for review only. Technician/Engineer verification is required before any action.",
    })


# ── Daily MR triage verdict (scope-aware; precomputed by the morning scheduler) ─
@mira_bp.route("/verdict", methods=["GET"])
def mr_triage_verdict():
    """Return the latest stored daily MR triage verdict for the selected scope.

    Scope is a runtime parameter (?scope=Stage 1 | Stage 2 | All). Single scopes
    return only their own assets; 'All' merges the stored single-scope verdicts.
    The verdict is precomputed each morning and read from disk here (no model call
    on the request path). Returns a Green "no data yet" verdict if nothing stored,
    so the widget always receives valid JSON. Same-origin (served by this app)."""
    scope = request.args.get("scope") or request.args.get("stage") or "All"
    try:
        import mr_triage_service as triage
        verdict = triage.get_verdict(scope)
    except Exception as exc:
        current_app.logger.warning("MR triage verdict read failed: %s", exc)
        from datetime import date as _d
        verdict = {
            "scope": str(scope), "date_reviewed": _d.today().isoformat(),
            "overall_verdict": "Green", "summary": "Triage data is not available yet.",
            "items": [], "watchlist": [],
        }
    resp = jsonify(verdict)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@mira_bp.route("/predictive", methods=["GET", "POST"])
def predictive_insights():
    """Predictive Maintenance Insights — Cards 1–4 + escalation data.

    Reads from already-memoised dashboard builders. Deterministic ranking;
    Ollama is classification-only (fault family tagging) with keyword fallback.
    """
    from .services import predictive_service as ps
    raw = _read_filters()
    filters = ctx.normalize_filters(raw)
    try:
        data = ps.build_predictive_insights(filters)
        return jsonify({"ok": True, "filters": filters, "data": data,
                        "draft_label": config.DRAFT_LABEL})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "filters": filters}), 500


def _predictive_wording_fallback(structured: dict) -> dict:
    machine = str(structured.get("machine") or "This machine").strip()
    issue = str(structured.get("selectedIssue") or "the selected issue").strip()
    mr_count = structured.get("mrCount")
    total = structured.get("totalMachineMr")
    latest = str(structured.get("latestOccurrence") or "").strip()
    keywords = structured.get("relatedKeywords") if isinstance(structured.get("relatedKeywords"), list) else []
    cause = str(structured.get("likelyCause") or "").strip()
    stock = str(structured.get("stockDecision") or "").strip()

    issue_lower = issue.lower().rstrip(".")
    summary = f"{machine} has a recurring {issue_lower} fault pattern."
    if mr_count not in (None, "") and total not in (None, ""):
        summary += f" This issue appeared in {mr_count} of {total} MR records"
        if latest:
            summary += f", with the latest occurrence on {latest}"
        summary += "."
    elif latest:
        summary += f" The latest occurrence was on {latest}."
    if keywords:
        kw_str = ", ".join(str(k) for k in keywords[:5])
        summary += f" Repeated keywords include {kw_str}, indicating possible wear or damage in related components."

    inspect_line = (f"Inspect {cause}." if cause else
                    f"Inspect the area related to {issue_lower} for leakage, looseness, or wear.")
    action = "\n\n".join([
        inspect_line,
        "Prepare the related spare parts listed below before the next repair.",
        (stock or "Check store quantity first. If stock is unavailable or below minimum, raise a purchase request using Gen PO vendor/price history as reference."),
    ])

    return {
        "faultPatternSummary": summary,
        "recommendedAction": action,
        "technicianNote": "Technician/Engineer verification required before action.",
        "translatedSpareParts": [],
    }


def _clean_predictive_wording(parsed: dict, fallback: dict) -> dict:
    cleaned = {}
    for key in ("faultPatternSummary", "technicianNote"):
        value = parsed.get(key) if isinstance(parsed, dict) else None
        value = str(value or "").strip()
        cleaned[key] = value[:800] if value else fallback.get(key, "")
    ra = (parsed.get("recommendedAction") or parsed.get("suggestedAction") or "") if isinstance(parsed, dict) else ""
    cleaned["recommendedAction"] = str(ra).strip()[:1200] or fallback.get("recommendedAction", "")
    translated = []
    raw_parts = (parsed.get("translatedSpareParts") or []) if isinstance(parsed, dict) else []
    if isinstance(raw_parts, list):
        for p in raw_parts[:10]:
            if not isinstance(p, dict):
                continue
            orig = str(p.get("originalName") or "").strip()
            eng = str(p.get("englishName") or "").strip()
            conf = str(p.get("translationConfidence") or "low").strip().lower()
            if orig:
                translated.append({
                    "originalName": orig,
                    "englishName": eng or "Translation to verify",
                    "translationConfidence": conf if conf in ("high", "medium", "low") else "low",
                })
    cleaned["translatedSpareParts"] = translated or fallback.get("translatedSpareParts", [])
    return cleaned


@mira_bp.route("/predictive-wording", methods=["POST"])
def predictive_wording():
    """Optional Ollama wording for already-calculated forecast modal data."""
    body = request.get_json(silent=True) or {}
    structured = body.get("structured") if isinstance(body.get("structured"), dict) else {}
    cache_key = str(body.get("cacheKey") or json.dumps(structured, sort_keys=True, default=str))[:900]
    if cache_key in _PREDICTIVE_WORDING_CACHE:
        return jsonify(_PREDICTIVE_WORDING_CACHE[cache_key])

    fallback = _predictive_wording_fallback(structured)
    response = {
        "ok": True,
        "provider": "rule_based",
        "fallback": True,
        "wording": fallback,
    }

    if config.PROVIDER_MODE not in ("auto", "ollama"):
        _PREDICTIVE_WORDING_CACHE[cache_key] = response
        return jsonify(response)

    try:
        from .providers import OllamaMiraProvider, generate_with_ollama
        provider = OllamaMiraProvider()
        model = provider.resolve_model()
        if not model:
            _PREDICTIVE_WORDING_CACHE[cache_key] = response
            return jsonify(response)

        raw_parts_for_trans = structured.get("rawSparePartsForTranslation") or []
        if not isinstance(raw_parts_for_trans, list):
            raw_parts_for_trans = []
        allowed = {
            "machine": structured.get("machine"),
            "selectedIssue": structured.get("selectedIssue"),
            "mrCount": structured.get("mrCount"),
            "totalMachineMr": structured.get("totalMachineMr"),
            "latestOccurrence": structured.get("latestOccurrence"),
            "medianInterval": structured.get("medianInterval"),
            "relatedKeywords": (structured.get("relatedKeywords") or [])[:8] if isinstance(structured.get("relatedKeywords"), list) else [],
            "likelyCause": structured.get("likelyCause"),
            "stockDecision": structured.get("stockDecision"),
            "rawSparePartsForTranslation": raw_parts_for_trans[:5],
        }
        system_prompt = (
            "You are rewriting maintenance dashboard text. Do not invent facts, item numbers, costs, vendors, "
            "stock status, or purchase decisions. Only rewrite the structured data provided. "
            "If translating Thai spare-part names, provide a short practical English translation and keep the "
            "original name. Return valid JSON only."
        )
        user_prompt = (
            "STRUCTURED_DATA_JSON:\n"
            + json.dumps(allowed, ensure_ascii=False, default=str, indent=2)
            + "\n\nReturn exactly this JSON schema:\n"
            + json.dumps({
                "faultPatternSummary": "short professional summary: machine name + issue + MR count/total + latest date + keywords",
                "recommendedAction": "3 paragraphs separated by \\n\\n: (1) inspection action, (2) prepare spare parts, (3) stock/purchase action",
                "technicianNote": "Technician/Engineer verification required before action.",
                "translatedSpareParts": [
                    {"originalName": "original", "englishName": "English translation", "translationConfidence": "high/medium/low"}
                ],
            }, ensure_ascii=False)
        )
        raw = generate_with_ollama(
            system_prompt,
            user_prompt,
            model=model,
            timeout=8,
            format_json=True,
        )
        parsed = json.loads(raw)
        response = {
            "ok": True,
            "provider": "ollama",
            "model": model,
            "fallback": False,
            "wording": _clean_predictive_wording(parsed, fallback),
        }
    except Exception as exc:
        current_app.logger.debug("predictive-wording: Ollama fallback (%s)", exc)

    _PREDICTIVE_WORDING_CACHE[cache_key] = response
    return jsonify(response)


@mira_bp.route("/verdict/run", methods=["POST"])
def mr_triage_run():
    """Manually trigger a triage run for a scope (admin/testing). Optional body:
    {"scope": "...", "date": "YYYY-MM-DD"}. Local-only convenience; the scheduler
    runs this automatically each morning."""
    body = request.get_json(silent=True) or {}
    scope = body.get("scope") or request.args.get("scope") or "Stage 1"
    date_str = body.get("date") or request.args.get("date")
    try:
        import mr_triage_service as triage
        review_date = None
        if date_str:
            from datetime import date as _d
            review_date = _d.fromisoformat(str(date_str)[:10])
        verdict = triage.run_triage_for_scope(scope, review_date)
        return jsonify({"ok": True, "verdict": verdict})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
