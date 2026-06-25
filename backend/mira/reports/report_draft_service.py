"""
reportDraftService — assembles a monthly maintenance report DRAFT.

Every figure is sourced from the dashboard KPI outputs via kpiQueryService, so the
report can never disagree with the dashboard. Output is plainly marked as a draft
for human review. No raw rows are included.
"""

from __future__ import annotations

from .. import config
from ..core import context as ctx
from ..privacy import privacy_guard_service as guard
from ..providers import get_provider
from ..services import kpi_query_service as kpi


def generate_monthly_maintenance_summary(filters: dict | None) -> dict:
    """Build a structured + narrative monthly maintenance report draft."""
    f = ctx.normalize_filters(filters)
    snapshot = kpi.get_dashboard_kpi_summary(f)
    reliability = kpi.get_data_reliability_issues(f)
    pm = kpi.get_pm_schedule_status(f)
    narrative = get_provider().generate("monthly_summary", snapshot)

    sections = [
        {
            "title": "Work Order Performance",
            "metrics": {
                "Total Work Orders": snapshot["work_orders"]["total"],
                "Open": snapshot["work_orders"]["open"],
                "Closed": snapshot["work_orders"]["closed"],
                "MTTR (hours)": snapshot["mttr_hours"],
                "MTBF (hours)": snapshot["mtbf_hours"],
            },
        },
        {
            "title": "Preventive vs Corrective",
            "metrics": {
                "Preventive": snapshot["preventive_count"],
                "Corrective": snapshot["corrective_count"],
                "Performance": snapshot["performance_status"],
            },
        },
        {
            "title": "PM Schedule",
            "metrics": {
                "Scheduled": pm["total_scheduled"],
                "Due This Month": pm["due_this_month"],
                "Overdue": pm["overdue"],
                "Backlog": pm["backlog"],
                "Compliance %": pm["compliance_pct"],
            },
        },
        {
            "title": "Data Reliability",
            "metrics": {
                "Needs Attention": reliability["requires_attention_count"],
                "Missing/Invalid TTR": reliability["invalid_missing_ttr_count"],
                "Duplicates": reliability["duplicate_work_order_count"],
            },
        },
    ]

    markdown = _to_markdown(f, narrative, sections)

    return {
        "ok": True,
        "mode": "report_draft",
        "title": f"Maintenance Report Draft — {ctx.month_label(f)}",
        "window": ctx.month_label(f),
        "stage": f["stage"],
        "narrative": guard.mark_draft(narrative),
        "sections": guard._deep_redact(sections),
        "markdown": markdown,
        "draft_label": config.DRAFT_LABEL,
        "disclaimer": config.MODEL_DISCLAIMER,
    }


def _to_markdown(f, narrative, sections) -> str:
    lines = [
        f"# Maintenance Report Draft — {ctx.month_label(f)}",
        f"_Stage: {f['stage']}_  ",
        f"> {config.DRAFT_LABEL}",
        "",
        "## Summary",
        narrative,
        "",
    ]
    for sec in sections:
        lines.append(f"## {sec['title']}")
        for label, value in sec["metrics"].items():
            lines.append(f"- **{label}:** {value if value is not None else 'not available'}")
        lines.append("")
    lines.append(f"_{config.MODEL_DISCLAIMER}_")
    return "\n".join(lines)


# camelCase alias
generateMonthlyMaintenanceSummary = generate_monthly_maintenance_summary
