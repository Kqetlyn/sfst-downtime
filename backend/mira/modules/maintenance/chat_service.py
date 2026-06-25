"""
MIRA chat service - intelligent, read-only dashboard Q&A.

Flow:
    question
    -> intent router
    -> period extraction
    -> verified data retrieval
    -> optional MR description theme classification
    -> verified context JSON
    -> Ollama explanation (or rule-based fallback)
    -> structured chat answer

Numbers always come from verified backend functions. The LLM only writes wording.
"""

from __future__ import annotations

import calendar
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime

from ... import config
from ...core import context as ctx
from ...providers import OllamaMiraProvider, generate_with_ollama, get_provider_status
from ...services import kpi_query_service as kpi

_TAGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
    "data",
    "mira_description_tags.json",
)

_INTENT_RULES = [
    (
        "daily_follow_up_query",
        (
            "what should be followed up today",
            "follow up today",
            "follow-up today",
            "followup today",
            "what should be followed up",
            "daily follow-up",
            "daily follow up",
            "today's follow-up",
            "todays follow-up",
            "action items",
            "priorities today",
        ),
    ),
    (
        "report_wording_query",
        (
            "one-line",
            "one line",
            "monthly report",
            "report summary",
            "report sentence",
            "headline",
            "executive sentence",
            "slide summary",
        ),
    ),
    (
        "fault_theme_query",
        (
            "most common fault",
            "common fault",
            "common issue",
            "main fault",
            "main issue",
            "fault pattern",
            "fault theme",
            "type of fault",
            "type of issue",
            "cause of breakdown",
            "main cause",
            "root cause",
            "operation related",
            "operation-related",
            "what is the most common",
        ),
    ),
    (
        "recurring_issue_query",
        (
            "recurring",
            "repeated",
            "repeat issue",
            "keeps happening",
            "again and again",
            "same problem",
            "repeated issues",
        ),
    ),
    (
        "pm_overdue_query",
        (
            "overdue pm",
            "pm overdue",
            "overdue preventive",
            "pm tasks overdue",
            "which pm tasks are overdue",
            "which pm are overdue",
            "overdue maintenance",
        ),
    ),
    ("backlog_query", ("backlog", "carry-over", "carry over")),
    (
        "top_asset_query",
        (
            "which asset has the most mr",
            "asset has the most mr",
            "most mr",
            "most maintenance request",
            "top asset",
            "asset with most",
            "worst asset",
            "top actual machine asset",
        ),
    ),
    (
        "top_functional_location_query",
        (
            "which functional location",
            "highest workload",
            "functional location",
            "which area has the highest workload",
            "which location",
            "location workload",
            "area workload",
            "machine group",
        ),
    ),
    (
        "open_mr_query",
        (
            "open mr",
            "still open",
            "outstanding mr",
            "open work order",
            "top open",
            "unresolved",
            "open maintenance",
            "open / in progress",
        ),
    ),
    (
        "risk_insight_query",
        (
            "risk",
            "need attention",
            "needs attention",
            "machines need attention",
            "high risk",
            "asset risk",
            "attention list",
        ),
    ),
    (
        "spare_parts_consumption_query",
        (
            "highest consumption",
            "most consumed",
            "top consumed",
            "consumed spare",
            "spare consumption",
            "spare part consumption",
            "consumption",
        ),
    ),
    (
        "spare_parts_summary",
        (
            "spare part",
            "spare parts",
            "inventory",
            "in-stock",
            "in stock",
            "drawn from store",
            "non-stock",
            "services value",
        ),
    ),
    (
        "pm_summary",
        (
            "pm compliance",
            "pm issue",
            "pm issues",
            "pm status",
            "preventive maintenance",
            "pm schedule",
            "main pm",
            "pm performance",
            "compliance",
        ),
    ),
    (
        "downtime_summary",
        (
            "mttr",
            "mtbf",
            "downtime",
            "closure rate",
            "work order",
            "closure",
            "wo created",
            "mr raised",
            "maintenance request",
        ),
    ),
    (
        "maintenance_summary",
        (
            "summarise",
            "summarize",
            "summary",
            "performance",
            "overview",
            "how are we doing",
            "maintenance performance",
            "maintenance summary",
            "report",
        ),
    ),
]
_DEFAULT_INTENT = "maintenance_summary"

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

_THEME_PATTERNS = [
    (
        "Refrigeration / Cooling Issue",
        (
            r"refriger",
            r"\bcooling\b",
            r"\bcompressor\b",
            r"\bfreezer\b",
            r"\bchiller\b",
            r"\bcondenser\b",
            r"\bevaporator\b",
            r"\bcold room\b",
            r"\btemperature\b",
            r"\bdefrost\b",
        ),
    ),
    (
        "Sensor / Instrumentation Issue",
        (
            r"\bsensor\b",
            r"\bprobe\b",
            r"\btransmitter\b",
            r"\bgauge\b",
            r"calibrat",
            r"instrument",
            r"\breading\b",
            r"\bmeter\b",
        ),
    ),
    (
        "Electrical Fault",
        (
            r"electric",
            r"\bwiring\b",
            r"\bvoltage\b",
            r"\bpower\b",
            r"\bcircuit\b",
            r"\bmotor\b",
            r"\bcontactor\b",
            r"\bfuse\b",
            r"short circuit",
            r"\bpanel\b",
            r"\brelay\b",
            r"\binverter\b",
        ),
    ),
    ("Cleaning-Related Issue", (r"\bclean", r"\bhygiene\b", r"sanitat", r"\bwash\b")),
    (
        "Spare-Part-Related Issue",
        (r"\bspare\b", r"\breplace", r"\bworn\b", r"\bbearing\b", r"\bseal\b", r"\bbelt\b", r"\bgasket\b", r"\bo-ring\b"),
    ),
    ("PM-Related Issue", (r"\bpm\b", r"\bpreventive\b", r"scheduled maintenance", r"\binspection\b")),
    (
        "Facility / Building Issue",
        (r"\bbuilding\b", r"\bdoor\b", r"\bfloor\b", r"\broof\b", r"\bwall\b", r"\blight\b", r"\bfacility\b", r"\bceiling\b"),
    ),
    (
        "Utility Issue",
        (r"\bwater\b", r"\bsteam\b", r"\bboiler\b", r"air compressor", r"\bgas\b", r"\butility\b", r"\bpump\b", r"\bvalve\b", r"\bdrain\b"),
    ),
    (
        "Possible Operation-Related Issue",
        (r"\bmisuse\b", r"\bwrong\b", r"improper", r"\boperator\b", r"\bhandling\b", r"\boverload\b", r"not follow", r"incorrect"),
    ),
    (
        "Mechanical Fault",
        (
            r"mechanic",
            r"\bjam\b",
            r"abnormal sound",
            r"\bnoise\b",
            r"vibrat",
            r"\bleak",
            r"\bbroken\b",
            r"\bcrack",
            r"\bgear\b",
            r"\bchain\b",
            r"\bshaft\b",
            r"movement",
            r"\bstuck\b",
            r"\bblock",
            r"\bdamage",
        ),
    ),
]
_THEME_COMPILED = [(name, [re.compile(pattern, re.IGNORECASE) for pattern in patterns]) for name, patterns in _THEME_PATTERNS]
_UNKNOWN_THEME = "Unknown / Insufficient Information"

_CHAT_SYSTEM_PROMPT = (
    "You are MIRA, a read-only Maintenance Intelligence and Reporting Assistant. "
    "You answer questions using only the verified dashboard data provided in the context JSON. "
    "Do not invent numbers. Do not estimate missing values. Do not claim a root cause unless the "
    "description data clearly supports it. For fault analysis use cautious wording such as suggests, "
    "indicates, or may be related to. Keep answers concise, professional, and suitable for engineering management."
)

_READ_ONLY_RESPONSE = "MIRA is currently read-only and cannot modify maintenance records."
_READ_ONLY_PATTERNS = (
    r"\b(create|submit|add|edit|update|change|modify|close|delete|remove|cancel|approve)\b",
    r"\b(mr|maintenance request|wo|work order|pm|maintenance record|d365|excel|sharepoint|source file|record)\b",
)
_PM_QUERY_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_PM_QUERY_TIMEOUT_SECONDS = 8
_PM_LOAD_WARNING = "PM verified detail is still loading from the source schedule files. Try the PM question again in a moment."

# ── Intent Router: 5-intent schema ──────────────────────────────────────────
# Maps new broad intents → legacy build_context intents + allowed metric names.
# qwen2.5:7b routes to these; keyword fallback also maps here.
METRIC_REGISTRY: dict[str, dict] = {
    "recurring_issue_analysis": {
        "description": "Recurring faults, common issues, fault patterns, what keeps breaking",
        "allowed_metrics": ["top_fault_category", "top_asset", "occurrence_count", "latest_date", "mtbf"],
        "legacy_intents": ["fault_theme_query", "recurring_issue_query", "risk_insight_query"],
        "default_legacy": "fault_theme_query",
    },
    "downtime_mr_wo_analysis": {
        "description": "Maintenance requests, work orders, MTTR, closure rate, open/closed counts, backlog, top asset, downtime",
        "allowed_metrics": ["mr_count", "wo_count", "mttr", "mtbf", "closure_rate", "open_count", "top_asset", "carry_over"],
        "legacy_intents": ["downtime_summary", "maintenance_summary", "backlog_query", "top_asset_query",
                           "open_mr_query", "top_functional_location_query", "daily_follow_up_query"],
        "default_legacy": "maintenance_summary",
    },
    "pm_schedule": {
        "description": "PM schedule, preventive maintenance, compliance %, overdue tasks, completion rate",
        "allowed_metrics": ["pm_compliance", "pm_overdue_count", "pm_completed", "pm_scheduled", "backlog"],
        "legacy_intents": ["pm_summary", "pm_overdue_query"],
        "default_legacy": "pm_summary",
    },
    "spare_parts": {
        "description": "Spare parts, inventory, stock, consumption, usage, parts drawn",
        "allowed_metrics": ["parts_in_stock", "consumption_value", "top_consumed_part"],
        "legacy_intents": ["spare_parts_summary", "spare_parts_consumption_query"],
        "default_legacy": "spare_parts_summary",
    },
    "predictive_risk": {
        "description": "Risk, attention assets, predictive maintenance, high-risk equipment, what to watch",
        "allowed_metrics": ["risk_score", "top_risk_assets", "recurrence_prediction"],
        "legacy_intents": ["risk_insight_query", "daily_follow_up_query"],
        "default_legacy": "risk_insight_query",
    },
}

_INTENT_ROUTER_PROMPT = (
    "You are an intent router for a maintenance dashboard assistant.\n"
    "Classify the question into EXACTLY ONE intent from this closed set:\n"
    "- recurring_issue_analysis: faults, recurring issues, common problems, fault patterns, "
    "what keeps breaking, most common issue, theme\n"
    "- downtime_mr_wo_analysis: maintenance requests, work orders, MTTR, closure rate, open tickets, "
    "downtime statistics, backlog, MR counts, top asset by failures, location analysis\n"
    "- pm_schedule: PM schedule, preventive maintenance, compliance %, overdue tasks, completion rate\n"
    "- spare_parts: spare parts, inventory, stock, consumption, usage, parts drawn, materials\n"
    "- predictive_risk: risk, attention assets, prediction, high-risk equipment, what to watch, alert\n\n"
    "Also extract explicit filters if mentioned (stage, year, month, machine_group, asset, fault_category).\n"
    "Map relative dates: 'this year'→current YTD, 'last month'→previous month, 'FY YYYY'→financial_year.\n"
    "Set interpretation_confidence 0.0-1.0; use <0.5 for ambiguous questions.\n\n"
    "Respond ONLY with valid JSON — no other text:\n"
    "{\n"
    '  "intent": "<one of the 5 above>",\n'
    '  "filters": {},\n'
    '  "metrics": ["<metric1>", ...],\n'
    '  "interpretation_confidence": 0.0,\n'
    '  "interpretation_text": "<one short sentence: what you understood>"\n'
    "}"
)

_ROUTER_CONFIDENCE_THRESHOLD = 0.4


def _route_intent_llm(question: str, filters: dict) -> dict | None:
    """Try qwen2.5:7b for structured routing JSON. Returns None if unavailable/failed."""
    try:
        from ...providers import generate_with_ollama, OllamaMiraProvider
        provider = OllamaMiraProvider()
        if not (config.LOCAL_LLM_ENABLED and provider.resolve_model()):
            return None
        year = filters.get("year", datetime.now().year)
        period = filters.get("period_mode", "ytd")
        user_prompt = (
            f'Question: "{question}"\n'
            f"Current dashboard period: {period} {year}\n"
            f"Stage filter: {filters.get('stage', 'all')}"
        )
        raw = generate_with_ollama(
            _INTENT_ROUTER_PROMPT, user_prompt,
            model=provider.resolve_model(), timeout=8,
        ).strip()
        # Extract JSON from response (LLM may wrap in markdown)
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            return None
        out = json.loads(json_match.group())
        if out.get("intent") not in METRIC_REGISTRY:
            return None
        return out
    except Exception:
        return None


def _route_intent_keyword(question: str) -> dict:
    """Keyword-based intent router that maps to the 5-intent schema."""
    q = (question or "").lower()
    # PM
    if re.search(r"\bpm\b|preventive|scheduled\s+maint|compliance\s*%|pm\s+overdue|pm\s+complet", q):
        return {"intent": "pm_schedule", "metrics": ["pm_compliance", "pm_overdue_count"],
                "interpretation_confidence": 0.75, "interpretation_text": "PM schedule analysis"}
    # Spare parts
    if re.search(r"\bspare\b|\bpart\b|inventory|stock|consumption|material|drawn\s+from", q):
        return {"intent": "spare_parts", "metrics": ["parts_in_stock", "top_consumed_part"],
                "interpretation_confidence": 0.75, "interpretation_text": "Spare parts analysis"}
    # Risk / predictive
    if re.search(r"\brisk\b|attention|predict|high.risk|what\s+to\s+watch|alert|escalat", q):
        return {"intent": "predictive_risk", "metrics": ["top_risk_assets"],
                "interpretation_confidence": 0.7, "interpretation_text": "Risk and predictive analysis"}
    # Recurring fault / theme
    if re.search(r"recurring|fault|common\s+issue|pattern|keeps?\s+break|most\s+common|theme|what\s+breaks", q):
        return {"intent": "recurring_issue_analysis", "metrics": ["top_fault_category", "occurrence_count"],
                "interpretation_confidence": 0.7, "interpretation_text": "Recurring fault analysis"}
    # Default: downtime / MR / WO
    return {"intent": "downtime_mr_wo_analysis", "metrics": ["mr_count", "mttr", "closure_rate"],
            "interpretation_confidence": 0.6, "interpretation_text": "Downtime and maintenance request analysis"}


