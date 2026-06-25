"""
kpiQueryService — MIRA's read-only window onto dashboard KPI outputs.

EVERY value returned here is extracted from the SAME builders the dashboard uses:

    * downtime_service.build_downtime_payload        -> MTTR, MTBF, open/closed WO,
                                                        data-reliability counts, WO rows
    * downtime page preventive/corrective classifier -> preventive vs corrective mix
    * pm_schedule_service.build_pm_schedule_metrics_payload -> PM schedule status, Stage 1/2
    * asset_mapping.load_asset_mapping               -> asset-group / stage context

MIRA does NOT recompute MTTR / MTBF / open work orders / PM status. It only reads,
selects, and reshapes already-computed outputs so its answers can never conflict
with the dashboard. Per-asset / per-group filters select subsets of rows the
builder ALREADY computed — they never trigger a different calculation.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
from collections import Counter
from datetime import date, datetime

# Existing dashboard builders (top-level modules on the backend path).
from downtime_service import build_downtime_payload
from pm_schedule_service import build_pm_schedule_metrics_payload

from ..core import context as ctx

# Tiny per-process memo so one MIRA request that needs several KPIs does not call
# the same heavy (already-cached) builder repeatedly. Short TTL; keyed by inputs.
_MEMO: dict[tuple, tuple[float, object]] = {}
_MEMO_TTL_SECONDS = 1800


def _memoized(key, producer):
    now = time.time()
    hit = _MEMO.get(key)
    if hit and (now - hit[0]) < _MEMO_TTL_SECONDS:
        return hit[1]
    value = producer()
    _MEMO[key] = (now, value)
    return value


# ── Underlying payloads (cached by the dashboard + memoised here) ────────────────
def _downtime_management(filters: dict) -> dict:
    period = ctx.resolve_downtime_period(filters)
    key = ("downtime", filters["stage"], period["period"], period["month"], period["start"], period["end"])

    def produce():
        payload = build_downtime_payload(
            period=period["period"],
            month=period["month"],
            start=period["start"],
            end=period["end"],
            work_orders_only=True,
            stage=filters["stage"],
            allow_excel_fallback=False,
        )
        return payload.get("management", {}) or {}

    return _memoized(key, produce)


def _pm_payload(filters: dict) -> dict:
    mode = filters.get("period_mode")
    start = filters.get("start")
    end = filters.get("end")
    key = ("pm", filters["stage"], filters["year"], filters["month"], mode, str(start), str(end))
    return _memoized(key, lambda: build_pm_schedule_metrics_payload(
        stage=filters["stage"], year=filters["year"], month=filters["month"],
        period_mode=mode, start=start, end=end, allow_excel_fallback=False,
    ))


def _sql_stage_label(stage: str | None) -> str | None:
    text = str(stage or "").strip().lower()
    if text == "stage1":
        return "Stage 1"
    if text == "stage2":
        return "Stage 2"
    return None


def _sql_spare_rows(filters: dict | None = None, *transaction_types: str) -> list[dict]:
    sql_stage = _sql_stage_label((filters or {}).get("stage"))
    key = ("sql-spare-rows", sql_stage, tuple(transaction_types))

    def produce():
        import db as _db

        if not transaction_types:
            return _db.load_spare_parts_from_sql(stage=sql_stage)
        rows: list[dict] = []
        for transaction_type in transaction_types:
            rows.extend(
                _db.load_spare_parts_from_sql(
                    stage=sql_stage,
                    transaction_type=transaction_type,
                )
            )
        return rows

    return _memoized(key, produce)


def _overview_freshness() -> dict:
    return _memoized(
        ("overview-freshness",),
        lambda: __import__("db").get_overview_freshness(),
    )


def _downtime_all_year_work_orders(filters: dict) -> list[dict]:
    key = ("downtime-all-years-rows", filters["stage"])

    def produce():
        payload = build_downtime_payload(
            period="all_years",
            month=None,
            start=None,
            end=None,
            work_orders_only=True,
            stage=filters["stage"],
            allow_excel_fallback=False,
        )
        management = payload.get("management", {}) or {}
        return management.get("work_orders", []) or []

    return _memoized(key, produce)


# ── Helpers to narrow already-computed rows by asset / group ─────────────────────
def _matches_asset_group(row: dict, filters: dict) -> bool:
    if filters.get("assetId"):
        rid = str(row.get("asset_id") or "").upper()
        if rid != filters["assetId"].upper():
            return False
    if filters.get("mainAssetGroup"):
        grp = str(row.get("machine_group") or row.get("mainAssetGroup") or "").lower()
        if filters["mainAssetGroup"].lower() not in grp:
            return False
    return True


def _matches_work_order_filters(row: dict, filters: dict) -> bool:
    if not _matches_asset_group(row, filters):
        return False
    if filters.get("status"):
        want = filters["status"].lower()
        cat = str(row.get("status_category") or "").lower()
        state = str(row.get("request_state") or "").lower()
        if want in {"open"} and not row.get("is_open"):
            return False
        if want in {"closed", "finished"} and row.get("is_open"):
            return False
        if want not in {"open", "closed", "finished"} and want not in cat and want not in state:
            return False
    if filters.get("maintenanceType"):
        mt = filters["maintenanceType"].lower()
        blob = " ".join(str(row.get(k) or "") for k in ("maintenance_job_type", "job_trade")).lower()
        if mt not in blob:
            return False
    if filters.get("subAssetGroup"):
        blob = " ".join(str(row.get(k) or "") for k in ("mappedSubAssetGroup", "mapped_sub_asset_group", "machine_group")).lower()
        if filters["subAssetGroup"].lower() not in blob:
            return False
    return True


def _clean_mix_text(value) -> str:
    text = str(value or "").strip()
    return text if text and text != "--" else ""


def _parse_mix_datetime(value):
    if value in (None, "", "--"):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized.split(".")[0]):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _mix_row_datetime(row: dict):
    for key in (
        "request_created_time",
        "created_date",
        "start_time",
        "actual_start_time",
        "actual_start",
        "maintenance_start_time",
    ):
        parsed = _parse_mix_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _filtered_work_order_rows(filters: dict) -> list[dict]:
    return [
        row for row in _downtime_all_year_work_orders(filters)
        if _matches_work_order_filters(row, filters)
    ]


_MR_OPEN_STATUSES = {"new", "in progress", "open", "ongoing"}
_MR_CLOSED_STATUSES = {"finished", "closed", "confirmed", "confirm", "completed", "complete", "resolved", "done"}
_MR_REJECTED_STATUSES = {"rejected", "reject", "cancelled", "canceled"}
_SEVERITY_ORDER = {"S1": 1, "S2": 2, "S3": 3, "S4": 4}


def _normalize_status_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _mr_status_bucket(row: dict) -> str:
    status = _normalize_status_text(row.get("request_state") or row.get("status") or row.get("lifecycle_state"))
    if not status:
        return "unknown"
    if status in _MR_OPEN_STATUSES or status.replace(" ", "") == "inprogress":
        return "open"
    if status in _MR_CLOSED_STATUSES:
        return "closed"
    if status in _MR_REJECTED_STATUSES:
        return "rejected"
    return "unknown"


def _mr_raised_date(row: dict) -> date | None:
    dt = _mix_row_datetime(row)
    return dt.date() if dt is not None else None


def _window_contains_raised_date(row: dict, window: dict) -> bool:
    raised_on = _mr_raised_date(row)
    return bool(raised_on and window["start_date"] <= raised_on <= window["end_date"])


def _selected_period_work_order_rows(filters: dict, rows: list[dict] | None = None) -> list[dict]:
    window = ctx.resolved_window(filters)
    filtered_rows = rows if rows is not None else _filtered_work_order_rows(filters)
    return [row for row in filtered_rows if _window_contains_raised_date(row, window)]


def _work_order_linked(row: dict) -> bool:
    work_order_id = str(row.get("work_order_id") or row.get("wo_id") or "").strip()
    return bool(work_order_id and work_order_id != "--")


def _asset_identity(row: dict) -> tuple[str | None, str]:
    asset_id = str(row.get("asset_id") or "").strip() or None
    asset_name = str(
        row.get("asset_display_name")
        or row.get("mapped_asset_name")
        or row.get("machine_name")
        or row.get("asset_name")
        or asset_id
        or "Unknown Asset"
    ).strip() or (asset_id or "Unknown Asset")
    return asset_id, asset_name


def _functional_location_value(row: dict) -> str:
    return str(
        row.get("raw_functional_location")
        or row.get("functional_location")
        or row.get("location")
        or row.get("building")
        or row.get("machine_group")
        or "Unknown"
    ).strip() or "Unknown"


# Asset IDs that are placeholders for a missing asset rather than a real asset.
_MISSING_ASSET_ID_TOKENS = {"", "--", "-", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "WO-ASSET"}

# Asset *names* that are general areas / zones, not a true machine asset
# (e.g. "Production Low Risk area", "Work Area"). Counted separately so they are
# never reported as if they were a specific machine.
_GENERAL_AREA_ASSET_RE = re.compile(
    r"\b(?:low|high|medium)\s+risk\s+area\b|\bwork\s+area\b|\bgeneral\s+area\b|\bproduction\s+area\b",
    re.IGNORECASE,
)


def _is_missing_asset_id(row: dict) -> bool:
    return str(row.get("asset_id") or "").strip().upper() in _MISSING_ASSET_ID_TOKENS


def _is_general_area_asset(name: str | None) -> bool:
    text = str(name or "").strip()
    return bool(text) and bool(_GENERAL_AREA_ASSET_RE.search(text))


def _severity_label(row: dict) -> str | None:
    raw = str(row.get("service_level") or row.get("priority") or row.get("severity") or "").strip()
    if not raw or raw == "--":
        return None
    cleaned = raw.upper().replace("LEVEL", "").replace(" ", "")
    if cleaned.startswith("S") and cleaned[1:].isdigit():
        return f"S{cleaned[1:]}"
    if cleaned.isdigit():
        return f"S{cleaned}"
    return raw


def _severity_sort_key(label: str) -> tuple[int, str]:
    return (_SEVERITY_ORDER.get(label, 99), label)


def _parse_calendar_date(value):
    parsed = _parse_mix_datetime(value)
    if parsed is not None:
        return parsed.date()
    if isinstance(value, date):
        return value
    return None


def _window_contains(value, filters: dict) -> bool:
    dt = _parse_calendar_date(value)
    if dt is None:
        return False
    window = ctx.resolved_window(filters)
    return window["start_date"] <= dt <= window["end_date"]


def _window_value_for_yoy(rows: list[dict], window: dict) -> float:
    total = 0.0
    for row in rows:
        dt = _parse_calendar_date(row.get("project_date"))
        if dt is None or dt < window["start_date"] or dt > window["end_date"]:
            continue
        total += float(row.get("total_consumption") or 0)
    return round(total, 2)


_SPARE_NON_STOCK_CLASSES = {
    "nonstocksparepart",
    "nonstocksparepartdirectpurchase",
}
_SPARE_SERVICE_CLASSES = {
    "servicelabourrepair",
    "nonsparepartservice",
}


def _spare_row_extra(row: dict) -> dict:
    try:
        data = json.loads(row.get("extra_json") or "{}") or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_spare_classification(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _sql_project_transaction_rows(filters: dict) -> list[dict]:
    rows = _sql_spare_rows(filters, "project_txn")
    parsed = []
    for row in rows:
        extra = _spare_row_extra(row)
        parsed.append({
            "project_date": row.get("transaction_date"),
            "transaction_id": extra.get("transaction_id") or row.get("item_number"),
            "work_order_id": row.get("pr_number"),
            "asset_id": row.get("asset_id"),
            "original_description": extra.get("original_description") or row.get("item_name"),
            "translated_description": extra.get("translated_description") or row.get("item_name"),
            "clean_description": extra.get("clean_description"),
            "item_category": extra.get("item_category") or row.get("category"),
            "quantity_used": extra.get("quantity_used") if extra.get("quantity_used") is not None else row.get("quantity"),
            "total_consumption": extra.get("total_consumption") if extra.get("total_consumption") is not None else row.get("total_value"),
            "unit_cost_estimate": extra.get("unit_cost_estimate") if extra.get("unit_cost_estimate") is not None else row.get("unit_price"),
            "link_status": extra.get("link_status") or row.get("classification"),
            "equipment_name": extra.get("equipment_name") or row.get("asset_name"),
        })
    return parsed


def _sql_movement_consumption_rows(filters: dict) -> list[dict]:
    rows = _sql_spare_rows(filters, "movement")
    parsed = []
    for row in rows:
        extra = _spare_row_extra(row)
        parsed.append({
            "project_date": row.get("transaction_date"),
            "transaction_id": extra.get("document_number") or row.get("item_number"),
            "work_order_id": extra.get("work_order_id"),
            "asset_id": row.get("asset_id"),
            "original_description": row.get("item_name"),
            "translated_description": row.get("item_name"),
            "clean_description": row.get("item_name"),
            "item_category": row.get("category"),
            "quantity_used": row.get("quantity"),
            "total_consumption": row.get("total_value"),
            "unit_cost_estimate": row.get("unit_price"),
            "link_status": "Linked" if row.get("asset_id") else "Unlinked",
            "equipment_name": row.get("asset_name"),
        })
    return parsed


def _previous_window(filters: dict) -> dict:
    current = ctx.resolved_window(filters)
    previous_year = current["start_date"].year - 1
    if current["mode"] == "month":
        month = current["start_date"].month
        last_day = calendar.monthrange(previous_year, month)[1]
        return {
            "mode": "month",
            "label": f"{calendar.month_name[month]} {previous_year}",
            "start_date": date(previous_year, month, 1),
            "end_date": date(previous_year, month, last_day),
        }
    if current["mode"] == "ytd":
        end_month = current["end_date"].month
        end_day = min(current["end_date"].day, calendar.monthrange(previous_year, end_month)[1])
        return {
            "mode": "ytd",
            "label": f"YTD {previous_year}",
            "start_date": date(previous_year, 1, 1),
            "end_date": date(previous_year, end_month, end_day),
        }
    return {
        "mode": "full_year",
        "label": f"Full Year {previous_year}",
        "start_date": date(previous_year, 1, 1),
        "end_date": date(previous_year, 12, calendar.monthrange(previous_year, 12)[1]),
    }


def _matches_mix_window(row: dict, filters: dict) -> bool:
    return _window_contains_raised_date(row, ctx.resolved_window(filters))


def _opening_backlog_rows(filters: dict, rows: list[dict] | None = None) -> list[dict]:
    """Open MR raised before the selected window (carry-over backlog rows)."""
    window = ctx.resolved_window(filters)
    filtered_rows = rows if rows is not None else _filtered_work_order_rows(filters)
    carry = []
    for row in filtered_rows:
        raised_on = _mr_raised_date(row)
        if raised_on is None:
            continue
        if raised_on < window["start_date"] and _mr_status_bucket(row) == "open":
            carry.append(row)
    return carry


def _opening_backlog_count(filters: dict, rows: list[dict] | None = None) -> int:
    return len(_opening_backlog_rows(filters, rows))


def _request_state_counts(rows: list[dict]) -> dict:
    """Raw request-state split (In progress / New / Finished / Confirm / Rejected)."""
    counts: Counter[str] = Counter(_clean_mix_text(r.get("request_state")) for r in rows)
    return {
        "in_progress": counts.get("In progress", 0),
        "new": counts.get("New", 0),
        "finished": counts.get("Finished", 0),
        "confirm": counts.get("Confirm", 0),
        "rejected": counts.get("Rejected", 0),
    }


def _format_focus_hours(value) -> str:
    if value is None:
        return "Not available"
    return f"{float(value):,.2f} h"


def _build_focus_asset_summary(
    *,
    lowest_mtbf_asset_row: dict | None,
    top_asset_by_work_orders: dict | None,
    mttr_summary: dict,
    downtime_summary: dict,
) -> dict:
    if lowest_mtbf_asset_row:
        name = lowest_mtbf_asset_row.get("asset_name") or lowest_mtbf_asset_row.get("asset_id")
        if name:
            hours = lowest_mtbf_asset_row.get("average_mtbf_hours")
            return {
                "name": name,
                "asset_id": lowest_mtbf_asset_row.get("asset_id"),
                "reason": f"Lowest MTBF in scope at {_format_focus_hours(hours)}",
                "kind": "asset",
            }
    if top_asset_by_work_orders:
        name = top_asset_by_work_orders.get("asset_name") or top_asset_by_work_orders.get("asset_id")
        if name:
            count = int(top_asset_by_work_orders.get("work_order_count") or 0)
            count_text = f"{count:,}" if count else "selected"
            return {
                "name": name,
                "asset_id": top_asset_by_work_orders.get("asset_id"),
                "reason": f"Highest MR count in scope ({count_text})",
                "kind": "asset",
            }
    highest_mttr_group = mttr_summary.get("highest_mttr_machine_group")
    if highest_mttr_group:
        return {
            "name": highest_mttr_group,
            "asset_id": None,
            "reason": f"Highest MTTR group at {_format_focus_hours(mttr_summary.get('highest_mttr_hours'))}",
            "kind": "machine_group",
        }
    highest_downtime_group = downtime_summary.get("highest_downtime_machine_group")
    if highest_downtime_group:
        return {
            "name": highest_downtime_group,
            "asset_id": None,
            "reason": "Highest downtime workload in scope",
            "kind": "machine_group",
        }
    return {"name": None, "asset_id": None, "reason": None, "kind": None}


_PC_PREVENTIVE_PATTERNS = [
    (re.compile(r"\bprevent(?:ive|ative)\b", re.IGNORECASE), "preventive"),
    (re.compile(r"\bplanned maintenance\b", re.IGNORECASE), "planned maintenance"),
    (re.compile(r"\bscheduled\b|\bschedule\b", re.IGNORECASE), "scheduled"),
    (re.compile(r"\broutine\b|\bperiodic\b", re.IGNORECASE), "routine / periodic"),
    (re.compile(r"\bpm\b|\bp\.m\.\b", re.IGNORECASE), "PM"),
    (re.compile(r"\binspect(?:ion)?\b|\bcheck(?:ing|list)?\b", re.IGNORECASE), "inspection / check"),
    (re.compile(r"\blubricat(?:e|ion)?\b|\bgreas(?:e|ing)?\b", re.IGNORECASE), "lubrication"),
    (re.compile(r"\bcalibrat(?:e|ion)?\b", re.IGNORECASE), "calibration"),
    (re.compile(r"\bclean(?:ing)?\b", re.IGNORECASE), "cleaning"),
    (re.compile(r"\bweekly\b|\bmonthly\b|\bquarterly\b|\bannual(?:ly)?\b", re.IGNORECASE), "frequency wording"),
]

_PC_CORRECTIVE_PATTERNS = [
    (re.compile(r"\bcorrective\b|\bcm\b", re.IGNORECASE), "corrective"),
    (re.compile(r"\bbreak\s*down\b|\bbreakdown\b", re.IGNORECASE), "breakdown"),
    (re.compile(r"\brepair\b|\bfix\b|\btroubleshoot", re.IGNORECASE), "repair / troubleshoot"),
    (re.compile(r"\bfail(?:ure|ed)?\b|\bfault\b|\berror\b", re.IGNORECASE), "failure / fault"),
    (re.compile(r"\bleak(?:age)?\b|\bdamage(?:d)?\b|\bbroken\b", re.IGNORECASE), "leak / damage"),
    (re.compile(r"\balarm\b|\btrip(?:ped)?\b|\babnormal\b", re.IGNORECASE), "alarm / abnormal"),
    (re.compile(r"\burgent\b|\bemergency\b", re.IGNORECASE), "urgent / emergency"),
]


def _match_mix_patterns(text: str, patterns) -> list[str]:
    value = str(text or "")
    return [label for regex, label in patterns if regex.search(value)]


def _mix_type_text(row: dict) -> str:
    return " | ".join(filter(None, (
        _clean_mix_text(row.get("maintenance_job_type")),
        _clean_mix_text(row.get("job_trade")),
        _clean_mix_text(row.get("maintenance_type")),
        _clean_mix_text(row.get("request_type")),
        _clean_mix_text(row.get("work_order_type")),
        _clean_mix_text(row.get("job_type")),
        _clean_mix_text(row.get("system")),
    )))


def _mix_narrative_text(row: dict) -> str:
    return " | ".join(filter(None, (
        _clean_mix_text(row.get("description_original")),
        _clean_mix_text(row.get("translated_description")),
        _clean_mix_text(row.get("description")),
        _clean_mix_text(row.get("remarks")),
        _clean_mix_text(row.get("notes")),
        _clean_mix_text(row.get("problem")),
        _clean_mix_text(row.get("details")),
    )))


def _performance_flag(preventive_count: int, corrective_count: int) -> str | None:
    if preventive_count + corrective_count <= 0:
        return None
    if corrective_count <= preventive_count:
        return "Good"
    if preventive_count <= 0:
        return "Critical"
    if corrective_count <= preventive_count * 1.2:
        return "Watch"
    return "Critical"


def _classify_preventive_corrective_row(row: dict) -> tuple[str, bool]:
    type_text = _mix_type_text(row)
    narrative_text = _mix_narrative_text(row)
    preventive_matches = _match_mix_patterns(type_text, _PC_PREVENTIVE_PATTERNS)
    corrective_matches = _match_mix_patterns(type_text, _PC_CORRECTIVE_PATTERNS)
    narrative_preventive_matches = _match_mix_patterns(narrative_text, _PC_PREVENTIVE_PATTERNS)
    is_preventive = bool(preventive_matches)
    review_flag = (not is_preventive) and bool(preventive_matches or narrative_preventive_matches)
    _ = corrective_matches
    return ("preventive" if is_preventive else "corrective"), review_flag


def _top_asset_summary(rows: list[dict], period: str) -> dict:
    counts: Counter[tuple[str | None, str]] = Counter()
    for row in rows:
        counts[_asset_identity(row)] += 1
    if not counts:
        return {"name": None, "asset_id": None, "count": 0, "is_placeholder": False, "reason": f"No MR were raised in {period}."}
    (asset_id, asset_name), count = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0][1], item[0][0] or ""),
    )[0]
    is_placeholder = _is_general_area_asset(asset_name) or str(asset_id or "").strip().upper() in _MISSING_ASSET_ID_TOKENS
    return {
        "name": asset_name,
        "asset_id": asset_id,
        "count": int(count),
        "is_placeholder": is_placeholder,
        "reason": f"Highest MR count in {period} ({int(count):,} MR)"
                  + (" — general area/placeholder, not a true machine asset" if is_placeholder else ""),
    }


def _top_actual_machine_asset_summary(rows: list[dict], period: str) -> dict:
    """Top asset by MR count, excluding general-area placeholders and missing IDs."""
    counts: Counter[tuple[str | None, str]] = Counter()
    for row in rows:
        if _is_missing_asset_id(row):
            continue
        asset_id, asset_name = _asset_identity(row)
        if _is_general_area_asset(asset_name):
            continue
        counts[(asset_id, asset_name)] += 1
    if not counts:
        return {"name": None, "asset_id": None, "count": 0,
                "reason": f"No specific machine asset MR were recorded in {period}."}
    (asset_id, asset_name), count = sorted(
        counts.items(),
        key=lambda item: (-item[1], item[0][1], item[0][0] or ""),
    )[0]
    return {
        "name": asset_name,
        "asset_id": asset_id,
        "count": int(count),
        "reason": f"Highest MR count among true machine assets in {period} ({int(count):,} MR)",
    }


def _top_functional_location_summary(rows: list[dict], period: str) -> dict:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[_functional_location_value(row)] += 1
    if not counts:
        return {"name": None, "count": 0, "reason": f"No functional location activity was recorded in {period}."}
    location, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return {
        "name": location,
        "count": int(count),
        "reason": f"Highest MR count in {period} ({int(count):,} MR)",
    }


def _severity_breakdown(rows: list[dict]) -> list[dict]:
    counts: Counter[str] = Counter()
    for row in rows:
        label = _severity_label(row)
        if label:
            counts[label] += 1
    return [
        {"label": label, "count": int(count)}
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], _severity_sort_key(item[0])))
    ]


def _mr_data_quality_counts(rows: list[dict]) -> dict:
    missing_asset = 0
    missing_functional_location = 0
    unknown_status = 0
    general_area = 0
    issue_rows = 0
    general_area_breakdown: Counter[str] = Counter()
    for row in rows:
        has_issue = False
        # Missing asset ID covers blanks AND placeholder tokens like "WO-ASSET".
        if _is_missing_asset_id(row):
            missing_asset += 1
            has_issue = True
        # General area / placeholder asset name (not a true machine asset).
        _asset_id, asset_name = _asset_identity(row)
        if _is_general_area_asset(asset_name):
            general_area += 1
            general_area_breakdown[asset_name] += 1
            has_issue = True
        if not str(row.get("raw_functional_location") or row.get("functional_location") or row.get("location") or row.get("building") or "").strip():
            missing_functional_location += 1
            has_issue = True
        if _mr_status_bucket(row) == "unknown":
            unknown_status += 1
            has_issue = True
        if has_issue:
            issue_rows += 1
    return {
        "issue_row_count": issue_rows,
        "missing_asset_count": missing_asset,
        "general_area_asset_count": general_area,
        "general_area_asset_breakdown": [
            {"name": name, "count": int(count)}
            for name, count in general_area_breakdown.most_common()
        ],
        "missing_functional_location_count": missing_functional_location,
        "unknown_status_count": unknown_status,
    }


def get_mr_activity_summary(filters: dict) -> dict:
    """Selected-period MR activity summary aligned to the Downtime page raised-date logic."""
    f = ctx.normalize_filters(filters)
    window = ctx.resolved_window(f)
    filtered_rows = _filtered_work_order_rows(f)
    selected_rows = _selected_period_work_order_rows(f, filtered_rows)

    open_count = 0
    closed_count = 0
    rejected_count = 0
    unknown_count = 0
    preventive_count = 0
    corrective_count = 0
    review_count = 0
    work_orders_linked = 0

    for row in selected_rows:
        bucket = _mr_status_bucket(row)
        if bucket == "open":
            open_count += 1
        elif bucket == "closed":
            closed_count += 1
        elif bucket == "rejected":
            rejected_count += 1
        else:
            unknown_count += 1

        classification, needs_review = _classify_preventive_corrective_row(row)
        if classification == "preventive":
            preventive_count += 1
        else:
            corrective_count += 1
        if needs_review:
            review_count += 1

        if _work_order_linked(row):
            work_orders_linked += 1

    mr_raised = len(selected_rows)
    carry_over_rows = _opening_backlog_rows(f, filtered_rows)
    carry_over_open_mr = len(carry_over_rows)
    selected_split = _request_state_counts(selected_rows)
    carry_split = _request_state_counts(carry_over_rows)
    total_active_workload = mr_raised + carry_over_open_mr
    # Rejected/cancelled MR are excluded from the closure rate denominator — they
    # were never actioned so including them would understate actual completion.
    valid_denominator = mr_raised - rejected_count
    closure_rate_pct = round((closed_count / valid_denominator) * 100.0, 1) if valid_denominator > 0 else None
    resolution_rate_pct = round(((closed_count + rejected_count) / mr_raised) * 100.0, 1) if mr_raised else None
    open_rate_pct = round((open_count / mr_raised) * 100.0, 1) if mr_raised else None
    wo_created_pct = round((work_orders_linked / mr_raised) * 100.0, 1) if mr_raised else None
    top_asset = _top_asset_summary(selected_rows, window["label"])
    top_actual_asset = _top_actual_machine_asset_summary(selected_rows, window["label"])
    top_functional_location = _top_functional_location_summary(selected_rows, window["label"])
    severity_breakdown = _severity_breakdown(selected_rows)
    data_quality = _mr_data_quality_counts(selected_rows)

    return {
        "window": window["label"],
        "window_mode": window["mode"],
        "stage": f["stage"],
        "mr_raised": mr_raised,
        "open_count": open_count,
        "closed_count": closed_count,
        "rejected_count": rejected_count,
        "unknown_count": unknown_count,
        # Status sub-splits for the selected period.
        "in_progress_count": selected_split["in_progress"],
        "new_count": selected_split["new"],
        "finished_count": selected_split["finished"],
        "confirm_count": selected_split["confirm"],
        "closure_rate_pct": closure_rate_pct,
        "resolution_rate_pct": resolution_rate_pct,
        "open_rate_pct": open_rate_pct,
        "carry_over_open_mr": carry_over_open_mr,
        "carry_over_in_progress": carry_split["in_progress"],
        "carry_over_new": carry_split["new"],
        "total_active_workload": total_active_workload,
        "preventive_count": preventive_count,
        "corrective_count": corrective_count,
        "preventive_ratio_pct": round((preventive_count / mr_raised) * 100.0, 1) if mr_raised else None,
        "corrective_ratio_pct": round((corrective_count / mr_raised) * 100.0, 1) if mr_raised else None,
        "performance_status": _performance_flag(preventive_count, corrective_count),
        "work_orders_with_linked_wo": work_orders_linked,
        "wo_created_pct": wo_created_pct,
        "top_recorded_asset_name": top_asset["name"],
        "top_recorded_asset_id": top_asset["asset_id"],
        "top_recorded_asset_count": top_asset["count"],
        "top_recorded_asset_is_placeholder": top_asset["is_placeholder"],
        "top_recorded_asset_reason": top_asset["reason"],
        # Back-compat aliases (older callers used top_asset_*).
        "top_asset_name": top_asset["name"],
        "top_asset_id": top_asset["asset_id"],
        "top_asset_count": top_asset["count"],
        "top_asset_reason": top_asset["reason"],
        "top_actual_machine_asset_name": top_actual_asset["name"],
        "top_actual_machine_asset_id": top_actual_asset["asset_id"],
        "top_actual_machine_asset_count": top_actual_asset["count"],
        "top_actual_machine_asset_reason": top_actual_asset["reason"],
        "top_functional_location_name": top_functional_location["name"],
        "top_functional_location_count": top_functional_location["count"],
        "top_functional_location_reason": top_functional_location["reason"],
        "severity_breakdown": severity_breakdown,
        "data_quality_issue_count": data_quality["issue_row_count"],
        "missing_asset_count": data_quality["missing_asset_count"],
        "general_area_asset_count": data_quality["general_area_asset_count"],
        "general_area_asset_breakdown": data_quality["general_area_asset_breakdown"],
        "missing_functional_location_count": data_quality["missing_functional_location_count"],
        "unknown_status_count": data_quality["unknown_status_count"],
        "filtered_work_order_rows_count": len(filtered_rows),
        "selected_work_order_rows_count": mr_raised,
        "review_count": review_count,
        "source": "downtime work-order rows filtered by raised date and selected-period status mapping",
    }


# ── Public KPI functions (snake_case primary; camelCase aliases at bottom) ───────
def get_mttr(filters: dict) -> dict:
    """MTTR (Mean Time To Repair), reused from the downtime management summary."""
    f = ctx.normalize_filters(filters)
    mgmt = _downtime_management(f)
    s = mgmt.get("summary", {})
    result = {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "overall_mttr_hours": s.get("overall_mttr_hours"),
        "highest_mttr_machine_group": s.get("highest_mttr_machine_group"),
        "highest_mttr_hours": s.get("highest_mttr_hours"),
        "valid_ttr_work_orders": s.get("valid_ttr_work_orders"),
        "total_work_orders": s.get("total_work_orders"),
        "unit": "hours",
        "source": "downtime dashboard (overall_mttr_hours)",
    }
    if f.get("mainAssetGroup") or f.get("assetId"):
        rows = [r for r in mgmt.get("machine_group_rows", []) if _matches_asset_group(r, f)]
        if rows:
            result["filtered_groups"] = [
                {"machine_group": r.get("machine_group"), "mttr_hours": r.get("mttr_hours"),
                 "work_order_count": r.get("work_order_count")}
                for r in rows[:10]
            ]
    return result


def get_mtbf(filters: dict) -> dict:
    """MTBF (Mean Time Between Failures), reused from the downtime MTBF views."""
    f = ctx.normalize_filters(filters)
    mgmt = _downtime_management(f)
    mtbf = mgmt.get("mtbf", {}) or {}
    views = mtbf.get("views", {}) or {}
    selected_key = mtbf.get("selected_view") or "selected_period"
    view = views.get(selected_key) or views.get("selected_period") or {}
    s = view.get("summary", {}) if isinstance(view, dict) else {}
    result = {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "overall_average_mtbf_hours": s.get("overall_average_mtbf_hours"),
        "lowest_mtbf_hours": s.get("lowest_mtbf_hours"),
        "lowest_mtbf_asset_name": s.get("lowest_mtbf_asset_name"),
        "highest_mtbf_hours": s.get("highest_mtbf_hours"),
        "assets_with_valid_mtbf": s.get("assets_with_valid_mtbf"),
        "repeated_failure_assets": s.get("repeated_failure_assets"),
        "selected_view": selected_key,
        "scope_label": s.get("scope_label"),
        "unit": "hours",
        "source": "downtime dashboard MTBF views",
    }
    if f.get("assetId") or f.get("mainAssetGroup"):
        rows = [r for r in view.get("asset_rows", []) if _matches_asset_group(r, f)] if isinstance(view, dict) else []
        if rows:
            result["filtered_assets"] = [
                {"asset_id": r.get("asset_id"), "asset_name": r.get("asset_name"),
                 "average_mtbf_hours": r.get("average_mtbf_hours"),
                 "reliability_status": r.get("reliability_status")}
                for r in rows[:10]
            ]
    return result


def get_open_work_orders(filters: dict) -> dict:
    """Selected-period MR raised/open/closed summary aligned to the Downtime page."""
    activity = get_mr_activity_summary(filters)
    return {
        "window": activity["window"],
        "stage": activity["stage"],
        "total_work_orders": activity["mr_raised"],
        "open_work_orders": activity["open_count"],
        "closed_work_orders": activity["closed_count"],
        "rejected_work_orders": activity["rejected_count"],
        "closure_rate_pct": activity["closure_rate_pct"],
        "open_rate_pct": activity["open_rate_pct"],
        "carry_over_open_mr": activity["carry_over_open_mr"],
        "total_active_workload": activity["total_active_workload"],
        "preventive_count": activity["preventive_count"],
        "corrective_count": activity["corrective_count"],
        "wo_created_pct": activity["wo_created_pct"],
        "top_asset_by_mr_count_name": activity["top_asset_name"],
        "top_asset_reason": activity["top_asset_reason"],
        "top_functional_location_name": activity["top_functional_location_name"],
        "top_functional_location_reason": activity["top_functional_location_reason"],
        "severity_mix": activity["severity_breakdown"],
        "requires_attention_count": activity["data_quality_issue_count"],
        "missing_asset_count": activity["missing_asset_count"],
        "missing_functional_location_count": activity["missing_functional_location_count"],
        "unknown_status_count": activity["unknown_status_count"],
        "source": activity["source"],
    }


def get_preventive_corrective_summary(filters: dict) -> dict:
    """Preventive vs corrective mix, using the Downtime page work-order classifier."""
    f = ctx.normalize_filters(filters)
    scoped_rows = _selected_period_work_order_rows(f)

    preventive_count = 0
    corrective_count = 0
    review_count = 0
    for row in scoped_rows:
        classification, needs_review = _classify_preventive_corrective_row(row)
        if classification == "preventive":
            preventive_count += 1
        else:
            corrective_count += 1
            if needs_review:
                review_count += 1

    total = preventive_count + corrective_count
    return {
        "window": ctx.month_label(f),
        "month": ctx.month_value(f),
        "preventive_count": preventive_count,
        "corrective_count": corrective_count,
        "preventive_ratio_pct": round((preventive_count / total) * 100.0, 1) if total else None,
        "corrective_ratio_pct": round((corrective_count / total) * 100.0, 1) if total else None,
        "performance_status": _performance_flag(preventive_count, corrective_count),
        "total": total,
        "review_count": review_count,
        "source": "downtime work-order preventive/corrective classifier",
    }


def get_data_reliability_issues(filters: dict) -> dict:
    """Data-quality / reliability issue counts, reused from the downtime payload."""
    f = ctx.normalize_filters(filters)
    mgmt = _downtime_management(f)
    s = mgmt.get("summary", {})
    group_rows = mgmt.get("machine_group_rows", []) or []
    mtbf_summary = ((mgmt.get("mtbf", {}) or {}).get("views", {}) or {}).get("selected_period", {})
    mtbf_summary = mtbf_summary.get("summary", {}) if isinstance(mtbf_summary, dict) else {}
    return {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "requires_attention_count": s.get("requires_attention_count"),
        "invalid_missing_ttr_count": s.get("invalid_missing_ttr_count"),
        "valid_ttr_work_orders": s.get("valid_ttr_work_orders"),
        "total_work_orders": s.get("total_work_orders"),
        "mttr_missing_total": sum(int(r.get("mttr_missing_count") or 0) for r in group_rows),
        "mtbf_missing_total": sum(int(r.get("mtbf_missing_count") or 0) for r in group_rows),
        "duplicate_work_order_count": mtbf_summary.get("duplicate_work_order_count"),
        "source": "downtime dashboard quality flags",
    }


def get_pm_schedule_status(filters: dict) -> dict:
    """Preventive maintenance schedule status, reused from build_pm_schedule_metrics_payload."""
    f = ctx.normalize_filters(filters)
    pm = _pm_payload(f)
    overview = pm.get("overview", {}) or {}
    kpis = overview.get("kpis", {}) or {}
    # Period-scoped KPIs (selected month/year) — match the PM dashboard page exactly.
    period = overview.get("periodKpis", {}) or {}
    charts = overview.get("charts", {}) or {}
    dq = (overview.get("dataQuality", {}) or {}).get("counts", {})

    def _eq(scope_key):
        s = pm.get(scope_key, {}) or {}
        return (s.get("periodKpis", {}) or {}).get("scheduledInMonth", (s.get("kpis", {}) or {}).get("totalScheduled"))

    return {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        # Period-scoped values (do NOT use whole-dataset totals).
        "total_scheduled": period.get("scheduledInMonth", kpis.get("dueThisMonth")),
        "due_this_month": period.get("dueThisMonth", kpis.get("dueThisMonth")),
        "due_soon": kpis.get("dueSoon"),
        "completed": period.get("completedInMonth", kpis.get("completed")),
        "on_time_completed": period.get("onTimeInMonth"),
        "completed_manual_only": True,
        "compliance_pct": period.get("compliancePct", kpis.get("compliancePct")),
        "overdue": period.get("overdueInMonth", kpis.get("overdue")),
        "backlog": period.get("backlogInMonth", kpis.get("backlog")),
        "deferred": period.get("deferredInMonth"),
        "late_completed": period.get("lateInMonth"),
        "no_pic": period.get("noPicInMonth"),
        "year_scheduled": period.get("yearTaskCount"),
        "total_scheduled_all_periods": kpis.get("totalScheduled"),
        "coverage": kpis.get("coverage"),
        "missing_mapping": kpis.get("missingMapping"),
        "needs_review": kpis.get("needsReview"),
        "by_stage": period.get("byStage", charts.get("scheduledByStage")),
        "by_main_group": period.get("byAssetCategory", charts.get("workloadByMainGroup")),
        "by_asset_category": period.get("byAssetCategory"),
        "by_functional_location": period.get("byFunctionalLocation"),
        "data_quality": dq,
        "equipment_total": _eq("equipment"),
        "utility_total": _eq("utility"),
        "source": "pm_schedule_service.build_pm_schedule_metrics_payload (period-scoped overview.periodKpis)",
        "period_scoped": True,
    }


def get_verified_pm_metrics(filters: dict) -> dict:
    """Verified, period-scoped PM metrics with a data envelope (Part C debug output).

    Numbers come from build_pm_schedule_metrics_payload's period-scoped overview.periodKpis
    (manual-Done only). Mirrors the downtime verified-metrics structure.
    """
    f = ctx.normalize_filters(filters)
    pm_payload = _pm_payload(f)
    meta = pm_payload.get("meta", {}) or {}
    status = get_pm_schedule_status(f)
    warnings = []
    if (status.get("missing_mapping") or 0) > 0:
        warnings.append(f"{status['missing_mapping']} PM records are missing asset mapping.")
    if (status.get("needs_review") or 0) > 0:
        warnings.append(f"{status['needs_review']} PM records still need review.")
    if status.get("total_scheduled") == 0:
        warnings.append("No PM tasks are scheduled for the selected month/year.")
    return {
        "summary_type": "pm_summary",
        "filters": {
            "year": f.get("year"),
            "month": f.get("month"),
            "stage": f.get("stage") or "all",
            "asset_category": f.get("mainAssetGroup") or "All",
            "functional_location": f.get("subAssetGroup") or "All",
        },
        "data_quality": {
            "source": "PM Schedule (pm_schedule_service.build_pm_schedule_metrics_payload)",
            "rows_loaded": meta.get("taskCountAllStages"),
            "rows_after_filter": status.get("total_scheduled"),
            "date_range": ctx.month_label(f),
            "last_refreshed": meta.get("generatedAt"),
            "warnings": warnings,
        },
        "metrics": {
            "scheduled_pm": status.get("total_scheduled"),
            "due_this_month": status.get("due_this_month"),
            "completed_pm": status.get("completed"),
            "on_time_pm": status.get("on_time_completed"),
            "overdue_pm": status.get("overdue"),
            "backlog_pm": status.get("backlog"),
            "deferred_pm": status.get("deferred"),
            "pm_compliance_percent": status.get("compliance_pct"),
            "year_scheduled": status.get("year_scheduled"),
            "pm_by_stage": status.get("by_stage"),
            "pm_by_asset_category": status.get("by_asset_category"),
            "pm_by_functional_location": status.get("by_functional_location"),
        },
        "completed_basis": "manual Done only (no auto-done; scheduled week is target only)",
    }


def get_spare_parts_summary(filters: dict) -> dict:
    """SQL-backed spare-parts snapshot and consumption summary."""
    f = ctx.normalize_filters(filters)
    window = ctx.resolved_window(f)
    all_stage_filters = dict(f)
    all_stage_filters["stage"] = "all"
    inventory_rows = _sql_spare_rows(f, "inventory")
    po_rows = _sql_spare_rows(f, "gen_po", "stage_po")
    project_transactions = _sql_project_transaction_rows(f)
    annual_transactions = list(project_transactions)
    data_notes = []

    if f.get("stage") != "all" and not inventory_rows:
        sitewide_inventory_rows = _sql_spare_rows(all_stage_filters, "inventory")
        if sitewide_inventory_rows:
            inventory_rows = sitewide_inventory_rows
            data_notes.append(
                "Stage-resolved spare-parts inventory rows are not yet available in SQL, so inventory metrics are temporarily using site-wide SQL data."
            )

    if f.get("stage") != "all" and not po_rows:
        sitewide_po_rows = _sql_spare_rows(all_stage_filters, "gen_po", "stage_po")
        if sitewide_po_rows:
            po_rows = sitewide_po_rows
            data_notes.append(
                "Stage-resolved spare-parts PO rows are not yet available in SQL, so PO metrics are temporarily using site-wide SQL data."
            )

    if f.get("stage") != "all" and not project_transactions:
        sitewide_project_transactions = _sql_project_transaction_rows(all_stage_filters)
        if sitewide_project_transactions:
            project_transactions = sitewide_project_transactions
            annual_transactions = list(sitewide_project_transactions)
            data_notes.append(
                "Stage-resolved spare-parts consumption rows are not yet available in SQL, so consumption metrics are temporarily using site-wide SQL data."
            )

    filtered_po_records = [row for row in po_rows if _window_contains(row.get("transaction_date"), f)]
    filtered_project_transactions = [row for row in project_transactions if _window_contains(row.get("project_date"), f)]

    consumption_source = "project transactions"
    if not filtered_project_transactions:
        fallback_rows = [row for row in _sql_movement_consumption_rows(f) if _window_contains(row.get("project_date"), f)]
        if fallback_rows:
            filtered_project_transactions = fallback_rows
            annual_transactions = _sql_movement_consumption_rows(f)
            consumption_source = "inventory movement fallback"

    top_part_map: dict[str, dict] = {}
    for row in filtered_project_transactions:
        key = str(
            row.get("translated_description")
            or row.get("clean_description")
            or row.get("original_description")
            or "Unknown"
        ).strip() or "Unknown"
        bucket = top_part_map.setdefault(key, {"part_name": key, "value": 0.0})
        bucket["value"] += float(row.get("total_consumption") or 0)
    top_consumed_part = max(top_part_map.values(), key=lambda item: item["value"], default=None)

    previous_window = _previous_window(f)
    current_window_value = _window_value_for_yoy(annual_transactions, window)
    previous_window_value = _window_value_for_yoy(annual_transactions, previous_window)
    yoy_pct = None
    if previous_window_value:
        yoy_pct = round(((current_window_value - previous_window_value) / previous_window_value) * 100.0, 1)

    non_stock_value = 0.0
    services_value = 0.0
    manual_review_po_items = 0
    for row in filtered_po_records:
        classification = _normalize_spare_classification(row.get("classification"))
        value = float(row.get("total_value") or 0)
        if classification in _SPARE_NON_STOCK_CLASSES:
            non_stock_value += value
        elif classification in _SPARE_SERVICE_CLASSES:
            services_value += value
        if row.get("needs_review"):
            manual_review_po_items += 1

    if consumption_source != "project transactions":
        data_notes.append(
            "Project transactions were not available in SQL for the selected window, so spare-parts consumption is using inventory movement as a fallback."
        )
    if not inventory_rows:
        data_notes.append("Inventory spare-parts SQL rows are unavailable.")
    if not po_rows:
        data_notes.append("Spare-parts purchase-order SQL rows are unavailable.")
    if not annual_transactions:
        data_notes.append("Historical spare-parts SQL transactions are unavailable for YoY comparison.")

    return {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "window_mode": window["mode"],
        "current_in_stock_items": (
            sum(1 for row in inventory_rows if float(row.get("quantity") or 0) > 0)
            if inventory_rows else None
        ),
        "current_in_stock_value": round(
            sum(
                float(row.get("total_value") or 0)
                for row in inventory_rows
                if float(row.get("quantity") or 0) > 0 and row.get("total_value") is not None
            ),
            2,
        ) if inventory_rows else None,
        "drawn_from_store_value": round(
            sum(float(row.get("total_consumption") or 0) for row in filtered_project_transactions), 2
        ) if filtered_project_transactions else None,
        "non_stock_value": round(non_stock_value, 2) if filtered_po_records else None,
        "services_value": round(services_value, 2) if filtered_po_records else None,
        "top_consumed_part": top_consumed_part["part_name"] if top_consumed_part else None,
        "top_consumed_part_value": round(float(top_consumed_part["value"]), 2) if top_consumed_part else None,
        "yoy_consumption_pct": yoy_pct,
        "yoy_label": f"{previous_window['label']} to {window['label']}",
        "services_note": "Services include repair and cleaning.",
        "inventory_rows_loaded": len(inventory_rows),
        "po_rows_loaded": len(po_rows),
        "project_transaction_rows_loaded": len(project_transactions),
        "annual_transaction_rows_loaded": len(annual_transactions),
        "po_rows_after_filter": len(filtered_po_records),
        "project_transaction_rows_after_filter": len(filtered_project_transactions),
        "manual_review_po_items": manual_review_po_items,
        "source": "SQL spare_parts summary",
        "data_notes": data_notes,
    }


def get_stage_summary(filters: dict) -> dict:
    """Stage 1 vs Stage 2 side-by-side, reusing the per-stage builders."""
    base = ctx.normalize_filters(filters)
    stages = {}
    for stage_key in ("stage1", "stage2"):
        sf = dict(base)
        sf["stage"] = stage_key
        stages[stage_key] = {
            "open_work_orders": get_open_work_orders(sf),
            "mttr": get_mttr(sf),
            "mtbf": get_mtbf(sf),
            "pm_schedule": get_pm_schedule_status(sf),
            "preventive_corrective": get_preventive_corrective_summary(sf),
        }
    return {"window": ctx.month_label(base), "stage1": stages["stage1"], "stage2": stages["stage2"]}


def get_dashboard_kpi_summary(filters: dict, *, include_spare_parts: bool = True) -> dict:
    """One consolidated KPI snapshot built entirely from existing dashboard outputs."""
    f = ctx.normalize_filters(filters)
    mr_activity = get_mr_activity_summary(f)
    mttr = get_mttr(f)
    mtbf = get_mtbf(f)
    dq = get_data_reliability_issues(f)
    pm = get_pm_schedule_status(f)
    spare = get_spare_parts_summary(f) if include_spare_parts else {}
    freshness = _overview_freshness()
    opening_backlog_count = mr_activity["carry_over_open_mr"]
    total_with_backlog_count = mr_activity["total_active_workload"]
    return {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "filters": f,
        "period_mode": ctx.resolved_window(f)["mode"],
        "last_updated": freshness.get("last_updated"),
        "latest_import_time": freshness.get("latest_import_time"),
        "source_files_used": freshness.get("source_files_used") or [],
        "data_freshness": freshness,
        "work_orders": {
            "total": mr_activity["mr_raised"],
            "open": mr_activity["open_count"],
            "closed": mr_activity["closed_count"],
            "rejected": mr_activity["rejected_count"],
            "closure_rate_pct": mr_activity["closure_rate_pct"],
            "open_rate_pct": mr_activity["open_rate_pct"],
        },
        "mttr_hours": mttr["overall_mttr_hours"],
        "mtbf_hours": mtbf["overall_average_mtbf_hours"],
        "preventive_count": mr_activity["preventive_count"],
        "corrective_count": mr_activity["corrective_count"],
        "performance_status": mr_activity["performance_status"],
        "data_reliability_issue_count": mr_activity["data_quality_issue_count"],
        "opening_backlog_count": opening_backlog_count,
        "total_with_backlog_count": total_with_backlog_count,
        "pm_schedule": {
            "total_scheduled": pm["total_scheduled"],
            "completed": pm["completed"],
            "due_this_month": pm["due_this_month"],
            "due_soon": pm["due_soon"],
            "overdue": pm["overdue"],
            "compliance_pct": pm["compliance_pct"],
            "backlog": pm["backlog"],
            "missing_mapping": pm["missing_mapping"],
            "needs_review": pm["needs_review"],
            "completed_manual_only": True,
        },
        "asset_groups": pm.get("by_main_group"),
        "stage_breakdown": pm.get("by_stage"),
        "downtime_summary": {
            "total_work_orders": mr_activity["mr_raised"],
            "open_work_orders": mr_activity["open_count"],
            "closed_work_orders": mr_activity["closed_count"],
            "rejected_work_orders": mr_activity["rejected_count"],
            "in_progress_count": mr_activity["in_progress_count"],
            "new_count": mr_activity["new_count"],
            "finished_count": mr_activity["finished_count"],
            "confirm_count": mr_activity["confirm_count"],
            "closure_rate_pct": mr_activity["closure_rate_pct"],
            "open_rate_pct": mr_activity["open_rate_pct"],
            "mttr_hours": mttr["overall_mttr_hours"],
            "mtbf_hours": mtbf["overall_average_mtbf_hours"],
            "carry_over_open_mr": opening_backlog_count,
            "carry_over_in_progress": mr_activity["carry_over_in_progress"],
            "carry_over_new": mr_activity["carry_over_new"],
            "opening_backlog_count": opening_backlog_count,
            "total_active_workload": total_with_backlog_count,
            "total_with_backlog_count": total_with_backlog_count,
            # Top RECORDED asset/area (may be a general placeholder, flagged).
            "top_recorded_asset_name": mr_activity["top_recorded_asset_name"],
            "top_recorded_asset_id": mr_activity["top_recorded_asset_id"],
            "top_recorded_asset_count": mr_activity["top_recorded_asset_count"],
            "top_recorded_asset_is_placeholder": mr_activity["top_recorded_asset_is_placeholder"],
            # Top ACTUAL machine asset (placeholders + missing IDs excluded).
            "top_actual_machine_asset_name": mr_activity["top_actual_machine_asset_name"],
            "top_actual_machine_asset_id": mr_activity["top_actual_machine_asset_id"],
            "top_actual_machine_asset_count": mr_activity["top_actual_machine_asset_count"],
            "focus_asset_name": mr_activity["top_recorded_asset_name"],
            "focus_asset_id": mr_activity["top_recorded_asset_id"],
            "focus_asset_reason": mr_activity["top_recorded_asset_reason"],
            "focus_asset_kind": "area/placeholder" if mr_activity["top_recorded_asset_is_placeholder"] else "asset",
            "focus_asset_count": mr_activity["top_recorded_asset_count"],
            "top_asset_by_mr_count_name": mr_activity["top_recorded_asset_name"],
            "top_asset_by_mr_count_id": mr_activity["top_recorded_asset_id"],
            "top_asset_by_mr_count": mr_activity["top_recorded_asset_count"],
            "top_functional_location_name": mr_activity["top_functional_location_name"],
            "top_functional_location_count": mr_activity["top_functional_location_count"],
            "top_functional_location_reason": mr_activity["top_functional_location_reason"],
            # "Worst asset" uses the real machine asset, not a general area.
            "worst_asset_name": mr_activity["top_actual_machine_asset_name"] or mr_activity["top_recorded_asset_name"],
            "worst_asset_id": mr_activity["top_actual_machine_asset_id"] or mr_activity["top_recorded_asset_id"],
            "worst_asset_work_order_count": mr_activity["top_actual_machine_asset_count"] or mr_activity["top_recorded_asset_count"],
            "top_work_order_machine_group": mr_activity["top_functional_location_name"],
            "top_work_order_machine_group_count": mr_activity["top_functional_location_count"],
            "worst_machine_group_name": mr_activity["top_functional_location_name"],
            "worst_machine_group_reason": mr_activity["top_functional_location_reason"],
            "top_mttr_machine_group": mttr.get("highest_mttr_machine_group"),
            "severity_mix": mr_activity["severity_breakdown"],
            "preventive_count": mr_activity["preventive_count"],
            "corrective_count": mr_activity["corrective_count"],
            "preventive_ratio_pct": mr_activity["preventive_ratio_pct"],
            "corrective_ratio_pct": mr_activity["corrective_ratio_pct"],
            "performance_status": mr_activity["performance_status"],
            "wo_created_count": mr_activity["work_orders_with_linked_wo"],
            "wo_created_pct": mr_activity["wo_created_pct"],
            "data_quality_issue_count": mr_activity["data_quality_issue_count"],
            "missing_asset_count": mr_activity["missing_asset_count"],
            "general_area_asset_count": mr_activity["general_area_asset_count"],
            "general_area_asset_breakdown": mr_activity["general_area_asset_breakdown"],
            "missing_functional_location_count": mr_activity["missing_functional_location_count"],
            "unknown_status_count": mr_activity["unknown_status_count"],
            "selected_work_order_rows_count": mr_activity["selected_work_order_rows_count"],
        },
        "data_reliability": dq,
        "spare_parts": spare,
    }


# ── Limited Filtered Rows Mode (NOT default) ─────────────────────────────────────
# Returns a small, field-limited slice of work-order rows the builder already
# computed. The privacy guard still scrubs and caps this before it leaves MIRA.
def get_verified_downtime_metrics(filters: dict) -> dict:
    """Verified downtime / MR metrics for the selected window."""
    summary = get_dashboard_kpi_summary(filters, include_spare_parts=False)
    return {
        "window": summary.get("window"),
        "stage": summary.get("stage"),
        "filters": summary.get("filters"),
        "work_orders": summary.get("work_orders"),
        "mttr_hours": summary.get("mttr_hours"),
        "mtbf_hours": summary.get("mtbf_hours"),
        "downtime_summary": summary.get("downtime_summary"),
        "data_reliability": summary.get("data_reliability"),
        "source": "verified dashboard downtime / MR summary",
    }


def get_verified_spare_parts_metrics(filters: dict) -> dict:
    """Verified spare-parts metrics for the selected window."""
    return get_spare_parts_summary(filters)


def get_verified_summary_metrics(filters: dict) -> dict:
    """Verified all-in-one maintenance summary for the selected window."""
    return get_dashboard_kpi_summary(filters)


def get_top_assets_by_mr_count(filters: dict, *, limit: int = 5) -> dict:
    """Top recorded assets/areas and top actual machine assets by MR count."""
    f = ctx.normalize_filters(filters)
    rows = _selected_period_work_order_rows(f)
    recorded_counts: Counter[tuple[str | None, str]] = Counter()
    actual_counts: Counter[tuple[str | None, str]] = Counter()
    for row in rows:
        asset = _asset_identity(row)
        recorded_counts[asset] += 1
        asset_id, asset_name = asset
        if asset_id and not _is_missing_asset_id(row) and not _is_general_area_asset(asset_name):
            actual_counts[asset] += 1

    def top_rows(counter: Counter[tuple[str | None, str]]):
        result = []
        for (asset_id, asset_name), count in counter.most_common(limit):
            result.append({
                "asset_id": asset_id,
                "asset_name": asset_name,
                "mr_count": int(count),
                "is_placeholder": _is_general_area_asset(asset_name) or str(asset_id or "").strip().upper() in _MISSING_ASSET_ID_TOKENS,
            })
        return result

    activity = get_mr_activity_summary(f)
    return {
        "window": activity["window"],
        "stage": activity["stage"],
        "top_recorded_asset": {
            "asset_id": activity["top_recorded_asset_id"],
            "asset_name": activity["top_recorded_asset_name"],
            "mr_count": activity["top_recorded_asset_count"],
            "is_placeholder": activity["top_recorded_asset_is_placeholder"],
            "reason": activity["top_recorded_asset_reason"],
        },
        "top_actual_machine_asset": {
            "asset_id": activity["top_actual_machine_asset_id"],
            "asset_name": activity["top_actual_machine_asset_name"],
            "mr_count": activity["top_actual_machine_asset_count"],
            "reason": activity["top_actual_machine_asset_reason"],
        },
        "top_recorded_assets": top_rows(recorded_counts),
        "top_actual_machine_assets": top_rows(actual_counts),
        "rows_loaded": len(rows),
        "source": activity["source"],
    }


def get_top_functional_locations(filters: dict, *, limit: int = 5) -> dict:
    """Functional locations ranked by selected-period MR count."""
    f = ctx.normalize_filters(filters)
    rows = _selected_period_work_order_rows(f)
    counts: Counter[str] = Counter(_functional_location_value(row) for row in rows)
    ranked = [
        {"functional_location": name, "mr_count": int(count)}
        for name, count in counts.most_common(limit)
    ]
    activity = get_mr_activity_summary(f)
    return {
        "window": activity["window"],
        "stage": activity["stage"],
        "top_functional_location": {
            "name": activity["top_functional_location_name"],
            "mr_count": activity["top_functional_location_count"],
            "reason": activity["top_functional_location_reason"],
        },
        "functional_locations": ranked,
        "rows_loaded": len(rows),
        "source": activity["source"],
    }


def get_open_mr_records(filters: dict, *, limit: int = 10) -> dict:
    """Open / in-progress selected-period MR records with safe public fields only."""
    f = ctx.normalize_filters(filters)
    rows = [
        row for row in _selected_period_work_order_rows(f)
        if _mr_status_bucket(row) == "open"
    ]

    def sort_key(row: dict):
        severity = _severity_label(row) or "S99"
        raised = _mr_raised_date(row)
        return (_SEVERITY_ORDER.get(severity, 99), -(raised.toordinal() if raised else 0))

    public_rows = []
    for row in sorted(rows, key=sort_key)[: max(int(limit or 0), 1)]:
        asset_id, asset_name = _asset_identity(row)
        raised = _mr_raised_date(row)
        public_rows.append({
            "request_id": str(row.get("request_id") or row.get("work_order_id") or "").strip() or None,
            "asset_id": asset_id,
            "asset_name": asset_name,
            "functional_location": _functional_location_value(row),
            "status": str(row.get("request_state") or row.get("status") or "Open").strip() or "Open",
            "severity": _severity_label(row),
            "raised_date": raised.isoformat() if raised else None,
        })

    activity = get_mr_activity_summary(f)
    return {
        "window": activity["window"],
        "stage": activity["stage"],
        "open_count": activity["open_count"],
        "carry_over_open_mr": activity["carry_over_open_mr"],
        "records": public_rows,
        "rows_loaded": len(rows),
        "source": activity["source"],
    }


def get_overdue_pm_records(filters: dict, *, limit: int = 10) -> dict:
    """Overdue PM tasks from the verified PM payload."""
    f = ctx.normalize_filters(filters)
    payload = _pm_payload(f)
    overdue_rows = (((payload.get("schedule") or {}).get("tables") or {}).get("overdue") or [])

    def matches(task: dict) -> bool:
        if f.get("assetId") and str(task.get("assetId") or "").strip().upper() != f["assetId"].upper():
            return False
        if f.get("mainAssetGroup"):
            group = str(task.get("mainAssetGroup") or "").lower()
            if f["mainAssetGroup"].lower() not in group:
                return False
        if f.get("subAssetGroup"):
            area = " ".join(str(task.get(key) or "") for key in ("systemArea", "subAssetGroup", "location")).lower()
            if f["subAssetGroup"].lower() not in area:
                return False
        return True

    filtered_rows = [task for task in overdue_rows if matches(task)]
    public_rows = []
    for task in filtered_rows[: max(int(limit or 0), 1)]:
        public_rows.append({
            "pm_task_id": task.get("pmTaskId"),
            "asset_id": task.get("assetId"),
            "asset_name": task.get("assetName"),
            "system_area": task.get("systemArea"),
            "planned_date": task.get("plannedDate"),
            "planned_month": task.get("plannedMonthLabel"),
            "pm_description": task.get("pmDescription"),
            "days_overdue": task.get("daysOverdue"),
            "stage": task.get("stage"),
            "scope": task.get("scope"),
        })

    status = get_pm_schedule_status(f)
    return {
        "window": status["window"],
        "stage": status["stage"],
        "overdue_count": len(filtered_rows),
        "backlog_count": status.get("backlog"),
        "records": public_rows,
        "rows_loaded": len(overdue_rows),
        "source": status["source"],
    }


def get_top_spare_parts_consumption(filters: dict, *, limit: int = 5) -> dict:
    """Top consumed spare parts for the selected window."""
    f = ctx.normalize_filters(filters)
    rows = _sql_project_transaction_rows(f)
    if not rows:
        rows = _sql_movement_consumption_rows(f)
    filtered_rows = [row for row in rows if _window_contains(row.get("project_date"), f)]
    part_map: dict[str, dict] = {}
    for row in filtered_rows:
        key = str(
            row.get("translated_description")
            or row.get("clean_description")
            or row.get("original_description")
            or "Unknown"
        ).strip() or "Unknown"
        bucket = part_map.setdefault(key, {"part_name": key, "value": 0.0, "quantity": 0.0, "transactions": 0})
        bucket["value"] += float(row.get("total_consumption") or 0)
        bucket["quantity"] += float(row.get("quantity_used") or 0)
        bucket["transactions"] += 1
    ranked = sorted(part_map.values(), key=lambda item: (-item["value"], item["part_name"]))[:limit]
    spare = get_spare_parts_summary(f)
    return {
        "window": spare["window"],
        "stage": spare["stage"],
        "top_consumed_part": spare.get("top_consumed_part"),
        "top_consumed_part_value": spare.get("top_consumed_part_value"),
        "rows_loaded": len(rows),
        "rows_after_filter": len(filtered_rows),
        "parts": [
            {
                "part_name": item["part_name"],
                "value": round(item["value"], 2),
                "quantity": round(item["quantity"], 2),
                "transactions": item["transactions"],
            }
            for item in ranked
        ],
        "source": "SQL spare-parts consumption summary",
    }


def get_work_orders(filters: dict, limit: int | None = None) -> dict:
    """Limited work-order lookup. Never returns the full raw dataset."""
    f = ctx.normalize_filters(filters)
    mgmt = _downtime_management(f)
    rows = mgmt.get("work_orders", []) or []

    filtered = [r for r in rows if _matches_work_order_filters(r, f)]
    total_matched = len(filtered)
    # The privacy guard enforces the final cap; we pass a generous pre-slice.
    # `limit` may arrive as a string (querystring) — coerce safely and keep the
    # pre-slice >= the guard's max cap so the guard can apply the real limit.
    try:
        hint = int(limit) if limit not in (None, "") else None
    except (TypeError, ValueError):
        hint = None
    pre_slice = filtered[: max(hint or 0, 50)]
    return {
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "total_matched": total_matched,
        "rows": pre_slice,                # raw rows; privacy guard scrubs + caps next
        "source": "downtime dashboard work_orders (filtered)",
    }


# ── camelCase aliases (match the spec's required function names) ─────────────────
getDashboardKpiSummary = get_dashboard_kpi_summary
getWorkOrders = get_work_orders
getMTTR = get_mttr
getMTBF = get_mtbf
getMrActivitySummary = get_mr_activity_summary
getOpenWorkOrders = get_open_work_orders
getPreventiveCorrectiveSummary = get_preventive_corrective_summary
getDataReliabilityIssues = get_data_reliability_issues
getPMScheduleStatus = get_pm_schedule_status
getSparePartsSummary = get_spare_parts_summary
getStageSummary = get_stage_summary
getVerifiedDowntimeMetrics = get_verified_downtime_metrics
getVerifiedSparePartsMetrics = get_verified_spare_parts_metrics
getVerifiedSummaryMetrics = get_verified_summary_metrics
getTopAssetsByMrCount = get_top_assets_by_mr_count
getTopFunctionalLocations = get_top_functional_locations
getOpenMrRecords = get_open_mr_records
getOverduePmRecords = get_overdue_pm_records
getTopSparePartsConsumption = get_top_spare_parts_consumption
