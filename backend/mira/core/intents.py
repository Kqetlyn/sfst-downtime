"""
Rule-based intent detection (no ML).

Maps a free-text maintenance question to one of MIRA's KPI Summary skills. This is
intentionally simple keyword routing — the "intelligence" is in reusing the
dashboard KPI outputs, not in language understanding.
"""

from __future__ import annotations

import re

# Ordered: the first intent whose keywords match wins. More specific intents first.
INTENT_RULES = [
    ("mttr", ("mttr", "mean time to repair", "repair time", "resolution time")),
    ("mtbf", ("mtbf", "mean time between", "between failures", "reliability interval")),
    ("data_quality", ("data reliability", "data quality", "reliability issue", "data issue",
                       "missing data", "quality issue", "attention")),
    ("pm_schedule", ("pm schedule", "preventive maintenance schedule", "pm status",
                     "schedule status", "compliance", "overdue pm", "due pm")),
    ("preventive_corrective", ("preventive vs corrective", "preventive and corrective",
                               "corrective", "preventive", "pm vs cm", "reactive")),
    ("open_work_orders", ("open work order", "open wo", "closed work order", "backlog",
                          "outstanding", "unresolved")),
    ("stage_compare", ("stage 1 and stage 2", "stage 1 vs stage 2", "compare stage",
                       "stage comparison", "both stages")),
    ("work_order_search", ("find work order", "search work order", "list work order",
                           "show work order", "which work order", "work orders for")),
    ("monthly_summary", ("summary", "summarise", "summarize", "overview", "performance",
                         "how are we doing", "report")),
]

DEFAULT_INTENT = "monthly_summary"


def detect_intent(question: str | None) -> str:
    text = (question or "").strip().lower()
    if not text:
        return DEFAULT_INTENT
    for intent, keywords in INTENT_RULES:
        if any(kw in text for kw in keywords):
            return intent
    return DEFAULT_INTENT


def wants_stage_comparison(question: str | None) -> bool:
    text = (question or "").strip().lower()
    return bool(re.search(r"stage\s*1.*stage\s*2|compare.*stage|both stage", text))