def _validate_router_output(router_out: dict) -> tuple[bool, str]:
    """Whitelist-validate the router's intent and confidence. Returns (ok, reason)."""
    intent = router_out.get("intent", "")
    if intent not in METRIC_REGISTRY:
        return False, f"Intent '{intent}' not in the allowed set."
    conf = float(router_out.get("interpretation_confidence") or 0.5)
    if conf < _ROUTER_CONFIDENCE_THRESHOLD:
        return False, (
            f"I'm not confident I understood your question correctly "
            f"(confidence {conf:.0%}). Could you rephrase? "
            f"Try specifying: what data you want, for which period, and for which stage/asset."
        )
    # Prune unknown metrics (non-blocking)
    allowed = set(METRIC_REGISTRY[intent]["allowed_metrics"])
    router_out["metrics"] = [m for m in (router_out.get("metrics") or []) if m in allowed]
    return True, ""


def _new_to_legacy_intent(router_out: dict, question: str) -> str:
    """Map new 5-intent router output to a legacy build_context intent."""
    new_intent = router_out.get("intent", "")
    reg = METRIC_REGISTRY.get(new_intent)
    if not reg:
        return classify_intent(question)  # full fallback to regex
    q = (question or "").lower()
    # Within each new intent, use keywords to pick the most specific legacy intent
    if new_intent == "downtime_mr_wo_analysis":
        if re.search(r"backlog|carry.over|open\s+mr|open\s+work", q):
            return "backlog_query"
        if re.search(r"top\s+asset|which\s+asset|most\s+mr|most\s+fail", q):
            return "top_asset_query"
        if re.search(r"location|building|store|office|area|functional", q):
            return "top_functional_location_query"
        if re.search(r"open|unresolved|not\s+closed|pending", q):
            return "open_mr_query"
        if re.search(r"today|follow.?up|action|priorities", q):
            return "daily_follow_up_query"
        if re.search(r"mtbf|mean.*between", q):
            return "downtime_summary"
        return "maintenance_summary"
    if new_intent == "pm_schedule":
        if re.search(r"overdue|behind|late|missed", q):
            return "pm_overdue_query"
        return "pm_summary"
    if new_intent == "recurring_issue_analysis":
        if re.search(r"risk|attention|score", q):
            return "risk_insight_query"
        if re.search(r"theme|descri|pattern|what.+common", q):
            return "fault_theme_query"
        return "recurring_issue_query"
    if new_intent == "spare_parts":
        if re.search(r"consum|usage|draw|used", q):
            return "spare_parts_consumption_query"
        return "spare_parts_summary"
    if new_intent == "predictive_risk":
        return "risk_insight_query"
    return reg["default_legacy"]


def _compute_answer_confidence(view_data: dict, router_out: dict) -> dict:
    """Compute answer confidence from data quality signals."""
    rows_str = view_data.get("rows_after_filter") or view_data.get("rows_loaded") or []
    # Extract first numeric value from rows list (may be "1086 rows" or just 1086)
    record_count = 0
    if isinstance(rows_str, list) and rows_str:
        try:
            record_count = int(re.search(r"\d+", str(rows_str[0])).group())
        except Exception:
            pass
    elif isinstance(rows_str, (int, float)):
        record_count = int(rows_str)

    router_conf = float(router_out.get("interpretation_confidence") or 0.5)
    score = 0.0
    parts: list[str] = []

    # Record count contribution
    if record_count >= 50:
        score += 0.35
    elif record_count >= 10:
        score += 0.2
    elif record_count >= 3:
        score += 0.1
    if record_count:
        parts.append(f"{record_count} MRs")

    # Router confidence contribution
    if router_conf >= 0.8:
        score += 0.35
    elif router_conf >= 0.6:
        score += 0.25
    elif router_conf >= 0.4:
        score += 0.15

    # Data warnings reduce confidence
    warnings = view_data.get("data_warnings") or []
    if warnings:
        score -= 0.1 * min(len(warnings), 2)

    score = max(0.0, min(1.0, score))

    if score >= 0.65:
        band = "High"
    elif score >= 0.35:
        band = "Med"
    else:
        band = "Low"

    denom = " · ".join(parts) if parts else "limited data"
    label = f"{band} — {denom}"
    return {"band": band, "score": round(score, 2), "label": label}


def classify_intent(question: str | None) -> str:
    text = (question or "").strip().lower()
    if not text:
        return _DEFAULT_INTENT
    for intent, keywords in _INTENT_RULES:
        if any(keyword in text for keyword in keywords):
            return intent
    return _DEFAULT_INTENT


def extract_period(question: str | None) -> dict:
    """Pull an explicit period from the question. Empty dict means use defaults."""
    text = (question or "").lower()
    out: dict = {}
    now = datetime.now()

    fy = re.search(r"\bfy\s*-?\s*(20\d{2})\b", text)
    if fy:
        out["year"] = int(fy.group(1))
        out["_fy"] = True

    ymatch = re.search(r"\b(20\d{2})\b", text)
    if ymatch and "year" not in out:
        out["year"] = int(ymatch.group(1))

    for name, idx in _MONTHS.items():
        if re.search(rf"\b{name}\b", text):
            out["month"] = idx
            break

    if "ytd" in text or "year to date" in text or "year-to-date" in text:
        out["month"] = None
        out["_ytd"] = True
    if "full year" in text or "all year" in text:
        out["month"] = None
        out["_full_year"] = True
    if "last month" in text or "previous month" in text:
        prev_m = now.month - 1 or 12
        out["month"] = prev_m
        out["year"] = now.year if now.month > 1 else now.year - 1
    elif "this month" in text or "current month" in text:
        out["_this_month"] = True
    return out


def resolve_filters(question: str, base_filters: dict | None) -> dict:
    """Apply the chat default of current-year YTD unless the question overrides it."""
    base = ctx.normalize_filters(base_filters)
    period = extract_period(question)
    now = datetime.now()
    merged = dict(base)
    merged["year"] = now.year
    merged["month"] = None
    merged["period_mode"] = "ytd"

    if "year" in period:
        merged["year"] = period["year"]
    if period.get("_fy"):
        merged["period_mode"] = "financial_year"
        merged["month"] = None
    elif period.get("_full_year"):
        merged["period_mode"] = "full_year"
        merged["month"] = None
    elif period.get("_ytd"):
        merged["period_mode"] = "ytd"
        merged["month"] = None
    elif period.get("_this_month"):
        merged["period_mode"] = "monthly"
        merged["month"] = base.get("month") or now.month
        merged["year"] = base.get("year") or now.year
    elif period.get("month"):
        merged["period_mode"] = "monthly"
        merged["month"] = period["month"]
        if "year" not in period:
            merged["year"] = base.get("year") or now.year

    return ctx.normalize_filters(merged)


def _row_description(row: dict) -> str:
    return str(row.get("translated_description") or row.get("description") or row.get("description_original") or "").strip()


def classify_theme(text: str) -> str:
    blob = str(text or "").strip()
    if len(blob) < 5:
        return _UNKNOWN_THEME
    for name, patterns in _THEME_COMPILED:
        if any(pattern.search(blob) for pattern in patterns):
            return name
    return _UNKNOWN_THEME


def _persist_description_tags(classified: list, filters: dict) -> int:
    """Best effort persistence of local AI-suggested theme tags for later review."""
    try:
        os.makedirs(os.path.dirname(_TAGS_PATH), exist_ok=True)
        store = {}
        if os.path.exists(_TAGS_PATH):
            try:
                with open(_TAGS_PATH, encoding="utf-8") as fh:
                    store = json.load(fh) or {}
            except Exception:
                store = {}
        period = ctx.month_label(filters)
        now = datetime.now().isoformat(timespec="seconds")
        for theme, row, desc in classified:
            mr_wo = str(row.get("work_order_id") or row.get("request_id") or "").strip()
            asset_id = str(row.get("asset_id") or "").strip()
            key = f"{mr_wo or asset_id or 'NA'}|{desc[:24]}"
            if key.strip("|") in ("", "NA"):
                continue
            store[key] = {
                "mr_wo": mr_wo,
                "asset_id": asset_id,
                "asset_name": str(row.get("machine_name") or "").strip(),
                "functional_location": str(row.get("raw_functional_location") or "").strip(),
                "description_snippet": re.sub(r"\s+", " ", desc)[:120],
                "suggested_theme": theme,
                "period": period,
                "classified_at": now,
                "source": "MIRA keyword classifier (AI-suggested; confirm by engineering review)",
            }
        if len(store) > 5000:
            store = dict(sorted(store.items(), key=lambda item: item[1].get("classified_at", ""), reverse=True)[:5000])
        tmp = _TAGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _TAGS_PATH)
        return len(store)
    except Exception:
        return 0


def get_mr_description_theme_summary(filters: dict) -> dict:
    """Keyword-based MR description theme summary for the selected period."""
    normalized = ctx.normalize_filters(filters)
    rows = kpi._selected_period_work_order_rows(normalized)
    classified = []
    theme_counts: Counter[str] = Counter()
    for row in rows:
        desc = _row_description(row)
        theme = classify_theme(desc)
        theme_counts[theme] += 1
        classified.append((theme, row, desc))
    _persist_description_tags(classified, normalized)

    total_classified = sum(count for theme, count in theme_counts.items() if theme != _UNKNOWN_THEME)
    top = [(theme, count) for theme, count in theme_counts.most_common() if theme != _UNKNOWN_THEME]
    top_theme, top_count = (top[0] if top else (None, 0))

    top_asset = None
    top_location = None
    examples: list[str] = []
    if top_theme:
        theme_rows = [(row, desc) for theme, row, desc in classified if theme == top_theme]
        asset_counts = Counter(str(row.get("machine_name") or row.get("asset_id") or "Unknown").strip() for row, _ in theme_rows)
        loc_counts = Counter(str(row.get("raw_functional_location") or "Unspecified").strip() for row, _ in theme_rows)
        top_asset = asset_counts.most_common(1)[0][0] if asset_counts else None
        top_location = loc_counts.most_common(1)[0][0] if loc_counts else None
        for _, desc in theme_rows[:3]:
            snippet = re.sub(r"\s+", " ", desc)[:90]
            if snippet:
                examples.append(snippet)

    pct = round((top_count / len(rows)) * 100, 1) if rows and top_count else None
    return {
        "period": ctx.month_label(normalized),
        "rows_loaded": len(rows),
        "classified_descriptions": total_classified,
        "unknown_count": theme_counts.get(_UNKNOWN_THEME, 0),
        "top_theme": top_theme,
        "top_theme_count": top_count,
        "top_theme_pct": pct,
        "top_theme_asset": top_asset,
        "top_theme_functional_location": top_location,
        "theme_breakdown": [{"theme": theme, "count": count} for theme, count in theme_counts.most_common()],
        "example_descriptions": examples,
        "note": "These are AI-suggested classifications based on MR/WO descriptions. Final root cause should be confirmed by engineering review.",
        "source": "downtime MR descriptions (selected period)",
    }


def _fmt(value):
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _currency(value) -> str:
    if value is None:
        return "unavailable"
    try:
        return f"THB {float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _row_metric(label: str, value) -> dict:
    return {"label": label, "value": _fmt(value)}


def _is_read_only_request(question: str | None) -> bool:
    text = (question or "").strip().lower()
    return all(re.search(pattern, text) for pattern in _READ_ONLY_PATTERNS)


def _downtime_warning_lines(mr: dict) -> list[str]:
    warnings = []
    if (mr.get("missing_asset_count") or 0) > 0:
        warnings.append(f"{_fmt(mr.get('missing_asset_count'))} MR records are missing Asset ID.")
    if (mr.get("general_area_asset_count") or 0) > 0:
        warnings.append(f"{_fmt(mr.get('general_area_asset_count'))} MR records use a general area or placeholder asset name.")
    if (mr.get("missing_functional_location_count") or 0) > 0:
        warnings.append(f"{_fmt(mr.get('missing_functional_location_count'))} MR records are missing functional location.")
    if (mr.get("unknown_status_count") or 0) > 0:
        warnings.append(f"{_fmt(mr.get('unknown_status_count'))} MR records have an unmapped status.")
    return warnings


def _follow_up(mr: dict, pm: dict, *, spare: dict | None = None, top_location: str | None = None) -> list[str]:
    items = []
    if mr.get("open_count"):
        items.append(f"Review {_fmt(mr['open_count'])} open / in-progress MR, including {_fmt(mr.get('carry_over_open_mr'))} carry-over open MR.")
    if pm.get("overdue_pm"):
        items.append(f"Action {_fmt(pm['overdue_pm'])} overdue PM tasks with the engineering team.")
    if pm.get("backlog_pm"):
        items.append(f"Clear {_fmt(pm['backlog_pm'])} backlog PM items that are still pending.")
    if top_location:
        items.append(f"Check workload concentration at {top_location}.")
    if mr.get("missing_asset_count") or mr.get("general_area_asset_count"):
        items.append("Validate MR master-data quality before sharing the summary externally.")
    if spare and (spare.get("top_consumed_part") or (spare.get("yoy_consumption_pct") or 0) > 10):
        items.append("Review high-consumption or high-value spare-parts usage.")
    return items[:4]


def _view_data_used(intent: str, filters: dict, warnings: list, *, source_tables=None, rows_loaded=None, rows_after_filter=None, kpi_values_used=None) -> dict:
    window = ctx.resolved_window(filters)
    return {
        "period_mode": filters.get("period_mode"),
        "period_label": ctx.month_label(filters),
        "date_range": f"{window['start_date'].isoformat()} to {window['end_date'].isoformat()}",
        "source_tables": source_tables or ["Downtime MR/WO rows", "PM schedule payload", "Spare parts payload"],
        "filters_applied": [
            f"Period used: {ctx.month_label(filters)}",
            f"Period mode: {filters.get('period_mode')}",
            f"Stage: {filters.get('stage')}",
            f"Asset category: {filters.get('mainAssetGroup') or 'All'}",
        ],
        "rows_loaded": rows_loaded or [],
        "rows_after_filter": rows_after_filter or [],
        "kpi_values_used": kpi_values_used or [],
        "data_warnings": warnings or [],
        "last_refreshed": datetime.now().astimezone().strftime("%d %b %Y, %I:%M %p"),
        "intent": intent,
    }


