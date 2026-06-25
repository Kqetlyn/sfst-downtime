"""
Maintenance risk insights (NOT failure prediction).

Backend-computed asset risk score from verified signals:
    WO Frequency + Severity + Recurring Issue + Overdue PM + Spare Consumption.

Scores are deterministic (the LLM never computes them). Wording must stay cautious
("may require closer follow-up"), never "will fail". Read-only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta

from ...core import context as ctx
from ...services import kpi_query_service as kpi
from . import chat_service as themes

# Severity weights: S1 highest.
_SEV_WEIGHT = {"1": 5, "2": 3, "3": 2, "4": 1}


def _wo_frequency_score(count: int) -> int:
    if count >= 20:
        return 5
    if count >= 11:
        return 4
    if count >= 6:
        return 3
    if count >= 3:
        return 2
    if count >= 1:
        return 1
    return 0


def _severity_score(rows: list[dict]) -> int:
    best = 0
    for r in rows:
        sl = str(r.get("service_level") or "").strip()
        best = max(best, _SEV_WEIGHT.get(sl, 0))
    return best


def _overdue_pm_asset_ids(f: dict) -> set[str]:
    """Asset IDs that currently have at least one overdue PM task."""
    payload = kpi._pm_payload(f)
    ids: set[str] = set()
    for scope in ("equipment", "utility"):
        tables = (payload.get(scope, {}) or {}).get("tables", {}) or {}
        for t in tables.get("overdue", []) or []:
            aid = str(t.get("assetId") or "").strip().upper()
            if aid:
                ids.add(aid)
    return ids


def get_asset_risk_insights(filters: dict, top_n: int = 8) -> dict:
    f = ctx.normalize_filters(filters)
    window = ctx.resolved_window(f)
    all_rows = kpi._downtime_all_year_work_orders(f)
    period_rows = [r for r in all_rows if kpi._matches_mix_window(r, f)]

    # 90-day window (ending at the selected period end) for recurrence.
    end = window["end_date"]
    start90 = end - timedelta(days=90)
    rows_90 = []
    for r in all_rows:
        d = kpi._mr_raised_date(r)
        if d and start90 <= d <= end:
            rows_90.append(r)

    # Aggregate by asset (skip missing-asset placeholders for the actual-asset view).
    by_asset_period: dict = defaultdict(list)
    for r in period_rows:
        aid = str(r.get("asset_id") or "").strip().upper()
        if not aid:
            continue
        by_asset_period[aid].append(r)

    by_asset_90: dict = defaultdict(list)
    for r in rows_90:
        aid = str(r.get("asset_id") or "").strip().upper()
        if aid:
            by_asset_90[aid].append(r)

    overdue_pm = _overdue_pm_asset_ids(f)

    insights = []
    for aid, rows in by_asset_period.items():
        name = str(rows[0].get("machine_name") or aid)
        is_placeholder = themes is not None and kpi._is_general_area_asset(name)
        wo_freq = _wo_frequency_score(len(rows))
        sev = _severity_score(rows)
        # Recurrence: same issue theme >=3 times (score 5) / ==2 (score 3) in 90 days.
        theme_counts = Counter(themes.classify_theme(themes._row_description(r)) for r in by_asset_90.get(aid, []))
        theme_counts.pop(themes._UNKNOWN_THEME, None)
        top_theme_count = theme_counts.most_common(1)[0][1] if theme_counts else 0
        recurring = 5 if top_theme_count >= 3 else (3 if top_theme_count == 2 else 0)
        overdue = 5 if aid in overdue_pm else 0
        spare = 0  # per-asset spare consumption comparison not available in this view
        total = wo_freq + sev + recurring + overdue + spare
        level = "High Attention" if total >= 15 else ("Medium Attention" if total >= 8 else "Normal Monitoring")
        insights.append({
            "asset_id": aid, "asset_name": name, "is_placeholder": is_placeholder,
            "mr_count": len(rows), "risk_score": total, "risk_level": level,
            "scores": {"wo_frequency": wo_freq, "severity": sev, "recurring": recurring,
                       "overdue_pm": overdue, "spare_consumption": spare},
            "top_recurring_theme": (theme_counts.most_common(1)[0][0] if theme_counts else None),
        })

    insights.sort(key=lambda x: (-x["risk_score"], -x["mr_count"], x["asset_name"]))
    high = [i for i in insights if i["risk_level"] == "High Attention"]
    medium = [i for i in insights if i["risk_level"] == "Medium Attention"]
    machine_insights = [i for i in insights if not i["is_placeholder"]]
    return {
        "period": window["label"],
        "assets_assessed": len(insights),
        "high_attention_count": len(high),
        "medium_attention_count": len(medium),
        "top_assets": insights[:top_n],
        "top_machine_assets": machine_insights[:top_n],
        "thresholds": {"high": ">= 15", "medium": "8-14", "normal": "< 8"},
        "scoring": "WO frequency + severity + recurring issue + overdue PM + spare consumption",
        "note": "Backend-calculated maintenance risk signals, not a failure prediction. "
                "Higher score means an asset may require closer follow-up; confirm with engineering review.",
        "source": "downtime MR rows + PM overdue tasks (verified)",
        "data_notes": ["Per-asset spare-parts consumption comparison is not yet wired (scored 0)."],
    }
