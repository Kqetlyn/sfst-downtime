"""
MIRA Asset Report Service

Calculates structured asset breakdown + repair-cost data deterministically.
Ollama/Qwen only generates the final wording from the calculated JSON.
Never sends raw Excel rows or full data tables to the LLM.

Flow:
  user question
  -> extract_asset_report_params()     (detect machine, stage, period, flags)
  -> build_asset_report()              (deterministic calculation)
  -> generate_asset_report_wording()   (LLM wording only, using compact JSON)
  -> structured response to frontend
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta

from ..core import context as ctx
from . import kpi_query_service as kpi

# ── Machine family definitions ─────────────────────────────────────────────────
# Each family has:
#   display     - canonical display name
#   aliases     - substrings that identify the family in any text field
#   unit_aliases - canonical unit name -> list of matching substrings

_MACHINE_FAMILIES: dict[str, dict] = {
    "combi_oven": {
        "display": "Combi Oven",
        "aliases": [
            "combi oven", "combi", "rational", "scc", "icombi",
        ],
        "unit_aliases": {
            "Combi Oven No.1": [
                "combi oven no.1", "combi oven no1", "combi oven 1",
                "combi oven1", "combi1", "combi 1", "oven no.1",
                "oven no1", "oven 1", "oven1", "rational 1", "rational no.1",
                "rational no1", "scc 1", "scc1", "scc no.1", "icombi 1",
            ],
            "Combi Oven No.2": [
                "combi oven no.2", "combi oven no2", "combi oven 2",
                "combi oven2", "combi2", "combi 2", "oven no.2",
                "oven no2", "oven 2", "oven2", "rational 2", "rational no.2",
                "rational no2", "scc 2", "scc2", "scc no.2", "icombi 2",
            ],
            "Combi Oven No.3": [
                "combi oven no.3", "combi oven no3", "combi oven 3",
                "combi oven3", "combi3", "combi 3", "oven no.3",
                "oven no3", "oven 3", "oven3", "rational 3", "rational no.3",
                "rational no3", "scc 3", "scc3", "scc no.3", "icombi 3",
            ],
            "Combi Oven No.4": [
                "combi oven no.4", "combi oven no4", "combi oven 4",
                "combi oven4", "combi4", "combi 4", "oven no.4",
                "oven no4", "oven 4", "oven4", "rational 4", "rational no.4",
                "rational no4", "scc 4", "scc4", "scc no.4", "icombi 4",
            ],
        },
    },
}

# ── Exclusion classification ───────────────────────────────────────────────────
# Ordered: first match wins. Row is "counted" if no rule matches.

_EXCLUSION_RULES: list[tuple[str, list[str]]] = [
    ("Preventive Maintenance", [
        r"\bpm\b", r"preventive\s*maintenance", r"scheduled\s*maintenance",
        r"\binspection\b", r"routine\s*check", r"\bservicing\b",
        r"periodic\s*check", r"routine\s*maintenance",
    ]),
    ("Hood / Exhaust / Support Work", [
        r"\bhood\b", r"\bexhaust\b", r"\bduct\b", r"\bcanopy\b",
        r"filter\s*hood", r"exhaust\s*fan",
    ]),
    ("Trolley / Cart / Accessory Issue", [
        r"\btrolley\b", r"\bcart\b", r"\brack\b", r"\bbasket\b",
        r"\baccessor", r"\battachment\b", r"\btray\b",
    ]),
    ("Floor / Area / Facility Issue", [
        r"\bfloor\b", r"\bwall\b", r"\bceiling\b", r"\bbuilding\b",
        r"\bfacility\b", r"\barea\s+drain\b", r"\bdrain.*area\b",
    ]),
    ("Drain / Lighting / Building", [
        r"\bdrain\b", r"\bsewer\b", r"\blighting\b", r"\bbulb\b",
        r"electric\s*supply\s*only", r"\bpipe\s*leak\b",
    ]),
    ("Shared / Support Service", [
        r"\bshared\b", r"support\s*service", r"general\s*service",
        r"cleaning\s*service", r"\bgeneral\s*clean\b",
    ]),
]

_EXCLUSION_COMPILED = [
    (reason, [re.compile(p, re.IGNORECASE) for p in patterns])
    for reason, patterns in _EXCLUSION_RULES
]

# ── Issue clustering ───────────────────────────────────────────────────────────

_ISSUE_CLUSTERS: list[tuple[str, list[str]]] = [
    ("Door / Glass / Seal / Sensor", [
        r"\bdoor\b", r"\bglass\b", r"\bseal\b", r"\bgasket\b",
        r"\bsensor\b", r"\bprobe\b", r"\bhinges?\b", r"\blatch\b",
        r"door.*seal", r"glass.*door",
    ]),
    ("Washing / CIP / Cleaning System", [
        r"\bcip\b", r"\bwash(ing)?\s+system\b", r"\bdetergent\b",
        r"\bdosing\b", r"solenoid.*wash", r"wash.*program",
        r"\bcleaning\s+cycle\b",
    ]),
    ("Heating / Temperature / Control", [
        r"\bheat(ing)?\b", r"\btemperature\b", r"\bcontrol(ler)?\b",
        r"\bthermostat\b", r"not\s+heat", r"no\s+heat",
        r"temperature.*error", r"\bPID\b",
    ]),
    ("Alarm / Error / Not Working", [
        r"\balarm\b", r"\berror\b", r"fault\s+code", r"not\s+working",
        r"\bmalfunction\b", r"\bbreakdown\b", r"stop\s+working",
        r"does\s+not\s+work", r"failed\s+to\s+start",
    ]),
    ("Steam / Water Leakage", [
        r"\bsteam\b", r"\bleak(ing|age)?\b", r"water.*leak",
        r"\bspray\b", r"water\s+drip",
    ]),
    ("Cabinet / Button / Cover", [
        r"\bcabinet\b", r"\bbutton\b", r"\bcover\b", r"\bpanel\b",
        r"\bknob\b", r"\bdisplay\b", r"\bscreen\b", r"\bLCD\b",
    ]),
    ("Electrical / Lighting", [
        r"\belectr(ic|ical)\b", r"\bwiring\b", r"\bpower\b",
        r"\bfuse\b", r"\bPCB\b", r"control\s+board",
        r"\bvoltage\b", r"\brelay\b",
    ]),
    ("Motor / Fan / Pump", [
        r"\bmotor\b", r"\bfan\b", r"\bpump\b", r"\bblower\b",
        r"\bimpeller\b", r"\bshaft\b", r"\bbearing\b",
    ]),
]

_ISSUE_CLUSTER_COMPILED = [
    (name, [re.compile(p, re.IGNORECASE) for p in patterns])
    for name, patterns in _ISSUE_CLUSTERS
]

# ── Cost exclusion patterns ────────────────────────────────────────────────────

_COST_EXCLUSION_RULES: list[tuple[str, str]] = [
    (r"\bhood\b|\bexhaust\b|\bduct\b", "Hood/exhaust work"),
    (r"general\s*clean|cleaning\s*service", "General cleaning"),
    (r"\bfacility\b|\bbuilding\b|\bfloor\b|\bwall\b", "Facility work"),
    (r"calibrat|inspection\s*only|survey\s*only", "Calibration/inspection only"),
    (r"\bshared\b|support\s*service", "Shared/support work"),
]

_COST_EXCLUSION_COMPILED = [
    (re.compile(p, re.IGNORECASE), reason)
    for p, reason in _COST_EXCLUSION_RULES
]

# ── Intent keywords for asset report detection ─────────────────────────────────

_ASSET_REPORT_KEYWORDS = (
    "breakdown", "broke down", "breakdowns", "repair cost",
    "issue by unit", "which unit had", "most issues",
    "common breakdown", "common fault for", "common issue for",
    "estimate repair", "estimated cost", "cost by unit",
    "purchase cost", "rows to exclude", "exclude before",
    "email-ready summary", "email ready summary",
    "asset breakdown", "summary for this asset",
    "by unit", "each unit", "per unit", "which unit",
    "breakdowns occurred", "occurred over", "how many breakdown",
    "what breakdown", "what issues", "issues with",
)


def is_asset_report_query(question: str) -> bool:
    """Return True if the question looks like an asset breakdown/cost report request."""
    q = (question or "").lower()

    has_machine = any(
        alias in q
        for fam in _MACHINE_FAMILIES.values()
        for alias in fam["aliases"]
    )
    if not has_machine:
        return False

    return any(kw in q for kw in _ASSET_REPORT_KEYWORDS)


# ── Parameter extraction ───────────────────────────────────────────────────────

def _detect_stage(question: str) -> str | None:
    t = question.lower()
    if re.search(r"stage\s*1\b", t):
        return "stage1"
    if re.search(r"stage\s*2\b", t):
        return "stage2"
    return None


def _detect_machine_family(question: str) -> tuple[str | None, dict | None]:
    t = question.lower()
    for key, fam in _MACHINE_FAMILIES.items():
        if any(alias in t for alias in fam["aliases"]):
            return key, fam
    return None, None


def _detect_period_text(question: str) -> str:
    t = question.lower()
    m = re.search(r"past\s+\d+\s+year", t)
    if m:
        return m.group(0)
    m = re.search(r"past\s+\d+\s+month", t)
    if m:
        return m.group(0)
    if "ytd" in t or "year to date" in t:
        return "ytd"
    m = re.search(r"fy\s*-?\s*20\d{2}", t)
    if m:
        return m.group(0)
    m = re.search(r"\b(20\d{2})\b", t)
    if m:
        return m.group(0)
    return "ytd"


def extract_asset_report_params(question: str, base_filters: dict | None = None) -> dict | None:
    """
    Extract parameters for an asset breakdown report from a natural-language question.
    Returns None if this doesn't match any known machine family.
    """
    fam_key, fam_def = _detect_machine_family(question)
    if fam_key is None:
        return None

    t = question.lower()
    stage = _detect_stage(question)

    if stage is None and base_filters:
        raw_stage = str(base_filters.get("stage") or "").lower()
        if raw_stage not in ("", "all"):
            stage = raw_stage

    period_text = _detect_period_text(question)
    include_cost = any(kw in t for kw in (
        "cost", "spend", "po ", "purchase", "repair cost", "estimated cost",
        "estimate cost", "how much",
    ))
    include_excluded = any(kw in t for kw in (
        "exclude", "excluded rows", "skip rows", "leave out", "remove rows",
        "which rows", "rows to exclude",
    ))
    is_email = any(kw in t for kw in (
        "email", "email-ready", "management summary", "summary for management",
        "share with management",
    ))

    return {
        "machine": fam_def["display"],
        "machine_family_key": fam_key,
        "stage": stage,
        "period_text": period_text,
        "include_cost": include_cost,
        "include_excluded_rows": include_excluded,
        "format": "management_summary" if is_email else "standard",
        "group_by": "unit",
    }


# ── Missing parameters check ───────────────────────────────────────────────────

def get_missing_params_question(params: dict) -> str | None:
    """
    Return a follow-up question if critical parameters are missing.
    Returns None if all required params are present.
    """
    missing = []
    if not params.get("stage"):
        missing.append("stage (Stage 1 or Stage 2)")
    if not params.get("period_text") or params["period_text"] == "ytd":
        pass  # YTD is an acceptable default
    if missing:
        return f"Which {' and '.join(missing)} should I use for this report?"
    return None


# ── Period date parsing ────────────────────────────────────────────────────────

def _parse_period_text(period_text: str) -> dict:
    t = (period_text or "").lower().strip()
    now = datetime.now()

    m = re.search(r"past\s+(\d+)\s+year", t)
    if m:
        years = int(m.group(1))
        return {
            "start": (now - timedelta(days=365 * years)).strftime("%Y-%m-%d"),
            "end": now.strftime("%Y-%m-%d"),
            "period_mode": "custom",
            "_label": f"Past {years} Year{'s' if years > 1 else ''}",
        }

    m = re.search(r"past\s+(\d+)\s+month", t)
    if m:
        months = int(m.group(1))
        return {
            "start": (now - timedelta(days=30 * months)).strftime("%Y-%m-%d"),
            "end": now.strftime("%Y-%m-%d"),
            "period_mode": "custom",
            "_label": f"Past {months} Month{'s' if months > 1 else ''}",
        }

    if "ytd" in t or "year to date" in t or "this year" in t:
        return {"year": now.year, "month": None, "period_mode": "ytd", "_label": f"YTD {now.year}"}

    m = re.search(r"fy\s*-?\s*(20\d{2})", t)
    if m:
        yr = m.group(1)
        return {"year": int(yr), "period_mode": "financial_year", "_label": f"FY {yr}"}

    m = re.search(r"\b(20\d{2})\b", t)
    if m:
        yr = m.group(1)
        return {"year": int(yr), "period_mode": "full_year", "_label": yr}

    return {"year": now.year, "period_mode": "ytd", "_label": f"YTD {now.year}"}


def _period_date_range(period_filters: dict) -> tuple[datetime | None, datetime | None]:
    now = datetime.now()
    start_dt = None
    end_dt = None

    if period_filters.get("start"):
        try:
            start_dt = datetime.fromisoformat(str(period_filters["start"]))
        except ValueError:
            pass
    if period_filters.get("end"):
        try:
            end_dt = datetime.fromisoformat(str(period_filters["end"]))
        except ValueError:
            pass

    mode = period_filters.get("period_mode", "")
    year = int(period_filters.get("year") or now.year)

    if mode == "ytd":
        start_dt = datetime(year, 1, 1)
        end_dt = now
    elif mode == "full_year":
        start_dt = datetime(year, 1, 1)
        end_dt = datetime(year, 12, 31, 23, 59, 59)
    elif mode == "financial_year":
        start_dt = datetime(year, 4, 1)
        end_dt = datetime(year + 1, 3, 31, 23, 59, 59)

    return start_dt, end_dt


# ── Row helpers ────────────────────────────────────────────────────────────────

def _row_text(row: dict) -> str:
    return " ".join([
        str(row.get("translated_description") or ""),
        str(row.get("description") or ""),
        str(row.get("job_type") or ""),
        str(row.get("trade") or ""),
        str(row.get("asset_name") or ""),
        str(row.get("functional_location") or ""),
        str(row.get("asset_id") or ""),
        str(row.get("machine_group") or ""),
    ])


def _row_date(row: dict) -> datetime | None:
    for key in ("actual_start", "created_date", "actual_start_time", "request_created_time"):
        val = row.get(key)
        if not val:
            continue
        if isinstance(val, datetime):
            return val.replace(tzinfo=None) if val.tzinfo else val
        text = str(val).strip().split(".")[0].replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def _row_in_period(row: dict, start_dt: datetime | None, end_dt: datetime | None) -> bool:
    if start_dt is None and end_dt is None:
        return True
    dt = _row_date(row)
    if dt is None:
        return True  # undated rows: include rather than silently drop
    if start_dt and dt < start_dt:
        return False
    if end_dt and dt > end_dt:
        return False
    return True


# ── Machine / unit matching ────────────────────────────────────────────────────

def _row_matches_family(row: dict, family: dict) -> bool:
    text = _row_text(row).lower()
    if any(alias in text for alias in family.get("aliases", [])):
        return True
    for aliases in family.get("unit_aliases", {}).values():
        if any(alias in text for alias in aliases):
            return True
    return False


def _resolve_unit(row: dict, family: dict) -> str:
    text = _row_text(row).lower()
    for unit_name, aliases in family.get("unit_aliases", {}).items():
        if any(alias in text for alias in aliases):
            return unit_name
    return "Unknown Unit"


# ── Classification ─────────────────────────────────────────────────────────────

def _classify_exclusion(row: dict) -> str | None:
    text = _row_text(row)
    for reason, patterns in _EXCLUSION_COMPILED:
        if any(p.search(text) for p in patterns):
            return reason
    return None


def _classify_issue_cluster(row: dict) -> str:
    text = " ".join([
        str(row.get("translated_description") or ""),
        str(row.get("description") or ""),
    ])
    for name, patterns in _ISSUE_CLUSTER_COMPILED:
        if any(p.search(text) for p in patterns):
            return name
    return "Other / Unclear"


# ── Cost matching ──────────────────────────────────────────────────────────────

def _cost_row_text(row: dict) -> str:
    return " ".join([
        str(row.get("item_name") or ""),
        str(row.get("description") or ""),
        str(row.get("asset_name") or ""),
        str(row.get("asset_id") or ""),
        str(row.get("po_number") or ""),
    ]).lower()


def _is_excluded_cost(row: dict) -> tuple[bool, str]:
    text = _cost_row_text(row)
    for pattern, reason in _COST_EXCLUSION_COMPILED:
        if pattern.search(text):
            return True, reason
    return False, ""


def _cost_row_matches_unit(row: dict, unit_name: str, family: dict) -> bool:
    text = _cost_row_text(row)
    aliases = family.get("unit_aliases", {}).get(unit_name, [])
    return any(alias in text for alias in aliases)


def _cost_row_matches_family(row: dict, family: dict) -> bool:
    text = _cost_row_text(row)
    if any(alias in text for alias in family.get("aliases", [])):
        return True
    for aliases in family.get("unit_aliases", {}).values():
        if any(alias in text for alias in aliases):
            return True
    return False


# ── Main report builder ────────────────────────────────────────────────────────

def build_asset_report(params: dict, base_filters: dict | None = None) -> dict:
    """
    Deterministic calculation engine.
    Returns structured JSON; the LLM only receives a compact summary of this.
    """
    machine = params.get("machine", "")
    fam_key = params.get("machine_family_key", "")
    family = _MACHINE_FAMILIES.get(fam_key, {})
    stage = params.get("stage")
    period_text = params.get("period_text", "ytd")
    include_cost = bool(params.get("include_cost"))

    filters = dict(base_filters or {})
    if stage:
        filters["stage"] = stage

    period_filters = _parse_period_text(period_text)
    period_label = period_filters.pop("_label", period_text)
    filters.update(period_filters)
    filters = ctx.normalize_filters(filters)

    start_dt, end_dt = _period_date_range(period_filters)

    # ── Load MR/WO rows ──────────────────────────────────────────────────────
    try:
        all_rows = kpi._downtime_all_year_work_orders(filters)
    except Exception:
        all_rows = []

    # ── Filter to machine family + period ─────────────────────────────────────
    machine_rows = [
        r for r in all_rows
        if _row_matches_family(r, family) and _row_in_period(r, start_dt, end_dt)
    ]
    total_machine_rows = len(machine_rows)

    # ── Classify counted vs excluded ─────────────────────────────────────────
    counted_rows: list[dict] = []
    excluded_rows: list[dict] = []

    for row in machine_rows:
        reason = _classify_exclusion(row)
        if reason:
            excluded_rows.append({**row, "_exclusion_reason": reason})
        else:
            counted_rows.append(row)

    total_counted = len(counted_rows)
    total_excluded = len(excluded_rows)

    # ── Unit breakdown ─────────────────────────────────────────────────────
    canonical_units = list(family.get("unit_aliases", {}).keys())
    unit_data: dict[str, dict] = {
        u: {"unit": u, "mr_count": 0, "issue_clusters": Counter(),
            "latest_mr_date": None, "rows": []}
        for u in canonical_units
    }
    unit_data["Unknown Unit"] = {
        "unit": "Unknown Unit", "mr_count": 0,
        "issue_clusters": Counter(), "latest_mr_date": None, "rows": [],
    }

    for row in counted_rows:
        unit = _resolve_unit(row, family)
        if unit not in unit_data:
            unit_data[unit] = {
                "unit": unit, "mr_count": 0,
                "issue_clusters": Counter(), "latest_mr_date": None, "rows": [],
            }
        bucket = unit_data[unit]
        bucket["mr_count"] += 1
        bucket["rows"].append(row)
        bucket["issue_clusters"][_classify_issue_cluster(row)] += 1

        dt = _row_date(row)
        if dt and (not bucket["latest_mr_date"] or dt > bucket["latest_mr_date"]):
            bucket["latest_mr_date"] = dt

    if unit_data.get("Unknown Unit", {}).get("mr_count", 0) == 0:
        unit_data.pop("Unknown Unit", None)

    # ── Cost estimation ─────────────────────────────────────────────────────
    cost_by_unit: dict[str, float] = {u: 0.0 for u in unit_data}
    excluded_cost_total = 0.0
    po_evidence_included: list[dict] = []
    po_evidence_excluded: list[dict] = []

    if include_cost:
        try:
            spare_rows = kpi._sql_spare_rows(filters, "gen_po", "stage_po")
        except Exception:
            spare_rows = []

        for cost_row in spare_rows:
            if not _cost_row_matches_family(cost_row, family):
                continue

            excl, excl_reason = _is_excluded_cost(cost_row)
            total_val = float(cost_row.get("total_value") or 0)

            if excl:
                excluded_cost_total += total_val
                po_evidence_excluded.append({
                    "po_number": cost_row.get("po_number"),
                    "item_name": cost_row.get("item_name"),
                    "total_value": total_val,
                    "reason": excl_reason,
                })
                continue

            matched_unit = next(
                (u for u in unit_data if _cost_row_matches_unit(cost_row, u, family)),
                None,
            )
            if matched_unit:
                cost_by_unit[matched_unit] = cost_by_unit.get(matched_unit, 0.0) + total_val
                po_evidence_included.append({
                    "po_number": cost_row.get("po_number"),
                    "item_name": cost_row.get("item_name"),
                    "total_value": total_val,
                    "supplier": cost_row.get("supplier"),
                    "unit": matched_unit,
                })

    # ── Common issue patterns (across all units) ──────────────────────────
    all_clusters: Counter = Counter()
    for bucket in unit_data.values():
        all_clusters.update(bucket["issue_clusters"])

    # ── Highest MR unit ───────────────────────────────────────────────────
    highest_unit = max(unit_data, key=lambda u: unit_data[u]["mr_count"], default=None)
    highest_count = unit_data[highest_unit]["mr_count"] if highest_unit else 0

    # ── Build units table ─────────────────────────────────────────────────
    units_table = []
    for unit in canonical_units + (["Unknown Unit"] if "Unknown Unit" in unit_data else []):
        if unit not in unit_data:
            continue
        bucket = unit_data[unit]
        top_issues = [iss for iss, _ in bucket["issue_clusters"].most_common(3)]
        entry: dict = {
            "unit": unit,
            "mr_count": bucket["mr_count"],
            "main_issues": top_issues,
            "latest_mr": (
                bucket["latest_mr_date"].strftime("%d %b %Y")
                if bucket["latest_mr_date"] else None
            ),
        }
        if include_cost:
            cost = cost_by_unit.get(unit, 0.0)
            entry["estimated_cost"] = cost
            entry["estimated_cost_formatted"] = (
                f"THB {cost:,.0f}" if cost > 0 else "No PO match found"
            )
        units_table.append(entry)

    # ── Exclusion summary ─────────────────────────────────────────────────
    excl_counter = Counter(r["_exclusion_reason"] for r in excluded_rows)

    # ── Evidence rows (capped for response size) ──────────────────────────
    evidence_counted = [
        {
            "mr_number": r.get("mr_number") or r.get("request_id"),
            "asset_name": r.get("asset_name"),
            "description": str(
                r.get("translated_description") or r.get("description") or ""
            )[:120],
            "status": r.get("status") or r.get("request_state"),
            "date": str(r.get("actual_start") or r.get("created_date") or "")[:10],
            "unit": _resolve_unit(r, family),
            "issue_cluster": _classify_issue_cluster(r),
        }
        for r in counted_rows[:50]
    ]

    evidence_excluded = [
        {
            "mr_number": r.get("mr_number") or r.get("request_id"),
            "asset_name": r.get("asset_name"),
            "description": str(
                r.get("translated_description") or r.get("description") or ""
            )[:120],
            "exclusion_reason": r["_exclusion_reason"],
            "date": str(r.get("actual_start") or r.get("created_date") or "")[:10],
        }
        for r in excluded_rows[:30]
    ]

    # ── Data warnings ─────────────────────────────────────────────────────
    warnings: list[str] = []
    if not all_rows:
        warnings.append(
            "No MR/WO data was found in the database. Ensure data has been imported."
        )
    elif not machine_rows:
        warnings.append(
            f"No MR/WO records for {machine} were found in the selected period/stage."
        )
    if total_counted == 0 and total_machine_rows > 0:
        warnings.append(
            "All matched records were classified as excluded (PM, facility, etc.). "
            "No actual breakdown MR were counted."
        )
    if include_cost and not po_evidence_included and not po_evidence_excluded:
        warnings.append(
            "No Gen PO records matched this machine family. "
            "Cost estimate is not available for this selection."
        )

    stage_label = (
        "Stage 1" if stage == "stage1"
        else "Stage 2" if stage == "stage2"
        else "All Stages"
    )

    return {
        "response_type": "asset_report",
        "machine": machine,
        "stage": stage,
        "stage_label": stage_label,
        "period_label": period_label,
        "title": f"{stage_label} {machine} — {period_label} Breakdown Summary",
        "total_machine_rows": total_machine_rows,
        "total_counted": total_counted,
        "total_excluded": total_excluded,
        "highest_mr_unit": highest_unit,
        "highest_mr_count": highest_count,
        "top_issue_patterns": [
            {"issue": iss, "count": cnt}
            for iss, cnt in all_clusters.most_common(5)
        ],
        "units_table": units_table,
        "exclusion_summary": [
            {"reason": r, "count": c} for r, c in excl_counter.most_common()
        ],
        "include_cost": include_cost,
        "total_estimated_cost": sum(cost_by_unit.values()) if include_cost else None,
        "excluded_cost_total": excluded_cost_total if include_cost else None,
        "cost_basis_note": (
            "Estimated PO-based repair / purchase cost — based on available Gen PO records. "
            "Requires validation. Shared/support works excluded unless separately allocated."
        ),
        "evidence": {
            "counted_rows": evidence_counted,
            "excluded_rows": evidence_excluded,
            "po_rows_included": po_evidence_included[:20],
            "po_rows_excluded": po_evidence_excluded[:10],
        },
        "data_warnings": warnings,
        "data_notes": [
            "Based on available MR/PO records — requires validation",
            "PM, facility, trolley/cart, hood/exhaust, drain/lighting rows are excluded from breakdown count",
            "Cost is PO-based estimate only; shared/support works excluded unless separately allocated",
        ],
        "_params": {k: v for k, v in params.items() if not k.startswith("_")},
        "_generated_at": datetime.now().isoformat(timespec="seconds"),
    }


# ── LLM wording ────────────────────────────────────────────────────────────────

_ASSET_REPORT_SYSTEM_PROMPT = (
    "You are MIRA, a maintenance dashboard assistant. "
    "Use only the provided calculated JSON. Do not invent numbers. Do not recalculate totals. "
    "If data is incomplete, say it is an estimate or requires validation. "
    "Keep the answer concise, professional, and suitable for management (3-6 sentences max). "
    "Never say 'confirmed exact repair cost', 'AI confirmed', or 'definite root cause'. "
    "Always use wording like 'estimated', 'based on available MR/PO records', "
    "'PO-based repair / purchase cost', 'requires validation'."
)


def generate_asset_report_wording(report: dict) -> str:
    """Generate LLM wording from calculated JSON. Falls back to rule-based."""
    try:
        from ..providers import generate_with_ollama, OllamaMiraProvider
        from .. import config
        provider = OllamaMiraProvider()
        if config.LOCAL_LLM_ENABLED and provider.resolve_model():
            compact = {
                "machine": report.get("machine"),
                "period": report.get("period_label"),
                "stage": report.get("stage_label"),
                "total_actual_breakdown_mr": report.get("total_counted"),
                "total_excluded_mr": report.get("total_excluded"),
                "highest_mr_unit": report.get("highest_mr_unit"),
                "highest_mr_count": report.get("highest_mr_count"),
                "top_issue_patterns": report.get("top_issue_patterns", [])[:3],
                "units_table": [
                    {k: v for k, v in u.items() if k not in ("estimated_cost",)}
                    for u in (report.get("units_table") or [])
                ],
                "include_cost": report.get("include_cost"),
                "total_estimated_cost_thb": (
                    f"THB {report['total_estimated_cost']:,.0f}"
                    if report.get("total_estimated_cost") else None
                ),
                "data_warnings": report.get("data_warnings", []),
            }
            user_prompt = (
                f"Write a concise management summary for this asset breakdown report:\n"
                f"{json.dumps(compact, indent=2)}\n\n"
                "Cover: total actual breakdown MR, highest MR unit, "
                "top 2-3 issue patterns, estimated cost if available. "
                "3-5 sentences. Use cautious wording throughout."
            )
            raw = generate_with_ollama(
                _ASSET_REPORT_SYSTEM_PROMPT,
                user_prompt,
                model=provider.resolve_model(),
                timeout=20,
            ).strip()
            if raw and len(raw) > 30:
                return raw
    except Exception:
        pass

    return _rule_based_wording(report)


def _rule_based_wording(report: dict) -> str:
    machine = report.get("machine", "equipment")
    period = report.get("period_label", "selected period")
    stage_label = report.get("stage_label", "")
    prefix = f"{stage_label} {machine}".strip()
    counted = report.get("total_counted") or 0
    excluded = report.get("total_excluded") or 0
    highest = report.get("highest_mr_unit")
    highest_count = report.get("highest_mr_count") or 0
    patterns = report.get("top_issue_patterns") or []
    top_issues_text = (
        ", ".join(p["issue"] for p in patterns[:3]) if patterns else "various issues"
    )

    parts = [
        f"Based on available MR records, {prefix} recorded {counted} actual breakdown MR "
        f"in the {period} (estimated — {excluded} row(s) separately excluded as PM, "
        f"facility, or support work, not counted above)."
    ]

    if highest and highest_count > 0:
        parts.append(
            f"{highest} had the highest MR count at {highest_count} breakdown(s)."
        )

    if patterns:
        parts.append(f"Main issue patterns: {top_issues_text}.")

    if report.get("include_cost"):
        total = report.get("total_estimated_cost") or 0
        if total > 0:
            parts.append(
                f"Estimated PO-based repair / purchase cost (based on available Gen PO records, "
                f"requires validation): THB {total:,.0f}. "
                f"Shared/support works are excluded unless separately allocated."
            )
        else:
            parts.append(
                "No Gen PO records were matched for cost estimation. "
                "Cost data requires manual review."
            )

    if report.get("data_warnings"):
        parts.append(f"Note: {report['data_warnings'][0]}")

    return " ".join(parts)