def _run_with_timeout(producer, *args, timeout_seconds: int = _PM_QUERY_TIMEOUT_SECONDS, **kwargs):
    future = _PM_QUERY_EXECUTOR.submit(producer, *args, **kwargs)
    try:
        return future.result(timeout=timeout_seconds), None
    except FutureTimeoutError:
        return None, _PM_LOAD_WARNING
    except Exception:
        return None, "PM verified detail could not be loaded from the source schedule files right now."


def _resolve_kpi_filters(question: str, base_filters: dict | None) -> dict:
    """KPI card analysis must respect the dashboard filters unless text overrides them."""
    base = ctx.normalize_filters(base_filters)
    period = extract_period(question or "")
    if not period:
        return base
    return resolve_filters(question or "", base)


def _selected_mr_rows(filters: dict) -> list[dict]:
    try:
        return list(kpi._selected_period_work_order_rows(ctx.normalize_filters(filters)))  # type: ignore[attr-defined]
    except Exception:
        return list((kpi.get_work_orders(filters, limit=1000) or {}).get("rows") or [])


def _filtered_mr_rows(filters: dict) -> list[dict]:
    try:
        return list(kpi._filtered_work_order_rows(ctx.normalize_filters(filters)))  # type: ignore[attr-defined]
    except Exception:
        return _selected_mr_rows(filters)


def _carry_over_mr_rows(filters: dict) -> list[dict]:
    try:
        return list(kpi._opening_backlog_rows(ctx.normalize_filters(filters), _filtered_mr_rows(filters)))  # type: ignore[attr-defined]
    except Exception:
        activity = kpi.get_mr_activity_summary(filters)
        return [{} for _ in range(int(activity.get("carry_over_open_mr") or 0))]


def _parse_dt(value):
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text or text == "--":
        return None
    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized.split(".")[0]):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _hours_between(start_value, end_value):
    start_dt = _parse_dt(start_value)
    end_dt = _parse_dt(end_value)
    if not start_dt or not end_dt or end_dt < start_dt:
        return None
    return round((end_dt - start_dt).total_seconds() / 3600, 2)


def _safe_float(value):
    try:
        if value in (None, "", "--"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values):
    clean = [float(v) for v in values if v is not None]
    return round(sum(clean) / len(clean), 2) if clean else None


def _count_metric(metrics: dict, *keys) -> int:
    total = 0
    for key in keys:
        try:
            total += int(metrics.get(key) or 0)
        except (TypeError, ValueError):
            pass
    return total


def _metric_lines(metrics: dict, *, limit: int = 8) -> list[str]:
    lines = []
    for key, value in metrics.items():
        if isinstance(value, (dict, list)):
            continue
        label = str(key).replace("_", " ").title()
        lines.append(f"{label}: {_fmt(value)}")
        if len(lines) >= limit:
            break
    return lines


def _top_counter(rows: list[dict], keys: tuple[str, ...], *, limit: int = 5) -> list[dict]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = ""
        for key in keys:
            value = str(row.get(key) or "").strip()
            if value:
                break
        counts[value or "Unknown"] += 1
    return [{"name": name, "count": int(count)} for name, count in counts.most_common(limit)]


def _kpi_context_base(definition: dict, filters: dict, *, record_count: int, rows_after_filter: int | None = None, source: str | None = None) -> dict:
    return {
        "kpiId": definition["id"],
        "label": definition["label"],
        "category": definition["category"],
        "dataSource": definition["data_source"],
        "requiredFields": definition["required_fields"],
        "analysisFocus": definition["analysis_focus"],
        "emptyStateMessage": definition["empty_state"],
        "selectedPeriod": ctx.month_label(filters),
        "selectedStage": filters.get("stage") or "all",
        "recordCount": int(record_count or 0),
        "rowsAfterFilter": int(rows_after_filter if rows_after_filter is not None else record_count or 0),
        "source": source or definition["data_source"],
        "metrics": {},
        "evidence": [],
        "findings": [],
        "issueFocusAreas": [],
        "riskIndicators": [],
        "followUpActions": [],
        "dataGaps": [],
    }


def _finish_context(context: dict) -> dict:
    if not context.get("recordCount") and not context.get("metrics"):
        context["dataGaps"].append(context.get("emptyStateMessage") or "No valid data is available for this selected KPI.")
    context["keyNumbers"] = _metric_lines(context.get("metrics") or {}, limit=10)
    return context


def buildMrTrackingContext(filters: dict, definition: dict) -> dict:
    rows = _selected_mr_rows(filters)
    activity = kpi.get_mr_activity_summary(filters)
    open_records = kpi.get_open_mr_records(filters, limit=10)
    top_assets = kpi.get_top_assets_by_mr_count(filters, limit=5)
    ack_counts = Counter(str(row.get("acknowledgement_status") or "Unknown").strip() or "Unknown" for row in rows)
    open_rows = [row for row in rows if str(row.get("status_category") or "").lower() == "open" or str(row.get("request_state") or "").lower().replace(" ", "") in {"new", "inprogress"}]
    oldest_open_days = None
    if open_rows:
        raised_dates = [_parse_dt(row.get("request_created_time") or row.get("start_time")) for row in open_rows]
        raised_dates = [dt for dt in raised_dates if dt]
        if raised_dates:
            oldest_open_days = max((datetime.now(dt.tzinfo) - dt).days for dt in raised_dates)

    context = _kpi_context_base(definition, filters, record_count=len(rows), source=activity.get("source"))
    context["metrics"] = {
        "mr_raised": activity.get("mr_raised"),
        "open_mr": activity.get("open_count"),
        "in_progress_mr": activity.get("in_progress_count"),
        "new_mr": activity.get("new_count"),
        "finished_mr": activity.get("finished_count"),
        "closed_confirmed_mr": activity.get("closed_count"),
        "carry_over_open_mr": activity.get("carry_over_open_mr"),
        "not_acknowledged_mr": ack_counts.get("Not Acknowledged", 0),
        "acknowledged_in_progress_mr": ack_counts.get("Acknowledged / In Progress", 0),
        "oldest_open_mr_age_days": oldest_open_days,
        "top_actual_machine_asset": ((top_assets.get("top_actual_machine_asset") or {}).get("asset_name")),
    }
    context["evidence"] = [
        f"{_fmt(activity.get('mr_raised'))} MR were raised in {ctx.month_label(filters)}.",
        f"{_fmt(activity.get('open_count'))} are open and {_fmt(activity.get('carry_over_open_mr'))} are carry-over open MR.",
        f"Acknowledgement split: {', '.join(f'{name} {_fmt(count)}' for name, count in ack_counts.most_common(4)) or 'unavailable'}.",
    ]
    context["findings"] = [
        f"MR tracking shows {_fmt(activity.get('open_count'))} open MR and {_fmt(activity.get('in_progress_count'))} in progress in the selected period.",
        f"Top actual machine asset by MR count is {_fmt(context['metrics']['top_actual_machine_asset'])}.",
    ]
    if context["metrics"]["not_acknowledged_mr"]:
        context["issueFocusAreas"].append(f"{_fmt(context['metrics']['not_acknowledged_mr'])} MR are not acknowledged.")
    if activity.get("carry_over_open_mr"):
        context["issueFocusAreas"].append(f"{_fmt(activity.get('carry_over_open_mr'))} carry-over MR need ownership review.")
    if oldest_open_days is not None:
        context["riskIndicators"].append(f"Oldest open MR age is {_fmt(oldest_open_days)} day(s), indicating carry-over follow-up risk.")
    context["followUpActions"] = [
        "Review not-acknowledged MR and confirm whether each has a linked WO.",
        "Prioritise carry-over open MR before new MR volume increases.",
        "Check the top actual machine asset for repeat workload drivers.",
    ]
    if not rows:
        context["dataGaps"].append(definition["empty_state"])
    if not any("acknowledgement_status" in row for row in rows):
        context["dataGaps"].append("Acknowledgement status field is not available in the selected MR rows.")
    context["sampleRecords"] = open_records.get("records") or []
    return _finish_context(context)


def buildWorkOrderResponseContext(filters: dict, definition: dict) -> dict:
    rows = _selected_mr_rows(filters)
    activity = kpi.get_mr_activity_summary(filters)
    response_hours = []
    completion_hours = []
    target_rows = 0
    target_exceeded = 0
    for row in rows:
        response = _hours_between(row.get("request_created_time"), row.get("maintenance_start_time"))
        if response is not None:
            response_hours.append(response)
            target = _safe_float(row.get("response_target_hours") or row.get("ack_target_hours"))
            if target is not None:
                target_rows += 1
                if response > target:
                    target_exceeded += 1
        completion = _safe_float(row.get("duration_hours"))
        if completion is not None:
            completion_hours.append(completion)

    context = _kpi_context_base(definition, filters, record_count=len(rows), source="downtime work-order rows with request/start/end timestamps")
    context["metrics"] = {
        "total_mr": activity.get("mr_raised"),
        "open_outstanding_mr": activity.get("open_count"),
        "closed_confirmed_mr": activity.get("closed_count"),
        "avg_response_hours": _avg(response_hours),
        "avg_completion_hours": _avg(completion_hours),
        "response_records_with_valid_dates": len(response_hours),
        "completion_records_with_valid_ttr": len(completion_hours),
        "work_orders_exceeding_response_target": target_exceeded if target_rows else None,
    }
    context["evidence"] = [
        f"{_fmt(len(response_hours))} row(s) have valid raised-to-start timing for response analysis.",
        f"{_fmt(len(completion_hours))} row(s) have valid completion/TTR timing.",
        f"{_fmt(activity.get('open_count'))} MR remain open or outstanding in the selected period.",
    ]
    response_summary = (
        f"Average response time is {_fmt(context['metrics']['avg_response_hours'])} hours from MR raised to maintenance start."
        if context["metrics"]["avg_response_hours"] is not None
        else "Average response time is unavailable because no valid raised-to-start date pairs were found."
    )
    completion_summary = (
        f"Average completion time is {_fmt(context['metrics']['avg_completion_hours'])} hours for valid completed records."
        if context["metrics"]["avg_completion_hours"] is not None
        else "Average completion time is unavailable because no valid completion/TTR records were found."
    )
    context["findings"] = [
        response_summary,
        completion_summary,
    ]
    if activity.get("open_count"):
        context["issueFocusAreas"].append(f"{_fmt(activity.get('open_count'))} open MR remain outstanding.")
    if target_rows:
        context["riskIndicators"].append(f"{_fmt(target_exceeded)} work order(s) exceeded configured response target hours.")
    else:
        context["dataGaps"].append("Response target hours are not exposed on the selected work-order rows, so target-exceeding count is unavailable.")
    if not response_hours:
        context["dataGaps"].append("No valid request-created to actual-start date pairs were found for response-time calculation.")
    context["followUpActions"] = [
        "Review open MR with no actual start timestamp.",
        "Check high response-duration rows for delayed acknowledgement patterns.",
        "Validate date fields before using response time in management reporting.",
    ]
    return _finish_context(context)


def buildPreventiveVsCorrectiveContext(filters: dict, definition: dict) -> dict:
    mix = kpi.get_preventive_corrective_summary(filters)
    activity = kpi.get_mr_activity_summary(filters)
    year = int(filters.get("year") or datetime.now().year)
    end_month = int(filters.get("month") or datetime.now().month)
    trend = []
    for month in range(1, max(1, end_month) + 1):
        mf = dict(filters, year=year, month=month, period_mode="monthly")
        try:
            row = kpi.get_preventive_corrective_summary(mf)
            trend.append({
                "month": calendar.month_abbr[month],
                "preventive": row.get("preventive_count"),
                "corrective": row.get("corrective_count"),
                "total": row.get("total"),
            })
        except Exception:
            continue

    context = _kpi_context_base(definition, filters, record_count=int(mix.get("total") or 0), source=mix.get("source"))
    context["metrics"] = {
        "preventive_mr": mix.get("preventive_count"),
        "corrective_mr": mix.get("corrective_count"),
        "preventive_ratio_pct": mix.get("preventive_ratio_pct"),
        "corrective_ratio_pct": mix.get("corrective_ratio_pct"),
        "total_classified_mr": mix.get("total"),
        "classification_review_rows": mix.get("review_count"),
        "top_actual_machine_asset": activity.get("top_actual_machine_asset_name"),
    }
    context["trend"] = trend
    context["evidence"] = [
        f"Downtime classifier counted {_fmt(mix.get('preventive_count'))} preventive MR and {_fmt(mix.get('corrective_count'))} corrective MR.",
        f"Corrective ratio is {_fmt(mix.get('corrective_ratio_pct'))}% for {ctx.month_label(filters)}.",
    ]
    context["findings"] = [
        f"Maintenance mix is {_fmt(mix.get('preventive_count'))} preventive vs {_fmt(mix.get('corrective_count'))} corrective MR.",
        f"Corrective demand is the larger share at {_fmt(mix.get('corrective_ratio_pct'))}%." if (mix.get("corrective_count") or 0) > (mix.get("preventive_count") or 0) else "Preventive activity is equal to or above corrective MR for the selected period.",
    ]
    if (mix.get("corrective_count") or 0) > (mix.get("preventive_count") or 0):
        context["issueFocusAreas"].append("Corrective workload is higher than preventive workload in the selected period.")
        context["riskIndicators"].append("A corrective-heavy mix can indicate reactive maintenance pressure if repeated across periods.")
    if mix.get("review_count"):
        context["dataGaps"].append(f"{_fmt(mix.get('review_count'))} MR classification row(s) need review in the preventive/corrective classifier.")
    context["followUpActions"] = [
        "Review assets or areas generating repeated corrective MR.",
        "Compare corrective-heavy areas against upcoming PM coverage.",
        "Validate classifier review rows before sharing the maintenance mix externally.",
    ]
    return _finish_context(context)


