"""
MIRA Predictive Maintenance Insights — category → machine-group hierarchy.

Governing rule: CONSUMER of already-memoised dashboard builders.
Never reads source files directly. Never recomputes MTTR / MTBF / backlog.
All row data flows through kpi_query_service which memoises the heavy builders.

New structure (replaces flat risk_groups):
  categories: [{ name, total_mrs, top_machines: [top-5 per category] }]
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import time

from ..core import context as ctx
from . import kpi_query_service as kpi

# ── Per-process memo (same pattern as kpi_query_service) ────────────────────────
_MEMO: dict[tuple, tuple[float, object]] = {}
_MEMO_TTL = 900  # 15 min


def _memoized(key, producer):
    now = time.time()
    hit = _MEMO.get(key)
    if hit and (now - hit[0]) < _MEMO_TTL:
        return hit[1]
    value = producer()
    _MEMO[key] = (now, value)
    return value


# ── Fault-family classification (broad, for Data Confidence + fault_pattern) ────
_FAULT_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "mira_fault_classifications.json"
)
_fault_cache: dict[str, str] = {}
_fault_cache_loaded = False

# ── Machine-group manual override store (confirmed by operators) ─────────────────
_MG_OVERRIDE_PATH = Path(__file__).resolve().parents[3] / "data" / "machine_group_overrides.json"
_mg_overrides: dict[str, dict] = {}
_mg_overrides_loaded = False
_mg_inference_cache: dict[str, dict] = {}  # desc_hash → ollama result, process-lifetime


def _load_mg_overrides() -> dict[str, dict]:
    global _mg_overrides, _mg_overrides_loaded
    if _mg_overrides_loaded:
        return _mg_overrides
    try:
        if _MG_OVERRIDE_PATH.exists():
            _mg_overrides = json.loads(_MG_OVERRIDE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _mg_overrides = {}
    _mg_overrides_loaded = True
    return _mg_overrides


def save_mg_override(key: str, machine_group: str, original_asset_id: str, confirmed_by: str = "operator") -> None:
    """Persist a manual machine-group confirmation. Called from API endpoint."""
    global _mg_overrides, _mg_overrides_loaded
    overrides = _load_mg_overrides()
    overrides[key] = {
        "machine_group": machine_group,
        "original_asset_id": original_asset_id,
        "confirmed_by": confirmed_by,
        "confirmed_at": datetime.utcnow().isoformat(),
    }
    try:
        _MG_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MG_OVERRIDE_PATH.write_text(json.dumps(overrides, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _mg_override_key(asset_id: str, desc_blob: str) -> str:
    raw = f"{asset_id.upper().strip()}|{desc_blob[:400]}"
    return hashlib.md5(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


FAULT_FAMILIES = [
    "Steam/Leakage",
    "Valve/Solenoid/Actuator",
    "Abnormal Noise/Vibration",
    "Heating/Temperature",
    "Display/Control Panel",
    "Water Filling/Drainage",
    "Electrical/Sensor",
    "Mechanical Wear",
    "Unclassified",
]

_FAULT_KEYWORDS: dict[str, list[str]] = {
    "Steam/Leakage":            ["steam", "leak", "น้ำรั่ว", "ไอน้ำ", "รั่ว", "seep"],
    "Valve/Solenoid/Actuator":  ["valve", "solenoid", "actuator", "วาล์ว", "โซลีนอยด์"],
    "Abnormal Noise/Vibration": ["noise", "vibrat", "เสียง", "สั่น", "ดัง", "rattle", "hum"],
    "Heating/Temperature":      ["heat", "temperature", "temp", "overheat", "ร้อน", "อุณหภูมิ", "thermal"],
    "Display/Control Panel":    ["display", "panel", "screen", "hmi", "จอ", "แสดง", "monitor"],
    "Water Filling/Drainage":   ["water", "drain", "fill", "น้ำ", "ระบาย", "overflow", "flood"],
    "Electrical/Sensor":        ["electric", "sensor", "power", "trip", "ไฟ", "เซ็นเซอร์", "circuit", "breaker", "fuse"],
    "Mechanical Wear":          ["wear", "bearing", "belt", "chain", "gear", "ชำรุด", "สึกหรอ", "crack", "worn", "broke"],
}

# ── Specific-issue classification (more granular than fault family) ──────────────
# Each tuple: (display_label, parent_fault_family, keyword_list)
SPECIFIC_ISSUES: list[tuple[str, str, list[str]]] = [
    ("Steam/Valve Leakage",         "Steam/Leakage",
        ["steam", "ไอน้ำ", "รั่ว", "น้ำรั่ว", "seep", "leak"]),
    ("Valve/Solenoid Fault",        "Valve/Solenoid/Actuator",
        ["valve", "solenoid", "actuator", "วาล์ว", "โซลีนอยด์"]),
    ("Water Filling/Drainage",      "Water Filling/Drainage",
        ["filling", "drain", "น้ำล้น", "น้ำท่วม", "overflow", "drainage", "flood"]),
    ("Plumbing/Pipe Issue",         "Water Filling/Drainage",
        ["pipe", "ท่อน้ำ", "plumb", "ก๊อก", "faucet", "sink", "อ่าง", "ท่อ"]),
    ("Heating/Temperature Fault",   "Heating/Temperature",
        ["heat", "temperature", "temp", "overheat", "ร้อน", "อุณหภูมิ", "thermal", "ไม่ร้อน"]),
    ("Display/Panel Not Responding","Display/Control Panel",
        ["display", "panel", "screen", "hmi", "จอ", "แสดง", "monitor", "controller"]),
    ("Abnormal Noise/Vibration",    "Abnormal Noise/Vibration",
        ["noise", "vibrat", "เสียง", "สั่น", "ดัง", "rattle", "hum", "grinding"]),
    ("Bearing/Motor Wear",          "Mechanical Wear",
        ["bearing", "ลูกปืน", "motor", "มอเตอร์", "rotor", "shaft", "สึก"]),
    ("Pump/Drive Failure",          "Mechanical Wear",
        ["pump", "drive", "inverter", "ปั๊ม", "compressor"]),
    ("Sensor/Electrical Fault",     "Electrical/Sensor",
        ["sensor", "electric", "power", "trip", "ไฟ", "เซ็นเซอร์", "circuit", "breaker", "fuse", "wiring"]),
    ("LED/Lighting Issue",          "Electrical/Sensor",
        ["led", "light", "lamp", "หลอดไฟ", "ไฟสว่าง", "ไฟฟ้า", "bulb"]),
    ("Door/Window Issue",           "Mechanical Wear",
        ["door", "window", "ประตู", "บานประตู", "หน้าต่าง", "บาน", "ล้อเลื่อน", "ราง"]),
    ("Structural/Building Repair",  "Mechanical Wear",
        ["crack", "ผนัง", "พื้น", "เพดาน", "structure", "ceiling", "floor", "wall", "ซ่อมแซม"]),
    ("Blade/Cutting Part Wear",     "Mechanical Wear",
        ["blade", "ใบมีด", "cutter", "knife", "cutting", "เปลี่ยนใบ"]),
    ("Mechanical Breakdown",        "Mechanical Wear",
        ["wear", "worn", "broke", "broken", "ชำรุด", "เสีย", "fail", "stuck", "jam"]),
    ("Unclassified",                "Unclassified",     []),
]

_SPECIFIC_ISSUE_LABELS = [s[0] for s in SPECIFIC_ISSUES]

_SPECIFIC_CACHE_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "mira_specific_issue_classifications.json"
)
_specific_cache: dict[str, str] = {}
_specific_cache_loaded = False

# Machine-name unit-number stripping (No.1, No 2, #3, trailing digit)
_UNIT_NUM_RE = re.compile(
    r"[\s\-\–]*(?:[Nn][Oo]\.?\s*\d+|#\s*\d+|\(\w*\s*\d+\w*\)|\s\d+(?=\s*$)|\s+unit\s*\d+)",
    re.UNICODE,
)

# Names that indicate a catch-all bucket rather than a specific machine
_CATCH_ALL_LOWER = frozenset(
    w.lower() for w in (
        "production low risk", "production high risk", "production medium risk",
        "low risk", "high risk", "medium risk",
        "work area", "production area", "production areas",
        "unknown / review", "unknown/review", "unknown", "review",
        "production equipment", "utilities", "facility / building",
        "refrigeration",
    )
)

# Display order for categories
_CATEGORY_ORDER = ["Production Equipment", "Utilities", "Facility / Building"]

# Minimum MRs per machine group to appear in top-5
_MIN_MACHINE_MRS = 2

AREA_LEVEL_GROUP = "Area-level MR / machine not specified"

_SPECIFIC_MACHINE_RULES: list[tuple[str, list[str]]] = [
    ("Air Blast Freezer", ["air blast freezer", "abf"]),
    ("Air Blast Chiller", ["air blast chill", "air blast chiller", "abc"]),
    ("Spiral Freezer", ["spiral freezer"]),
    ("Cold Room Condenser", ["cold room condenser"]),
    ("Evaporator", ["evaporator"]),
    ("Ice Maker", ["ice maker", "icemaker"]),
    ("Sand Filter Tank", ["sand filter tank", "sand filter"]),
    ("Carbon Filter Tank", ["carbon filter tank", "carbon filter"]),
    ("Resin Tank", ["resin tank"]),
    ("RO Filter / RO System", ["ro filter", "ro system", "reverse osmosis", "ro machine", " ro "]),
    ("Transfer Pump", ["transfer pump"]),
    ("UV Machine", ["uv machine", "ultraviolet"]),
    ("Steam Boiler", ["steam boiler"]),
    ("Hot Oil Boiler", ["hot oil boiler"]),
    ("Air Compressor", ["air compressor"]),
    ("Air Dryer", ["air dryer"]),
    ("Bratt Pan", ["bratt pan", "bratt"]),
    ("Combi Oven", ["combi oven", "combi"]),
    ("Steam Box", ["steam box", "steambox"]),
    ("X-Ray", ["x-ray", "xray", "x ray"]),
    ("Checkweigher", ["checkweigher", "check weigher"]),
    ("Index Conveyor", ["index conveyor"]),
    ("Transport Conveyor", ["transport conveyor", "belt conveyor"]),
    ("Conveyor", ["conveyor"]),
]

_AREA_LEVEL_PATTERNS = [
    r"production\s+(?:high|medium|low)\s+risk",
    r"\bwork\s+area\b",
    r"\bfacility\s*/?\s*building\b",
    r"\bproduction\s+areas?\b",
]


# ── Classification cache helpers ─────────────────────────────────────────────────

def _load_fault_cache() -> None:
    global _fault_cache, _fault_cache_loaded
    if _fault_cache_loaded:
        return
    _fault_cache_loaded = True
    try:
        if _FAULT_CACHE_PATH.exists():
            with open(_FAULT_CACHE_PATH, encoding="utf-8") as fh:
                _fault_cache = json.load(fh) or {}
    except Exception:
        _fault_cache = {}


def _save_fault_cache() -> None:
    try:
        with open(_FAULT_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_fault_cache, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_specific_cache() -> None:
    global _specific_cache, _specific_cache_loaded
    if _specific_cache_loaded:
        return
    _specific_cache_loaded = True
    try:
        if _SPECIFIC_CACHE_PATH.exists():
            with open(_SPECIFIC_CACHE_PATH, encoding="utf-8") as fh:
                _specific_cache = json.load(fh) or {}
    except Exception:
        _specific_cache = {}


def _save_specific_cache() -> None:
    try:
        with open(_SPECIFIC_CACHE_PATH, "w", encoding="utf-8") as fh:
            json.dump(_specific_cache, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _desc_hash(text: str) -> str:
    return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def _keyword_classify(text: str) -> str:
    t = text.lower()
    for family, keywords in _FAULT_KEYWORDS.items():
        for kw in keywords:
            if kw in t:
                return family
    return "Unclassified"


def _keyword_specific(text: str) -> str:
    t = text.lower()
    for label, _family, keywords in SPECIFIC_ISSUES:
        if label == "Unclassified":
            continue
        for kw in keywords:
            if kw in t:
                return label
    return "Unclassified"


def _ollama_classify(description: str, labels: list[str], system: str) -> Optional[str]:
    """Attempt Ollama classification (5-second cap). Returns None on any failure."""
    try:
        from .. import config
        if not getattr(config, "LOCAL_LLM_ENABLED", False):
            return None
        from ..providers.ollama_provider import generate_with_ollama
        labels_str = " | ".join(labels)
        result = generate_with_ollama(
            system,
            f"Description: {description}\n\nCategories: {labels_str}\n\nCategory:",
            timeout=5,
        )
        result = (result or "").strip()
        for lbl in labels:
            if lbl.lower() in result.lower():
                return lbl
        return None
    except Exception:
        return None


def _ollama_infer_machine_group(
    descriptions: list[str],
    original_asset_id: str,
    original_asset_name: str,
    approved_groups: list[str],
) -> Optional[dict]:
    """Ask Ollama to infer the real machine group from area-level MR/WO descriptions.

    Returns dict with keys:
      inferred_machine_group, confidence, reason, matched_keywords, needs_manual_review
    Returns None when LOCAL_LLM_ENABLED=False or on any error.
    """
    try:
        from .. import config
        if not getattr(config, "LOCAL_LLM_ENABLED", False):
            return None
        from ..providers.ollama_provider import generate_with_ollama

        desc_lines = "\n".join(f"- {d}" for d in descriptions[:12] if d.strip())
        if not desc_lines.strip():
            return None
        groups_str = "\n".join(f'"{g}"' for g in sorted(set(approved_groups))[:40])

        system_prompt = (
            "You are a maintenance data analyst for a food production facility. "
            "Maintenance requests (MR/WO) for generic area assets often describe the actual machine. "
            "Given a list of MR descriptions and an approved machine group list, identify the most likely "
            "real machine group. Reply ONLY with valid JSON — no text before or after the JSON object."
        )
        user_prompt = (
            f"Original Asset ID: {original_asset_id}\n"
            f"Original Asset Name: {original_asset_name}\n\n"
            f"MR/WO Descriptions:\n{desc_lines}\n\n"
            f"Approved Machine Groups:\n{groups_str}\n\n"
            'Respond with ONLY this exact JSON format:\n'
            '{"inferred_machine_group": "<name from approved list, or null if not determinable>", '
            '"confidence": "High|Medium|Low|Unknown", '
            '"reason": "<one sentence>", '
            '"matched_keywords": ["keyword1", "keyword2"], '
            '"needs_manual_review": false}'
        )

        raw = generate_with_ollama(system_prompt, user_prompt, timeout=8)
        if not raw:
            return None

        json_match = re.search(r'\{[\s\S]*\}', raw.strip())
        if not json_match:
            return None
        parsed = json.loads(json_match.group())

        inferred = str(parsed.get("inferred_machine_group") or "").strip()
        conf = str(parsed.get("confidence") or "Unknown").strip()
        if conf not in ("High", "Medium", "Low", "Unknown"):
            conf = "Unknown"

        # Validate against approved groups (case-insensitive, partial match as fallback)
        approved_lower = {g.lower(): g for g in approved_groups}
        canonical = approved_lower.get(inferred.lower())
        if not canonical and inferred:
            for g_lower, g_canon in approved_lower.items():
                if inferred.lower() in g_lower or g_lower in inferred.lower():
                    canonical = g_canon
                    break

        if not canonical:
            return {
                "inferred_machine_group": None,
                "confidence": "Unknown",
                "reason": f"Ollama suggested '{inferred[:60]}' which is not in the approved group list",
                "matched_keywords": [],
                "needs_manual_review": True,
            }

        return {
            "inferred_machine_group": canonical,
            "confidence": conf,
            "reason": str(parsed.get("reason") or "")[:200],
            "matched_keywords": [str(k) for k in (parsed.get("matched_keywords") or [])[:8]],
            "needs_manual_review": bool(parsed.get("needs_manual_review")) or conf in ("Low", "Unknown"),
        }
    except Exception:
        return None


def classify_fault(description: str) -> str:
    """Classify into broad fault family. Cache-backed + Ollama."""
    if not description or not description.strip():
        return "Unclassified"
    _load_fault_cache()
    key = _desc_hash(description)
    if key in _fault_cache:
        return _fault_cache[key]
    result = _ollama_classify(
        description, FAULT_FAMILIES,
        "You classify maintenance descriptions into fault families. "
        "Reply with only the exact family name. If unsure, reply 'Unclassified'.",
    ) or _keyword_classify(description)
    _fault_cache[key] = result
    _save_fault_cache()
    return result


def classify_specific_issue(description: str) -> str:
    """Classify into a specific recurring issue label. Separate cache + Ollama."""
    if not description or not description.strip():
        return "Unclassified"
    _load_specific_cache()
    key = "si:" + _desc_hash(description)
    if key in _specific_cache:
        return _specific_cache[key]
    result = _keyword_specific(description)
    if result == "Unclassified":
        result = _ollama_classify(
            description, _SPECIFIC_ISSUE_LABELS,
            "You classify maintenance descriptions into specific issue types. "
            "Reply with only the exact issue label. If unsure, reply 'Unclassified'.",
        ) or "Unclassified"
    _specific_cache[key] = result
    _save_specific_cache()
    return result


# ── Machine-type normalisation ───────────────────────────────────────────────────

def _is_catch_all(name: str) -> bool:
    nl = name.lower().strip()
    if nl in _CATCH_ALL_LOWER:
        return True
    return any(x in nl for x in ("low risk", "high risk", "medium risk"))


def _normalize_machine_type(name: str) -> str:
    """Strip unit numbers/suffixes → canonical machine type name."""
    n = _UNIT_NUM_RE.sub("", name).strip()
    n = re.sub(r"\s+", " ", n).strip()
    return n or name


def _row_machine_type(row: dict) -> str:
    """Specific machine type for a row (normalised name or job_trade for catch-alls)."""
    mn = str(row.get("machine_name") or row.get("machine_equipment_name") or "").strip()
    jt = str(row.get("job_trade") or "").strip()
    if mn and not _is_catch_all(mn):
        return _normalize_machine_type(mn)
    # Catch-all → use job_trade bucket if meaningful
    if jt and jt not in ("Work Order", ""):
        return jt
    return mn or "Unknown"


def _row_broad_category(row: dict) -> str:
    return str(
        row.get("machine_group") or row.get("equipment_category") or "Unknown"
    ).strip()


# ── Row field accessors ──────────────────────────────────────────────────────────

def _row_description(row: dict) -> str:
    return str(
        row.get("description") or row.get("mr_description") or row.get("wo_description") or ""
    ).strip()


def _row_wo_id(row: dict) -> str:
    return str(
        row.get("work_order_id") or row.get("wo_id") or row.get("wo_number") or ""
    ).strip()


def _row_mr_id(row: dict) -> str:
    return str(
        row.get("mr_number") or row.get("request_id") or row.get("record_id") or ""
    ).strip()


def _row_asset_id(row: dict) -> str:
    return str(row.get("asset_id") or "").strip()


def _row_has_dates(row: dict) -> bool:
    start = row.get("actual_start") or row.get("actual_start_time")
    end = row.get("actual_end") or row.get("actual_end_time")
    return bool(start) and bool(end)


def _row_latest_date(row: dict) -> Optional[date]:
    for key in (
        "actual_end_time", "actual_end",
        "latest_event_time", "actual_start_time", "actual_start",
        "request_created_time",
    ):
        val = row.get(key)
        if val:
            parsed = kpi._parse_mix_datetime(val)
            if parsed:
                return parsed.date()
    return None


def _row_machine_group(row: dict) -> str:
    return (
        str(row.get("machine_group") or row.get("mainAssetGroup") or
            row.get("mapped_main_asset_group") or "Unknown").strip()
        or "Unknown"
    )


# ── Machine-group resolver (Asset_Master[Machine Group], not job_trade) ─────────
#
# WO/MR records do NOT match Asset_Master by Asset ID (0% in source data) and
# carry only generic asset names, so the true machine group must be RESOLVED:
#   1. exact Asset ID  → Asset_Master[Machine Group] + Category  (High)
#   2. keyword/description inference (EN + Thai substring)        (Medium)
#   3. otherwise → Unknown / Review (capped, never shown in Top-5)(Low)
#
# Ranking, counting, MTBF and dates remain Python-only. Ollama is only a labelling
# fallback for the recurring-issue text, never for grouping numbers.

UNKNOWN_GROUP = "Unknown / Review"

# Asset_Master[Machine Group] values that are themselves catch-all buckets (the
# generic "Production Low/Medium/High Risk" area assets are catalogued here). A
# direct Asset ID hit on one of these is NOT accepted as the machine group — the
# description almost always names a real machine, so we re-infer from text instead.
_CATCH_ALL_GROUPS = frozenset({
    "production areas", "miscellaneous", "unknown / review", "unknown", "review",
    "n/a", "na", "",
})

# Asset_Master[Category] → display section used by the predictive UI.
_CAT_DISPLAY = {
    "Production Equipment": "Production Equipment",
    "Utilities": "Utilities",
    "Utilities / Support": "Utilities",
    "Refrigeration": "Utilities",
    "Facility / Building": "Facility / Building",
}

# Curated EN + Thai keyword phrases per real machine group. Matched as lowercase
# substrings against (machine_name + description + translated). Longer phrase wins
# (specificity), so "combi oven" beats "door" and "index conveyor" beats a bare
# trade word. Bare ambiguous words (e.g. "conveyor", "compressor") are intentionally
# omitted or made multi-word so they cannot mis-bucket.
_GROUP_KEYWORDS_CURATED: dict[str, list[str]] = {
    "Combi Ovens":             ["combi oven", "combi", "คอมบิ", "เตาอบคอมบิ"],
    "Bratt Pans":              ["bratt pan", "brat pan", "bratt", "แบรทแพน", "กระทะทอด"],
    "Fryers":                  ["fryer", "deep fry", "หม้อทอด", "เครื่องทอด"],
    "Bowl Cutters":            ["bowl cutter", "bowlcutter", "โบลคัตเตอร์"],
    "Vacuum Tumblers":         ["vacuum tumbler", "tumbler", "เครื่องหมัก", "ทัมเบลอร์"],
    "Checkweighers":           ["checkweigher", "check weigher", "เครื่องตรวจน้ำหนัก"],
    "Digital Weighing Scales": ["weighing scale", "digital scale", "weighbridge", "เครื่องชั่ง", "ตาชั่ง"],
    "X-Ray":                   ["x-ray", "xray", "x ray", "เอกซเรย์"],
    "Steam Boxes":             ["steam box", "steambox", "ตู้อบไอน้ำ", "ตู้นึ่ง"],
    "Index Conveyors":         ["index conveyor"],
    "Transport Conveyors":     ["transport conveyor", "belt conveyor", "สายพานลำเลียง"],
    "Inline Printers":         ["inline printer", "inkjet printer", "เครื่องพิมพ์วันที่", "เครื่องยิงวันที่"],
    "Crimping Machines":       ["crimping", "เครื่องรีดปาก"],
    "Water System":            ["water system", "water pump", "water treatment", "ระบบน้ำ", "ปั๊มน้ำ", "ประปา", "น้ำดี", "น้ำเสีย",
                                "อ่างล้าง", "ก็อกน้ำ", "ก๊อกน้ำ", "สายฉีด"],
    "HVAC":                    ["air conditioner", "air condition", "air handling", "hvac", "aircon", "เครื่องปรับอากาศ", "ระบบปรับอากาศ", "แอร์"],
    "Electrical":              ["switchboard", "power supply", "circuit breaker", "electrical system", "ระบบไฟฟ้า", "ตู้ไฟ", "หม้อแปลง",
                                "ปลั๊กไฟ", "สายไฟ", "เบรกเกอร์", "ปลั๊ก"],
    "Peelers":                 ["peeler", "เครื่องปอก", "ปอกมันฝรั่ง", "ปอก"],
    "Conveyors":               ["conveyor belt", "สายพาน"],
    "Boiler / Compressed Air": ["compressed air", "air compressor", "boiler", "หม้อไอน้ำ", "บอยเลอร์", "ปั๊มลม", "ระบบลม"],
    "Laundry":                 ["washing machine", "laundry", "เครื่องซักผ้า", "ซักผ้า"],
    "Fire Safety":             ["fire pump", "fire alarm", "sprinkler", "ระบบดับเพลิง", "ปั๊มดับเพลิง"],
    "Pressure Vessel":         ["pressure vessel", "ถังแรงดัน"],
    "Refrigeration":           ["refrigerat", "condenser", "evaporator", "cold room", "chiller", "freezer",
                                "ห้องเย็น", "ตู้เย็น", "คอนเดนเซอร์", "อีวาพอเรเตอร์", "ระบบทำความเย็น"],
    "Facility / Building":     ["lighting", "ceiling", "building", "facility", "cctv",
                                "อาคาร", "ฝ้าเพดาน", "ไฟส่องสว่าง", "ประตู", "หน้าต่าง",
                                "หลอดไฟ", "ผ้าม่าน", "ห้องน้ำ", "สุขา", "บันได", "ผนัง", "ม่านพลาสติก"],
}

_GROUP_INDEX_CACHE: dict = {"sig": None, "index": None}


def _norm_blob(*values) -> str:
    return " ".join(str(v or "") for v in values).lower()


def _is_area_level_text(*values) -> bool:
    text = _norm_blob(*values)
    return any(re.search(pat, text) for pat in _AREA_LEVEL_PATTERNS)


def _specific_from_text(*values) -> Optional[str]:
    text = " " + _norm_blob(*values) + " "
    for label, keywords in _SPECIFIC_MACHINE_RULES:
        for kw in keywords:
            k = kw.lower()
            if k.strip() == "ro":
                if re.search(r"\bro\b", text):
                    return label
            elif k in text:
                return label
    return None


# ── Unit-level resolution (spec §2A) ────────────────────────────────────────────
# The new unit of analysis is the INDIVIDUAL physical asset (e.g. "Bratt pan No.3"),
# never a machine group.  Area-bucket MRs are resolved via qwen or keyword extraction;
# if no unit can be determined the row routes to the Facility bucket and is NEVER ranked.

_UNIT_NUM_SEARCH_RE = re.compile(
    r'(?:no\.?\s*|#\s*|ที่\s*)(\d+)',
    re.IGNORECASE | re.UNICODE,
)


def _keyword_extract_unit_from_text(text: str) -> Optional[str]:
    """Extract specific machine type + unit number from free text (keyword fallback).

    Returns "MachineName No.N" when both a known machine type AND a unit number are
    found in the text.  Returns None when the text gives no unit number — grouping
    unnamed machines would mix unrelated units, so we route them to Facility.
    """
    t = " " + text.lower() + " "
    for label, keywords in _SPECIFIC_MACHINE_RULES:
        for kw in keywords:
            k = kw.lower()
            if k.strip() == "ro":
                if not re.search(r"\bro\b", t):
                    continue
            elif k not in t:
                continue
            # Found machine type; require a unit number for specificity.
            m = (
                re.search(rf'(?:{re.escape(k)})\s*(?:no\.?\s*|#\s*|ที่\s*)?(\d+)', t)
                or _UNIT_NUM_SEARCH_RE.search(t)
            )
            if m:
                return f"{label} No.{m.group(1)}"
            return None  # machine type found but no unit number → not specific
    return None


def _qwen_extract_unit(description: str, index: dict) -> Optional[str]:
    """Spec §4 prompt: ask qwen2.5 to extract the specific unit name from a MR description.

    Returns the canonical unit name (e.g. "Bratt Pan No.4") or None if qwen is offline,
    returns null/low-confidence, or the machine is not in the allow-list.
    Falls back to keyword extraction internally before returning None.
    """
    if not description.strip():
        return None
    key = "unit2:" + _desc_hash(description)
    cached = _mg_inference_cache.get(key)
    if cached is not None:
        return cached.get("unit_name")

    # Build allow-list from Asset_Master display names (specific, non-area units)
    allowed_machines = list(dict.fromkeys(
        display_name
        for aid, (specific, mg, cat, display_name, is_area) in index.get("id_to_specific", {}).items()
        if not is_area and display_name and not _is_catch_all(display_name)
    ))[:40]

    result: Optional[str] = None
    try:
        from .. import config
        if getattr(config, "LOCAL_LLM_ENABLED", False):
            from ..providers.ollama_provider import generate_with_ollama
            system_prompt = (
                "You map ONE maintenance request to a machine UNIT and a symptom. "
                "Reply with ONLY a JSON object, no prose."
            )
            allowed_str = json.dumps(allowed_machines)
            user_prompt = (
                f"ALLOWED_MACHINES: {allowed_str}\n\n"
                'ALLOWED_CLUSTERS: ["Steam/Valve Leak","Door/Window","Sensor/Electrical",'
                '"Noise/Vibration","Heating/Temp","Lighting","Plumbing/Sink","Facility/Building","Other"]\n\n'
                "Rules:\n"
                "- machine MUST map to ALLOWED_MACHINES, else null.\n"
                "- ALWAYS extract unit_number if the text gives one.\n"
                '- door/light/sink/toilet/ceiling with no production machine -> machine=null, cluster="Facility/Building".\n'
                "- cluster MUST be in ALLOWED_CLUSTERS.\n\n"
                f'Description: "{description[:300]}"\n'
                'Return: {"machine":...,"unit_number":...,"cluster":...,"confidence":0-1}'
            )
            raw = generate_with_ollama(system_prompt, user_prompt, timeout=5)
            if raw:
                m = re.search(r'\{[\s\S]*\}', raw.strip())
                if m:
                    parsed = json.loads(m.group())
                    machine = str(parsed.get("machine") or "").strip()
                    unit_num = parsed.get("unit_number")
                    confidence = float(parsed.get("confidence") or 0)
                    if confidence >= 0.6 and machine:
                        al_lower = {nm.lower(): nm for nm in allowed_machines}
                        canonical = al_lower.get(machine.lower())
                        if not canonical:
                            for al, canon in al_lower.items():
                                if machine.lower() in al:
                                    canonical = canon
                                    break
                        if canonical:
                            if unit_num is not None and str(unit_num) not in canonical:
                                canonical = re.sub(r'\s+No\.\d+$', '', canonical) + f" No.{unit_num}"
                            result = canonical
    except Exception:
        pass

    _mg_inference_cache[key] = {"unit_name": result}
    return result


def resolve_to_unit(row: dict) -> tuple[Optional[str], str, Optional[str]]:
    """Resolve a MR/WO row to a specific physical unit.

    Returns (unit_name, asset_id, display_category).
    unit_name is None → route to Facility/Building (never ranked).

    Priority:
      1. Direct Asset ID match that is NOT an area bucket
         → unit_name = Asset_Master.Asset Name  (High)
      2. Area-bucket Asset ID (ENWA-*, Production Low/Medium/High Risk)
         → qwen extracts unit + unit_number from Description
         → keyword fallback
         → None if still no specific unit
      3. No Asset_Master hit → keyword extraction or None
    """
    index = _group_index()
    aid = _row_asset_id(row).upper()

    if aid:
        entry = index.get("id_to_specific", {}).get(aid)
        if entry:
            specific, mg, cat, display_name, is_area = entry
            disp_cat = index["cat_display"].get(cat, "Facility / Building")
            if not is_area and display_name and not _is_catch_all(display_name):
                return display_name, aid, disp_cat
            # Area bucket: extract unit from description
            desc_text = _row_issue_text(row) or _row_description(row)
            unit = (_qwen_extract_unit(desc_text, index)
                    or _keyword_extract_unit_from_text(_resolve_text(row)))
            if unit:
                return unit, aid, disp_cat
            return None, aid, "Facility / Building"

    # No direct Asset_Master hit
    text = _resolve_text(row)
    unit = _keyword_extract_unit_from_text(text)
    if unit:
        mg, disp_cat, _ = resolve_machine_group(row)
        if mg != UNKNOWN_GROUP and disp_cat:
            return unit, aid, disp_cat
    return None, aid, "Facility / Building"


def _specific_from_master_entry(entry: dict, fallback_group: str) -> str:
    specific = _specific_from_text(
        entry.get("display_name"),
        entry.get("mappedAssetName"),
        entry.get("mapped_asset_name"),
        entry.get("mappedSubAssetGroup"),
        entry.get("mapped_sub_asset_group"),
        entry.get("asset_machine_group"),
        entry.get("mappedMachineGroup"),
        entry.get("mappedSystemArea"),
        entry.get("mapped_system_area"),
        entry.get("location"),
    )
    return specific or str(fallback_group or "").strip() or UNKNOWN_GROUP


def _compute_group_index(mapping: dict) -> dict:
    asset_map = mapping.get("asset_map", {}) or {}
    id_to_group: dict[str, tuple[str, str]] = {}
    id_to_specific: dict[str, tuple[str, str, str, str, bool]] = {}
    mg_to_cat_counter: dict[str, Counter] = defaultdict(Counter)

    for entry in asset_map.values():
        mg = str(entry.get("asset_machine_group") or "").strip()
        cat = str(entry.get("machine_group") or "").strip()  # Asset_Master[Category]
        if not mg:
            continue
        mg_to_cat_counter[mg][cat] += 1
        aid = str(entry.get("asset_id") or "").strip().upper()
        if aid:
            id_to_group[aid] = (mg, cat)
            specific = _specific_from_master_entry(entry, mg)
            display_name = str(entry.get("display_name") or entry.get("mapped_asset_name") or "").strip()
            is_area = _is_area_level_text(display_name, mg, cat, entry.get("mappedSubAssetGroup"), entry.get("mapped_sub_asset_group"))
            id_to_specific[aid] = (specific, mg, cat, display_name, is_area)

    mg_to_cat = {mg: c.most_common(1)[0][0] for mg, c in mg_to_cat_counter.items()}

    # Flatten curated keywords → (keyword_lower, machine_group, specificity).
    keywords: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for mg, kws in _GROUP_KEYWORDS_CURATED.items():
        # Only index machine groups that actually exist in this Asset Master.
        if mg not in mg_to_cat:
            continue
        for kw in kws:
            k = kw.strip().lower()
            if not k or (k, mg) in seen:
                continue
            seen.add((k, mg))
            keywords.append((k, mg, len(k)))
    # Longest (most specific) first so the matcher can short-circuit deterministically.
    keywords.sort(key=lambda t: (-t[2], t[1], t[0]))

    return {
        "id_to_group": id_to_group,
        "id_to_specific": id_to_specific,
        "mg_to_cat": mg_to_cat,
        "keywords": keywords,
        "cat_display": _CAT_DISPLAY,
    }


def _group_index() -> dict:
    """Cached machine-group index, rebuilt when Asset_Master changes."""
    try:
        from pathlib import Path as _P
        import asset_mapping as _am
        data_dir = str(_P(__file__).resolve().parents[3] / "data")
        mapping = _am.load_asset_mapping(data_dir)
    except Exception:
        return {"id_to_group": {}, "id_to_specific": {}, "mg_to_cat": {}, "keywords": [], "cat_display": _CAT_DISPLAY}
    sig = mapping.get("last_synced")
    if _GROUP_INDEX_CACHE["sig"] == sig and _GROUP_INDEX_CACHE["index"] is not None:
        return _GROUP_INDEX_CACHE["index"]
    index = _compute_group_index(mapping)
    _GROUP_INDEX_CACHE.update(sig=sig, index=index)
    return index


def _resolve_text(row: dict) -> str:
    return " ".join(str(p or "") for p in (
        row.get("machine_name"), row.get("machine_name_display"), row.get("raw_machine_name"),
        _row_description(row),
        row.get("translated_description"), row.get("wo_translated_description"),
    )).lower()


def resolve_machine_group(row: dict) -> tuple[str, Optional[str], str]:
    """Resolve a WO/MR row to (machine_group, display_category, confidence).

    display_category is None when unresolved (Unknown / Review) so the caller can
    exclude it from the Top-5 sections without inventing a parent.
    """
    index = _group_index()

    # 1) Direct Asset ID match → Asset_Master[Machine Group] + Category (High),
    #    UNLESS the asset sits in a catch-all group (generic area asset) — then the
    #    description is the better signal, so fall through to inference.
    aid = _row_asset_id(row).upper()
    direct = index["id_to_group"].get(aid) if aid else None
    if direct and direct[0].strip().lower() not in _CATCH_ALL_GROUPS:
        mg, cat = direct
        return mg, index["cat_display"].get(cat, "Facility / Building"), "High"

    # 2) Keyword / description inference (EN + Thai). Longest phrase wins.
    text = _resolve_text(row)
    for kw, mg, _spec in index["keywords"]:  # already sorted longest-first
        if kw in text:
            cat = index["mg_to_cat"].get(mg, "")
            return mg, index["cat_display"].get(cat, "Facility / Building"), "Medium"

    # 3) Unresolved — capped, never shown in a Top-5.
    return UNKNOWN_GROUP, None, "Low"


def resolve_specific_machine_group(row: dict) -> tuple[str, str, Optional[str], str, str, bool]:
    """Resolve a row to Level 2 specific machine family.

    Returns:
      specific_machine_group, main_system, display_category, confidence,
      representative_asset_name, is_area_level
    """
    index = _group_index()
    aid = _row_asset_id(row).upper()
    direct = index.get("id_to_specific", {}).get(aid) if aid else None
    if direct:
        specific, main_system, cat, asset_name, is_area = direct
        disp_cat = index["cat_display"].get(cat, "Facility / Building")
        if is_area:
            text_specific = _specific_from_text(_row_description(row), _row_issue_text(row))
            if text_specific:
                return text_specific, main_system, disp_cat, "Medium", asset_name, True
            return AREA_LEVEL_GROUP, main_system, disp_cat, "Low", asset_name, True
        return specific, main_system, disp_cat, "High", asset_name, False

    main_system, disp_cat, conf = resolve_machine_group(row)
    if main_system == UNKNOWN_GROUP or not disp_cat:
        return UNKNOWN_GROUP, main_system, disp_cat, "Low", "", False

    text_specific = _specific_from_text(_resolve_text(row), _row_issue_text(row))
    if text_specific:
        return text_specific, main_system, disp_cat, "Medium", "", False

    if _is_area_level_text(_resolve_text(row)):
        return AREA_LEVEL_GROUP, main_system, disp_cat, "Low", "", True

    return main_system, main_system, disp_cat, conf, "", False


def _recurrence_band(median_days: Optional[float], interval_count: int, record_count: int) -> tuple[str, Optional[str]]:
    """Fallback band used when no last-occurrence date is available.

    Labels mirror _likely_recurrence so the frontend sees a consistent set.
    """
    if record_count < 3 or interval_count < 2 or median_days is None:
        return "Not enough history", None
    if median_days <= 3:
        return "Likely anytime now", "now"
    if median_days <= 7:
        return "Likely within 1 week", "week"
    if median_days <= 14:
        return "Likely within 2 weeks", "2weeks"
    if median_days <= 30:
        return "Likely within 1 month", "month"
    if median_days <= 90:
        return "Likely within 1–3 months", "months"
    return "Monitor only", "monitor"


# ── MTBF at machine-type level (computed from raw rows) ─────────────────────────

def _parse_dt(val) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    try:
        s = str(val).strip().replace("Z", "").split("+")[0]
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _compute_mtbf_detail(rows: list[dict]) -> tuple[Optional[float], int, float]:
    """Return (median_gap_days, clean_interval_count, date_coverage_fraction).

    median_gap_days is None when there are fewer than 2 dated events. The interval
    count and date coverage drive the reliability gate so sub-1-day MTBF from
    batch-logged buckets is suppressed rather than shown as a real prediction.
    """
    total = len(rows)
    events: list[tuple[datetime, datetime]] = []
    dated = 0
    for r in rows:
        s = _parse_dt(r.get("actual_start_time") or r.get("actual_start"))
        e = _parse_dt(r.get("actual_end_time") or r.get("actual_end"))
        if s and e and e >= s:
            events.append((s, e))
            dated += 1
    coverage = (dated / total) if total else 0.0
    if len(events) < 2:
        return None, 0, coverage
    events.sort(key=lambda x: x[0])
    gaps = []
    for i in range(1, len(events)):
        gap_sec = (events[i][0] - events[i - 1][1]).total_seconds()
        if gap_sec >= 900:  # drop <15-min same-batch duplicates per spec §5
            gaps.append(gap_sec / 86400.0)
    if not gaps:
        return None, 0, coverage
    g = sorted(gaps)
    mid = len(g) // 2
    median = g[mid] if len(g) % 2 else (g[mid - 1] + g[mid]) / 2.0
    return median, len(gaps), coverage


def _compute_mtbf_days(rows: list[dict]) -> Optional[float]:
    """Backwards-compatible median-only accessor."""
    median, _n, _cov = _compute_mtbf_detail(rows)
    return median


def _compute_cluster_recurrence(
    cluster_rows: list[dict],
) -> tuple[Optional[float], Optional[float], int, int, list[float]]:
    """Compute recurrence interval within a specific issue cluster.

    Uses raised/actual dates of each record (not start→end gaps) because
    cluster rows are individual MR occurrences of the same issue, not
    durations of a single event.

    Returns (median_days, avg_days, n_intervals, n_records, gap_list).
    n_records < 3 → caller should show "Insufficient history".
    """
    n_records = len(cluster_rows)
    if n_records < 2:
        return None, None, 0, n_records, []
    dates: list[date] = []
    for r in cluster_rows:
        d = _row_latest_date(r)
        if not d:
            try:
                raw = r.get("actual_start_time") or r.get("actual_start") or r.get("raised_date")
                if raw:
                    d = datetime.fromisoformat(str(raw)[:10]).date()
            except Exception:
                pass
        if d:
            dates.append(d)
    dates.sort()
    if len(dates) < 2:
        return None, None, 0, n_records, []
    gaps = [
        float((dates[i] - dates[i - 1]).days)
        for i in range(1, len(dates))
        if (dates[i] - dates[i - 1]).days > 0
    ]
    if not gaps:
        return None, None, 0, n_records, []
    g = sorted(gaps)
    mid = len(g) // 2
    median = float(g[mid] if len(g) % 2 else (g[mid - 1] + g[mid]) / 2.0)
    avg = sum(gaps) / len(gaps)
    return round(median, 1), round(avg, 1), len(gaps), n_records, gaps


# Reliability gate: a specific MTBF value + next-likely date are only trustworthy
# with enough clean intervals AND reasonable date coverage. Below this, batch
# logging makes the numbers meaningless, so we show a band / "insufficient data".
_MTBF_MIN_INTERVALS = 3
_MTBF_MIN_COVERAGE = 0.60


def _mtbf_is_reliable(n_intervals: int, coverage: float) -> bool:
    return n_intervals >= _MTBF_MIN_INTERVALS and coverage >= _MTBF_MIN_COVERAGE


def _likely_recurrence(last_date: date, mtbf_days: float, today: date) -> tuple[None, str]:
    """Returns (None, timing_band_label) — no dates exposed to frontend.

    Computes estimated_next = last_date + median_gap then maps the delta to a
    forward-facing timing band only.  Never says "overdue", "active", or "passed".
    Negative delta (window already elapsed) collapses to "Likely anytime now".
    """
    if not last_date or not mtbf_days:
        return None, "Not enough history"
    estimated_next = last_date + timedelta(days=mtbf_days)
    delta = max(0, (estimated_next - today).days)   # clamp negatives to 0
    if delta <= 3:
        return None, "Likely anytime now"
    if delta <= 7:
        return None, "Likely within 1 week"
    if delta <= 14:
        return None, "Likely within 2 weeks"
    if delta <= 30:
        return None, "Likely within 1 month"
    if delta <= 90:
        return None, "Likely within 1–3 months"
    return None, "Monitor only"


def _clean_issue_phrase(description: str, max_words: int = 7) -> str:
    """Trim a raw MR description to a short, readable issue phrase for the
    Low-confidence fallback (used only when no specific issue cluster is strong
    enough). Drops asset-code noise and over-long text; never invents content."""
    text = re.sub(r"\s+", " ", str(description or "").strip())
    if not text:
        return "Issue described in MR text"
    # Drop leading asset codes / ticket numbers.
    text = re.sub(r"^[A-Z]{2,}-?\d[\w\-/]*\s+", "", text)
    words = text.split()
    phrase = " ".join(words[:max_words])
    if len(words) > max_words:
        phrase = phrase.rstrip(",.;:") + "…"
    return phrase or "Issue described in MR text"


# ── Spare ↔ fault alignment ──────────────────────────────────────────────────────

# Extra spare-relevant repair-material terms per issue, used ONLY for matching a
# spare part to a fault (never for classifying the issue itself). Lets genuine
# repair consumables surface inline (e.g. silicone/gasket for a steam leak, PVC
# fitting for a pipe fault, roller/hinge for a door fault) without loosening the
# issue taxonomy.
_SPARE_EXTRA_KEYWORDS: dict[str, list[str]] = {
    "Steam/Valve Leakage":          ["gasket", "seal", "silicone", "o-ring", "oring", "packing", "ปะเก็น", "ซีล", "ซิลิโคน"],
    "Valve/Solenoid Fault":         ["gasket", "seal", "o-ring", "oring", "diaphragm", "ปะเก็น", "ซีล"],
    "Water Filling/Drainage":       ["pvc", "pipe", "fitting", "hose", "elbow", "ท่อ", "ข้อต่อ", "สายยาง"],
    "Plumbing/Pipe Issue":          ["pvc", "pipe", "fitting", "hose", "elbow", "coupling", "ท่อ", "ข้อต่อ"],
    "Door/Window Issue":            ["roller", "hinge", "rail", "gasket", "seal", "ล้อ", "บานพับ", "ราง"],
    "Bearing/Motor Wear":           ["bearing", "belt", "pulley", "ลูกปืน", "สายพาน", "พู่เลย์"],
    "Pump/Drive Failure":           ["bearing", "seal", "impeller", "belt", "ลูกปืน", "ซีล"],
    "Blade/Cutting Part Wear":      ["blade", "knife", "ใบมีด", "มีด"],
    "Sensor/Electrical Fault":      ["tape", "temflex", "fuse", "breaker", "relay", "contactor", "terminal", "เทป", "ฟิวส์"],
    "LED/Lighting Issue":           ["led", "lamp", "bulb", "tube", "หลอด", "ไฟ"],
}


def _issue_keywords_for(issue_label: str) -> list[str]:
    """Fault keywords for an issue label: the specific-issue keywords, its parent
    fault-family keywords, and the spare-only repair-material terms — all strictly
    fault-scoped (broader recall without loosening the issue taxonomy)."""
    kws: list[str] = []
    for label, family, keywords in SPECIFIC_ISSUES:
        if label == issue_label:
            kws = list(keywords) + list(_FAULT_KEYWORDS.get(family, []))
            break
    kws += _SPARE_EXTRA_KEYWORDS.get(issue_label, [])
    return [k for k in dict.fromkeys(k.strip().lower() for k in kws) if k]


def _part_aligns_with_issue(part: dict, issue_label: str) -> bool:
    """True if a spare part plausibly relates to the row's dominant issue, so we
    don't suggest LED bulbs against a door fault. Drilldown still keeps every part.

    Match ONLY the part's own text (description + item category) against the fault
    keywords. `fault_type` is deliberately excluded: it is set to the dominant issue
    label itself, so including it made every part self-match and silently defeated
    the whole filter (e.g. argon gas surfaced against a steam-leak fault).
    """
    # Pre-filter: consumables, PPE, and painting tools are never spare parts.
    label_low = str(part.get("description") or part.get("part_name") or part.get("label") or "").lower()
    if any(sub in label_low for sub in _NON_SPARE_ITEM_SUBSTRINGS):
        return False
    kws = _issue_keywords_for(issue_label)
    if not kws:
        return False
    blob = " ".join(str(part.get(k) or "") for k in ("description", "classification")).lower()
    return any(kw in blob for kw in kws)


# ── Risk/trend helpers ───────────────────────────────────────────────────────────

def _recency_factor(days_since: int) -> float:
    if days_since <= 7:   return 1.0
    if days_since <= 14:  return 0.8
    if days_since <= 30:  return 0.6
    if days_since <= 90:  return 0.4
    return 0.2


def _trend_arrow(current: int, prior: int) -> str:
    if prior == 0:
        return "new"
    if current > prior * 1.1:
        return "up"
    if current < prior * 0.9:
        return "down"
    return "flat"


def _median_of(values: list) -> float:
    """Return median of a non-empty list."""
    s = sorted(float(v) for v in values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _row_service_level(row: dict) -> Optional[int]:
    """Return numeric service level (1–4) from a MR/WO row, or None."""
    raw = str(row.get("service_level") or row.get("severity") or row.get("priority") or "").strip()
    m = re.search(r"\d", raw)
    return int(m.group()) if m else None


def _severity_weight(sl: Optional[int]) -> float:
    """SL 1 (highest urgency) → weight 4.0, SL 4 (lowest) → 1.0 (spec §2D)."""
    return {1: 4.0, 2: 3.0, 3: 2.0, 4: 1.0}.get(sl, 2.0)


def _row_is_critical(row: dict) -> bool:
    val = row.get("am_is_critical") or row.get("is_critical")
    if val is not None:
        return bool(val)
    return "critical" in str(row.get("am_criticality") or row.get("criticality") or "").lower()


_NO_CONFIRMED_SPARE = "No confirmed spare part found. Manual review required."

# Items that are clearly consumables, PPE, painting tools, or non-spare categories
# and must never be shown as spare-part recommendations, regardless of keyword hits.
_NON_SPARE_ITEM_SUBSTRINGS: frozenset[str] = frozenset([
    "paint roll", "paint brush", "roller brush", "painting brush",
    "floor mop", "mop head", "broom",
    "safety boot", "safety shoe", "work boot",
    "safety glove", "latex glove", "rubber glove", "nitrile glove",
    "hard hat", "safety helmet", "safety vest",
    "face mask", "dust mask", "surgical mask",
    "trash bag", "garbage bag", "waste bag",
    "cleaning solution", "disinfect", "sanitiz",
    "stationery", "ball point", "ball-point",
])

_ISSUE_SUMMARY_TEMPLATES: dict[str, str] = {
    "Steam/Valve Leakage": "Repeated steam/leakage symptoms suggest a recurring sealing or valve-related issue.",
    "Valve/Solenoid Fault": "Repeated valve and solenoid wording suggests a recurring control-flow issue trend.",
    "Water Filling/Drainage": "Repeated filling and drainage symptoms suggest a recurring water-flow issue trend.",
    "Plumbing/Pipe Issue": "Repeated pipe and plumbing wording suggests a recurring piping or fitting issue trend.",
    "Heating/Temperature Fault": "Repeated heat and temperature wording suggests a recurring heating-control issue trend.",
    "Display/Panel Not Responding": "Repeated display and panel wording suggests a recurring control-panel issue trend.",
    "Abnormal Noise/Vibration": "Repeated noise and vibration wording suggests a recurring rotating-part or alignment issue trend.",
    "Bearing/Motor Wear": "Repeated bearing and motor wording suggests a recurring drive-component wear trend.",
    "Pump/Drive Failure": "Repeated pump and drive wording suggests a recurring drive-system issue trend.",
    "Sensor/Electrical Fault": "Repeated sensor and electrical wording suggests a recurring control or power issue trend.",
    "LED/Lighting Issue": "Repeated lighting wording suggests a recurring lamp or electrical accessory issue trend.",
    "Door/Window Issue": "Repeated door and window wording suggests a recurring access-panel or seal issue trend.",
    "Structural/Building Repair": "Repeated structural wording suggests a recurring building or support repair trend.",
    "Blade/Cutting Part Wear": "Repeated blade and cutting wording suggests a recurring cutting-part wear trend.",
    "Mechanical Breakdown": "Repeated mechanical failure wording suggests a recurring mechanical breakdown trend.",
}

_ISSUE_CAUSE_TEMPLATES: dict[str, str] = {
    "Steam/Valve Leakage": "Possible worn seal, gasket, or valve component based on repeated leakage wording.",
    "Valve/Solenoid Fault": "Possible valve coil, solenoid, actuator, or seal wear based on repeated valve wording.",
    "Water Filling/Drainage": "Possible blocked drain path, fill component wear, or water-flow restriction based on repeated wording.",
    "Plumbing/Pipe Issue": "Possible pipe, hose, coupling, or fitting wear based on repeated plumbing wording.",
    "Heating/Temperature Fault": "Possible heater, thermostat, thermal sensor, or controller issue based on repeated temperature wording.",
    "Display/Panel Not Responding": "Possible HMI, display, control-panel, or wiring issue based on repeated panel wording.",
    "Abnormal Noise/Vibration": "Possible alignment, bearing, motor, or rotating-part wear based on repeated vibration wording.",
    "Bearing/Motor Wear": "Possible bearing, motor, shaft, or belt wear based on repeated drive-component wording.",
    "Pump/Drive Failure": "Possible pump, seal, impeller, belt, or inverter issue based on repeated drive wording.",
    "Sensor/Electrical Fault": "Possible sensor, relay, contactor, fuse, or wiring issue based on repeated electrical wording.",
    "LED/Lighting Issue": "Possible lamp, LED, ballast, or basic electrical accessory issue based on repeated lighting wording.",
    "Door/Window Issue": "Possible hinge, roller, rail, gasket, or alignment wear based on repeated door wording.",
    "Structural/Building Repair": "Possible panel, frame, wall, floor, or support deterioration based on repeated structural wording.",
    "Blade/Cutting Part Wear": "Possible blade, knife, cutter, or mounting wear based on repeated cutting wording.",
    "Mechanical Breakdown": "Possible worn or jammed mechanical components based on repeated breakdown wording.",
}

_ISSUE_TERM_DISPLAY = {
    "leak": "leakage",
    "steam": "steam",
    "valve": "valve",
    "solenoid": "solenoid",
    "noise": "noise",
    "vibrat": "vibration",
    "bearing": "bearing",
    "motor": "motor",
    "pump": "pump",
    "drive": "drive",
    "heat": "heat",
    "temperature": "temperature",
    "temp": "temperature",
    "sensor": "sensor",
    "electric": "electrical",
    "drain": "drainage",
    "water": "water",
    "pipe": "pipe",
    "door": "door",
    "window": "window",
    "blade": "blade",
    "cut": "cutting",
    "panel": "panel",
    "display": "display",
}

_ISSUE_TEXT_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "have", "has", "had",
    "been", "were", "was", "into", "onto", "after", "before", "because", "while",
    "machine", "asset", "issue", "fault", "repair", "replace", "replaced", "check",
    "checked", "please", "work", "order", "maintenance", "request", "line", "unit",
    "area", "system", "found", "need", "needs", "required", "follow", "up", "not",
}


def _norm_key(value: str | None) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9\s/-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _row_issue_text(row: dict) -> str:
    parts = []
    for value in (
        _row_description(row),
        row.get("translated_description"),
        row.get("wo_translated_description"),
        row.get("remarks"),
        row.get("notes"),
    ):
        text = str(value or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def _row_status_bucket(row: dict) -> str:
    status = str(row.get("status") or row.get("mr_status") or row.get("wo_status") or "").lower()
    if any(token in status for token in ("finish", "finished", "confirm", "confirmed", "complete", "completed", "closed", "ended")):
        return "finished"
    if any(token in status for token in ("open", "progress", "started", "pending", "created", "active", "scheduled")):
        return "open"
    return "other"


def _extract_issue_tokens(cluster_rows: list[dict], dominant_issue: str) -> list[str]:
    recent_rows = sorted(cluster_rows, key=lambda row: (_row_latest_date(row) or date.min), reverse=True)[:3]
    keyword_hits: Counter[str] = Counter()
    for keyword in _issue_keywords_for(dominant_issue):
        token = keyword.strip().lower()
        if not token:
            continue
        for row in recent_rows:
            if token in _norm_key(_row_issue_text(row)):
                keyword_hits[token] += 1
                break
    if keyword_hits:
        picked = [token for token, _count in keyword_hits.most_common(3)]
        return [_ISSUE_TERM_DISPLAY.get(token, token) for token in picked]

    token_hits: Counter[str] = Counter()
    for row in recent_rows:
        norm_text = _norm_key(_row_issue_text(row))
        for token in norm_text.split():
            if len(token) < 4 or token in _ISSUE_TEXT_STOPWORDS:
                continue
            token_hits[token] += 1
    picked = [token for token, _count in token_hits.most_common(3)]
    return [_ISSUE_TERM_DISPLAY.get(token, token) for token in picked]


def _build_main_observed_issue(dominant_issue: str, symptom_terms: list[str]) -> str:
    template = _ISSUE_SUMMARY_TEMPLATES.get(dominant_issue)
    if template:
        return template
    if symptom_terms:
        joined = "/".join(symptom_terms[:3])
        return f"Repeated {joined} wording suggests a recurring issue trend that needs review."
    return "Repeated MR/WO wording suggests a recurring issue trend that needs review."


def _build_likely_cause_candidate(dominant_issue: str, recommended_parts: list[dict]) -> str:
    template = _ISSUE_CAUSE_TEMPLATES.get(dominant_issue)
    if template:
        return template
    if recommended_parts:
        labels = [part.get("label") for part in recommended_parts[:2] if part.get("label")]
        if labels:
            return f"Possible wear in related components such as {', '.join(labels)} based on repeated wording and matching spare history."
    return "Possible recurring component issue based on repeated MR/WO wording only. Manual review required."


def _format_issue_evidence(
    dominant_count: int,
    latest_date: Optional[date],
    symptom_terms: list[str],
    mtbf_label: str,
    open_count: int,
    dominant_issue: str = "",
) -> str:
    parts = [f"{dominant_count} related record{'s' if dominant_count != 1 else ''}"]
    if latest_date:
        parts.append(f"latest {latest_date.isoformat()}")
    # Use the classified issue label to describe what was repeated — the raw token
    # extraction fallback was picking machine name fragments (e.g. "bratt/pan4//low").
    if dominant_issue:
        short = (
            dominant_issue
            .removesuffix(" Fault").removesuffix(" Issue")
            .removesuffix(" Problem").removesuffix(" Leakage")
            .strip()
        )
        parts.append(f"repeated {short} wording")
    if open_count:
        parts.append(f"{open_count} still open/recent")
    parts.append(f"MTBF {mtbf_label}" if mtbf_label != "Insufficient data" else "MTBF signal limited")
    return ", ".join(parts)


def _simple_stock_status(inv_record: dict | None) -> str:
    if not inv_record:
        return "Unknown"
    qty = inv_record.get("current_quantity")
    try:
        if qty is not None and float(qty) > 0:
            return "In stock"
        if qty is not None and float(qty) == 0:
            return "Not in stock"
    except Exception:
        pass
    status_group = str(inv_record.get("stock_status_group") or inv_record.get("stock_status") or "").lower()
    if "out of stock" in status_group:
        return "Not in stock"
    if status_group:
        return "In stock"
    return "Unknown"


def _build_inventory_lookup(spare_payload: dict) -> dict:
    records = (((spare_payload or {}).get("inventory") or {}).get("records") or [])
    by_code = {
        str(row.get("code") or "").strip().upper(): row
        for row in records
        if str(row.get("code") or "").strip()
    }
    return {"records": records, "by_code": by_code}


def _find_inventory_match(item_code: str | None, label: str | None, inventory_lookup: dict) -> dict | None:
    code = str(item_code or "").strip().upper()
    if code and code in (inventory_lookup.get("by_code") or {}):
        return inventory_lookup["by_code"][code]
    label_norm = _norm_key(label)
    if not label_norm:
        return None
    for row in inventory_lookup.get("records") or []:
        row_norm = _norm_key(row.get("name") or row.get("description") or row.get("code"))
        if row_norm and (label_norm in row_norm or row_norm in label_norm):
            return row
    return None


_STAGE1_SPARES_CATALOGUE_PATH = Path(__file__).resolve().parents[3] / "data" / "Spare Parts- Assets_Stage 1.xlsx"
_STAGE1_SPARES_CATALOGUE_CACHE: dict[str, object] = {"sig": None, "rows": []}
_SPARE_PREP_CACHE: dict[tuple, dict] = {}
_OTHER_COMMON_FAULTS_CACHE: dict[tuple, list[dict]] = {}

_CATALOGUE_MACHINE_ALIASES: dict[str, list[str]] = {
    "bratt pan": ["bratt pans", "bratt pan"],
    "combi oven": ["combi ovens", "combi oven"],
    "steam box": ["steam boxes", "steam box", "steambox"],
    "air blast freezer": ["air blast freezers", "air blast freezer"],
    "air blast chiller": ["air blast chillers", "air blast chiller"],
    "spiral freezer": ["spiral freezers", "spiral freezer"],
    "x ray": ["x ray", "x-ray"],
    "checkweigher": ["checkweighers", "checkweigher"],
    "conveyor": ["conveyors", "conveyor"],
}

_ISSUE_SPARE_HINTS: list[tuple[str, list[str], list[str]]] = [
    ("Plumbing/pipe/valve issue", ["plumbing", "pipe", "water", "leak", "steam", "valve"], ["valve", "gasket", "sealant", "steam hose", "hose", "pipe", "fitting", "union", "flange"]),
    ("Steam/valve leakage", ["steam", "valve", "leak"], ["steam hose", "valve", "gasket", "sealant", "fitting", "union", "flange"]),
    ("Door/gasket issue", ["door", "gasket", "window", "seal"], ["door gasket", "gasket", "seal", "trolley gasket", "hinge", "roller"]),
    ("Heating/temperature issue", ["heat", "heating", "temperature", "temp", "thermal"], ["heating", "heater", "temperature controller", "relay", "contactor", "control pcb", "thermostat", "sensor"]),
    ("Belt/conveyor issue", ["belt", "conveyor", "roller", "drive"], ["belt", "roller", "drive shaft", "revolving shaft", "inverter", "bearing", "motor"]),
    ("Sensor/electrical issue", ["sensor", "electric", "electrical", "power", "trip", "relay"], ["sensor", "relay", "plc", "control pcb", "timer", "tower light", "contactor", "breaker"]),
    ("Refrigerant/cooling issue", ["refrigerant", "cool", "freezer", "chiller", "compressor"], ["refrigerant", "compressor oil", "fan motor", "compressor", "expansion valve", "condenser", "evaporator"]),
    ("Mechanical holder/pot wear", ["mechanical", "holder", "pot", "hinge", "handle", "bearing", "shaft", "bracket", "wear"], ["holder", "hinge", "handle", "bearing", "shaft", "bracket", "seal", "gasket"]),
    ("Drainage/water filling issue", ["drain", "drainage", "filling", "overflow", "water level", "blocked", "inlet", "outlet"], ["drain", "inlet", "outlet", "valve", "hose", "pipe", "fitting", "gasket", "seal"]),
]

_SPARE_TOKEN_STOPWORDS = {
    "and", "for", "with", "the", "set", "assy", "assembly", "machine", "part",
    "spare", "no", "type", "size", "pcs", "pc", "unit", "material",
}

_COMMON_FAULT_SIGNATURES: list[tuple[str, list[str], str]] = [
    ("Plumbing / Pipe / Water / Leak", ["pipe", "water", "leak", "faucet", "tap", "drain", "hose", "plumbing", "fitting", "coupling", "union", "gasket", "sealant"], "Inspect pipe holder, water inlet, valve, hose, gasket, and coupling."),
    ("Steam / Valve Leakage", ["steam", "valve", "pressure", "leakage", "leak", "solenoid", "steam hose", "gasket", "fitting", "coupling"], "Inspect valve, steam hose, gasket, and fittings."),
    ("Heating / Temperature Issue", ["heat", "heating", "temperature", "temp", "hot", "not hot", "burner", "heater", "thermostat", "heating element"], "Check heating control, thermostat, relay, and electrical connection."),
    ("Sensor / Electrical Fault", ["sensor", "electrical", "electric", "alarm", "trip", "plc", "relay", "switch", "control", "error", "timer", "tower light", "cable", "breaker"], "Check sensor, relay, control wiring, PLC/error signal, and electrical protection."),
    ("Mechanical / Pot / Holder Wear", ["holder", "pot", "hinge", "handle", "mechanical", "broken", "wear", "loose", "bearing", "shaft", "bracket"], "Inspect holder, hinge, handle, shaft, bracket, and mechanical wear points."),
    ("Drainage / Water Filling", ["drain", "drainage", "filling", "overflow", "water level", "blocked", "inlet", "outlet"], "Check drain path, inlet/outlet, fill level, and blockage points."),
    ("Door / Seal / Gasket", ["door", "seal", "gasket", "rubber", "closing", "latch", "hinge", "silicone gasket"], "Inspect door alignment, latch, hinge, rubber seal, and gasket condition."),
    ("Belt / Conveyor", ["belt", "conveyor", "roller", "shaft", "bearing", "motor", "inverter", "chain", "sprocket"], "Check belt, roller, shaft, bearing, motor, inverter, chain, and sprocket."),
]

_COMMON_FAULT_ALIASES = {
    "plumbing pipe issue": "Plumbing / Pipe / Water / Leak",
    "water filling drainage": "Drainage / Water Filling",
    "steam valve leakage": "Steam / Valve Leakage",
    "valve solenoid fault": "Steam / Valve Leakage",
    "heating temperature fault": "Heating / Temperature Issue",
    "sensor electrical fault": "Sensor / Electrical Fault",
    "door window issue": "Door / Seal / Gasket",
    "abnormal noise vibration": "Mechanical / Pot / Holder Wear",
    "bearing motor wear": "Mechanical / Pot / Holder Wear",
    "mechanical breakdown": "Mechanical / Pot / Holder Wear",
    "pump drive failure": "Mechanical / Pot / Holder Wear",
}


def _catalogue_col(df, wanted: str):
    wanted_norm = re.sub(r"[^a-z0-9]+", "", wanted.lower())
    for col in df.columns:
        col_norm = re.sub(r"[^a-z0-9]+", "", str(col).lower())
        if col_norm == wanted_norm:
            return col
    for col in df.columns:
        col_norm = re.sub(r"[^a-z0-9]+", "", str(col).lower())
        if wanted_norm in col_norm or col_norm in wanted_norm:
            return col
    return None


def _split_catalogue_parts(value) -> list[str]:
    text = str(value or "").replace("\r", "\n")
    text = re.sub(r"[\u2022\xb7]", "\n", text)
    chunks = re.split(r"\n+|;+", text)
    if len(chunks) <= 1 and text.count(",") >= 2:
        chunks = text.split(",")
    parts: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        item = re.sub(r"^\s*[-*\d.)]+\s*", "", str(chunk or "")).strip(" \t,;:-")
        item = _clean_part_label(item, max_len=90)
        key = _norm_key(item)
        if item and key and key not in seen:
            seen.add(key)
            parts.append(item)
    return parts


def _load_stage1_spares_catalogue() -> list[dict]:
    path = _STAGE1_SPARES_CATALOGUE_PATH
    try:
        sig = (path.stat().st_mtime, path.stat().st_size)
    except Exception:
        return []
    if _STAGE1_SPARES_CATALOGUE_CACHE.get("sig") == sig:
        return list(_STAGE1_SPARES_CATALOGUE_CACHE.get("rows") or [])

    rows: list[dict] = []
    try:
        import pandas as pd
        xl = pd.ExcelFile(path)
        sheet = "Stage 1 - Spares by Machine Gro"
        if sheet not in xl.sheet_names:
            sheet = next((s for s in xl.sheet_names if "spares" in s.lower() and "machine" in s.lower()), xl.sheet_names[0])
        df = pd.read_excel(path, sheet_name=sheet)
        c_group = _catalogue_col(df, "Machine Group")
        c_parts = _catalogue_col(df, "Relevant Spare Parts")
        c_issues = _catalogue_col(df, "General Common Issues")
        if not (c_group and c_parts):
            raw_df = pd.read_excel(path, sheet_name=sheet, header=None)
            header_idx = None
            for idx, raw_row in raw_df.iterrows():
                values = [str(v or "").strip() for v in raw_row.tolist()]
                normed = [re.sub(r"[^a-z0-9]+", "", v.lower()) for v in values]
                if "machinegroup" in normed and "relevantspareparts" in normed:
                    header_idx = idx
                    break
            if header_idx is not None:
                headers = [str(v or "").strip() for v in raw_df.iloc[header_idx].tolist()]
                df = raw_df.iloc[header_idx + 1:].copy()
                df.columns = headers
                c_group = _catalogue_col(df, "Machine Group")
                c_parts = _catalogue_col(df, "Relevant Spare Parts")
                c_issues = _catalogue_col(df, "General Common Issues")
        if c_group and c_parts:
            for _, raw in df.iterrows():
                group = str(raw.get(c_group) or "").strip()
                parts = _split_catalogue_parts(raw.get(c_parts))
                if not group or not parts:
                    continue
                rows.append({
                    "machine_group": group,
                    "machine_key": _norm_key(group),
                    "parts": parts,
                    "common_issues": str(raw.get(c_issues) or "").strip() if c_issues else "",
                })
    except Exception as exc:
        print(f"[predictive] Stage 1 spare-parts catalogue failed: {exc}")
        rows = []

    _STAGE1_SPARES_CATALOGUE_CACHE["sig"] = sig
    _STAGE1_SPARES_CATALOGUE_CACHE["rows"] = rows
    _SPARE_PREP_CACHE.clear()
    return list(rows)


def _catalogue_group_candidates(machine_group: str, main_system: str | None = None) -> list[str]:
    raw = _norm_key(machine_group)
    base = re.sub(r"\b(no|number|unit)\s*\d+\b", " ", raw)
    base = re.sub(r"\b\d+\b", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    candidates = [raw, base]
    for key, aliases in _CATALOGUE_MACHINE_ALIASES.items():
        if key in raw or key in base:
            candidates.extend(_norm_key(a) for a in aliases)
    if main_system:
        candidates.append(_norm_key(main_system))
    out: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _find_catalogue_machine_row(machine_group: str, main_system: str | None = None) -> dict | None:
    rows = _load_stage1_spares_catalogue()
    if not rows:
        return None
    candidates = _catalogue_group_candidates(machine_group, main_system)
    for cand in candidates:
        for row in rows:
            if cand and cand == row.get("machine_key"):
                return row
    for cand in candidates:
        for row in rows:
            key = str(row.get("machine_key") or "")
            if cand and key and (cand in key or key in cand):
                return row
    return None


def _part_tokens(label: str) -> set[str]:
    return {
        tok for tok in re.findall(r"[a-z0-9]+", _norm_key(label))
        if len(tok) >= 3 and tok not in _SPARE_TOKEN_STOPWORDS
    }


def _extract_catalogue_item_code(label: str) -> str | None:
    text = str(label or "").strip()
    match = re.match(r"^([A-Z0-9][A-Z0-9._/-]{3,})\s+[-:]\s+(.+)$", text, flags=re.I)
    return match.group(1).strip().upper() if match else None


def _po_description(row: dict) -> str:
    return str(row.get("part_description") or row.get("part_name") or row.get("description") or "").strip()


def _po_date(row: dict) -> Optional[date]:
    raw = row.get("po_date") or row.get("date_gen_po") or row.get("date")
    parsed = _parse_dt(raw)
    return parsed.date() if parsed else None


def _po_matches_catalogue_part(part_label: str, item_code: str | None, po_row: dict) -> bool:
    code = str(item_code or "").strip().upper()
    po_code = str(po_row.get("item_code") or po_row.get("code") or "").strip().upper()
    if code and po_code and code == po_code:
        return True
    desc_norm = _norm_key(_po_description(po_row))
    part_norm = _norm_key(part_label)
    if not desc_norm or not part_norm:
        return False
    if len(part_norm) >= 8 and (part_norm in desc_norm or desc_norm in part_norm):
        return True
    ptoks = _part_tokens(part_label)
    dtoks = _part_tokens(desc_norm)
    if len(ptoks) >= 2:
        return len(ptoks & dtoks) >= max(2, min(len(ptoks), 3))
    return bool(ptoks and ptoks <= dtoks)


def _issue_spare_reason(part_label: str, dominant_issue: str, symptom_terms: list[str], common_issues: str, po_matches: list[dict]) -> str | None:
    issue_blob = _norm_key(" ".join([dominant_issue or "", " ".join(symptom_terms or [])]))
    part_blob = _norm_key(part_label)
    common_blob = _norm_key(common_issues)
    po_blob = _norm_key(" ".join(_po_description(row) for row in po_matches[:5]))

    for reason, issue_terms, part_terms in _ISSUE_SPARE_HINTS:
        issue_hit = any(_norm_key(term) in issue_blob for term in issue_terms)
        part_hit = any(_norm_key(term) in part_blob for term in part_terms)
        po_hit = any(_norm_key(term) in po_blob for term in part_terms)
        if issue_hit and (part_hit or po_hit):
            return reason
    if issue_blob and common_blob:
        issue_tokens = _part_tokens(issue_blob)
        common_tokens = _part_tokens(common_blob)
        if issue_tokens and len(issue_tokens & common_tokens) >= 1:
            return "Stage 1 common issue match"
    if _part_aligns_with_issue({"description": part_label, "classification": "Stage 1 catalogue"}, dominant_issue):
        return "Issue signature matched spare-part description"
    return None


def _stock_decision(inv: dict | None, po_matches: list[dict]) -> tuple[object, str, str]:
    qty = None
    if inv:
        qty = inv.get("current_quantity")
        try:
            if qty is not None and float(qty) > 0:
                return qty, "In stock", "Prepare in store"
        except Exception:
            pass
        if po_matches:
            return qty, "Purchase required / reorder", "Purchase required / reorder"
            return qty, "No purchase history found — verify manually", "Verify manually"
    if po_matches:
        return qty, "Stock not confirmed — check store", "Check store; purchase if unavailable"
    return qty, "No purchase history found — verify manually", "Verify manually"


def _build_catalogue_spare_prepare(
    machine_group: str,
    dominant_issue: str,
    parts_context: dict,
    inventory_lookup: dict,
    main_system: str | None = None,
    symptom_terms: list[str] | None = None,
) -> dict:
    all_purchase_rows = (parts_context or {}).get("allPurchaseParts") or (parts_context or {}).get("purchaseParts") or []
    cache_key = (
        _norm_key(machine_group),
        _norm_key(main_system),
        _norm_key(dominant_issue),
        tuple(sorted(_norm_key(term) for term in (symptom_terms or []))),
        len(all_purchase_rows),
        len((inventory_lookup or {}).get("records") or []),
        _STAGE1_SPARES_CATALOGUE_CACHE.get("sig"),
    )
    cached = _SPARE_PREP_CACHE.get(cache_key)
    if cached is not None:
        return cached

    catalogue_row = _find_catalogue_machine_row(machine_group, main_system)
    if not catalogue_row:
        result = {
            "available": False,
            "parts": [],
            "suggested_inline": _NO_CONFIRMED_SPARE,
            "overall_stock_status": "Unknown",
            "history_part_count": 0,
            "basis": "No Stage 1 spare-parts catalogue match for this machine group. Manual review required.",
            "catalogue_machine_group": None,
        }
        _SPARE_PREP_CACHE[cache_key] = result
        return result

    recommendations: list[dict] = []
    fallback: list[dict] = []
    common_issues = str(catalogue_row.get("common_issues") or "")
    for part_label in catalogue_row.get("parts") or []:
        item_code = _extract_catalogue_item_code(part_label)
        po_matches = [row for row in all_purchase_rows if _po_matches_catalogue_part(part_label, item_code, row)]
        reason = _issue_spare_reason(part_label, dominant_issue, symptom_terms or [], common_issues, po_matches)
        inv = _find_inventory_match(item_code, part_label, inventory_lookup)
        on_hand_qty, stock_status, purchase_recommendation = _stock_decision(inv, po_matches)
        dated = sorted(
            [(d, row) for row in po_matches if (d := _po_date(row))],
            key=lambda pair: pair[0],
            reverse=True,
        )
        latest_po = dated[0][1] if dated else (po_matches[0] if po_matches else {})
        this_year = date.today().year
        ytd_rows = [row for row in po_matches if (_po_date(row) and _po_date(row).year == this_year)]
        try:
            ytd_qty = sum(float(row.get("quantity") or 0) for row in ytd_rows)
        except Exception:
            ytd_qty = 0.0
        try:
            latest_price = latest_po.get("value")
            if latest_price is not None:
                latest_price = float(latest_price)
        except Exception:
            latest_price = None
        rec = {
            "item_code": item_code or (latest_po.get("item_code") if latest_po else None),
            "label": part_label,
            "name": part_label,
            "match_reason": reason or f"Stage 1 catalogue match for {catalogue_row.get('machine_group')}",
            "gen_po_validation_status": "Found in purchase history / YTD" if ytd_rows else "Found in historical purchase records" if po_matches else "No Gen PO purchase history found",
            "last_purchased_date": dated[0][0].isoformat() if dated else None,
            "last_purchase_date": dated[0][0].isoformat() if dated else None,
            "ytd_po_count": len(ytd_rows),
            "total_ytd_qty_purchased": round(ytd_qty, 3),
            "latest_vendor": latest_po.get("vendor") if latest_po else "",
            "latest_price": latest_price,
            "lead_time_days": latest_po.get("lead_time_days") if latest_po else None,
            "on_hand_qty": on_hand_qty,
            "current_quantity": on_hand_qty,
            "stock_status": stock_status,
            "purchase_recommendation": purchase_recommendation,
            "source": f"Stage 1 spare-parts catalogue: {catalogue_row.get('machine_group')}",
            "catalogue_machine_group": catalogue_row.get("machine_group"),
            "purchase_rows": len(po_matches),
            "po_evidence": po_matches[:3],
            "evidence_tags": [
                "Stage 1 catalogue",
                "Gen PO validated" if po_matches else "No Gen PO match",
                "Inventory matched" if inv else "Inventory not confirmed",
            ],
        }
        if reason:
            recommendations.append(rec)
        else:
            fallback.append(rec)

    if not recommendations:
        recommendations = fallback[:5]
    else:
        recommendations = recommendations[:5]

    stock_states = {part.get("stock_status") for part in recommendations}
    overall_stock_status = (
        "In stock" if "In stock" in stock_states
        else "Purchase required / reorder" if "Purchase required / reorder" in stock_states
        else "Stock not confirmed — check store" if "Stock not confirmed — check store" in stock_states
        else "Unknown"
    )
    inline = " | ".join(
        f"{part['label']} ({part['stock_status']})" for part in recommendations[:3]
    ) or _NO_CONFIRMED_SPARE
    result = {
        "available": bool(recommendations),
        "parts": recommendations,
        "suggested_inline": inline,
        "overall_stock_status": overall_stock_status,
        "history_part_count": sum(1 for part in recommendations if part.get("purchase_rows")),
        "basis": (
            f"Stage 1 catalogue is the approved spare-part source "
            f"({catalogue_row.get('machine_group')}); Gen PO validates purchase history only."
        ),
        "catalogue_machine_group": catalogue_row.get("machine_group"),
    }
    _SPARE_PREP_CACHE[cache_key] = result
    return result


def _common_fault_key(label: str | None) -> str:
    return _norm_key(label).replace("/", " ")


def _classify_common_fault_signature(text: str) -> tuple[str, list[str], str]:
    blob = _norm_key(text)
    best: tuple[str, list[str], str, int] | None = None
    for label, keywords, check in _COMMON_FAULT_SIGNATURES:
        hits = []
        for kw in keywords:
            norm_kw = _norm_key(kw)
            if norm_kw and norm_kw in blob:
                hits.append(kw)
        if hits and (best is None or len(hits) > best[3]):
            best = (label, hits, check, len(hits))
    if best:
        return best[0], best[1], best[2]

    classified = classify_specific_issue(text)
    alias = _COMMON_FAULT_ALIASES.get(_common_fault_key(classified))
    if alias:
        for label, keywords, check in _COMMON_FAULT_SIGNATURES:
            if label == alias:
                return label, keywords[:4], check
    return "Unclassified", [], "Review MR notes and inspect the affected machine area."


def _catalogue_group_label_for_unit(unit_name: str, category_name: str | None = None) -> str:
    row = _find_catalogue_machine_row(unit_name, category_name)
    return str((row or {}).get("machine_group") or unit_name or "").strip()


def _part_stock_action(parts: list[dict]) -> str:
    if not parts:
        return "Verify manually"
    if any("purchase required" in str(p.get("stock_status") or p.get("purchase_recommendation") or "").lower()
           or "reorder" in str(p.get("stock_status") or p.get("purchase_recommendation") or "").lower()
           for p in parts):
        return "Reorder / purchase required"
    if any("check store" in str(p.get("stock_status") or p.get("purchase_recommendation") or "").lower()
           or "not confirmed" in str(p.get("stock_status") or p.get("purchase_recommendation") or "").lower()
           for p in parts):
        return "Stock not confirmed — check store"
    if any("in stock" in str(p.get("stock_status") or "").lower() for p in parts):
        return "In stock"
    return "Verify manually"


def _build_other_common_faults(
    unit_name: str,
    exact_rows: list[dict],
    selected_issue: str,
    unit_groups: dict[str, list],
    parts_context: dict,
    inventory_lookup: dict,
    category_name: str,
    max_faults: int = 5,
) -> list[dict]:
    catalogue_group = _catalogue_group_label_for_unit(unit_name, category_name)
    selected_label, _, _ = _classify_common_fault_signature(selected_issue or "")
    selected_key = _common_fault_key(selected_label if selected_label != "Unclassified" else selected_issue)

    def _count_faults(rows: list[dict]) -> Counter:
        counter: Counter[str] = Counter()
        for row in rows:
            label, _, _ = _classify_common_fault_signature(_row_issue_text(row) or _row_description(row))
            if label != "Unclassified" and _common_fault_key(label) != selected_key:
                counter[label] += 1
        return counter

    exact_counter = _count_faults(exact_rows)
    use_fallback = sum(exact_counter.values()) < 2 or len(exact_counter) == 0
    support_rows = list(exact_rows)
    basis = "Based on this machine's MR history"
    if use_fallback:
        for other_unit, rows in (unit_groups or {}).items():
            if other_unit == unit_name:
                continue
            if _catalogue_group_label_for_unit(other_unit, category_name) == catalogue_group:
                support_rows.extend(rows or [])
        if len(support_rows) > len(exact_rows):
            basis = f"Based on similar {catalogue_group} records"

    cache_key = (
        _norm_key(unit_name),
        _norm_key(catalogue_group),
        _norm_key(selected_issue),
        len(exact_rows),
        len(support_rows),
        len((parts_context or {}).get("allPurchaseParts") or []),
        len((inventory_lookup or {}).get("records") or []),
        _STAGE1_SPARES_CATALOGUE_CACHE.get("sig"),
    )
    cached = _OTHER_COMMON_FAULTS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    grouped: dict[str, list[tuple[dict, list[str], str]]] = defaultdict(list)
    for row in support_rows:
        label, hits, check = _classify_common_fault_signature(_row_issue_text(row) or _row_description(row))
        if label == "Unclassified" or _common_fault_key(label) == selected_key:
            continue
        grouped[label].append((row, hits, check))

    denominator = len(support_rows) or len(exact_rows) or 1
    result: list[dict] = []
    for label, pairs in sorted(
        grouped.items(),
        key=lambda item: (-len(item[1]), max((_row_latest_date(pair[0]) or date.min for pair in item[1]))),
    )[:max_faults]:
        rows = [pair[0] for pair in pairs]
        latest_row = max(rows, key=lambda row: (_row_latest_date(row) or date.min))
        latest_date = _row_latest_date(latest_row)
        keywords = list(dict.fromkeys(kw for _, hits, _ in pairs for kw in hits))[:8]
        check = pairs[0][2] if pairs else "Review MR notes and inspect the affected machine area."
        spare = _build_catalogue_spare_prepare(
            unit_name,
            label,
            parts_context,
            inventory_lookup,
            main_system=category_name,
            symptom_terms=keywords,
        )
        parts = (spare.get("parts") or [])[:5]
        desc = _row_description(latest_row) or str(latest_row.get("translated_description") or "")
        result.append({
            "issue_signature": label,
            "mr_count": len(rows),
            "pct_of_machine_mr": round((len(rows) / denominator) * 100, 1),
            "last_occurrence": latest_date.isoformat() if latest_date else None,
            "recent_example_mr_id": _row_mr_id(latest_row),
            "recent_example_wo_id": _row_wo_id(latest_row),
            "latest_description": _clean_issue_phrase(desc, max_words=16),
            "suggested_check": check,
            "spare_parts_to_prepare": parts,
            "stock_status": spare.get("overall_stock_status") or _part_stock_action(parts),
            "purchase_recommendation": _part_stock_action(parts),
            "basis": basis,
            "catalogue_machine_group": catalogue_group,
            "examples": [
                {
                    "mr_id": _row_mr_id(row),
                    "wo_id": _row_wo_id(row),
                    "date": (_row_latest_date(row).isoformat() if _row_latest_date(row) else None),
                    "description": _clean_issue_phrase(_row_description(row), max_words=16),
                }
                for row in sorted(rows, key=lambda r: (_row_latest_date(r) or date.min), reverse=True)[:5]
            ],
        })

    _OTHER_COMMON_FAULTS_CACHE[cache_key] = result
    return result


# ── Gen PO → machine-group detection ─────────────────────────────────────────

def _detect_machine_group_from_po_row(po_row: dict) -> tuple[str | None, str, list[str]]:
    """Detect which machine group a Gen PO row belongs to.

    Returns (machine_group or None, confidence 'High'/'Medium'/'Low', matched_keywords).

    Strategy:
      1. pd_machine / translated_pd_machine field on the PO row — explicit machine link → High
      2. Combined description + group_of_cost text inference → Medium / Low
    """
    def _try_resolve(text: str) -> tuple[str | None, str]:
        if not text.strip():
            return None, "Low"
        mg, _, _, conf, _, is_area = resolve_specific_machine_group({"asset_id": "", "description": text})
        if mg and mg not in (UNKNOWN_GROUP, AREA_LEVEL_GROUP) and not is_area:
            return mg, conf
        return None, "Low"

    # 1. Explicit pd_machine field — treat as High confidence (direct machine link)
    for fld in ("translated_pd_machine", "pd_machine"):
        pd = str(po_row.get(fld) or "").strip()
        if not pd:
            continue
        mg, conf = _try_resolve(pd)
        if mg:
            return mg, "High" if conf == "High" else "Medium", [pd[:60]]

    # 2. Description + group_of_cost text inference
    desc = str(po_row.get("translated_description") or po_row.get("description") or "").strip()
    goc = str(po_row.get("translated_group_of_cost") or po_row.get("group_of_cost") or "").strip()
    combined = " ".join(p for p in (desc, goc) if p)
    if combined:
        mg, conf = _try_resolve(combined)
        if mg:
            return mg, "Low" if conf == "Low" else "Medium", []

    return None, "Unknown", []


# ── Spare parts per machine group (PO↔WO↔MR chain) ──────────────────────────────

def _compute_machine_spare(
    machine_group: str,
    group_rows: list[dict],
    dominant_issue: str,
    parts_context: dict,
    inventory_lookup: dict,
    median_gap_days: Optional[float] = None,
    main_system: str | None = None,
    symptom_terms: list[str] | None = None,
) -> dict:
    """Verified spare recommendations for one machine group and issue cluster."""
    usage_rows = (parts_context or {}).get("sparePartsUsed") or []
    purchase_rows = (parts_context or {}).get("purchaseParts") or []

    catalogue = _build_catalogue_spare_prepare(
        machine_group,
        dominant_issue,
        parts_context,
        inventory_lookup,
        main_system=main_system,
        symptom_terms=symptom_terms or [],
    )
    recommended = catalogue.get("parts") or []

    def _catalogue_part_to_lead(p: dict) -> dict:
        return {
            "name": p.get("label") or p.get("name"),
            "code": p.get("item_code"),
            "on_hand": p.get("on_hand_qty"),
            "evidence": p.get("gen_po_validation_status"),
            "lead_time_days": p.get("lead_time_days"),
            "unit_price": p.get("latest_price"),
            "vendor": p.get("latest_vendor") or "",
            "reorder": p.get("stock_status") == "Purchase required / reorder",
            "stock_status": p.get("stock_status") or "Unknown",
            "source": p.get("source"),
            "evidence_tags": p.get("evidence_tags") or [],
        }

    def _catalogue_part_to_kit(p: dict) -> dict:
        return {
            "name": p.get("label") or p.get("name"),
            "code": p.get("item_code"),
            "on_hand": p.get("on_hand_qty"),
            "reorder": p.get("stock_status") == "Purchase required / reorder",
            "stock_status": p.get("stock_status") or "Unknown",
        }

    if catalogue is not None:
        return {
            "available": bool(recommended),
            "parts": recommended,
            "spare_parts_to_prepare": recommended,
            "spare_lead": _catalogue_part_to_lead(recommended[0]) if recommended else None,
            "spare_kit": [_catalogue_part_to_kit(p) for p in recommended[1:]],
            "suggested_inline": catalogue.get("suggested_inline") or _NO_CONFIRMED_SPARE,
            "overall_stock_status": catalogue.get("overall_stock_status") or "Unknown",
            "history_part_count": catalogue.get("history_part_count", 0),
            "basis": catalogue.get("basis") or _NO_CONFIRMED_SPARE,
            "catalogue_machine_group": catalogue.get("catalogue_machine_group"),
            "linked_wo_count": len({_row_wo_id(r) for r in group_rows if _row_wo_id(r)}),
            "linked_transaction_count": len(usage_rows),
            "machine_group": machine_group,
        }

    candidates: dict[str, dict] = {}

    def _candidate_key(item_code: str | None, label: str | None) -> str:
        return str(item_code or "").strip().upper() or _norm_key(label)

    def _ensure_candidate(item_code: str | None, label: str | None, classification: str | None = None) -> dict | None:
        clean_label = _clean_part_label(label or item_code or "")
        if not clean_label:
            return None
        key = _candidate_key(item_code, clean_label)
        if not key:
            return None
        if key in candidates:
            return candidates[key]
        candidates[key] = {
            "item_code": str(item_code or "").strip() or None,
            "label": clean_label,
            "classification": str(classification or ""),
            "source_types": set(),
            "usage_quantity": 0.0,
            "usage_rows": 0,
            "purchase_quantity": 0.0,
            "purchase_rows": 0,
            "history_score": 0,
            "last_used": None,
            "last_purchase_date": None,
            "inventory": None,
            "stock_status": "Unknown",
            "estimated_value": None,
        }
        return candidates[key]

    for row in usage_rows:
        label = row.get("part_name") or row.get("item_code")
        if not _part_aligns_with_issue({"description": label, "classification": "Usage history"}, dominant_issue):
            continue
        cand = _ensure_candidate(row.get("item_code"), label, "Usage history")
        if not cand:
            continue
        cand["source_types"].add("Usage history")
        cand["usage_quantity"] += float(row.get("quantity") or 0) or 1.0
        cand["usage_rows"] += 1
        cand["history_score"] += 35 if row.get("is_direct_match") else 22
        used_date = str(row.get("date") or "").strip()
        if used_date and (cand["last_used"] is None or used_date > cand["last_used"]):
            cand["last_used"] = used_date
        try:
            value = row.get("value")
            qty = float(row.get("quantity") or 0)
            if value is not None and qty > 0:
                cand["estimated_value"] = round(float(value) / qty, 2)
        except Exception:
            pass

    for row in purchase_rows:
        if row.get("classification") not in {"Stock Spare Part", "Non-Stock Spare Part"}:
            continue
        label = row.get("part_description") or row.get("part_name") or row.get("item_code")
        if not _part_aligns_with_issue({"description": label, "classification": row.get("classification")}, dominant_issue):
            continue
        cand = _ensure_candidate(row.get("item_code"), label, row.get("classification"))
        if not cand:
            continue
        # Stage-specific source label so the UI can show "Gen PO Stage 1" vs "Gen PO Stage 2"
        raw_stage = str(row.get("source_stage") or "").strip()
        source_label = (
            "Gen PO Stage 1" if "1" in raw_stage
            else "Gen PO Stage 2" if "2" in raw_stage
            else "Gen PO history"
        )
        cand["source_types"].add(source_label)
        cand["purchase_quantity"] += float(row.get("quantity") or 0) or 1.0
        cand["purchase_rows"] += 1
        # Score by machine-detection confidence; Low-confidence PO matches score less
        mg_conf = row.get("machine_detection_confidence") or ("High" if row.get("is_direct_match") else "Medium")
        score = 30 if mg_conf == "High" else 20 if mg_conf == "Medium" else 8
        cand["history_score"] += score
        po_date = str(row.get("po_date") or "").strip()
        if po_date and (cand["last_purchase_date"] is None or po_date > cand["last_purchase_date"]):
            cand["last_purchase_date"] = po_date
        # Traceability: keep the best PO evidence per candidate
        cand.setdefault("po_evidence", []).append({
            "po_no": row.get("po_no"),
            "vendor": str(row.get("vendor") or "").strip(),
            "stage": raw_stage or "Gen PO",
            "date": po_date,
            "confidence": mg_conf,
            "lead_time_days": row.get("lead_time_days"),
        })
        try:
            value = row.get("value")
            qty = float(row.get("quantity") or 0)
            if value is not None and qty > 0:
                cand["estimated_value"] = round(float(value) / qty, 2)
        except Exception:
            pass

    for cand in candidates.values():
        inv = _find_inventory_match(cand.get("item_code"), cand.get("label"), inventory_lookup)
        if inv:
            cand["inventory"] = inv
            cand["stock_status"] = _simple_stock_status(inv)
            cand["source_types"].update({"On-hand list", "Spare catalogue"})
            if cand.get("estimated_value") is None and inv.get("unit_cost") is not None:
                try:
                    cand["estimated_value"] = round(float(inv.get("unit_cost")), 2)
                except Exception:
                    pass

    if not candidates:
        for inv in inventory_lookup.get("records") or []:
            label = _clean_part_label(inv.get("name") or inv.get("description") or inv.get("code"))
            if not label:
                continue
            if not _part_aligns_with_issue({"description": label, "classification": inv.get("item_group") or inv.get("category")}, dominant_issue):
                continue
            cand = _ensure_candidate(inv.get("code"), label, inv.get("item_group") or inv.get("category"))
            if not cand:
                continue
            cand["inventory"] = inv
            cand["stock_status"] = _simple_stock_status(inv)
            cand["source_types"].update({"On-hand list", "Spare catalogue"})
            if cand.get("estimated_value") is None and inv.get("unit_cost") is not None:
                try:
                    cand["estimated_value"] = round(float(inv.get("unit_cost")), 2)
                except Exception:
                    pass

    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            -item["history_score"],
            -(1 if item.get("stock_status") == "In stock" else 0),
            -(item.get("usage_rows") or 0),
            -(item.get("purchase_rows") or 0),
            -(item.get("usage_quantity") or 0),
            -(item.get("purchase_quantity") or 0),
            str(item.get("label") or ""),
        ),
    )

    recommended = [
        {
            "item_code": item.get("item_code"),
            "label": item.get("label"),
            "source": " + ".join(sorted(item.get("source_types") or [])),
            "stock_status": item.get("stock_status") or "Unknown",
            "current_quantity": (item.get("inventory") or {}).get("current_quantity"),
            "usage_rows": item.get("usage_rows"),
            "purchase_rows": item.get("purchase_rows"),
            "last_used": item.get("last_used"),
            "last_purchase_date": item.get("last_purchase_date"),
            "estimated_value": item.get("estimated_value"),
            "po_evidence": item.get("po_evidence") or [],
            "evidence_tags": [],
        }
        for item in ranked[:3]
    ]

    if not recommended:
        return {
            "available": False,
            "parts": [],
            "suggested_inline": _NO_CONFIRMED_SPARE,
            "overall_stock_status": "Unknown",
            "history_part_count": 0,
            "basis": _NO_CONFIRMED_SPARE,
            "linked_wo_count": len({_row_wo_id(r) for r in group_rows if _row_wo_id(r)}),
            "linked_transaction_count": len(usage_rows),
            "machine_group": machine_group,
        }

    # Deduplicate by normalised label — same part can appear in multiple transactions.
    _seen_norm: set[str] = set()
    _deduped: list[dict] = []
    for _cand in recommended:
        _nk = _norm_key(_cand.get("label") or "")
        if _nk and _nk not in _seen_norm:
            _seen_norm.add(_nk)
            _deduped.append(_cand)
    recommended = _deduped

    # ── Per-part lead time (median of PO evidence, winsorize >180d) + reorder flag ─
    for part in recommended:
        po_ev = part.get("po_evidence") or []
        raw_lt = [
            min(float(e["lead_time_days"]), 180.0)
            for e in po_ev
            if e.get("lead_time_days") is not None
        ]
        part_lead_time = round(_median_of(raw_lt)) if raw_lt else None
        part["lead_time_days"] = part_lead_time

        reorder = False
        on_hand = part.get("current_quantity")
        if (
            part_lead_time is not None
            and median_gap_days is not None
            and median_gap_days > 0
            and on_hand is not None
        ):
            try:
                stock = float(on_hand)
                expected_before_restock = part_lead_time / median_gap_days
                usage_qty = float(part.get("usage_quantity") or 1)
                usage_ct = max(1, part.get("usage_rows") or 1)
                qty_per_repair = usage_qty / usage_ct
                reorder = stock - (expected_before_restock * qty_per_repair) < 0
            except Exception:
                pass
        part["reorder_flag"] = reorder

    # ── Evidence tags and PO traceability per part ────────────────────────────
    for part in recommended:
        tags: list[str] = []
        if part.get("usage_rows"):
            tags.append("Used before")
        po_ev_list = part.get("po_evidence") or []
        if po_ev_list:
            # Sort by date descending to surface the most recent PO
            po_ev_list_sorted = sorted(
                po_ev_list, key=lambda e: str(e.get("date") or ""), reverse=True
            )
            best = po_ev_list_sorted[0]
            conf = best.get("confidence") or ""
            stage = best.get("stage") or "Gen PO"
            tag = f"Purchased for {machine_group}"
            if conf and conf not in ("High",):
                tag += f" · {conf.lower()} confidence"
            tags.append(tag)
            part["last_po_no"] = best.get("po_no")
            part["po_vendor"] = best.get("vendor") or ""
            part["po_stage"] = stage
            part["machine_detection_confidence"] = conf
        if part.get("stock_status") == "In stock":
            tags.append("In stock")
        elif part.get("stock_status") == "Not in stock":
            tags.append("Out of stock")
        part["evidence_tags"] = tags

    inline = " | ".join(f"{part['label']} ({part['stock_status']})" for part in recommended)
    stock_states = {part.get("stock_status") for part in recommended}
    overall_stock_status = (
        "In stock" if "In stock" in stock_states
        else "Not in stock" if stock_states == {"Not in stock"}
        else "Unknown"
    )
    history_part_count = sum(
        1 for part in recommended
        if any(
            kw in str(part.get("source") or "")
            for kw in ("Usage history", "Gen PO Stage 1", "Gen PO Stage 2", "Gen PO history")
        )
    )
    onhand_matches = sum(
        1 for p in recommended if p.get("stock_status") not in (None, "Unknown")
    )
    basis = (
        f"{len(usage_rows)} usage row(s), {len(purchase_rows)} Gen PO purchase row(s), "
        f"{onhand_matches} on-hand match(es), "
        f"{history_part_count} recommended part(s) with matching history."
    )
    # Build spec §3 lead / kit structure
    def _part_to_lead(p: dict) -> dict:
        total_wos = len({_row_wo_id(r) for r in group_rows if _row_wo_id(r)}) or 1
        u_rows = p.get("usage_rows") or 0
        evidence_str = f"{u_rows} of {total_wos} WOs" if u_rows else (
            f"{p.get('purchase_rows') or 0} PO record(s)"
        )
        return {
            "name": p.get("label"),
            "code": p.get("item_code"),
            "on_hand": p.get("current_quantity"),
            "evidence": evidence_str,
            "lead_time_days": p.get("lead_time_days"),
            "unit_price": p.get("estimated_value"),
            "vendor": p.get("po_vendor") or "",
            "reorder": p.get("reorder_flag", False),
            "stock_status": p.get("stock_status") or "Unknown",
            "source": p.get("source"),
            "evidence_tags": p.get("evidence_tags") or [],
        }

    def _part_to_kit(p: dict) -> dict:
        return {
            "name": p.get("label"),
            "code": p.get("item_code"),
            "on_hand": p.get("current_quantity"),
            "reorder": p.get("reorder_flag", False),
            "stock_status": p.get("stock_status") or "Unknown",
        }

    return {
        "available": True,
        "parts": recommended,
        "spare_lead": _part_to_lead(recommended[0]) if recommended else None,
        "spare_kit": [_part_to_kit(p) for p in recommended[1:]],
        "suggested_inline": inline,
        "overall_stock_status": overall_stock_status,
        "history_part_count": history_part_count,
        "basis": basis,
        "linked_wo_count": len({_row_wo_id(r) for r in group_rows if _row_wo_id(r)}),
        "linked_transaction_count": len(usage_rows),
        "machine_group": machine_group,
    }


def _clean_part_label(desc: str, max_len: int = 38) -> str:
    """Normalise a raw PO/transaction line into a short readable spare label.
    Drops mojibake (�), leading quantities, trailing fragments and dangling
    punctuation/brackets. Returns "" when nothing readable is left so the caller
    drops it rather than showing a bare placeholder."""
    text = str(desc or "").replace("�", " ")        # drop � replacement chars
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^\d+\s*[xX*]\s*", "", text)          # drop leading "4 x" quantity
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text)          # drop trailing (parenthetical)
    text = re.sub(r"[\s,;:.\-\"'(]+$", "", text)          # drop dangling trailing punctuation / open bracket
    text = re.sub(r"^[\s,;:.\-]+", "", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip(" ,;:.-\"'(") + "…"
    return text


# ── Top-5 machine groups per category ───────────────────────────────────────────

def _build_top_machines(
    category_name: str,
    mg_groups: dict[str, list[tuple[dict, str]]],
    prior_by_group: dict[str, int],
    inventory_lookup: dict,
    group_parts_context_getter,
    today: date,
    top_n: int = 5,
) -> list[dict]:
    """Score and rank top-N real machine groups within a single category.

    mg_groups maps machine_group → list of (row, resolution_confidence). Groups are
    real Asset_Master[Machine Group] values; Unknown / Review is never passed in.
    """
    # Approved groups for Ollama validation — derived once from the live Asset_Master index
    _idx = _group_index()
    approved_groups: list[str] = list(_idx.get("mg_to_cat", {}).keys())

    scored: list[dict] = []
    for machine_group, pairs in mg_groups.items():
        if machine_group == UNKNOWN_GROUP:
            continue
        rows = [pair[0] for pair in pairs]
        if len(rows) < _MIN_MACHINE_MRS:
            continue
        main_system_counts = Counter((pair[2] or "") for pair in pairs if len(pair) > 2 and pair[2])
        main_system = main_system_counts.most_common(1)[0][0] if main_system_counts else category_name
        is_area_level = machine_group == AREA_LEVEL_GROUP or any((len(pair) > 4 and pair[4]) for pair in pairs)

        # ── Hybrid machine-group inference for area-level rows ────────────────────
        inferred_machine_group: Optional[str] = None
        inference_source: str = (
            "asset_id" if "High" in {pair[1] for pair in pairs if len(pair) > 1}
            else "keyword"
        )
        inference_confidence: Optional[str] = None
        inference_reason: Optional[str] = None
        inference_keywords: list[str] = []
        needs_manual_review: bool = False
        original_asset_names: list[str] = []
        override_key: Optional[str] = None
        sample_descriptions: list[str] = []

        if is_area_level:
            inference_source = "area_level"
            # Collect original catch-all asset names (from pair[3] = representative_asset_name)
            asset_name_counter: Counter[str] = Counter()
            for pair in pairs:
                aname = str(pair[3] if len(pair) > 3 else "").strip()
                if aname:
                    asset_name_counter[aname] += 1
            original_asset_names = [n for n, _ in asset_name_counter.most_common(3)]

            # Derive a representative asset ID (first non-empty)
            top_asset_id = ""
            for row in rows:
                aid = _row_asset_id(row)
                if aid:
                    top_asset_id = aid
                    break

            # Aggregate unique descriptions (for override key + Ollama)
            descs: list[str] = []
            seen_descs: set[str] = set()
            for row in rows:
                d = str(_row_description(row) or "").strip()[:200]
                if d and d not in seen_descs:
                    seen_descs.add(d)
                    descs.append(d)
            sample_descriptions = descs[:5]

            desc_blob = " | ".join(descs[:8])
            override_key = _mg_override_key(top_asset_id or machine_group, desc_blob)

            # 1) Check manual override
            overrides = _load_mg_overrides()
            override = overrides.get(override_key)
            if override and override.get("machine_group"):
                inferred_machine_group = override["machine_group"]
                inference_source = "manual_override"
                inference_confidence = "High"
                inference_reason = f"Manually confirmed by {override.get('confirmed_by', 'operator')}"
                needs_manual_review = False
            elif descs and approved_groups:
                # 2) Try Ollama (cached by description hash to avoid repeat calls)
                cache_key = "mg_infer:" + _desc_hash(desc_blob)
                ollama_result = _mg_inference_cache.get(cache_key)
                if ollama_result is None:
                    top_asset_name = original_asset_names[0] if original_asset_names else ""
                    ollama_result = _ollama_infer_machine_group(
                        descs, top_asset_id or machine_group, top_asset_name, approved_groups
                    ) or {}
                    _mg_inference_cache[cache_key] = ollama_result

                if ollama_result:
                    inferred_mg = ollama_result.get("inferred_machine_group")
                    ol_conf = ollama_result.get("confidence", "Unknown")
                    if inferred_mg:
                        inferred_machine_group = inferred_mg
                        inference_source = "ollama_description"
                        inference_confidence = ol_conf
                        inference_reason = ollama_result.get("reason", "")
                        inference_keywords = ollama_result.get("matched_keywords", [])
                        needs_manual_review = bool(ollama_result.get("needs_manual_review")) or ol_conf in ("Low", "Unknown")
                    else:
                        needs_manual_review = True
                else:
                    needs_manual_review = bool(descs)
            else:
                needs_manual_review = True
        # ─────────────────────────────────────────────────────────────────────────

        recurrence = len(rows)

        # Last occurrence (prefer actual_end_time, then start, then latest_event)
        valid_dates = [d for d in (_row_latest_date(r) for r in rows) if d]
        last_date = max(valid_dates) if valid_dates else None
        days_since = (today - last_date).days if last_date else 999
        r_factor = _recency_factor(days_since)

        issue_rows = [
            (row, classify_specific_issue(_row_issue_text(row) or _row_description(row)))
            for row in rows
        ]
        issue_counter = Counter(issue for _row, issue in issue_rows)
        recent_issue_rows = sorted(issue_rows, key=lambda pair: (_row_latest_date(pair[0]) or date.min), reverse=True)
        named_scores = []
        for issue, count in issue_counter.items():
            if issue == "Unclassified":
                continue
            cluster = [row for row, row_issue in issue_rows if row_issue == issue]
            latest_cluster_date = max((_row_latest_date(row) for row in cluster if _row_latest_date(row)), default=None)
            days = (today - latest_cluster_date).days if latest_cluster_date else 999
            recent_count = sum(1 for row, row_issue in recent_issue_rows[:4] if row_issue == issue)
            open_count = sum(1 for row in cluster if _row_status_bucket(row) == "open")
            score = count * 5 + recent_count * 4 + open_count * 2 + max(0.0, (30 - days) / 10.0)
            named_scores.append((score, issue, count, recent_count, open_count, cluster, latest_cluster_date))

        issue_reason = ""
        if named_scores:
            named_scores.sort(key=lambda item: (-item[0], -item[2], str(item[1])))
            _score, dominant_issue, dominant_count, recent_cluster_count, open_cluster_count, cluster_rows, latest_cluster_date = named_scores[0]
            if dominant_count >= 3 and recent_cluster_count >= 2:
                issue_confidence = "High"
            elif dominant_count >= 2:
                issue_confidence = "Medium"
            else:
                issue_confidence = "Low"
                issue_reason = "Recent wording is clustered, but the trend is still weak."
        else:
            recent_row = max(rows, key=lambda r: (_row_latest_date(r) or date.min))
            dominant_issue = _clean_issue_phrase(_row_issue_text(recent_row) or _row_description(recent_row))
            dominant_count = 1
            recent_cluster_count = 1
            open_cluster_count = 1 if _row_status_bucket(recent_row) == "open" else 0
            cluster_rows = [recent_row]
            latest_cluster_date = _row_latest_date(recent_row)
            issue_confidence = "Low"
            issue_reason = "Generated from the latest description because no repeated named cluster was strong enough."

        # Broad fault family (for secondary label / spare alignment)
        faults = [classify_fault(_row_issue_text(r) or _row_description(r)) for r in rows]
        dominant_fault = Counter(faults).most_common(1)[0][0]

        # All-rows MTBF (used for data_confidence card and confidence score only).
        # Sub-1-day median = batch-logging artifact; suppress rather than display.
        mtbf_days, n_intervals, coverage = _compute_mtbf_detail(rows)
        reliable = (
            mtbf_days is not None
            and mtbf_days >= 1.0
            and _mtbf_is_reliable(n_intervals, coverage)
        )
        mtbf_label = f"~{round(mtbf_days, 1)}d" if reliable else "Insufficient data"

        # Cluster-specific recurrence interval drives the Recurrence Gauge column.
        # Uses only records in the dominant issue cluster; average is supporting evidence.
        c_median, c_avg, c_n_intervals, _, c_gaps = _compute_cluster_recurrence(cluster_rows)
        c_reliable = (
            dominant_count >= 3
            and c_n_intervals >= 2
            and c_median is not None
            and c_median >= 1.0
        )
        likely_label, recurrence_band_key = _recurrence_band(c_median, c_n_intervals, dominant_count)
        likely_date_str = None  # never expose dates to frontend
        if c_reliable:
            ref_date = latest_cluster_date or last_date
            _, timing_label = _likely_recurrence(ref_date, c_median, today)
            likely_label = timing_label  # today-relative band overrides interval-only band

        # ── next_due_days + overdue + gap-based trend (spec §2C) ─────────────
        if c_median is not None and days_since < 999:
            raw_next = round(c_median - days_since)
            next_due_days: Optional[int] = max(0, raw_next)
            overdue = raw_next < 0
        else:
            next_due_days = None
            overdue = False

        if len(c_gaps) >= 4:
            n_third = max(1, len(c_gaps) // 3)
            older_med = _median_of(c_gaps[:-n_third])
            recent_med = _median_of(c_gaps[-n_third:])
            interval_trend = (
                "degrading" if recent_med < older_med * 0.85
                else "stabilizing" if recent_med > older_med * 1.15
                else "stable"
            )
        else:
            interval_trend = "stable"

        # ── Severity weighting + criticality multiplier (spec §2D) ───────────
        sl_values = [_row_service_level(r) for r in rows]
        sl_values = [v for v in sl_values if v is not None]
        dominant_sl: Optional[int] = Counter(sl_values).most_common(1)[0][0] if sl_values else None
        avg_weight = _median_of([_severity_weight(v) for v in sl_values]) if sl_values else 2.0
        critical_count = sum(1 for r in rows if _row_is_critical(r))
        is_critical = critical_count > len(rows) / 2
        criticality_mult = 1.5 if is_critical else 1.0

        symptom_terms = _extract_issue_tokens(cluster_rows, dominant_issue)
        parts_context = group_parts_context_getter(machine_group)
        spare = _compute_machine_spare(
            machine_group,
            rows,
            dominant_issue,
            parts_context,
            inventory_lookup,
            c_median,
            main_system=main_system,
            symptom_terms=symptom_terms,
        )
        main_observed_issue = _build_main_observed_issue(dominant_issue, symptom_terms)
        likely_cause_candidate = _build_likely_cause_candidate(dominant_issue, spare.get("parts") or [])
        evidence_summary = _format_issue_evidence(
            dominant_count=dominant_count,
            latest_date=latest_cluster_date,
            symptom_terms=symptom_terms,
            mtbf_label=mtbf_label,
            open_count=open_cluster_count,
            dominant_issue=dominant_issue,
        )
        # ── Confidence: spec §2C thresholds ──────────────────────────────────
        consumption_count = len(parts_context.get("sparePartsUsed") or []) if parts_context else 0
        if c_n_intervals >= 6 and consumption_count >= 3:
            confidence = "High"
            confidence_reason = (
                f"{len(rows)} MRs, {c_n_intervals} clean intervals, "
                f"{consumption_count} consumption records."
            )
        elif c_n_intervals >= 3:
            confidence = "Medium"
            confidence_reason = (
                f"{len(rows)} MRs, {c_n_intervals} clean intervals "
                f"(need ≥3 consumption records for High)."
            )
        else:
            confidence = "Low"
            confidence_reason = (
                f"Not enough history — {c_n_intervals} clean interval(s) "
                f"(need ≥3 for Medium, ≥6+consumption for High)."
            )
        if issue_confidence == "Low" and confidence != "Low":
            confidence = "Low"
            confidence_reason = "Weak issue cluster; " + confidence_reason

        risk_score = recurrence * r_factor * avg_weight * criticality_mult

        # Escalation: dominant specific issue repeated ≥ 3 times (candidate only).
        escalation = None
        if dominant_count >= 3 and issue_confidence != "Low":
            mr_ids = list(dict.fromkeys(_row_mr_id(r) for r in rows if _row_mr_id(r)))[:12]
            wo_ids_list = list(dict.fromkeys(_row_wo_id(r) for r in rows if _row_wo_id(r)))[:8]
            escalation = {
                "triggered": True,
                "trigger": dominant_issue,
                "trigger_count": dominant_count,
                "reason": f"'{dominant_issue}' repeated {dominant_count}× for {machine_group}",
                "label": "Potential escalation candidate",
                "note": "Review for management escalation if unresolved.",
                "mr_ids": mr_ids,
                "wo_ids": wo_ids_list,
            }

        trend = _trend_arrow(recurrence, prior_by_group.get(machine_group, 0))
        asset_ids = {_row_asset_id(r) for r in rows if _row_asset_id(r)}
        asset_count = len(asset_ids)
        related_asset_counter: Counter[str] = Counter()
        for pair in pairs:
            row = pair[0]
            asset_name = str(pair[3] if len(pair) > 3 else "").strip()
            if not asset_name:
                asset_name = str(
                    row.get("mapped_asset_name")
                    or row.get("mappedAssetName")
                    or row.get("asset_display_name")
                    or row.get("machine_name_display")
                    or row.get("machine_name")
                    or row.get("asset_name")
                    or row.get("asset_id")
                    or ""
                ).strip()
            if asset_name and not _is_area_level_text(asset_name):
                related_asset_counter[asset_name] += 1
        related_assets = [
            {"asset_name": name, "mr_count": count}
            for name, count in related_asset_counter.most_common(5)
        ]

        # Group resolution confidence: High if any row matched by Asset ID, else
        # Medium (inferred from description). Surfaced for transparency.
        confs = {pair[1] for pair in pairs if len(pair) > 1}
        group_match_conf = "High" if "High" in confs else "Medium" if "Medium" in confs else "Low"
        if is_area_level and group_match_conf == "High":
            group_match_conf = "Low"

        # Recent issue evidence for the "View issues" drill-down (most recent first).
        issue_evidence: list[dict] = []
        note_snippets: list[str] = []
        for r, iss in recent_issue_rows:
            desc = _row_description(r)
            if not desc:
                continue
            ev_date = _row_latest_date(r)
            note_text = str(r.get("notes") or r.get("remarks") or "").strip()
            if note_text:
                clean_note = _clean_issue_phrase(note_text, max_words=14)
                if clean_note not in note_snippets:
                    note_snippets.append(clean_note)
            translated = str(
                r.get("translated_description") or r.get("wo_translated_description") or ""
            ).strip()
            issue_evidence.append({
                "mr_id": _row_mr_id(r),
                "wo_id": _row_wo_id(r),
                "date": ev_date.isoformat() if ev_date else None,
                "issue": iss if iss != "Unclassified" else None,
                "description": _clean_issue_phrase(desc, max_words=16),
                "translated_description": _clean_issue_phrase(translated, max_words=16) if translated else None,
                "status": str(r.get("status") or "").strip() or None,
            })
            if len(issue_evidence) >= 6:
                break

        scored.append({
            "machine_type": machine_group,
            "machine_group": machine_group,
            "specific_machine_group": machine_group,
            "main_system": main_system,
            "is_area_level": is_area_level,
            "is_critical": is_critical,
            "asset_count": asset_count,
            "related_assets": related_assets,
            "group_match_confidence": group_match_conf,
            # ── Machine-group inference traceability ──────────────────────────
            "inference_source": inference_source,
            "inferred_machine_group": inferred_machine_group,
            "inference_confidence": inference_confidence,
            "inference_reason": inference_reason,
            "inference_keywords": inference_keywords,
            "needs_manual_review": needs_manual_review,
            "original_asset_names": original_asset_names,
            "override_key": override_key,
            "sample_descriptions": sample_descriptions,
            # ─────────────────────────────────────────────────────────────────
            "mr_count": recurrence,
            "recurring_issue": dominant_issue,
            "recurring_issue_confidence": issue_confidence,
            "recurring_issue_reason": issue_reason,
            "recurring_issue_fault_family": dominant_fault,
            "main_observed_issue": main_observed_issue,
            "evidence_summary": evidence_summary,
            "likely_cause_candidate": likely_cause_candidate,
            "suggested_spare_parts": spare.get("parts", []),
            "spare_parts_to_prepare": spare.get("spare_parts_to_prepare") or spare.get("parts", []),
            "stock_status": spare.get("overall_stock_status") or "Unknown",
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            "symptom_keywords": symptom_terms,
            "note_snippets": note_snippets[:3],
            "issue_breakdown": [
                {"issue": iss, "count": cnt}
                for iss, cnt in issue_counter.most_common(5) if iss != "Unclassified"
            ],
            "issue_evidence": issue_evidence,
            "last_occurrence": last_date.isoformat() if last_date else None,
            "days_since_last": days_since if days_since < 999 else None,
            "dominant_count": dominant_count,
            "cluster_last_occurrence": latest_cluster_date.isoformat() if latest_cluster_date else None,
            "mtbf_days": round(mtbf_days, 1) if (reliable and mtbf_days) else None,
            "mtbf_label": mtbf_label,
            "mtbf_reliable": reliable,
            "recurrence_interval_days": c_median,
            "recurrence_interval_avg_days": c_avg,
            "recurrence_interval_n": c_n_intervals,
            "recurrence_gauge": likely_label,
            "recurrence_band": recurrence_band_key,
            "likely_recurrence_date": likely_date_str,
            "likely_recurrence_label": likely_label,
            # ── Spec §3 output blocks ─────────────────────────────────────────
            "timing": {
                "next_due_days": next_due_days,
                "median_gap_days": c_median,
                "days_since_last": days_since if days_since < 999 else None,
                "trend": interval_trend,
                "overdue": overdue,
            },
            "severity": {
                "weighted_score": round(risk_score, 2),
                "dominant_service_level": dominant_sl,
            },
            # ─────────────────────────────────────────────────────────────────
            "suggested_spare": spare.get("suggested_inline"),
            "spare_parts": spare.get("parts", []),
            "spare_lead": spare.get("spare_lead"),
            "spare_kit": spare.get("spare_kit", []),
            "spare_available": spare.get("available", False),
            "spare_linked_wo_count": spare.get("linked_wo_count", 0),
            "spare_linked_transaction_count": spare.get("linked_transaction_count", 0),
            "spare_recommendation_basis": spare.get("basis") or _NO_CONFIRMED_SPARE,
            "escalation": escalation,
            "trend": trend,
            "prior_count": prior_by_group.get(machine_group, 0),
            "risk_score": round(risk_score, 2),
        })

    scored.sort(key=lambda x: -x["risk_score"])
    return [dict(m, rank=i + 1) for i, m in enumerate(scored[:top_n])]


# ── Prior period ─────────────────────────────────────────────────────────────────

def _prior_period(window: dict) -> tuple[date, date]:
    start, end = window["start_date"], window["end_date"]
    span = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=span - 1)
    return prior_start, prior_end


def _row_in_range(row: dict, start: date, end: date) -> bool:
    raised = kpi._mr_raised_date(row)
    return bool(raised and start <= raised <= end)


# ── MTBF from existing management payload (category-level, kept for compat) ─────

def _extract_mtbf_by_group(mgmt: dict) -> dict[str, dict]:
    mtbf = mgmt.get("mtbf", {}) or {}
    views = mtbf.get("views", {}) or {}
    all_asset_rows: list[dict] = []
    for view in views.values():
        if isinstance(view, dict):
            all_asset_rows.extend(view.get("asset_rows", []) or [])
    acc: dict[str, dict] = {}
    for row in all_asset_rows:
        mg = str(row.get("machine_group") or "").strip()
        if not mg or row.get("average_mtbf_hours") is None:
            continue
        if mg not in acc:
            acc[mg] = {"total": 0.0, "count": 0, "max_gaps": 0}
        acc[mg]["total"] += float(row.get("average_mtbf_hours") or 0)
        acc[mg]["count"] += 1
        gaps = int(row.get("valid_mtbf_gap_count") or 0)
        if gaps > acc[mg]["max_gaps"]:
            acc[mg]["max_gaps"] = gaps
    return {
        mg: {"avg_mtbf_hours": d["total"] / d["count"], "max_gap_count": d["max_gaps"]}
        for mg, d in acc.items() if d["count"] > 0
    }


# ── Data Confidence (Card 4) ─────────────────────────────────────────────────────

def _compute_data_confidence(period_rows: list[dict]) -> dict:
    total = len(period_rows)
    if total == 0:
        return {
            "band": "Low", "tone": "critical",
            "label": "No MR data for this period.",
            "total": 0,
            "asset_mapping_pct": None,
            "date_completeness_pct": None,
            "wo_link_pct": None,
        }
    mapped = sum(1 for r in period_rows if not kpi._is_missing_asset_id(r))
    dated = sum(1 for r in period_rows if _row_has_dates(r))
    wo_linked = sum(1 for r in period_rows if _row_wo_id(r))

    asset_pct = round(mapped / total * 100, 1)
    date_pct = round(dated / total * 100, 1)
    wo_pct = round(wo_linked / total * 100, 1)

    strong = sum(1 for p in (asset_pct, date_pct, wo_pct) if p >= 80.0)
    medium = sum(1 for p in (asset_pct, date_pct, wo_pct) if p >= 60.0)

    if strong >= 3:
        band, tone = "High", "good"
    elif medium >= 2:
        band, tone = "Medium", "watch"
    else:
        band, tone = "Low", "critical"

    label = (
        f"{band} — based on {total} MR, "
        f"{asset_pct}% asset-mapped, "
        f"{date_pct}% complete dates, "
        f"{wo_pct}% WO-linked."
    )
    return {
        "band": band, "tone": tone, "label": label, "total": total,
        "asset_mapping_pct": asset_pct,
        "date_completeness_pct": date_pct,
        "wo_link_pct": wo_pct,
    }


# ── Top-5 INDIVIDUAL UNITS per category (spec §2D / §3) ─────────────────────────
# This replaces _build_top_machines().  The unit of analysis is one physical asset
# (e.g. "Bratt pan No.3").  No area-level rows.  No machine_group column.

def _build_top_units(
    category_name: str,
    unit_groups: dict[str, list],          # unit_name → [row, ...]
    unit_asset_ids: dict[str, str],        # unit_name → canonical asset_id
    prior_by_unit: dict[str, int],
    inventory_lookup: dict,
    unit_parts_context_getter,
    today: date,
    top_n: int = 5,
) -> list[dict]:
    scored: list[dict] = []

    for unit_name, rows in unit_groups.items():
        if len(rows) < _MIN_MACHINE_MRS:
            continue

        asset_id = unit_asset_ids.get(unit_name) or ""
        recurrence = len(rows)

        valid_dates = [d for d in (_row_latest_date(r) for r in rows) if d]
        last_date = max(valid_dates) if valid_dates else None
        days_since = (today - last_date).days if last_date else 999
        r_factor = _recency_factor(days_since)

        # ── Issue cluster classification ──────────────────────────────────────
        issue_rows = [
            (row, classify_specific_issue(_row_issue_text(row) or _row_description(row)))
            for row in rows
        ]
        issue_counter = Counter(iss for _, iss in issue_rows)
        recent_issue_rows = sorted(
            issue_rows, key=lambda p: (_row_latest_date(p[0]) or date.min), reverse=True
        )
        named_scores = []
        for issue, count in issue_counter.items():
            if issue == "Unclassified":
                continue
            cluster = [r for r, ri in issue_rows if ri == issue]
            latest_cd = max((_row_latest_date(r) for r in cluster if _row_latest_date(r)), default=None)
            days_cd = (today - latest_cd).days if latest_cd else 999
            recent_ct = sum(1 for r, ri in recent_issue_rows[:4] if ri == issue)
            open_ct = sum(1 for r in cluster if _row_status_bucket(r) == "open")
            score = count * 5 + recent_ct * 4 + open_ct * 2 + max(0.0, (30 - days_cd) / 10.0)
            named_scores.append((score, issue, count, recent_ct, open_ct, cluster, latest_cd))

        issue_reason = ""
        if named_scores:
            named_scores.sort(key=lambda x: (-x[0], -x[2], str(x[1])))
            _, dominant_issue, dominant_count, recent_cluster_ct, open_cluster_ct, cluster_rows, latest_cluster_date = named_scores[0]
            issue_confidence = "High" if dominant_count >= 3 and recent_cluster_ct >= 2 else "Medium" if dominant_count >= 2 else "Low"
            if dominant_count < 3 and recent_cluster_ct < 2:
                issue_reason = "Recent wording is clustered but trend is still weak."
        else:
            recent_row = max(rows, key=lambda r: (_row_latest_date(r) or date.min))
            dominant_issue = _clean_issue_phrase(_row_issue_text(recent_row) or _row_description(recent_row))
            dominant_count = 1; recent_cluster_ct = 1; open_cluster_ct = 0
            cluster_rows = [recent_row]; latest_cluster_date = _row_latest_date(recent_row)
            issue_confidence = "Low"
            issue_reason = "Generated from the latest description — no repeated cluster strong enough."

        dominant_fault = Counter(
            classify_fault(_row_issue_text(r) or _row_description(r)) for r in rows
        ).most_common(1)[0][0]

        # ── Proof string: auditable, not templated (spec §2B) ─────────────────
        cluster_unit_count = dominant_count
        issue_pct = round(cluster_unit_count / recurrence * 100)
        proof = (
            f"{_keyword_specific_short(dominant_issue)} wording in "
            f"{issue_pct}% of {recurrence} MRs"
        )

        # ── MTBF ─────────────────────────────────────────────────────────────
        mtbf_days, n_intervals, coverage = _compute_mtbf_detail(rows)
        reliable = (
            mtbf_days is not None and mtbf_days >= 1.0
            and _mtbf_is_reliable(n_intervals, coverage)
        )
        mtbf_label = f"~{round(mtbf_days, 1)}d" if reliable else "Insufficient data"

        # ── Cluster recurrence + timing (spec §2C) ────────────────────────────
        c_median, c_avg, c_n_intervals, _, c_gaps = _compute_cluster_recurrence(cluster_rows)
        c_reliable = dominant_count >= 3 and c_n_intervals >= 2 and c_median is not None and c_median >= 1.0

        likely_label, recurrence_band_key = _recurrence_band(c_median, c_n_intervals, dominant_count)
        if c_reliable:
            ref_date = latest_cluster_date or last_date
            _, timing_label = _likely_recurrence(ref_date, c_median, today)
            likely_label = timing_label

        if c_median is not None and days_since < 999:
            raw_next = round(c_median - days_since)
            next_due_days: Optional[int] = max(0, raw_next)
            overdue = raw_next < 0
        else:
            next_due_days = None
            overdue = False

        if len(c_gaps) >= 4:
            n_third = max(1, len(c_gaps) // 3)
            older_med = _median_of(c_gaps[:-n_third])
            recent_med = _median_of(c_gaps[-n_third:])
            interval_trend = (
                "degrading" if recent_med < older_med * 0.85
                else "stabilizing" if recent_med > older_med * 1.15
                else "stable"
            )
        else:
            interval_trend = "stable"

        # ── Severity + criticality (spec §2D) ────────────────────────────────
        sl_values = [v for v in (_row_service_level(r) for r in rows) if v is not None]
        dominant_sl: Optional[int] = Counter(sl_values).most_common(1)[0][0] if sl_values else None
        avg_weight = _median_of([_severity_weight(v) for v in sl_values]) if sl_values else 2.0
        critical_ct = sum(1 for r in rows if _row_is_critical(r))
        is_critical = critical_ct > len(rows) / 2
        criticality_mult = 1.5 if is_critical else 1.0

        symptom_terms = _extract_issue_tokens(cluster_rows, dominant_issue)

        # ── Spare recommendation (spec §2E) ─────────────────────────────────
        # Cluster WO IDs → consumption for those WOs only
        cluster_wo_ids = {_row_wo_id(r) for r in cluster_rows if _row_wo_id(r)}
        parts_context = unit_parts_context_getter(unit_name, cluster_wo_ids)
        spare = _compute_machine_spare(
            unit_name,
            rows,
            dominant_issue,
            parts_context,
            inventory_lookup,
            c_median,
            main_system=category_name,
            symptom_terms=symptom_terms,
        )

        # ── Confidence (spec §2C) ─────────────────────────────────────────────
        consumption_count = len(parts_context.get("sparePartsUsed") or []) if parts_context else 0
        if c_n_intervals >= 6 and consumption_count >= 3:
            confidence = "High"
            confidence_reason = f"{recurrence} MRs, {c_n_intervals} clean intervals, {consumption_count} consumption records."
        elif c_n_intervals >= 3:
            confidence = "Medium"
            confidence_reason = f"{recurrence} MRs, {c_n_intervals} clean intervals (need ≥3 consumption records for High)."
        else:
            confidence = "Low"
            confidence_reason = f"Not enough history — {c_n_intervals} clean interval(s) (need ≥3 for Medium)."
        if issue_confidence == "Low" and confidence != "Low":
            confidence = "Low"
            confidence_reason = "Weak issue cluster; " + confidence_reason

        risk_score = recurrence * r_factor * avg_weight * criticality_mult

        # ── Escalation ────────────────────────────────────────────────────────
        escalation = None
        if dominant_count >= 3 and issue_confidence != "Low":
            escalation = {
                "triggered": True,
                "trigger": dominant_issue,
                "trigger_count": dominant_count,
                "reason": f"'{dominant_issue}' repeated {dominant_count}× for {unit_name}",
                "mr_ids": list(dict.fromkeys(_row_mr_id(r) for r in rows if _row_mr_id(r)))[:12],
                "wo_ids": list(dict.fromkeys(_row_wo_id(r) for r in rows if _row_wo_id(r)))[:8],
            }

        trend = _trend_arrow(recurrence, prior_by_unit.get(unit_name, 0))
        main_observed_issue = _build_main_observed_issue(dominant_issue, symptom_terms)
        likely_cause_candidate = _build_likely_cause_candidate(dominant_issue, spare.get("parts") or [])
        evidence_summary = _format_issue_evidence(
            dominant_count, latest_cluster_date, symptom_terms, mtbf_label, open_cluster_ct, dominant_issue
        )

        # ── Recent evidence for drill-down ────────────────────────────────────
        issue_evidence: list[dict] = []
        for r, iss in recent_issue_rows:
            desc = _row_description(r)
            if not desc:
                continue
            ev_date = _row_latest_date(r)
            translated = str(r.get("translated_description") or "").strip()
            issue_evidence.append({
                "mr_id": _row_mr_id(r),
                "wo_id": _row_wo_id(r),
                "date": ev_date.isoformat() if ev_date else None,
                "issue": iss if iss != "Unclassified" else None,
                "description": _clean_issue_phrase(desc, max_words=16),
                "translated_description": _clean_issue_phrase(translated, max_words=16) if translated else None,
                "status": str(r.get("status") or "").strip() or None,
            })
            if len(issue_evidence) >= 6:
                break

        other_common_faults = _build_other_common_faults(
            unit_name,
            rows,
            dominant_issue,
            unit_groups,
            parts_context,
            inventory_lookup,
            category_name,
        )

        scored.append({
            # ── Spec §3 identity ──────────────────────────────────────────────
            "unit": unit_name,
            "asset_id": asset_id,
            "category": category_name,
            "is_critical": is_critical,
            "mr_count": recurrence,
            # ── Issue ─────────────────────────────────────────────────────────
            "issue": {"cluster": dominant_issue, "proof": proof},
            "recurring_issue": dominant_issue,
            "recurring_issue_confidence": issue_confidence,
            "recurring_issue_reason": issue_reason,
            "recurring_issue_fault_family": dominant_fault,
            "main_observed_issue": main_observed_issue,
            "evidence_summary": evidence_summary,
            "likely_cause_candidate": likely_cause_candidate,
            "issue_breakdown": [
                {"issue": iss, "count": cnt}
                for iss, cnt in issue_counter.most_common(5) if iss != "Unclassified"
            ],
            "issue_evidence": issue_evidence,
            "other_common_faults": other_common_faults,
            # ── Timing (spec §3) ──────────────────────────────────────────────
            "timing": {
                "next_due_days": next_due_days,
                "median_gap_days": c_median,
                "days_since_last": days_since if days_since < 999 else None,
                "trend": interval_trend,
                "overdue": overdue,
            },
            # ── Severity (spec §3) ────────────────────────────────────────────
            "severity": {
                "weighted_score": round(risk_score, 2),
                "dominant_service_level": dominant_sl,
            },
            # ── Spare (spec §3) ───────────────────────────────────────────────
            "spare_parts": spare.get("parts", []),
            "spare_parts_to_prepare": spare.get("spare_parts_to_prepare") or spare.get("parts", []),
            "spare_lead": spare.get("spare_lead"),
            "spare_kit": spare.get("spare_kit", []),
            "spare_available": spare.get("available", False),
            "suggested_spare_parts": spare.get("parts", []),
            "suggested_spare": spare.get("suggested_inline"),
            "stock_status": spare.get("overall_stock_status") or "Unknown",
            "spare_recommendation_basis": spare.get("basis") or _NO_CONFIRMED_SPARE,
            "spare_linked_wo_count": spare.get("linked_wo_count", 0),
            "spare_linked_transaction_count": spare.get("linked_transaction_count", 0),
            # ── Confidence (spec §2C / §3) ────────────────────────────────────
            "confidence": confidence,
            "confidence_reason": confidence_reason,
            # ── Internals ─────────────────────────────────────────────────────
            "last_occurrence": last_date.isoformat() if last_date else None,
            "days_since_last": days_since if days_since < 999 else None,
            "dominant_count": dominant_count,
            "cluster_last_occurrence": latest_cluster_date.isoformat() if latest_cluster_date else None,
            "mtbf_days": round(mtbf_days, 1) if (reliable and mtbf_days) else None,
            "mtbf_label": mtbf_label,
            "mtbf_reliable": reliable,
            "recurrence_interval_days": c_median,
            "recurrence_interval_avg_days": c_avg,
            "recurrence_interval_n": c_n_intervals,
            "recurrence_gauge": likely_label,
            "recurrence_band": recurrence_band_key,
            "likely_recurrence_label": likely_label,
            "symptom_keywords": symptom_terms,
            "escalation": escalation,
            "trend": trend,
            "prior_count": prior_by_unit.get(unit_name, 0),
            "risk_score": round(risk_score, 2),
        })

    scored.sort(key=lambda x: -x["risk_score"])
    return [dict(m, rank=i + 1) for i, m in enumerate(scored[:top_n])]


def _keyword_specific_short(issue_label: str) -> str:
    """Return a short auditable string for the proof field."""
    short = (
        issue_label
        .removesuffix(" Fault").removesuffix(" Issue")
        .removesuffix(" Problem").removesuffix(" Leakage")
        .strip()
    )
    return short or issue_label


# ── Main builder ─────────────────────────────────────────────────────────────────

def build_predictive_insights(filters: dict) -> dict:
    """Build predictive insights: category → top-5 machine groups.

    All data flows from already-memoised kpi_query_service builders.
    """
    f = ctx.normalize_filters(filters)
    key = ("predictive_v2", f["stage"], f["year"], f["month"], f.get("period_mode"))
    return _memoized(key, lambda: _build_predictive_insights_inner(f))


def _build_predictive_insights_inner(f: dict) -> dict:
    """Unit-level predictive pipeline (spec §2A–§2E).

    Aggregation unit = individual physical asset (e.g. "Bratt pan No.3").
    Area-bucket MRs are resolved via qwen / keyword extraction.
    MRs that cannot be attributed to a specific unit go to the Facility bucket
    and are NEVER ranked.  The output never contains "machine_group" or
    "Area-level MR / machine not specified" rows.
    """
    window = ctx.resolved_window(f)
    today = window["end_date"]

    all_rows = kpi._filtered_work_order_rows(f)
    period_rows = kpi._selected_period_work_order_rows(f, all_rows)

    if not period_rows:
        return {
            "period": window["label"],
            "total_mrs": 0,
            "empty": True,
            "categories": [],
            "fault_pattern": None,
            "data_confidence": _compute_data_confidence([]),
        }

    prior_start, prior_end = _prior_period(window)
    prior_rows = [r for r in all_rows if _row_in_range(r, prior_start, prior_end)]

    # ── Spare-parts context: WO-level consumption + Gen PO ───────────────────
    # _wo_spare_lookup[work_order_id] = [consumption txn dicts]
    # _po_spare_lookup[item_code_upper] = [po dicts] (for Gen PO evidence)
    inventory_lookup: dict = {"records": [], "by_code": {}}
    _wo_spare_lookup: dict[str, list[dict]] = {}    # WO ID → consumption
    _po_spare_lookup: dict[str, list[dict]] = {}    # item_code_upper → PO rows
    _po_spare_all: list[dict] = []                  # all spare Gen PO rows for description validation
    try:
        import spare_parts_service as sps
        spare_payload = sps.build_spare_parts_payload()
        inventory_lookup = _build_inventory_lookup(spare_payload)

        # Project transactions → WO-level consumption (spec §2E join)
        pt = sps.build_project_transactions_payload()
        for txn in (pt.get("transactions") or []):
            wo_id = str(txn.get("work_order_id") or "").strip()
            if wo_id:
                _wo_spare_lookup.setdefault(wo_id, []).append({
                    "item_code": txn.get("transaction_id"),
                    "part_name": (
                        txn.get("translated_description")
                        or txn.get("original_description")
                        or ""
                    ),
                    "classification": txn.get("item_category") or "",
                    "quantity": float(txn.get("quantity_used") or 0),
                    "date": str(txn.get("project_date") or ""),
                    "is_direct_match": True,
                    "value": txn.get("unit_cost_estimate"),
                })

        # Gen PO → supporting purchase-history evidence keyed by item code
        try:
            po_records = (spare_payload.get("po_classification") or {}).get("records") or []
            for po_row in po_records:
                if po_row.get("classification") not in {"Stock Spare Part", "Non-Stock Spare Part"}:
                    continue
                raw_stage = str(po_row.get("source_stage") or "").strip()
                stage_label = (
                    "Gen PO Stage 1" if "1" in raw_stage
                    else "Gen PO Stage 2" if "2" in raw_stage
                    else "Gen PO"
                )
                code_key = str(po_row.get("code") or "").strip().upper()
                entry = {
                    "item_code": po_row.get("code"),
                    "part_description": po_row.get("translated_description") or po_row.get("description") or "",
                    "part_name": po_row.get("translated_description") or po_row.get("description") or "",
                    "classification": po_row.get("classification") or "",
                    "quantity": float(po_row.get("quantity_ordered") or 0),
                    "po_date": po_row.get("po_date"),
                    "po_no": po_row.get("po_number"),
                    "vendor": po_row.get("vendor_name") or po_row.get("supplier") or "",
                    "source_stage": stage_label,
                    "machine_detection_confidence": "Medium",
                    "matched_keywords": [],
                    "is_direct_match": False,
                    "value": po_row.get("unit_cost"),
                    "lead_time_days": po_row.get("lead_time_days"),
                    "pd_machine": po_row.get("pd_machine"),
                    "translated_pd_machine": po_row.get("translated_pd_machine"),
                }
                _po_spare_all.append(entry)
                if code_key:
                    _po_spare_lookup.setdefault(code_key, []).append(entry)
        except Exception as po_exc:
            print(f"[predictive] Gen PO lookup failed: {po_exc}")

    except Exception:
        inventory_lookup = {"records": [], "by_code": {}}

    # ── Resolve every MR to a SPECIFIC UNIT (spec §2A) ───────────────────────
    by_cat_unit: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    unit_asset_ids: dict[str, Counter] = {}        # unit_name → Counter of asset_ids
    resolved_count = 0
    facility_count = 0

    for row in period_rows:
        unit_name, asset_id, disp_cat = resolve_to_unit(row)
        if unit_name is None or disp_cat == "Facility / Building":
            facility_count += 1
            continue
        by_cat_unit[disp_cat][unit_name].append(row)
        if unit_name not in unit_asset_ids:
            unit_asset_ids[unit_name] = Counter()
        if asset_id:
            unit_asset_ids[unit_name][asset_id] += 1
        resolved_count += 1

    # canonical asset_id per unit = most frequent
    canonical_asset_id: dict[str, str] = {
        unit: c.most_common(1)[0][0] if c else ""
        for unit, c in unit_asset_ids.items()
    }

    # ── Prior counts per unit ─────────────────────────────────────────────────
    prior_by_unit: Counter[str] = Counter()
    for r in prior_rows:
        unit_name, _, _ = resolve_to_unit(r)
        if unit_name:
            prior_by_unit[unit_name] += 1

    # ── Parts context: WO-level for usage, PO fallback for purchase history ───
    def unit_parts_context_getter(unit_name: str, cluster_wo_ids: set) -> dict:
        # Consumption: rows from the cluster's WO IDs (verified fix kit for this issue)
        usage: list[dict] = []
        for wo_id in cluster_wo_ids:
            usage.extend(_wo_spare_lookup.get(wo_id, []))
        # Fallback: all WOs for the unit if cluster lookup is empty
        if not usage:
            # Pull from the unit's own rows (all WOs, not just cluster)
            pass  # caller already has the cluster_rows; kept empty to avoid false positives

        # Gen PO evidence: items already matched to consumption codes
        purchase: list[dict] = []
        all_purchase: list[dict] = list(_po_spare_all)
        seen_codes = {str(u.get("item_code") or "").strip().upper() for u in usage}
        for code in seen_codes:
            if code:
                purchase.extend(_po_spare_lookup.get(code, []))
        # Also add PO rows whose pd_machine matches the unit name
        unit_lower = unit_name.lower()
        for po_list in _po_spare_lookup.values():
            for po in po_list:
                pdm = str(po.get("translated_pd_machine") or po.get("pd_machine") or "").lower()
                if pdm and unit_lower in pdm or (pdm and pdm in unit_lower):
                    if po not in purchase:
                        purchase.append(po)

        return {"sparePartsUsed": usage, "purchaseParts": purchase, "allPurchaseParts": all_purchase}

    # ── Build top-5 per category ──────────────────────────────────────────────
    categories: list[dict] = []
    for cat_name in _CATEGORY_ORDER:
        unit_groups = by_cat_unit.get(cat_name, {})
        cat_total = sum(len(rows) for rows in unit_groups.values())
        top_units = _build_top_units(
            cat_name,
            unit_groups,
            canonical_asset_id,
            prior_by_unit,
            inventory_lookup,
            unit_parts_context_getter,
            today,
        )
        categories.append({
            "name": cat_name,
            "total_mrs": cat_total,
            "unit_count": len(unit_groups),
            "top_machines": top_units,   # key kept for frontend compat
        })

    # ── Cross-category fault pattern ──────────────────────────────────────────
    all_fault_counts: Counter[str] = Counter()
    fault_by_row: dict[int, str] = {}
    for row in period_rows:
        fam = classify_fault(_row_description(row))
        fault_by_row[id(row)] = fam
        if fam != "Unclassified":
            all_fault_counts[fam] += 1

    fault_pattern = None
    if all_fault_counts:
        top_fault, top_count = all_fault_counts.most_common(1)[0]
        affected_units: list[str] = []
        for cat_name, unit_rows_map in by_cat_unit.items():
            for uname, urows in unit_rows_map.items():
                if any(fault_by_row.get(id(r)) == top_fault for r in urows):
                    affected_units.append(uname)
        fault_pattern = {
            "fault_family": top_fault,
            "count": top_count,
            "pct_of_total": round(top_count / len(period_rows) * 100, 1),
            "affected_groups": affected_units[:5],
        }

    data_conf = _compute_data_confidence(period_rows)
    data_conf["resolved_to_unit"] = resolved_count
    data_conf["facility_count"] = facility_count
    data_conf["resolution_pct"] = round(resolved_count / len(period_rows) * 100, 1) if period_rows else 0.0

    return {
        "period": window["label"],
        "total_mrs": len(period_rows),
        "empty": False,
        "categories": categories,
        "fault_pattern": fault_pattern,
        "data_confidence": data_conf,
    }
