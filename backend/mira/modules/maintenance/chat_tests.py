"""
Golden-set tests for the Ask MIRA intent router.

Each entry is: (question, expected_new_intent, expected_legacy_intent_prefix, notes)
Run with: python -m pytest backend/mira/modules/maintenance/chat_tests.py -v
"""
from __future__ import annotations

import pytest

from .chat_service import (
    METRIC_REGISTRY,
    _new_to_legacy_intent,
    _route_intent_keyword,
    _validate_router_output,
)


# ── Golden set ────────────────────────────────────────────────────────────────
# (question, expected_5_intent, expected_legacy_prefix)
GOLDEN: list[tuple[str, str, str]] = [
    # ── Recurring issue analysis ──────────────────────────────────────────────
    ("What keeps breaking in production?",                      "recurring_issue_analysis",  "fault_theme"),
    ("What's the most recurring fault this month?",             "recurring_issue_analysis",  "fault_theme"),
    ("Which machine has the most repeat failures?",             "recurring_issue_analysis",  "recurring_issue"),
    ("Show me fault patterns for the past 6 months",            "recurring_issue_analysis",  "fault_theme"),
    ("What are the common issues on Chiller 1?",                "recurring_issue_analysis",  "recurring_issue"),

    # ── Downtime / MR-WO analysis ────────────────────────────────────────────
    ("How many MRs were raised this month?",                    "downtime_mr_wo_analysis",   "maintenance_summary"),
    ("What is the MTTR for Stage 2 last quarter?",              "downtime_mr_wo_analysis",   "downtime_summary"),
    ("Show me open work orders",                                "downtime_mr_wo_analysis",   "open_mr"),
    ("What's the closure rate for maintenance requests?",       "downtime_mr_wo_analysis",   "maintenance_summary"),
    ("Which asset has the most maintenance requests?",          "downtime_mr_wo_analysis",   "top_asset"),
    ("What is the backlog count?",                              "downtime_mr_wo_analysis",   "backlog"),
    ("How many WOs carry over from last month?",                "downtime_mr_wo_analysis",   "maintenance_summary"),
    ("Top location by MR count",                               "downtime_mr_wo_analysis",   "top_functional_location"),
    ("What should be followed up today?",                       "downtime_mr_wo_analysis",   "daily_follow_up"),
    ("Give me a maintenance summary for YTD",                   "downtime_mr_wo_analysis",   "maintenance_summary"),

    # ── PM schedule ──────────────────────────────────────────────────────────
    ("What is the PM compliance rate?",                         "pm_schedule",               "pm_summary"),
    ("How many PMs are overdue?",                               "pm_schedule",               "pm_overdue"),
    ("Show preventive maintenance schedule",                    "pm_schedule",               "pm_summary"),
    ("Which PMs were completed last week?",                     "pm_schedule",               "pm_summary"),
    ("PM backlog for June",                                     "pm_schedule",               "pm_summary"),

    # ── Spare parts ──────────────────────────────────────────────────────────
    ("What spare parts are in stock?",                          "spare_parts",               "spare_parts_summary"),
    ("Show top consumed spare parts",                           "spare_parts",               "spare_parts_consumption"),
    ("What is the inventory value?",                            "spare_parts",               "spare_parts_summary"),

    # ── Predictive risk ──────────────────────────────────────────────────────
    ("Which assets are at highest risk?",                       "predictive_risk",           "risk_insight"),
    ("Show me the predictive maintenance outlook",              "predictive_risk",           "risk_insight"),
    ("What is the recurrence prediction for Cooling Tower 2?",  "predictive_risk",           "risk_insight"),
]


@pytest.mark.parametrize("question,expected_5_intent,expected_legacy_prefix", GOLDEN)
def test_keyword_router_intent(question: str, expected_5_intent: str, expected_legacy_prefix: str):
    """Keyword router assigns the correct 5-intent for each golden question."""
    out = _route_intent_keyword(question)
    assert out["intent"] == expected_5_intent, (
        f"Q: {question!r}\n  got intent={out['intent']!r}, want {expected_5_intent!r}"
    )


@pytest.mark.parametrize("question,expected_5_intent,expected_legacy_prefix", GOLDEN)
def test_legacy_mapping(question: str, expected_5_intent: str, expected_legacy_prefix: str):
    """_new_to_legacy_intent maps into an intent whose name starts with the expected prefix."""
    router_out = {"intent": expected_5_intent, "metrics": [], "filters": {}, "interpretation_confidence": 0.9}
    legacy = _new_to_legacy_intent(router_out, question)
    assert legacy.startswith(expected_legacy_prefix), (
        f"Q: {question!r}\n  legacy intent={legacy!r}, want prefix {expected_legacy_prefix!r}"
    )


def test_validate_known_intents():
    """All intents in METRIC_REGISTRY pass validation."""
    for intent in METRIC_REGISTRY:
        out = {
            "intent": intent,
            "metrics": METRIC_REGISTRY[intent]["allowed_metrics"][:1],
            "filters": {},
            "interpretation_confidence": 0.8,
            "interpretation_text": "test",
        }
        valid, msg = _validate_router_output(out)
        assert valid, f"Intent {intent!r} failed validation: {msg}"


def test_validate_unknown_intent_fails():
    out = {"intent": "delete_all_records", "metrics": [], "filters": {}, "interpretation_confidence": 0.9}
    valid, _ = _validate_router_output(out)
    assert not valid


def test_validate_low_confidence_fails():
    out = {"intent": "pm_schedule", "metrics": [], "filters": {}, "interpretation_confidence": 0.2}
    valid, msg = _validate_router_output(out)
    assert not valid
    assert "confidence" in msg.lower() or "unclear" in msg.lower()


def test_validate_unknown_metrics_pruned():
    """Unknown metrics are pruned (non-blocking), not a validation failure."""
    out = {
        "intent": "pm_schedule",
        "metrics": ["pm_compliance", "secret_formula", "delete_everything"],
        "filters": {},
        "interpretation_confidence": 0.85,
    }
    valid, _ = _validate_router_output(out)
    assert valid
    allowed = set(METRIC_REGISTRY["pm_schedule"]["allowed_metrics"])
    assert all(m in allowed for m in out["metrics"])