def buildDataReliabilityContext(filters: dict, definition: dict) -> dict:
    rows = _selected_mr_rows(filters)
    dq = kpi.get_data_reliability_issues(filters)
    flag_counts: Counter[str] = Counter()
    for row in rows:
        flags = row.get("data_quality_flags") or row.get("data_quality_flag") or []
        if isinstance(flags, str):
            flags = [flag.strip() for flag in flags.split(";") if flag.strip()]
        for flag in flags:
            flag_counts[str(flag).strip()] += 1

    missing_start = _count_metric(flag_counts, "Missing start date for finished MR")
    missing_end = _count_metric(flag_counts, "Missing finished date for finished MR")
    invalid_logic = _count_metric(flag_counts, "Finished date before start date", "Finished date before raised date")
    context = _kpi_context_base(
        definition,
        filters,
        record_count=int(dq.get("total_work_orders") or len(rows)),
        rows_after_filter=len(rows),
        source=dq.get("source"),
    )
    context["metrics"] = {
        "total_work_orders": dq.get("total_work_orders"),
        "records_requiring_attention": dq.get("requires_attention_count"),
        "missing_start_date": missing_start,
        "missing_end_date": missing_end,
        "invalid_date_logic": invalid_logic,
        "duplicate_records": dq.get("duplicate_work_order_count"),
        "missing_asset_id": kpi.get_mr_activity_summary(filters).get("missing_asset_count"),
        "missing_functional_location": kpi.get_mr_activity_summary(filters).get("missing_functional_location_count"),
        "unknown_status": kpi.get_mr_activity_summary(filters).get("unknown_status_count"),
        "mttr_missing_total": dq.get("mttr_missing_total"),
        "mtbf_missing_total": dq.get("mtbf_missing_total"),
    }
    context["affectedKpis"] = ["response time", "completion time", "MTTR", "MTBF", "MR ageing", "open backlog"]
    top_issues = []
    for label, count in sorted(flag_counts.items(), key=lambda item: (-item[1], item[0]))[:5]:
        top_issues.append({"issue": label, "count": int(count), "impact": "May affect KPI accuracy or filtering."})
    context["topIssues"] = top_issues
    context["evidence"] = [
        f"{_fmt(dq.get('requires_attention_count'))} downtime records require attention.",
        f"MTTR missing total is {_fmt(dq.get('mttr_missing_total'))}; MTBF missing total is {_fmt(dq.get('mtbf_missing_total'))}.",
    ]
    context["findings"] = [
        f"Data reliability issues affect {_fmt(dq.get('requires_attention_count'))} selected records.",
        f"Top data issue: {_fmt(top_issues[0]['issue'])} ({_fmt(top_issues[0]['count'])})" if top_issues else "No row-level data-quality flags were found in the selected rows.",
    ]
    for issue in top_issues[:3]:
        context["issueFocusAreas"].append(f"{issue['issue']}: {_fmt(issue['count'])} row(s).")
    if invalid_logic:
        context["riskIndicators"].append("Invalid date logic may distort response time, completion time, MTTR, and MR ageing.")
    if not rows:
        context["dataGaps"].append(definition["empty_state"])
    if not flag_counts:
        context["dataGaps"].append("No detailed row-level data-quality flag breakdown is available for the selected rows.")
    context["followUpActions"] = [
        "Correct missing start/end dates on finished MR before management reporting.",
        "Validate missing asset ID and functional location rows with engineering.",
        "Re-check duplicate and invalid-date records before relying on trend KPIs.",
    ]
    return _finish_context(context)


def buildYearlyMrMovementContext(filters: dict, definition: dict) -> dict:
    base_year = int(filters.get("year") or datetime.now().year)
    years = [base_year - 2, base_year - 1, base_year]
    trend = []
    for year in years:
        yf = dict(filters, year=year, month=None, period_mode="full_year")
        try:
            activity = kpi.get_mr_activity_summary(yf)
            trend.append({
                "year": year,
                "mr_raised": activity.get("mr_raised"),
                "mr_finished": activity.get("closed_count"),
                "open_mr": activity.get("open_count"),
                "carry_over_open_mr": activity.get("carry_over_open_mr"),
                "closure_rate_pct": activity.get("closure_rate_pct"),
            })
        except Exception:
            trend.append({"year": year, "data_gap": "Year could not be loaded."})
    current = trend[-1] if trend else {}
    previous = trend[-2] if len(trend) > 1 else {}
    movement = None
    if current.get("mr_raised") is not None and previous.get("mr_raised") is not None:
        movement = int(current.get("mr_raised") or 0) - int(previous.get("mr_raised") or 0)

    context = _kpi_context_base(definition, filters, record_count=sum(int(row.get("mr_raised") or 0) for row in trend), source="downtime work-order rows grouped by year")
    context["metrics"] = {
        "selected_year": base_year,
        "current_year_mr_raised": current.get("mr_raised"),
        "current_year_mr_finished": current.get("mr_finished"),
        "current_year_open_mr": current.get("open_mr"),
        "current_year_carry_over_open_mr": current.get("carry_over_open_mr"),
        "mr_raised_change_vs_prior_year": movement,
        "current_year_closure_rate_pct": current.get("closure_rate_pct"),
    }
    context["trend"] = trend
    context["evidence"] = [f"{row.get('year')}: raised {_fmt(row.get('mr_raised'))}, finished {_fmt(row.get('mr_finished'))}, carry-over {_fmt(row.get('carry_over_open_mr'))}." for row in trend]
    context["findings"] = [
        f"{base_year} MR raised total is {_fmt(current.get('mr_raised'))}, with {_fmt(current.get('mr_finished'))} finished/closed.",
        f"MR raised movement vs prior year is {_fmt(movement)}." if movement is not None else "Prior-year movement is unavailable.",
    ]
    if current.get("carry_over_open_mr"):
        context["issueFocusAreas"].append(f"{_fmt(current.get('carry_over_open_mr'))} carry-over MR remain in {base_year}.")
        context["riskIndicators"].append("Carry-over backlog indicates closure is not fully keeping up with MR creation.")
    context["followUpActions"] = [
        "Compare MR raised vs finished by year with the engineering review cadence.",
        "Review carry-over trend before setting monthly closure targets.",
        "Check whether recurring assets are driving year-on-year MR growth.",
    ]
    return _finish_context(context)


def _criticality_text(row: dict) -> str:
    return " ".join(str(row.get(key) or "") for key in ("criticality", "raw_criticality", "normalized_criticality", "equipment_criticality", "asset_criticality")).lower()


def _is_existing_critical_asset(row: dict) -> bool:
    text = _criticality_text(row)
    if "non-critical" in text or "non critical" in text or "facility" in text:
        return False
    return bool(re.search(r"\bcritical\b", text))


def buildCriticalMachineActivityContext(filters: dict, definition: dict) -> dict:
    rows = _selected_mr_rows(filters)
    critical_rows = [
        row for row in rows
        if _is_existing_critical_asset(row)
    ]
    open_critical = [row for row in critical_rows if str(row.get("status_category") or "").lower() == "open" or str(row.get("request_state") or "").lower().replace(" ", "") in {"new", "inprogress"}]
    repeated = _top_counter(critical_rows, ("asset_display_name", "mapped_asset_name", "machine_name", "asset_name"), limit=5)
    downtime_hours = round(sum(float(row.get("duration_hours") or 0) for row in critical_rows if _safe_float(row.get("duration_hours")) is not None), 2)
    context = _kpi_context_base(definition, filters, record_count=len(critical_rows), rows_after_filter=len(critical_rows), source="downtime rows enriched by existing asset criticality list")
    context["metrics"] = {
        "critical_machine_mr": len(critical_rows),
        "active_critical_machine_mr": len(open_critical),
        "critical_machine_downtime_hours": downtime_hours,
        "critical_assets_with_repeat_mr": sum(1 for row in repeated if row.get("count", 0) > 1),
        "top_critical_asset": repeated[0]["name"] if repeated else None,
        "top_critical_asset_mr_count": repeated[0]["count"] if repeated else None,
    }
    context["topCriticalAssets"] = repeated
    context["evidence"] = [
        f"{_fmt(len(critical_rows))} selected MR rows are linked to existing critical asset classification.",
        f"{_fmt(len(open_critical))} critical-machine MR are active/open.",
    ]
    context["findings"] = [
        f"Critical machine activity count is {_fmt(len(critical_rows))} for {ctx.month_label(filters)}.",
        f"Top critical asset is {_fmt(context['metrics']['top_critical_asset'])} with {_fmt(context['metrics']['top_critical_asset_mr_count'])} MR." if repeated else "No repeated critical-machine asset appears in the selected data.",
    ]
    if open_critical:
        context["issueFocusAreas"].append(f"{_fmt(len(open_critical))} critical-machine MR remain open or in progress.")
        context["riskIndicators"].append("Open activity on existing critical assets requires follow-up because production impact can be higher.")
    if not critical_rows and rows:
        context["dataGaps"].append("No selected MR rows are tagged as critical by the existing asset criticality fields.")
    if rows and not any(_criticality_text(row).strip() or row.get("is_critical") is not None for row in rows):
        context["dataGaps"].append("Criticality fields are not available on selected MR rows, so critical machine analysis is limited.")
    context["followUpActions"] = [
        "Review open MR linked to existing critical assets.",
        "Check repeated critical-machine MR for maintenance planning follow-up.",
        "Validate criticality mapping for rows with missing asset IDs before reporting.",
    ]
    return _finish_context(context)


def buildPmScheduleContext(filters: dict, definition: dict) -> dict:
    pm_bundle, pm_warning = _run_with_timeout(kpi.get_verified_pm_metrics, filters)
    overdue, overdue_warning = (None, None)
    if pm_bundle:
        overdue, overdue_warning = _run_with_timeout(kpi.get_overdue_pm_records, filters, limit=8)
    pm_bundle = pm_bundle or {"metrics": {}, "data_quality": {}}
    metrics = pm_bundle.get("metrics") or {}
    scheduled = metrics.get("scheduled_pm")
    completed = metrics.get("completed_pm")
    pending = int(scheduled or 0) - int(completed or 0) if scheduled is not None and completed is not None else None
    context = _kpi_context_base(
        definition,
        filters,
        record_count=int((pm_bundle.get("data_quality") or {}).get("rows_loaded") or scheduled or 0),
        rows_after_filter=int((pm_bundle.get("data_quality") or {}).get("rows_after_filter") or scheduled or 0),
        source=(pm_bundle.get("data_quality") or {}).get("source"),
    )
    context["metrics"] = {
        "scheduled_pm": scheduled,
        "completed_pm_manual_done_only": completed,
        "pending_pm": pending,
        "due_this_month": metrics.get("due_this_month"),
        "overdue_pm": metrics.get("overdue_pm"),
        "backlog_pm": metrics.get("backlog_pm"),
        "deferred_pm": metrics.get("deferred_pm"),
        "pm_compliance_percent": metrics.get("pm_compliance_percent"),
        "focus_card": definition["label"],
    }
    context["evidence"] = [
        f"{_fmt(scheduled)} PM tasks are scheduled for {ctx.month_label(filters)}.",
        f"{_fmt(completed)} PM tasks are manually marked Done; auto-done is not counted.",
        f"{_fmt(metrics.get('overdue_pm'))} PM tasks are overdue and {_fmt(metrics.get('backlog_pm'))} are backlog.",
    ]
    compliance_value = metrics.get("pm_compliance_percent")
    compliance_text = (
        f"PM compliance is {_fmt(compliance_value)}% based on manual Done only."
        if compliance_value is not None
        else "PM compliance is unavailable because verified PM detail did not finish loading."
    )
    context["findings"] = [
        compliance_text,
        f"Selected focus is {definition['label']}, using PM schedule period KPIs.",
    ]
    if metrics.get("overdue_pm"):
        context["issueFocusAreas"].append(f"{_fmt(metrics.get('overdue_pm'))} overdue PM tasks need follow-up.")
    if pending:
        context["issueFocusAreas"].append(f"{_fmt(pending)} PM tasks remain pending in the selected period.")
    if metrics.get("pm_compliance_percent") is not None and float(metrics.get("pm_compliance_percent") or 0) < 80:
        context["riskIndicators"].append("PM compliance is below 80%, which may increase reactive workload if unresolved.")
    context["followUpActions"] = [
        "Review overdue and backlog PM with the engineering team.",
        "Confirm completed PM records are manually marked Done.",
        "Check PM workload by stage, asset category, and functional location.",
    ]
    context["sampleRecords"] = (overdue or {}).get("records") or []
    for warning in ((pm_bundle.get("data_quality") or {}).get("warnings") or []):
        context["dataGaps"].append(warning)
    if pm_warning:
        context["dataGaps"].append(pm_warning)
    if overdue_warning:
        context["dataGaps"].append(overdue_warning)
    return _finish_context(context)


def buildDowntimeContext(filters: dict, definition: dict) -> dict:
    downtime = kpi.get_mr_activity_summary(filters)
    mttr = kpi.get_mttr(filters)
    mtbf = kpi.get_mtbf(filters)
    top_assets = kpi.get_top_assets_by_mr_count(filters, limit=5)
    top_locations = kpi.get_top_functional_locations(filters, limit=5)
    context = _kpi_context_base(definition, filters, record_count=int(downtime.get("selected_work_order_rows_count") or downtime.get("mr_raised") or 0), source=downtime.get("source"))
    context["metrics"] = {
        "total_mr": downtime.get("mr_raised"),
        "open_mr": downtime.get("open_count"),
        "closed_confirmed_mr": downtime.get("closed_count"),
        "mttr_hours": mttr.get("overall_mttr_hours"),
        "mtbf_hours": mtbf.get("overall_average_mtbf_hours"),
        "highest_mttr_machine_group": mttr.get("highest_mttr_machine_group"),
        "lowest_mtbf_asset": mtbf.get("lowest_mtbf_asset_name"),
        "top_actual_machine_asset": ((top_assets.get("top_actual_machine_asset") or {}).get("asset_name")),
        "top_functional_location": ((top_locations.get("top_functional_location") or {}).get("name")),
        "focus_card": definition["label"],
    }
    context["evidence"] = [
        f"{_fmt(downtime.get('mr_raised'))} MR are in the selected downtime scope.",
        f"MTTR is {_fmt(mttr.get('overall_mttr_hours'))} h and MTBF is {_fmt(mtbf.get('overall_average_mtbf_hours'))} h.",
    ]
    context["findings"] = [
        f"Downtime focus card is {definition['label']}, using the verified downtime dashboard metrics.",
        f"Top actual machine asset is {_fmt(context['metrics']['top_actual_machine_asset'])}.",
    ]
    if downtime.get("open_count"):
        context["issueFocusAreas"].append(f"{_fmt(downtime.get('open_count'))} MR remain open in the downtime scope.")
    if mtbf.get("lowest_mtbf_asset_name"):
        context["riskIndicators"].append(f"Lowest MTBF asset is {_fmt(mtbf.get('lowest_mtbf_asset_name'))}; this is a repeat-interval indicator, not a severity assignment.")
    context["followUpActions"] = [
        "Review assets or functional locations with high MR count.",
        "Check low-MTBF assets and high-MTTR groups for repeated delays.",
        "Validate downtime date fields where MTTR/MTBF records are missing.",
    ]
    context["dataGaps"] = _downtime_warning_lines(downtime)
    return _finish_context(context)


