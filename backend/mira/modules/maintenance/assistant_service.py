"""
miraAssistantService — orchestrates one MIRA turn.

    question + filters
      -> detect intent
      -> call the matching kpiQueryService function   (reuse dashboard KPIs)
      -> privacy guard                                (scrub / cap / redact)
      -> provider.generate()                          (mock by default)
      -> { answer, data, intent, mode, draft_label }

KPI Summary Mode is the default. Limited Filtered Rows Mode is only used for the
explicit work-order-search intent, and even then the privacy guard caps + scrubs.
"""

from __future__ import annotations

from ... import config
from ...core import context as ctx
from ...core.intents import detect_intent
from ...privacy import privacy_guard_service as guard
from ...providers import get_provider
from ...services import kpi_query_service as kpi
from ...services import presentation_service as presentation

# intent -> KPI function (KPI Summary Mode)
_SUMMARY_INTENTS = {
    "mttr": kpi.get_mttr,
    "mtbf": kpi.get_mtbf,
    "open_work_orders": kpi.get_open_work_orders,
    "preventive_corrective": kpi.get_preventive_corrective_summary,
    "data_quality": kpi.get_data_reliability_issues,
    "pm_schedule": kpi.get_pm_schedule_status,
    "stage_compare": kpi.get_stage_summary,
    "monthly_summary": kpi.get_dashboard_kpi_summary,
}


def ask(question: str | None, filters: dict | None, *, limit: int | None = None) -> dict:
    """Answer a maintenance question using dashboard KPI outputs only."""
    filters = ctx.normalize_filters(filters)
    intent = detect_intent(question)

    if intent == "work_order_search":
        return _answer_work_order_search(question, filters, limit)

    producer = _SUMMARY_INTENTS.get(intent, kpi.get_dashboard_kpi_summary)
    raw = producer(filters)
    guarded = guard.guard_summary(raw, mode="kpi_summary")
    provider = get_provider()
    answer = provider.generate(intent, raw, question)
    presentation_model = presentation.build_presentation(
        intent,
        raw,
        filters,
        mode="kpi_summary",
        provider_name=provider.name,
        question=question,
    )

    return {
        "ok": True,
        "intent": intent,
        "mode": "kpi_summary",
        "question": guard.redact_secrets(question or ""),
        "answer": guard.mark_draft(answer),
        "data": guarded["data"],
        "presentation": guard._deep_redact(presentation_model),
        "provider": provider.name,
        "draft_label": config.DRAFT_LABEL,
        "disclaimer": config.MODEL_DISCLAIMER,
    }


def _answer_work_order_search(question, filters, limit) -> dict:
    """Limited Filtered Rows Mode — capped, field-reduced, never the full dataset."""
    raw = kpi.get_work_orders(filters, limit=limit)
    guarded = guard.guard_work_orders(raw, requested_limit=limit)
    provider = get_provider()
    answer = provider.generate("work_order_search", guarded, question)
    presentation_model = presentation.build_presentation(
        "work_order_search",
        guarded,
        filters,
        mode="limited_filtered_rows",
        provider_name=provider.name,
        question=question,
    )
    return {
        "ok": True,
        "intent": "work_order_search",
        "mode": "limited_filtered_rows",
        "question": guard.redact_secrets(question or ""),
        "answer": guard.mark_draft(answer),
        "data": guarded,
        "presentation": guard._deep_redact(presentation_model),
        "provider": provider.name,
        "draft_label": config.DRAFT_LABEL,
        "disclaimer": config.MODEL_DISCLAIMER,
    }


# camelCase alias
askMira = ask