def buildSparePartsContext(filters: dict, definition: dict) -> dict:
    spare = kpi.get_verified_spare_parts_metrics(filters)
    top = kpi.get_top_spare_parts_consumption(filters, limit=5)
    try:
        payload = kpi._spare_parts_payload()  # type: ignore[attr-defined]
    except Exception:
        payload = {}
    inventory_summary = ((payload.get("inventory") or {}).get("summary") or {})
    po_summary = ((payload.get("po_classification") or {}).get("summary") or {})
    context = _kpi_context_base(
        definition,
        filters,
        record_count=int(spare.get("inventory_rows_loaded") or 0),
        rows_after_filter=int(spare.get("project_transaction_rows_after_filter") or spare.get("po_rows_after_filter") or 0),
        source=spare.get("source"),
    )
    context["metrics"] = {
        "current_in_stock_spare_parts": spare.get("current_in_stock_items"),
        "current_in_stock_value": spare.get("current_in_stock_value"),
        "drawn_from_store_value": spare.get("drawn_from_store_value"),
        "non_stock_value": spare.get("non_stock_value"),
        "services_value": spare.get("services_value"),
        "low_stock_items": inventory_summary.get("low_stock_items"),
        "out_of_stock_items": inventory_summary.get("out_of_stock_items"),
        "reorder_required_items": inventory_summary.get("reorder_required_items"),
        "critical_equipment_parts_below_minimum": inventory_summary.get("critical_equipment_parts_below_minimum"),
        "pending_po_items": po_summary.get("spare_part_po_count"),
        "top_consumed_part": spare.get("top_consumed_part"),
        "yoy_consumption_pct": spare.get("yoy_consumption_pct"),
        "focus_card": definition["label"],
    }
    context["evidence"] = [
        f"{_fmt(spare.get('current_in_stock_items'))} in-stock spare part items are recorded.",
        f"Drawn-from-store value is {_currency(spare.get('drawn_from_store_value'))}; non-stock value is {_currency(spare.get('non_stock_value'))}.",
        "Services include repair and cleaning." if spare.get("services_value") is not None else "",
    ]
    context["findings"] = [
        f"Spare-parts focus card is {definition['label']}, using the spare-parts dashboard payload.",
        f"Top consumed part is {_fmt(spare.get('top_consumed_part'))}.",
    ]
    if inventory_summary.get("reorder_required_items"):
        context["issueFocusAreas"].append(f"{_fmt(inventory_summary.get('reorder_required_items'))} stocked items require reorder.")
    if inventory_summary.get("out_of_stock_items"):
        context["riskIndicators"].append(f"{_fmt(inventory_summary.get('out_of_stock_items'))} items are out of stock.")
    if inventory_summary.get("critical_equipment_parts_below_minimum"):
        context["riskIndicators"].append(f"{_fmt(inventory_summary.get('critical_equipment_parts_below_minimum'))} critical-equipment linked parts are below minimum based on existing inventory criticality.")
    context["followUpActions"] = [
        "Review below-minimum and out-of-stock items with stores.",
        "Check high-consumption parts against open MR and upcoming PM needs.",
        "Validate manual-review PO classifications before using purchase totals.",
    ]
    context["topParts"] = top.get("parts") or []
    context["dataGaps"] = list(spare.get("data_notes") or [])
    if not inventory_summary:
        context["dataGaps"].append("Detailed stock-health summary is unavailable from the spare-parts payload.")
    return _finish_context(context)


_KPI_REGISTRY = {
    "mr_tracking_acknowledgement": {
        "id": "mr_tracking_acknowledgement",
        "label": "MR Tracking & Acknowledgement",
        "category": "Maintenance Request",
        "data_source": "Downtime MR/WO rows",
        "required_fields": ["MR number", "status", "actualStart", "actualEnd", "acknowledgement", "assetId", "machineGroup", "createdDate"],
        "analysis_focus": ["open MR", "acknowledgement gaps", "oldest open MR", "MR status movement", "follow-up actions"],
        "empty_state": "No MR tracking records are available for the selected filters.",
        "builder": buildMrTrackingContext,
    },
    "mr_open": {
        "id": "mr_open", "label": "Open MR", "category": "Maintenance Request", "data_source": "Downtime open MR rows",
        "required_fields": ["MR number", "status", "createdDate", "assetId", "machineGroup"],
        "analysis_focus": ["open MR", "carry-over work", "oldest open MR", "follow-up actions"],
        "empty_state": "No open MR records are available for the selected filters.", "builder": buildMrTrackingContext,
    },
    "mr_in_progress": {
        "id": "mr_in_progress", "label": "In-Progress MR", "category": "Maintenance Request", "data_source": "Downtime MR status rows",
        "required_fields": ["MR number", "status", "actualStart", "assetId", "machineGroup"],
        "analysis_focus": ["in-progress MR", "acknowledgement", "completion movement"],
        "empty_state": "No in-progress MR records are available for the selected filters.", "builder": buildMrTrackingContext,
    },
    "wo_response_time": {
        "id": "wo_response_time", "label": "Work Order Response", "category": "Work Order", "data_source": "Downtime work-order timestamp rows",
        "required_fields": ["MR number", "WO number", "createdDate", "actualStart", "actualEnd", "status"],
        "analysis_focus": ["response time", "completion time", "open WO", "delayed WO", "date gaps"],
        "empty_state": "No work-order response records are available for the selected filters.", "builder": buildWorkOrderResponseContext,
    },
    "preventive_corrective_mix": {
        "id": "preventive_corrective_mix", "label": "Preventive vs Corrective", "category": "Maintenance Mix", "data_source": "Downtime preventive/corrective classifier",
        "required_fields": ["MR number", "maintenance type", "description", "createdDate", "status"],
        "analysis_focus": ["maintenance mix", "planned vs reactive work", "corrective trend", "classification review"],
        "empty_state": "No preventive/corrective MR classification data is available for the selected filters.", "builder": buildPreventiveVsCorrectiveContext,
    },
    "data_quality": {
        "id": "data_quality", "label": "Data Reliability", "category": "Data Quality", "data_source": "Downtime quality flags",
        "required_fields": ["MR number", "createdDate", "actualStart", "actualEnd", "status", "assetId", "functionalLocation"],
        "analysis_focus": ["missing dates", "invalid records", "duplicate records", "asset ID gaps", "KPI reliability"],
        "empty_state": "No data-reliability rows are available for the selected filters.", "builder": buildDataReliabilityContext,
    },
    "yearly_mr_movement": {
        "id": "yearly_mr_movement", "label": "Yearly MR Movement", "category": "Maintenance Request", "data_source": "Downtime all-year MR rows",
        "required_fields": ["MR number", "createdDate", "status", "actualEnd"],
        "analysis_focus": ["MR raised by year", "MR finished by year", "carry-over by year", "closure trend"],
        "empty_state": "No yearly MR movement data is available.", "builder": buildYearlyMrMovementContext,
    },
    "critical_machine_activity": {
        "id": "critical_machine_activity", "label": "Critical Machine Activity", "category": "Critical Assets", "data_source": "Downtime rows enriched from existing asset criticality list",
        "required_fields": ["assetId", "criticality", "status", "createdDate", "duration"],
        "analysis_focus": ["critical asset workload", "active critical MR/WO", "repeat activity", "downtime on critical machines"],
        "empty_state": "No existing critical-machine activity is available for the selected filters.", "builder": buildCriticalMachineActivityContext,
    },
}

for _pm_id, _pm_label in {
    "pm_due_today": "PM Due Today",
    "pm_completed": "PM Completed",
    "pm_pending": "PM Pending",
    "pm_overdue": "PM Overdue",
    "pm_completion_rate": "PM Completion Rate",
    "pm_upcoming_7_days": "Upcoming PM Next 7 Days",
}.items():
    _KPI_REGISTRY[_pm_id] = {
        "id": _pm_id,
        "label": _pm_label,
        "category": "PM Schedule",
        "data_source": "PM schedule period KPIs",
        "required_fields": ["PM task", "plannedDate", "status", "assetId", "stage", "scope"],
        "analysis_focus": ["due PM", "manual Done completion", "overdue PM", "backlog PM", "compliance"],
        "empty_state": "No PM schedule tasks are available for the selected filters.",
        "builder": buildPmScheduleContext,
    }

for _dt_id, _dt_label in {
    "downtime_active": "Current Active Downtime",
    "downtime_incidents": "Downtime Incidents",
    "downtime_total_hours": "Total Downtime Hours",
    "downtime_mttr": "MTTR",
    "downtime_mtbf": "MTBF",
    "downtime_top_machine_group": "Top Machine Groups",
    "downtime_repeat_assets": "Repeated Downtime Assets",
}.items():
    _KPI_REGISTRY[_dt_id] = {
        "id": _dt_id,
        "label": _dt_label,
        "category": "Downtime",
        "data_source": "Downtime dashboard verified metrics",
        "required_fields": ["MR number", "assetId", "machineGroup", "actualStart", "actualEnd", "duration", "status"],
        "analysis_focus": ["downtime workload", "MTTR", "MTBF", "repeat assets", "open downtime"],
        "empty_state": "No downtime records are available for the selected filters.",
        "builder": buildDowntimeContext,
    }

for _sp_id, _sp_label in {
    "spare_parts_low_stock": "Items Below Minimum Stock",
    "spare_parts_consumption": "High-Consumption Parts",
    "spare_parts_pending_po": "Pending PO / External Purchase",
    "spare_parts_stockout_risk": "Stock-Out Risk",
}.items():
    _KPI_REGISTRY[_sp_id] = {
        "id": _sp_id,
        "label": _sp_label,
        "category": "Spare Parts",
        "data_source": "Spare-parts inventory, PO, and project transaction payloads",
        "required_fields": ["partNumber", "stockQty", "minStock", "maxStock", "poDate", "quantity", "value"],
        "analysis_focus": ["below minimum stock", "consumption", "pending PO", "stock-out risk", "manual review"],
        "empty_state": "No spare-parts records are available for the selected filters.",
        "builder": buildSparePartsContext,
    }


def _selected_kpi_definitions(selected_kpis: list[str], selected_kpi_labels: list[str]) -> list[dict]:
    definitions = []
    for index, kpi_id in enumerate(selected_kpis):
        if kpi_id in _KPI_REGISTRY:
            definitions.append(_KPI_REGISTRY[kpi_id])
        else:
            label = selected_kpi_labels[index] if index < len(selected_kpi_labels) else kpi_id
            definitions.append({
                "id": kpi_id,
                "label": label,
                "category": "Unknown KPI",
                "data_source": "Unavailable",
                "required_fields": [],
                "analysis_focus": ["selected KPI could not be matched to a backend context builder"],
                "empty_state": f"No backend KPI context builder is registered for {label}.",
                "builder": None,
            })
    return definitions


def _build_kpi_contexts(filters: dict, definitions: list[dict]) -> list[dict]:
    contexts = []
    for definition in definitions:
        builder = definition.get("builder")
        if not builder:
            context = _kpi_context_base(definition, filters, record_count=0)
            context["dataGaps"].append(definition["empty_state"])
            contexts.append(_finish_context(context))
            continue
        try:
            contexts.append(builder(filters, definition))
        except Exception as exc:
            context = _kpi_context_base(definition, filters, record_count=0)
            context["dataGaps"].append(f"{definition['label']} context could not be built: {exc}")
            contexts.append(_finish_context(context))
    return contexts


def _section_from_kpi_context(context: dict) -> dict:
    summary = (
        f"{context['label']} for {context['selectedPeriod']} uses {context['dataSource']} "
        f"with {_fmt(context.get('recordCount'))} source row(s)."
    )
    if context.get("findings"):
        summary += " " + str(context["findings"][0])
    return {
        "kpi_id": context["kpiId"],
        "title": context["label"],
        "summary": summary,
        "key_findings": context.get("findings") or [],
        "issue_focus_areas": context.get("issueFocusAreas") or [],
        "risk_indicators": context.get("riskIndicators") or [],
        "follow_up_actions": context.get("followUpActions") or [],
        "data_gaps": context.get("dataGaps") or [],
    }


def _fallback_kpi_answer(contexts: list[dict]) -> tuple[str, list[dict]]:
    sections = [_section_from_kpi_context(context) for context in contexts]
    if not contexts:
        return "No KPI cards were selected, so MIRA could not build a KPI-specific analysis.", sections
    if len(contexts) == 1:
        section = sections[0]
        answer = f"KPI Summary: {section['summary']}"
    else:
        labels = ", ".join(context["label"] for context in contexts)
        answer = f"Overall Summary: MIRA analysed {len(contexts)} selected KPI areas for the current dashboard filters: {labels}."
    return answer, sections


def _kpi_prompt(question: str, filters: dict, contexts: list[dict]) -> str:
    output_format = (
        "For single KPI selection, use sections: 1. KPI Summary 2. Key Findings 3. Issue Focus Areas "
        "4. Predictive / Risk Indicators 5. Follow-up Actions 6. Data Gaps."
        if len(contexts) == 1
        else "For multiple KPI selections, use sections: 1. Overall Summary 2. Findings by Selected KPI "
        "3. Cross-KPI Issue Focus 4. Predictive / Risk Indicators 5. Follow-up Actions 6. Data Gaps. "
        "Include a separate subsection for each selected KPI."
    )
    compact = {
        "question": question,
        "selected_period": ctx.month_label(filters),
        "period_mode": filters.get("period_mode"),
        "stage": filters.get("stage"),
        "selected_kpi_contexts": contexts,
    }
    return (
        f'Answer this KPI Analysis request: "{question}"\n\n'
        "You are analysing only the selected KPI area unless multiple KPI cards are selected. "
        "Do not produce a generic maintenance summary. Use the KPI context provided. "
        "Focus on the selected KPI's evidence, patterns, risks, and follow-up actions. "
        "Do not invent values. Do not recommend or assign severity. MIRA is read-only.\n\n"
        f"{output_format}\n\n"
        f"VERIFIED_KPI_CONTEXT_JSON:\n{json.dumps(compact, default=str, ensure_ascii=False)}\n"
    )


def _answer_kpi_analysis(question: str, base_filters: dict | None, selected_kpis: list[str], selected_kpi_labels: list[str]) -> dict:
    filters = _resolve_kpi_filters(question or "", base_filters)
    definitions = _selected_kpi_definitions(selected_kpis, selected_kpi_labels)
    contexts = _build_kpi_contexts(filters, definitions)
    answer_text, sections = _fallback_kpi_answer(contexts)

    provider = OllamaMiraProvider()
    used_llm = False
    if config.LOCAL_LLM_ENABLED and config.PROVIDER_MODE in ("auto", "ollama") and provider.resolve_model() and contexts:
        try:
            llm = generate_with_ollama(
                _CHAT_SYSTEM_PROMPT,
                _kpi_prompt(question or "", filters, contexts),
                model=provider.resolve_model(),
                timeout=20,
            ).strip()
            if llm:
                answer_text = llm
                used_llm = True
        except Exception:
            used_llm = False

    key_numbers = []
    warnings = []
    for context in contexts:
        key_numbers.extend([f"{context['label']} - {line}" for line in (context.get("keyNumbers") or [])[:6]])
        warnings.extend(context.get("dataGaps") or [])

    view_data = _view_data_used(
        "kpi_analysis",
        filters,
        warnings,
        source_tables=[f"{context['label']}: {context['dataSource']}" for context in contexts],
        rows_loaded=[_row_metric(context["label"], context.get("recordCount")) for context in contexts],
        rows_after_filter=[_row_metric(context["label"], context.get("rowsAfterFilter")) for context in contexts],
        kpi_values_used=key_numbers[:40],
    )
    view_data["selected_kpis"] = [context["kpiId"] for context in contexts]
    view_data["analysis_focus"] = [
        {"kpi": context["label"], "focus": context.get("analysisFocus") or []}
        for context in contexts
    ]

    status = get_provider_status()
    follow_up = []
    for context in contexts:
        for item in context.get("followUpActions") or []:
            if item not in follow_up:
                follow_up.append(item)

    return {
        "ok": True,
        "intent": "kpi_analysis",
        "mode": "kpi_analysis",
        "period": ctx.month_label(filters),
        "period_used": f"Period used: {ctx.month_label(filters)}",
        "filters": filters,
        "answer": answer_text,
        "key_numbers_used": key_numbers[:24],
        "insight": [f"Selected KPI focus areas: {', '.join(context['label'] for context in contexts) or 'None'}."],
        "recommended_follow_up": follow_up[:8],
        "kpi_analysis_contexts": contexts,
        "kpi_analysis_sections": sections,
        "selected_kpis": [context["kpiId"] for context in contexts],
        "selected_kpi_labels": [context["label"] for context in contexts],
        "view_data_used": view_data,
        "provider": "ollama" if used_llm else "rule_based",
        "provider_status": status["status"],
        "provider_mode_label": "Ollama connected" if used_llm else "Rule-based fallback",
        "llm_active": used_llm,
        "read_only": True,
    }


def build_context(intent: str, filters: dict, question: str) -> dict:
    """Return the verified context bundle that powers one chat response."""
    period = ctx.month_label(filters)
    out = {
        "intent": intent,
        "period": period,
        "answer_seed": "",
        "key_numbers": [],
        "insight": [],
        "follow_up": [],
        "theme": None,
        "risk": None,
        "context": {},
        "view_data_used": None,
        "warnings": [],
    }

    if intent == "maintenance_summary":
        verified = kpi.get_verified_downtime_metrics(filters)
        mr = verified.get("downtime_summary") or {}
        pm_bundle, pm_warning = _run_with_timeout(kpi.get_verified_pm_metrics, filters)
        pm = (pm_bundle or {}).get("metrics", {})
        out["context"] = {"downtime": mr, "pm": pm}
        out["answer_seed"] = f"For {period}, {_fmt(mr.get('total_work_orders'))} MR were raised and {_fmt(mr.get('closed_work_orders'))} were closed / confirmed."
        if pm_bundle:
            out["answer_seed"] += (
                f" PM compliance was {_fmt(pm.get('pm_compliance_percent'))}% with "
                f"{_fmt(pm.get('overdue_pm'))} overdue PM tasks."
            )
        elif pm_warning:
            out["answer_seed"] += f" {pm_warning}"
        out["key_numbers"] = [
            f"MR Raised: {_fmt(mr.get('total_work_orders'))}",
            f"Open / In Progress MR: {_fmt(mr.get('open_work_orders'))}",
            f"Closed / Confirmed MR: {_fmt(mr.get('closed_work_orders'))}",
            f"Closure Rate: {_fmt(mr.get('closure_rate_pct'))}%",
            f"Top Actual Machine Asset: {_fmt(mr.get('top_actual_machine_asset_name'))}",
            f"MTTR: {_fmt(verified.get('mttr_hours'))} h",
        ]
        if pm_bundle:
            out["key_numbers"].insert(4, f"PM Compliance: {_fmt(pm.get('pm_compliance_percent'))}%")
            out["key_numbers"].insert(5, f"PM Overdue: {_fmt(pm.get('overdue_pm'))}")
        out["insight"] = [
            f"Carry-over open MR remain at {_fmt(mr.get('carry_over_open_mr'))}, bringing total active workload to {_fmt(mr.get('total_active_workload'))}.",
            f"Corrective MR remain the larger share of workload at {_fmt(mr.get('corrective_count'))} versus {_fmt(mr.get('preventive_count'))} preventive MR.",
            f"Data quality issues flagged for follow-up: {_fmt(mr.get('data_quality_issue_count'))}.",
        ]
        if pm_warning:
            out["insight"].append(pm_warning)
        out["follow_up"] = _follow_up(mr, pm if pm_bundle else {"overdue_pm": 0, "backlog_pm": 0}, top_location=mr.get("top_functional_location_name"))
        out["warnings"] = _downtime_warning_lines(mr) + ((pm_bundle or {}).get("data_quality", {}).get("warnings") or [])
        if pm_warning:
            out["warnings"].append(pm_warning)
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Downtime MR/WO rows", "Downtime MTTR/MTBF summary"] + (["PM schedule payload"] if pm_bundle else []),
            rows_loaded=[
                _row_metric("Selected-period MR loaded", mr.get("selected_work_order_rows_count")),
                _row_metric("Carry-over open MR", mr.get("carry_over_open_mr")),
            ] + ([_row_metric("PM tasks loaded", pm.get("scheduled_pm"))] if pm_bundle else []),
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "downtime_summary":
        verified = kpi.get_verified_downtime_metrics(filters)
        mr = verified.get("downtime_summary") or {}
        out["context"] = verified
        out["answer_seed"] = (
            f"For {period}, {_fmt(mr.get('total_work_orders'))} MR were raised, {_fmt(mr.get('closed_work_orders'))} were "
            f"closed / confirmed, and {_fmt(mr.get('open_work_orders'))} remain open or in progress."
        )
        out["key_numbers"] = [
            f"MR Raised: {_fmt(mr.get('total_work_orders'))}",
            f"Open / In Progress MR: {_fmt(mr.get('open_work_orders'))}",
            f"Closed / Confirmed MR: {_fmt(mr.get('closed_work_orders'))}",
            f"Closure Rate: {_fmt(mr.get('closure_rate_pct'))}%",
            f"Carry-over Open MR: {_fmt(mr.get('carry_over_open_mr'))}",
            f"Total Active Workload: {_fmt(mr.get('total_active_workload'))}",
            f"Top Recorded Asset / Area: {_fmt(mr.get('top_recorded_asset_name'))}",
            f"Top Actual Machine Asset: {_fmt(mr.get('top_actual_machine_asset_name'))}",
            f"Top Functional Location: {_fmt(mr.get('top_functional_location_name'))}",
            f"MTTR: {_fmt(verified.get('mttr_hours'))} h",
            f"MTBF: {_fmt(verified.get('mtbf_hours'))} h",
        ]
        out["insight"] = [
            f"Closure rate was {_fmt(mr.get('closure_rate_pct'))}% for the selected period.",
            f"Corrective MR dominated period activity at {_fmt(mr.get('corrective_count'))} versus {_fmt(mr.get('preventive_count'))} preventive MR.",
            f"Data quality issues flagged: {_fmt(mr.get('data_quality_issue_count'))}.",
        ]
        out["follow_up"] = _follow_up(mr, {"overdue_pm": 0, "backlog_pm": 0}, top_location=mr.get("top_functional_location_name"))
        out["warnings"] = _downtime_warning_lines(mr)
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Downtime MR/WO rows", "Downtime MTTR/MTBF summary"],
            rows_loaded=[
                _row_metric("Selected-period MR loaded", mr.get("selected_work_order_rows_count")),
                _row_metric("Carry-over open MR", mr.get("carry_over_open_mr")),
            ],
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "pm_summary":
        pm_bundle, pm_warning = _run_with_timeout(kpi.get_verified_pm_metrics, filters)
        overdue, overdue_warning = _run_with_timeout(kpi.get_overdue_pm_records, filters, limit=5)
        pm = (pm_bundle or {}).get("metrics", {})
        overdue = overdue or {}
        out["context"] = {"pm": pm, "overdue": overdue, "data_quality": (pm_bundle or {}).get("data_quality")}
        if not pm_bundle:
            warning_text = overdue_warning or pm_warning or _PM_LOAD_WARNING
            out["answer_seed"] = f"PM verified detail for {period} is still loading from the source schedule files."
            out["insight"] = [warning_text]
            out["follow_up"] = ["Try the PM question again in a moment once the verified schedule detail has loaded."]
            out["warnings"] = [warning_text]
            out["view_data_used"] = _view_data_used(intent, filters, out["warnings"], source_tables=["PM schedule payload"])
            return out
        out["answer_seed"] = (
            f"For {period}, {_fmt(pm.get('scheduled_pm'))} PM tasks are scheduled, {_fmt(pm.get('completed_pm'))} are manually completed, "
            f"and compliance stands at {_fmt(pm.get('pm_compliance_percent'))}%."
        )
        out["key_numbers"] = [
            f"PM Scheduled: {_fmt(pm.get('scheduled_pm'))}",
            f"PM Completed: {_fmt(pm.get('completed_pm'))}",
            f"PM Due This Month: {_fmt(pm.get('due_this_month'))}",
            f"PM Overdue: {_fmt(pm.get('overdue_pm'))}",
            f"PM Backlog: {_fmt(pm.get('backlog_pm'))}",
            f"PM Compliance: {_fmt(pm.get('pm_compliance_percent'))}%",
        ]
        out["insight"] = [
            "PM completion is counted only when manually marked Done.",
            f"Overdue PM currently totals {_fmt(pm.get('overdue_pm'))}, with backlog at {_fmt(pm.get('backlog_pm'))}.",
            f"{_fmt(overdue.get('overdue_count'))} overdue PM task rows are available for follow-up detail.",
        ]
        out["follow_up"] = [
            f"Review {_fmt(pm.get('overdue_pm'))} overdue PM tasks with the engineering team.",
            f"Work through {_fmt(pm.get('backlog_pm'))} backlog PM items still pending." if pm.get("backlog_pm") else "No PM backlog is currently flagged.",
        ]
        out["warnings"] = pm_bundle["data_quality"]["warnings"] + ([overdue_warning] if overdue_warning else [])
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["PM schedule payload"],
            rows_loaded=[_row_metric("PM tasks loaded", pm.get("scheduled_pm"))],
            rows_after_filter=[_row_metric("Overdue PM rows", overdue.get("overdue_count"))],
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "pm_overdue_query":
        pm_bundle, pm_warning = _run_with_timeout(kpi.get_verified_pm_metrics, filters)
        overdue, overdue_warning = _run_with_timeout(kpi.get_overdue_pm_records, filters, limit=5)
        pm = (pm_bundle or {}).get("metrics", {})
        overdue = overdue or {}
        out["context"] = {"pm": pm, "overdue": overdue}
        if not pm_bundle or not overdue:
            warning_text = overdue_warning or pm_warning or _PM_LOAD_WARNING
            out["answer_seed"] = f"PM overdue detail for {period} is still loading from the source schedule files."
            out["insight"] = [warning_text]
            out["follow_up"] = ["Try this PM overdue question again in a moment for the latest verified task list."]
            out["warnings"] = [warning_text]
            out["view_data_used"] = _view_data_used(intent, filters, out["warnings"], source_tables=["PM schedule payload"])
            return out
        first = (overdue.get("records") or [None])[0]
        out["answer_seed"] = (
            f"{_fmt(overdue.get('overdue_count'))} PM tasks are currently overdue in {period}."
            + (f" The highest visible follow-up item is {first.get('asset_name')} at {first.get('system_area')}." if first else "")
        )
        out["key_numbers"] = [
            f"PM Overdue: {_fmt(overdue.get('overdue_count'))}",
            f"PM Backlog: {_fmt(pm.get('backlog_pm'))}",
        ]
        for index, task in enumerate(overdue.get("records") or [], start=1):
            out["key_numbers"].append(
                f"Overdue Task {index}: {_fmt(task.get('asset_name'))} / {_fmt(task.get('system_area'))} ({_fmt(task.get('days_overdue'))} days)"
            )
        out["insight"] = [
            f"PM overdue count is {_fmt(overdue.get('overdue_count'))} for the selected scope.",
            f"PM backlog remains {_fmt(pm.get('backlog_pm'))}.",
        ]
        out["follow_up"] = [
            "Prioritise the overdue PM list starting with the oldest items.",
            f"Review whether backlog PM at {_fmt(pm.get('backlog_pm'))} needs rescheduling or immediate action.",
        ]
        out["warnings"] = pm_bundle["data_quality"]["warnings"] + ([overdue_warning] if overdue_warning else [])
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["PM schedule payload"],
            rows_loaded=[_row_metric("All overdue PM rows", overdue.get("rows_loaded"))],
            rows_after_filter=[_row_metric("Filtered overdue PM rows", overdue.get("overdue_count"))],
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "backlog_query":
        mr = kpi.get_mr_activity_summary(filters)
        pm_bundle, pm_warning = _run_with_timeout(kpi.get_verified_pm_metrics, filters)
        pm = (pm_bundle or {}).get("metrics", {})
        out["context"] = {"downtime": mr, "pm": pm}
        out["answer_seed"] = f"In {period}, backlog is mainly {_fmt(mr.get('carry_over_open_mr'))} carry-over open MR."
        if pm_bundle:
            out["answer_seed"] += f" PM backlog is {_fmt(pm.get('backlog_pm'))} tasks with {_fmt(pm.get('overdue_pm'))} overdue."
        elif pm_warning:
            out["answer_seed"] += f" {pm_warning}"
        out["key_numbers"] = [
            f"Carry-over Open MR: {_fmt(mr.get('carry_over_open_mr'))}",
            f"Open / In Progress MR: {_fmt(mr.get('open_count'))}",
        ]
        if pm_bundle:
            out["key_numbers"].append(f"PM Backlog: {_fmt(pm.get('backlog_pm'))}")
            out["key_numbers"].append(f"PM Overdue: {_fmt(pm.get('overdue_pm'))}")
        out["insight"] = [
            f"Total active MR workload rises to {_fmt(mr.get('total_active_workload'))} once carry-over open MR are included.",
        ]
        if pm_bundle:
            out["insight"].append("PM backlog and overdue PM should be reviewed together because both affect schedule compliance.")
        elif pm_warning:
            out["insight"].append(pm_warning)
        out["follow_up"] = _follow_up(mr, pm if pm_bundle else {"overdue_pm": 0, "backlog_pm": 0})
        out["warnings"] = _downtime_warning_lines(mr) + ((pm_bundle or {}).get("data_quality", {}).get("warnings") or [])
        if pm_warning:
            out["warnings"].append(pm_warning)
        out["view_data_used"] = _view_data_used(intent, filters, out["warnings"], kpi_values_used=out["key_numbers"])

    elif intent == "top_asset_query":
        assets = kpi.get_top_assets_by_mr_count(filters)
        recorded = assets.get("top_recorded_asset") or {}
        actual = assets.get("top_actual_machine_asset") or {}
        out["context"] = assets
        out["answer_seed"] = (
            f"The top recorded asset or area in {period} is {_fmt(recorded.get('asset_name'))} with {_fmt(recorded.get('mr_count'))} MR."
            + (f" The top actual machine asset is {_fmt(actual.get('asset_name'))} with {_fmt(actual.get('mr_count'))} MR." if actual.get("asset_name") else "")
        )
        out["key_numbers"] = [
            f"Top Recorded Asset / Area: {_fmt(recorded.get('asset_name'))} ({_fmt(recorded.get('mr_count'))} MR)"
            + (" - placeholder / general area" if recorded.get("is_placeholder") else ""),
            f"Top Actual Machine Asset: {_fmt(actual.get('asset_name'))} ({_fmt(actual.get('mr_count'))} MR)",
        ]
        out["insight"] = ["Recorded asset and actual machine asset are kept separate so general areas do not hide the real machine follow-up."]
        if recorded.get("is_placeholder"):
            out["insight"].append("The top recorded item is a placeholder or area, so the top actual machine asset is the better engineering follow-up point.")
        out["follow_up"] = [f"Review {_fmt(actual.get('asset_name') or recorded.get('asset_name'))} with engineering for repeat workload drivers."]
        out["warnings"] = [recorded.get("reason")] if recorded.get("is_placeholder") else []
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Downtime MR/WO rows"],
            rows_loaded=[_row_metric("Selected-period MR loaded", assets.get("rows_loaded"))],
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "top_functional_location_query":
        locations = kpi.get_top_functional_locations(filters, limit=5)
        top = locations.get("top_functional_location") or {}
        out["context"] = locations
        out["answer_seed"] = f"The highest workload functional location in {period} is {_fmt(top.get('name'))} with {_fmt(top.get('mr_count'))} MR."
        out["key_numbers"] = [f"Top Functional Location: {_fmt(top.get('name'))} ({_fmt(top.get('mr_count'))} MR)"]
        for index, row in enumerate(locations.get("functional_locations") or [], start=1):
            out["key_numbers"].append(f"Location {index}: {_fmt(row.get('functional_location'))} ({_fmt(row.get('mr_count'))} MR)")
        out["insight"] = ["Functional location highlights where maintenance workload is concentrated operationally."]
        out["follow_up"] = [f"Review workload concentration at {_fmt(top.get('name'))}."]
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Downtime MR/WO rows"],
            rows_loaded=[_row_metric("Selected-period MR loaded", locations.get("rows_loaded"))],
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "open_mr_query":
        open_rows = kpi.get_open_mr_records(filters, limit=5)
        out["context"] = open_rows
        first = (open_rows.get("records") or [None])[0]
        out["answer_seed"] = (
            f"{_fmt(open_rows.get('open_count'))} MR are still open or in progress in {period}, with "
            f"{_fmt(open_rows.get('carry_over_open_mr'))} carry-over open MR from before the period."
            + (f" One visible example is {first.get('asset_name')} at {first.get('functional_location')}." if first else "")
        )
        out["key_numbers"] = [
            f"Open / In Progress MR: {_fmt(open_rows.get('open_count'))}",
            f"Carry-over Open MR: {_fmt(open_rows.get('carry_over_open_mr'))}",
        ]
        for index, row in enumerate(open_rows.get("records") or [], start=1):
            out["key_numbers"].append(f"Open MR {index}: {_fmt(row.get('asset_name'))} / {_fmt(row.get('functional_location'))} ({_fmt(row.get('status'))})")
        out["insight"] = [f"Carry-over open MR remain material at {_fmt(open_rows.get('carry_over_open_mr'))}."]
        out["follow_up"] = ["Review the open MR list and prioritise the highest-severity unresolved items."]
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Downtime MR/WO rows"],
            rows_loaded=[_row_metric("Open selected-period MR rows", open_rows.get("rows_loaded"))],
            rows_after_filter=[_row_metric("Returned MR rows", len(open_rows.get("records") or []))],
            kpi_values_used=out["key_numbers"],
        )

    elif intent in ("fault_theme_query", "recurring_issue_query"):
        theme = get_mr_description_theme_summary(filters)
        out["theme"] = theme
        out["context"] = {"theme_analysis": theme}
        if theme.get("top_theme"):
            if intent == "recurring_issue_query":
                assets = kpi.get_top_assets_by_mr_count(filters)
                actual = assets.get("top_actual_machine_asset") or {}
                out["context"]["top_assets"] = assets
                out["answer_seed"] = (
                    f"The strongest recurring pattern in {period} is {_fmt(theme.get('top_theme'))}, and the top actual machine asset "
                    f"by MR count is {_fmt(actual.get('asset_name'))} with {_fmt(actual.get('mr_count'))} MR."
                )
                out["key_numbers"] = [
                    f"Top Theme: {_fmt(theme.get('top_theme'))} ({_fmt(theme.get('top_theme_count'))}/{_fmt(theme.get('rows_loaded'))} MR)",
                    f"Top Actual Machine Asset: {_fmt(actual.get('asset_name'))} ({_fmt(actual.get('mr_count'))} MR)",
                    f"Top Functional Location for Theme: {_fmt(theme.get('top_theme_functional_location'))}",
                ]
                out["insight"] = [
                    "Recurring issue questions combine the theme pattern and the highest-frequency machine follow-up point.",
                    theme.get("note"),
                ]
                out["follow_up"] = ["Review the repeated-issue machine with engineering and confirm the actual root cause from MR history."]
            else:
                out["answer_seed"] = (
                    f"The most common fault theme in {period} is {_fmt(theme.get('top_theme'))}, based on "
                    f"{_fmt(theme.get('top_theme_count'))} of {_fmt(theme.get('rows_loaded'))} MR descriptions."
                )
                out["key_numbers"] = [
                    f"Top Fault Theme: {_fmt(theme.get('top_theme'))}",
                    f"Theme Count: {_fmt(theme.get('top_theme_count'))} of {_fmt(theme.get('rows_loaded'))} MR ({_fmt(theme.get('top_theme_pct'))}%)",
                    f"Top Related Asset: {_fmt(theme.get('top_theme_asset'))}",
                    f"Top Related Functional Location: {_fmt(theme.get('top_theme_functional_location'))}",
                ]
                out["insight"] = [
                    theme.get("note"),
                    f"Unknown or insufficient descriptions account for {_fmt(theme.get('unknown_count'))} MR." if theme.get("unknown_count") else "Description coverage is adequate for a theme indication.",
                ]
                out["follow_up"] = ["Confirm the suggested fault theme through engineering review before assigning a root cause."]
        else:
            out["answer_seed"] = (
                "Description theme analysis did not return a dominant pattern for the selected period. "
                "I can still summarise MR counts, top assets, functional locations, and open workload from verified dashboard data."
            )
            out["warnings"] = ["MR description theme analysis did not return a dominant classified pattern for the selected period."]
            out["follow_up"] = ["Use the verified downtime summary to review top assets, functional locations, and open workload."]
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Downtime MR descriptions"],
            rows_loaded=[_row_metric("MR descriptions loaded", theme.get("rows_loaded"))],
            rows_after_filter=[_row_metric("Classified descriptions", theme.get("classified_descriptions"))],
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "daily_follow_up_query":
        mr = kpi.get_mr_activity_summary(filters)
        pm_bundle, pm_warning = _run_with_timeout(kpi.get_verified_pm_metrics, filters)
        pm = (pm_bundle or {}).get("metrics", {})
        top_location = kpi.get_top_functional_locations(filters, limit=1)
        spare_top = kpi.get_top_spare_parts_consumption(filters, limit=3)
        out["context"] = {"downtime": mr, "pm": pm, "top_location": top_location, "spare": spare_top}
        out["answer_seed"] = f"Today's follow-up for {period} is centered on {_fmt(mr.get('open_count'))} open or in-progress MR."
        if pm_bundle:
            out["answer_seed"] += (
                f" PM follow-up also includes {_fmt(pm.get('overdue_pm'))} overdue PM tasks and "
                f"{_fmt(pm.get('backlog_pm'))} backlog PM items."
            )
        elif pm_warning:
            out["answer_seed"] += f" {pm_warning}"
        out["key_numbers"] = [
            f"Open MR: {_fmt(mr.get('open_count'))}",
            f"Carry-over Open MR: {_fmt(mr.get('carry_over_open_mr'))}",
            f"Top Functional Location: {_fmt((top_location.get('top_functional_location') or {}).get('name'))}",
            f"Data Quality Issues: {_fmt(mr.get('data_quality_issue_count'))}",
            f"Top Consumed Spare Part: {_fmt(spare_top.get('top_consumed_part'))}",
        ]
        if pm_bundle:
            out["key_numbers"].insert(2, f"PM Overdue: {_fmt(pm.get('overdue_pm'))}")
            out["key_numbers"].insert(3, f"PM Backlog: {_fmt(pm.get('backlog_pm'))}")
        out["insight"] = [
            f"Total active MR workload is {_fmt(mr.get('total_active_workload'))} once carry-over open MR are included.",
            f"The highest workload functional location is {_fmt((top_location.get('top_functional_location') or {}).get('name'))}.",
            f"Data quality issues remain at {_fmt(mr.get('data_quality_issue_count'))} flagged MR rows.",
        ]
        if pm_warning:
            out["insight"].append(pm_warning)
        out["follow_up"] = _follow_up(
            mr,
            pm if pm_bundle else {"overdue_pm": 0, "backlog_pm": 0},
            spare={"top_consumed_part": spare_top.get("top_consumed_part")},
            top_location=(top_location.get("top_functional_location") or {}).get("name"),
        )
        out["warnings"] = _downtime_warning_lines(mr) + ((pm_bundle or {}).get("data_quality", {}).get("warnings") or [])
        if pm_warning:
            out["warnings"].append(pm_warning)
        out["view_data_used"] = _view_data_used(intent, filters, out["warnings"], kpi_values_used=out["key_numbers"])

    elif intent == "risk_insight_query":
        from . import risk_service

        risk = risk_service.get_asset_risk_insights(filters)
        top = (risk.get("top_assets") or [None])[0]
        out["context"] = {"risk": risk}
        out["risk"] = risk
        out["answer_seed"] = (
            f"For {period}, {_fmt(risk.get('high_attention_count'))} assets are High Attention and "
            f"{_fmt(risk.get('medium_attention_count'))} are Medium Attention."
            + (f" The highest visible risk item is {top.get('asset_name')} (risk {top.get('risk_score')})." if top else "")
        )
        out["key_numbers"] = [
            f"High Attention Assets: {_fmt(risk.get('high_attention_count'))}",
            f"Medium Attention Assets: {_fmt(risk.get('medium_attention_count'))}",
            f"Assets Assessed: {_fmt(risk.get('assets_assessed'))}",
        ]
        for asset in risk.get("top_assets") or []:
            out["key_numbers"].append(
                f"{_fmt(asset.get('asset_name'))}: risk {_fmt(asset.get('risk_score'))} ({_fmt(asset.get('risk_level'))}, {_fmt(asset.get('mr_count'))} MR)"
            )
        out["insight"] = [risk.get("note") or "Risk is a follow-up signal, not a failure prediction."]
        out["follow_up"] = ["Prioritise the High Attention asset list with engineering review."]
        out["warnings"] = risk.get("data_notes", [])
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Downtime MR/WO rows", "PM schedule payload", "Spare parts payload"],
            kpi_values_used=out["key_numbers"],
        )

    elif intent in ("spare_parts_summary", "spare_parts_consumption_query"):
        spare = kpi.get_verified_spare_parts_metrics(filters)
        top = kpi.get_top_spare_parts_consumption(filters, limit=5)
        out["context"] = {"spare": spare, "consumption": top}
        out["answer_seed"] = (
            f"For {period}, current in-stock spare parts total {_fmt(spare.get('current_in_stock_items'))} items, "
            f"while drawn-from-store value is {_currency(spare.get('drawn_from_store_value'))}."
        )
        out["key_numbers"] = [
            f"Current In-Stock Spare Parts: {_fmt(spare.get('current_in_stock_items'))}",
            f"Current In-Stock Value: {_currency(spare.get('current_in_stock_value'))}",
            f"Drawn from Store Value: {_currency(spare.get('drawn_from_store_value'))}",
            f"Non-Stock Value: {_currency(spare.get('non_stock_value'))}",
            f"Services Value: {_currency(spare.get('services_value'))}",
            f"Top Consumed Spare Part: {_fmt(spare.get('top_consumed_part'))}",
            f"YoY Consumption: {_fmt(spare.get('yoy_consumption_pct'))}%",
        ]
        for index, item in enumerate(top.get("parts") or [], start=1):
            out["key_numbers"].append(f"Top Part {index}: {_fmt(item.get('part_name'))} ({_currency(item.get('value'))})")
        out["insight"] = [
            "Services include repair and cleaning.",
            f"The top consumed spare part is {_fmt(spare.get('top_consumed_part'))}.",
            f"YoY consumption change is {_fmt(spare.get('yoy_consumption_pct'))}% for {spare.get('yoy_label')}.",
        ]
        out["follow_up"] = ["Review high-consumption parts and service-related spend with the maintenance and stores teams."]
        out["warnings"] = spare.get("data_notes") or []
        out["view_data_used"] = _view_data_used(
            intent,
            filters,
            out["warnings"],
            source_tables=["Spare parts payload", "Project transactions history"],
            rows_loaded=[
                _row_metric("Inventory rows loaded", spare.get("inventory_rows_loaded")),
                _row_metric("Project transaction rows loaded", top.get("rows_loaded")),
            ],
            rows_after_filter=[_row_metric("Filtered project transaction rows", top.get("rows_after_filter"))],
            kpi_values_used=out["key_numbers"],
        )

    elif intent == "report_wording_query":
        verified = kpi.get_verified_downtime_metrics(filters)
        mr = verified.get("downtime_summary") or {}
        pm_bundle, pm_warning = _run_with_timeout(kpi.get_verified_pm_metrics, filters)
        pm = (pm_bundle or {}).get("metrics", {})
        out["context"] = {"downtime": mr, "pm": pm}
        out["answer_seed"] = (
            f"{period} maintenance performance shows {_fmt(mr.get('closed_work_orders'))} MR closed or confirmed out of "
            f"{_fmt(mr.get('total_work_orders'))} raised"
        )
        if pm_bundle:
            out["answer_seed"] += (
                f", while PM compliance stands at {_fmt(pm.get('pm_compliance_percent'))}% and "
                f"follow-up is still needed on {_fmt(pm.get('overdue_pm'))} overdue PM tasks."
            )
        else:
            out["answer_seed"] += ", while PM detail is still loading from the source schedule files."
        out["key_numbers"] = [
            f"MR Raised: {_fmt(mr.get('total_work_orders'))}",
            f"Closed / Confirmed MR: {_fmt(mr.get('closed_work_orders'))}",
            f"Closure Rate: {_fmt(mr.get('closure_rate_pct'))}%",
        ]
        if pm_bundle:
            out["key_numbers"].append(f"PM Compliance: {_fmt(pm.get('pm_compliance_percent'))}%")
        out["insight"] = ["This line is designed for slide or report use and stays grounded in verified KPI values."]
        out["warnings"] = _downtime_warning_lines(mr) + ((pm_bundle or {}).get("data_quality", {}).get("warnings") or [])
        if pm_warning:
            out["warnings"].append(pm_warning)
        out["view_data_used"] = _view_data_used(intent, filters, out["warnings"], kpi_values_used=out["key_numbers"])

    else:
        out["intent"] = "general_dashboard_help"
        out["answer_seed"] = (
            "I can summarise verified maintenance KPIs, downtime and MR trends, PM status, spare-parts consumption, "
            "top assets, functional locations, open MR, overdue PM, fault themes, and daily follow-up items."
        )
        out["insight"] = ["Examples: Summarise YTD maintenance performance, Which asset has the most MR, What are the main PM issues."]
        out["view_data_used"] = _view_data_used(intent, filters, out["warnings"])

    return out


def _rule_based_answer(intent: str, period: str, context_data: dict, key_numbers: list, theme: dict | None) -> str:
    if key_numbers:
        return f"For {period}: " + "; ".join(key_numbers[:5]) + "."
    if theme and theme.get("top_theme"):
        return (
            f"The most common issue theme in {period} is {theme['top_theme']}, based on "
            f"{theme['top_theme_count']} of {theme['rows_loaded']} classified MR descriptions."
        )
    return f"No verified data was available for {period}."


def _provider_mode_label(status: dict) -> str:
    status_text = str((status or {}).get("status") or "").strip().lower()
    if (status or {}).get("provider") == "ollama" or (status or {}).get("llm"):
        return "Ollama connected"
    if "not running" in status_text:
        return "LLM unavailable"
    return "Rule-based fallback"


def _read_only_response(question: str, base_filters: dict | None) -> dict:
    status = get_provider_status()
    filters = resolve_filters(question or "", base_filters)
    return {
        "ok": True,
        "intent": "read_only_guard",
        "period": ctx.month_label(filters),
        "period_used": f"Period used: {ctx.month_label(filters)}",
        "filters": filters,
        "answer": _READ_ONLY_RESPONSE,
        "key_numbers_used": [],
        "insight": ["I can explain verified maintenance dashboard data, but I cannot create, edit, close, or update source records."],
        "recommended_follow_up": ["Use the normal maintenance workflow to update MR, WO, PM, D365, or source files."],
        "view_data_used": _view_data_used("read_only_guard", filters, []),
        "provider": "rule_based",
        "provider_status": status["status"],
        "provider_mode_label": _provider_mode_label(status),
        "llm_active": False,
        "read_only": True,
    }


def _clean_selected_items(items) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _apply_kpi_analysis_context(result: dict, selected_kpis: list[str], selected_kpi_labels: list[str]) -> dict:
    if not selected_kpis and not selected_kpi_labels:
        return result

    selected_label_text = ", ".join(selected_kpi_labels or selected_kpis)
    result["mode"] = "kpi_analysis"
    result["selected_kpis"] = selected_kpis
    result["selected_kpi_labels"] = selected_kpi_labels
    result["intent"] = "kpi_analysis"

    insight = result.get("insight") or []
    result["insight"] = [f"Selected KPI focus areas: {selected_label_text}."] + insight

    view_data = result.get("view_data_used") or {}
    view_data["selected_kpis"] = selected_kpis
    if selected_kpi_labels:
        values_used = list(view_data.get("kpi_values_used") or [])
        values_used.extend(f"Selected KPI: {label}" for label in selected_kpi_labels)
        view_data["kpi_values_used"] = values_used
    result["view_data_used"] = view_data
    return result


def _answer_asset_report(question: str, base_filters: dict | None) -> dict:
    """Route an asset breakdown / repair-cost query to the deterministic report service."""
    from ...services import asset_report_service as ars

    params = ars.extract_asset_report_params(question or "", base_filters)
    if params is None:
        return None  # not an asset report query after all

    # Check for missing critical parameters and ask a follow-up
    follow_up_q = ars.get_missing_params_question(params)
    if follow_up_q:
        status = get_provider_status()
        return {
            "ok": True,
            "intent": "asset_report_clarification",
            "response_type": "asset_report_clarification",
            "answer": follow_up_q,
            "period_used": "Clarification needed",
            "insight": ["Please provide the missing details so I can calculate the exact breakdown."],
            "recommended_follow_up": [follow_up_q],
            "provider_mode_label": "Asset Report",
            "read_only": True,
            "mode": "chat",
            "provider_status": status.get("status"),
            "llm_active": False,
        }

    filters = ctx.normalize_filters(base_filters or {})
    try:
        report = ars.build_asset_report(params, filters)
        report["answer"] = ars.generate_asset_report_wording(report)
        report["ok"] = True
        report["intent"] = "asset_report_query"
        report["period_used"] = f"Period used: {report.get('period_label', params.get('period_text', ''))}"
        report["read_only"] = True
        report["provider_mode_label"] = "Asset Report (deterministic)"
        report["llm_active"] = False
        report["mode"] = "chat"
        report["provider_status"] = get_provider_status().get("status")
        return report
    except Exception as exc:
        status = get_provider_status()
        return {
            "ok": False,
            "intent": "asset_report_query",
            "response_type": "asset_report",
            "answer": f"Asset report generation encountered an error: {exc}. Please check that data has been imported.",
            "period_used": "Error",
            "insight": [],
            "recommended_follow_up": ["Ensure MR/WO data is imported and try again."],
            "provider_mode_label": "Asset Report (error)",
            "read_only": True,
            "mode": "chat",
            "provider_status": status.get("status"),
            "llm_active": False,
        }


def answer(
    question: str,
    base_filters: dict | None,
    *,
    mode: str | None = None,
    selected_kpis=None,
    selected_kpi_labels=None,
) -> dict:
    selected_kpis = _clean_selected_items(selected_kpis)
    selected_kpi_labels = _clean_selected_items(selected_kpi_labels)
    if mode == "kpi_analysis" or selected_kpis or selected_kpi_labels:
        return _answer_kpi_analysis(question or "", base_filters, selected_kpis, selected_kpi_labels)

    if _is_read_only_request(question):
        result = _read_only_response(question or "", base_filters)
        return _apply_kpi_analysis_context(result, selected_kpis, selected_kpi_labels)

    # ── Asset breakdown / repair-cost report query ───────────────────────────
    from ...services import asset_report_service as ars
    if ars.is_asset_report_query(question or ""):
        result = _answer_asset_report(question or "", base_filters)
        if result is not None:
            return result

    # ── Stage 1: Intent routing ──────────────────────────────────────────────
    # Try qwen2.5:7b first; fall back to keyword classifier.
    base_f = ctx.normalize_filters(base_filters)
    router_out = _route_intent_llm(question or "", base_f) or _route_intent_keyword(question or "")

    # ── Stage 2: Validation + confidence gate ────────────────────────────────
    is_valid, clarify_msg = _validate_router_output(router_out)
    if not is_valid:
        status = get_provider_status()
        return {
            "ok": True,
            "intent": "clarification_needed",
            "period": ctx.month_label(base_f),
            "period_used": f"Period used: {ctx.month_label(base_f)}",
            "filters": base_f,
            "answer": clarify_msg,
            "key_numbers_used": [],
            "insight": [],
            "recommended_follow_up": [],
            "view_data_used": None,
            "provider": "validation_gate",
            "provider_status": status["status"],
            "provider_mode_label": "Validation gate",
            "llm_active": False,
            "read_only": True,
            "confidence": {"band": "Low", "score": 0.0, "label": "Low — clarification needed"},
            "interpretation": {
                "text": router_out.get("interpretation_text", "Question not clearly understood."),
                "intent_label": router_out.get("intent", "unknown"),
                "confidence": float(router_out.get("interpretation_confidence") or 0.0),
            },
            "mode": "chat",
        }

    # ── Stage 3: Map to legacy intent + resolve filters ──────────────────────
    intent = _new_to_legacy_intent(router_out, question or "")
    # Merge any filter overrides the router extracted
    extracted_filters = router_out.get("filters") or {}
    if extracted_filters.get("stage"):
        base_filters = {**(base_filters or {}), "stage": extracted_filters["stage"]}
    filters = resolve_filters(question or "", base_filters)

    # ── Stage 4: Compute verified data ──────────────────────────────────────
    built = build_context(intent, filters, question or "")
    period = built["period"]

    rule_text = built.get("answer_seed") or _rule_based_answer(
        built["intent"],
        period,
        built["context"],
        built["key_numbers"],
        built["theme"],
    )

    # ── Stage 5: Rephrase with qwen2.5:7b (numbers locked in template) ──────
    provider = OllamaMiraProvider()
    used_llm = False
    answer_text = rule_text
    if config.LOCAL_LLM_ENABLED and config.PROVIDER_MODE in ("auto", "ollama") and provider.resolve_model():
        try:
            compact = {
                "question": question,
                "intent": built["intent"],
                "period_mode": filters.get("period_mode"),
                "period_label": period,
                "date_range": (built["view_data_used"] or {}).get("date_range"),
                "verified_key_numbers": built["key_numbers"],
                "insight": built.get("insight"),
                "recommended_follow_up": built["follow_up"],
                "data_warnings": built["warnings"],
            }
            if built["theme"] and built["theme"].get("top_theme"):
                theme = built["theme"]
                compact["fault_theme"] = {
                    "top_theme": theme["top_theme"],
                    "count": theme["top_theme_count"],
                    "total": theme["rows_loaded"],
                    "pct": theme["top_theme_pct"],
                    "top_asset": theme["top_theme_asset"],
                    "top_location": theme["top_theme_functional_location"],
                    "examples": theme["example_descriptions"],
                }
            style_instruction = (
                "Return one management-ready sentence only."
                if built["intent"] == "report_wording_query"
                else "Return only the short direct Answer in ≤2 sentences with ≤3 key numbers. No headings or bullets."
            )
            user_prompt = (
                f'Answer this question: "{question}"\n\n'
                "Use ONLY the verified figures in the JSON below; never introduce a number not present in it. "
                "If a value is unavailable, say so. For fault themes use cautious wording "
                "(suggests/indicates) and recommend engineering review. "
                f"{style_instruction}\n\n"
                f"VERIFIED_CONTEXT_JSON:\n{json.dumps(compact, default=str, ensure_ascii=False)}\n"
            )
            llm = generate_with_ollama(
                _CHAT_SYSTEM_PROMPT,
                user_prompt,
                model=provider.resolve_model(),
                timeout=15,
            ).strip()
            if llm:
                answer_text = llm
                used_llm = True
        except Exception:
            answer_text = rule_text

    # ── Stage 6: Compute answer confidence ──────────────────────────────────
    confidence = _compute_answer_confidence(built.get("view_data_used") or {}, router_out)

    # ── Stage 7: Build interpretation echo ──────────────────────────────────
    intent_label_map = {
        "recurring_issue_analysis": "Recurring issue analysis",
        "downtime_mr_wo_analysis": "Downtime / MR analysis",
        "pm_schedule": "PM schedule analysis",
        "spare_parts": "Spare parts analysis",
        "predictive_risk": "Risk / predictive analysis",
    }
    new_intent = router_out.get("intent", "downtime_mr_wo_analysis")
    interpretation = {
        "text": router_out.get("interpretation_text") or intent_label_map.get(new_intent, "Maintenance analysis"),
        "intent_label": intent_label_map.get(new_intent, new_intent),
        "resolved_as": (
            f"{intent_label_map.get(new_intent, new_intent)} · "
            f"Stage {filters.get('stage', 'all').replace('stage', '').strip().upper() if filters.get('stage') != 'all' else 'All'} · "
            f"{period}"
        ),
        "confidence": float(router_out.get("interpretation_confidence") or 0.5),
        "router_source": "ollama" if used_llm else "keyword",
    }

    status = get_provider_status()
    result = {
        "ok": True,
        "intent": built["intent"],
        "period": period,
        "period_used": f"Period used: {period}",
        "filters": filters,
        "answer": answer_text,
        "key_numbers_used": built["key_numbers"],
        "insight": built.get("insight") or [],
        "recommended_follow_up": built["follow_up"],
        "view_data_used": built["view_data_used"],
        "provider": "ollama" if used_llm else "rule_based",
        "provider_status": status["status"],
        "provider_mode_label": "Ollama connected" if used_llm else "Rule-based fallback",
        "llm_active": used_llm,
        "read_only": True,
        "confidence": confidence,
        "interpretation": interpretation,
    }
    if built["theme"]:
        result["theme_analysis"] = built["theme"]
    if built.get("risk"):
        result["risk_insights"] = built["risk"]
    if mode == "kpi_analysis" or selected_kpis or selected_kpi_labels:
        result = _apply_kpi_analysis_context(result, selected_kpis, selected_kpi_labels)
    else:
        result["mode"] = "chat"
    return result
