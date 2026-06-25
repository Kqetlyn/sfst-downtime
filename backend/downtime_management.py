import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd

from asset_mapping import (
    load_asset_mapping,
    classify_work_order,
    get_asset_mapping_meta,
    group_to_category,
    MACHINE_GROUPS,
)

CRITICALITY_CRITICAL = "Critical"
CRITICALITY_NON_CRITICAL = "Non-Critical / Facility"
CRITICALITY_ORDER = [CRITICALITY_CRITICAL, CRITICALITY_NON_CRITICAL]
CRITICALITY_RANK = {label: index for index, label in enumerate(CRITICALITY_ORDER, start=1)}
CRITICALITY_RANK["Support Systems"] = CRITICALITY_RANK[CRITICALITY_NON_CRITICAL]
CRITICALITY_RANK["Facility / Non-Critical"] = CRITICALITY_RANK[CRITICALITY_NON_CRITICAL]
CRITICALITY_RANK["Facility/Non-Critical"] = CRITICALITY_RANK[CRITICALITY_NON_CRITICAL]
CRITICALITY_RANK["Non-Critical"] = CRITICALITY_RANK[CRITICALITY_NON_CRITICAL]
CRITICALITY_RANK["Facility"] = CRITICALITY_RANK[CRITICALITY_NON_CRITICAL]
CRITICALITY_RANK["Unclassified"] = CRITICALITY_RANK[CRITICALITY_NON_CRITICAL]
CRITICALITY_RANK["Unmapped"] = CRITICALITY_RANK[CRITICALITY_NON_CRITICAL]
ASSET_ID_PATTERN = re.compile(r"([A-Z]{2,}[A-Z0-9]*-\d+)")
REFRIGERATION_GROUP = "Refrigeration"

HIGH_MTTR_THRESHOLD_HOURS = 48.0
HIGH_DOWNTIME_THRESHOLD_HOURS = 72.0
CRITICAL_HIGH_DOWNTIME_THRESHOLD_HOURS = 120.0
REPEATED_WORK_ORDER_THRESHOLD = 3
LOW_MTBF_THRESHOLD_HOURS = 168.0
HIGH_MTBF_THRESHOLD_HOURS = 720.0
# MTBF gap must exceed this floor (1 minute); ≤ this value is treated as a data
# artefact (duplicate/related WOs stamped within seconds of each other).
MTBF_MIN_GAP_HOURS = 1 / 60
# Gaps shorter than this are valid but suspicious and are flagged in the output.
MTBF_SUSPICIOUS_GAP_HOURS = 1.0
# Asset names / groups that represent physical areas, not specific machines.
# MTBF between area-level WOs is not meaningful.
_MTBF_GENERAL_AREA_RE = re.compile(
    r"\b(risk\s*area|work\s*area|high\s+risk|low\s+risk|medium\s+risk"
    r"|general\s+area|production\s+area)\b",
    re.IGNORECASE,
)

YEAR_START_MONTH = 1
YEAR_START_DAY = 1


def _clean_text(value, fallback=""):
    text = str(value or "").replace("\ufeff", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text or fallback


def _normalize_key(value):
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())



def _normalized_text(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _normalize_asset_id(value):
    return _clean_text(value).upper()


def _normalize_criticality(value):
    cleaned = _clean_text(value)
    normalized = _normalize_key(cleaned)
    if normalized in {"critical", "semicritical", "semicriticalsystem", "productioncritical", "productioncriticalsystem"}:
        return CRITICALITY_CRITICAL
    return CRITICALITY_NON_CRITICAL


def _normalize_display_criticality(value):
    return _normalize_criticality(value)


def _extract_asset_ids(value):
    matches = []
    for match in ASSET_ID_PATTERN.findall(str(value or "").upper()):
        if match not in matches:
            matches.append(match)
    return matches


def _parse_timestamp(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed_dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed_dt.replace(tzinfo=None) if parsed_dt.tzinfo else parsed_dt
    except ValueError:
        pass
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    parsed_dt = parsed.to_pydatetime()
    return parsed_dt.replace(tzinfo=None) if parsed_dt.tzinfo else parsed_dt


def _normalize_status(value):
    return _clean_text(value).lower()


def _is_mtbf_general_area(row: dict) -> bool:
    """True when the row represents a physical area/location rather than a
    specific machine — MTBF between area-level WOs is not meaningful."""
    name_fields = [
        row.get("machine_name") or "",
        row.get("asset_display_name") or "",
        row.get("machine_name_display") or "",
        row.get("machine_group") or "",
        row.get("raw_functional_location") or "",
    ]
    combined = " ".join(name_fields)
    return bool(_MTBF_GENERAL_AREA_RE.search(combined))


def _is_unresolved_status(value):
    return _is_open_work_order_status(value)


def _is_mtbf_eligible_status(value):
    normalized = _normalize_status(value)
    if not normalized:
        return False
    return normalized in {"finished", "completed", "closed", "resolved", "done"}


def _is_open_work_order_status(value):
    normalized = _normalize_status(value)
    return normalized in {"new", "in progress", "inprogress"}


def _infer_criticality(asset_id, machine_name, location, job_trade, description):
    haystack = " ".join([asset_id, machine_name, location, job_trade, description]).lower()
    critical_keywords = [
        "production", "fryer", "oven", "bratt", "bowl cutter", "chopper", "conveyor",
        "steambox", "x-ray", "check weight", "vacuum", "meatball", "strap", "sealer", "sbf",
    ]
    semi_keywords = [
        "water", "cool", "refriger", "evap", "condenc", "condens", "hvac", "boiler", "compressor",
        "pump", "tank", "filter",
    ]
    support_keywords = [
        "lighting", "cctv", "alarm", "distribution board", "transformer", "monitor", "electrical",
        "hood", "lpg", "vaporizer", "uv machine",
    ]
    if any(keyword in haystack for keyword in critical_keywords):
        return CRITICALITY_CRITICAL
    if any(keyword in haystack for keyword in semi_keywords):
        return CRITICALITY_CRITICAL
    if any(keyword in haystack for keyword in support_keywords):
        return CRITICALITY_NON_CRITICAL
    return CRITICALITY_NON_CRITICAL



def _build_fallback_mapping(asset_id, machine_name, location, job_trade, description):
    display_name = _clean_text(machine_name) or _clean_text(asset_id) or "Unmapped Asset"
    normalized_location = _clean_text(location, "Unassigned")
    criticality = _infer_criticality(asset_id, machine_name, location, job_trade, description)
    mapping_status = "Unmapped" if _normalize_asset_id(asset_id) else "Missing Asset ID"
    return {
        "asset_id": _normalize_asset_id(asset_id) or _clean_text(asset_id),
        "machine_group": display_name,
        "machine_name_display": display_name,
        "asset_label": _clean_text(asset_id),
        "asset_display_name": display_name,
        "location": normalized_location,
        "building": normalized_location,
        "mappedStage": mapping_status,
        "mappedAssetName": display_name,
        "mappedMainAssetGroup": display_name,
        "mappedSubAssetGroup": "",
        "mappedLocation": normalized_location,
        "mappedSystemArea": "",
        "mappingStatus": mapping_status,
        "criticality": criticality,
        "raw_criticality": "",
        "criticality_rank": CRITICALITY_RANK.get(criticality, CRITICALITY_RANK["Unmapped"]),
        "mapping_source": "fallback",
        "classification_source": "fallback",
        "has_assetlist_classification": False,
        "group_asset_ids": [_normalize_asset_id(asset_id)] if _normalize_asset_id(asset_id) else [],
    }


def load_grouped_machine_mapping(data_dir):
    """Thin wrapper — delegates to asset_mapping.load_asset_mapping."""
    return load_asset_mapping(data_dir)


def enrich_work_order_records(records, data_dir):
    mapping = load_asset_mapping(data_dir)
    enriched = []
    for row in records or []:
        asset_id = _normalize_asset_id(row.get("asset_id") or row.get("machine_code"))
        machine_name = _clean_text(row.get("raw_machine_name") or row.get("machine_name"))
        location = _clean_text(row.get("raw_location") or row.get("area"), "Unassigned")
        job_trade = _clean_text(row.get("job_trade") or row.get("system"))
        description = _clean_text(row.get("remarks") or row.get("description"))

        # Build a search record that includes all text fields for keyword matching
        search_record = {
            **row,
            "asset_id": asset_id,
            "machine_name": machine_name,
            "description": description,
        }
        classified = classify_work_order(search_record, mapping)

        mapped_from_asset_list = classified.get("mapping_source") == "Asset_Master.xlsx"
        machine_group = classified["machine_group"]
        mapping_status = classified.get("mappingStatus") or classified.get("mapping_status") or ("Mapped" if mapped_from_asset_list else "Unmapped")
        mapped_stage = classified.get("mappedStage") or classified.get("mapped_stage") or mapping_status

        merged = {
            **row,
            "asset_id": asset_id or _clean_text(row.get("asset_id") or row.get("machine_code")),
            "machine_group": machine_group,
            # Shared Production-Equipment / Utilities / Unclassified category (same
            # classifier the Spare-Parts page uses) — drives the downtime category filter.
            "equipment_category": group_to_category(machine_group),
            "machine_name_display": classified["machine_name_display"],
            "asset_label": classified.get("asset_label") or asset_id or _clean_text(row.get("asset_id") or row.get("machine_code")),
            "asset_display_name": classified.get("asset_display_name") or machine_name or classified["machine_name_display"],
            "location": classified["location"],
            "building": classified["building"],
            "mappedStage": mapped_stage,
            "mappedAssetName": classified.get("mappedAssetName") or classified.get("mapped_asset_name") or classified.get("asset_display_name") or machine_name,
            "mappedMainAssetGroup": classified.get("mappedMainAssetGroup") or classified.get("mapped_main_asset_group") or machine_group,
            # Real Asset_Master[Machine Group] column (Bratt Pans, Combi Ovens, Water
            # System, HVAC…). Distinct from machine_group, which holds the Category.
            # Surfaced here so consumers (MIRA predictive) can group by the true
            # machine group instead of falling back to job_trade buckets.
            "mappedMachineGroup": classified.get("mappedMachineGroup") or classified.get("asset_machine_group") or "",
            "asset_machine_group": classified.get("mappedMachineGroup") or classified.get("asset_machine_group") or "",
            "mappedSubAssetGroup": classified.get("mappedSubAssetGroup") or classified.get("mapped_sub_asset_group") or "",
            "mappedLocation": classified.get("mappedLocation") or classified.get("mapped_location") or classified.get("location") or "",
            "mappedSystemArea": classified.get("mappedSystemArea") or classified.get("mapped_system_area") or "",
            "mappingStatus": mapping_status,
            "mapped_stage": mapped_stage,
            "mapped_asset_name": classified.get("mappedAssetName") or classified.get("mapped_asset_name") or classified.get("asset_display_name") or machine_name,
            "mapped_main_asset_group": classified.get("mappedMainAssetGroup") or classified.get("mapped_main_asset_group") or machine_group,
            "mapped_sub_asset_group": classified.get("mappedSubAssetGroup") or classified.get("mapped_sub_asset_group") or "",
            "mapped_location": classified.get("mappedLocation") or classified.get("mapped_location") or classified.get("location") or "",
            "mapped_system_area": classified.get("mappedSystemArea") or classified.get("mapped_system_area") or "",
            "mapping_status": mapping_status,
            "criticality": classified["criticality"],
            "raw_criticality": classified.get("raw_criticality", ""),
            "normalized_criticality": _normalize_display_criticality(classified["criticality"]),
            "criticality_rank": CRITICALITY_RANK.get(classified["criticality"], CRITICALITY_RANK["Unmapped"]),
            "mapping_source": classified["mapping_source"],
            "classification_source": classified["classification_source"],
            "has_assetlist_classification": bool(classified.get("has_assetlist_classification")),
            "has_asset_master_mapping": bool(classified.get("has_asset_master_mapping") or mapping_status == "Mapped"),
            "group_asset_ids": classified.get("group_asset_ids", []),
            "refrigeration_group_match": machine_group == REFRIGERATION_GROUP,
            "ttr_hours": round(float(row.get("duration_hours") or 0), 3) if row.get("duration_hours") is not None else None,
            "request_state": _clean_text(row.get("status")),
            "description": description,
            "job_trade": job_trade,
            "is_open": _is_open_work_order_status(row.get("status")),
            "latest_event_time": row.get("end_time") or row.get("actual_end_time") or row.get("maintenance_end_time") or row.get("request_created_time") or row.get("start_time"),
            "actual_start_time": row.get("actual_start_time") or row.get("maintenance_start_time"),
            "actual_end_time": row.get("actual_end_time") or row.get("maintenance_end_time"),
        }
        merged["machine_name"] = merged["machine_name_display"]
        merged["area"] = merged["location"]
        enriched.append(merged)
    return enriched


def _build_alert_flags(total_hours, mttr_hours, work_order_count, criticality, open_count):
    flags = []
    if total_hours >= HIGH_DOWNTIME_THRESHOLD_HOURS:
        flags.append("High TTR logged")
    if mttr_hours is not None and mttr_hours >= HIGH_MTTR_THRESHOLD_HOURS:
        flags.append("High MTTR")
    if work_order_count >= REPEATED_WORK_ORDER_THRESHOLD:
        flags.append("Repeated work orders")
    if criticality == "Critical" and open_count > 0:
        flags.append("Open critical issue")
    return flags


def _build_status_flag(total_hours, mttr_hours, work_order_count, criticality, open_count):
    if criticality == "Critical" and open_count > 0:
        return "critical"
    if total_hours >= CRITICAL_HIGH_DOWNTIME_THRESHOLD_HOURS:
        return "critical"
    if mttr_hours is not None and mttr_hours >= HIGH_MTTR_THRESHOLD_HOURS:
        return "warning"
    if work_order_count >= REPEATED_WORK_ORDER_THRESHOLD or total_hours >= HIGH_DOWNTIME_THRESHOLD_HOURS:
        return "warning"
    return "stable"


def _format_hours(hours):
    if hours is None:
        return None
    if hours <= 0:
        return "0 min"
    if hours < 1:
        return f"{round(hours * 60)} min"
    whole = int(hours)
    minutes = round((hours - whole) * 60)
    if minutes == 60:
        return f"{whole + 1} hr"
    if minutes > 0:
        return f"{whole} hr {minutes} min"
    return f"{whole} hr"


def _resolve_year_floor(period_start, period_end):
    reference = period_start or period_end or datetime.now()
    return reference.replace(month=YEAR_START_MONTH, day=YEAR_START_DAY, hour=0, minute=0, second=0, microsecond=0)


def _calculate_bounded_hours(row, floor_start, period_end=None):
    original_hours = float(row.get("ttr_hours") or row.get("duration_hours") or 0)
    if original_hours <= 0:
        return 0.0

    actual_start = _parse_timestamp(row.get("actual_start_time") or row.get("start_time"))
    actual_end = _parse_timestamp(row.get("actual_end_time") or row.get("end_time"))
    if actual_end is None:
        return round(original_hours, 3)
    if actual_start is None:
        actual_start = actual_end - timedelta(hours=original_hours)

    bounded_start = max(actual_start, floor_start) if floor_start else actual_start
    bounded_end = min(actual_end, period_end) if period_end else actual_end
    bounded_duration = (bounded_end - bounded_start).total_seconds() / 3600
    if bounded_duration <= 0:
        return 0.0
    return round(min(original_hours, bounded_duration), 3)


def _get_work_order_start(row):
    return _parse_timestamp(row.get("actual_start_time") or row.get("maintenance_start_time") or row.get("start_time"))


def _get_work_order_end(row):
    return _parse_timestamp(row.get("actual_end_time") or row.get("maintenance_end_time") or row.get("end_time"))


def _is_missing_text(value):
    return not _clean_text(value)


def _get_ttr_value(row):
    raw_ttr = row.get("ttr_hours") if row.get("ttr_hours") is not None else row.get("duration_hours")
    return raw_ttr, pd.to_numeric(raw_ttr, errors="coerce")


def _build_mttr_missing_reasons(row):
    reasons = []
    raw_ttr, ttr_value = _get_ttr_value(row)
    open_status = _is_open_work_order_status(row.get("request_state"))
    if raw_ttr is None or pd.isna(ttr_value):
        if not open_status:
            reasons.append("Missing or invalid TTR")
    elif float(ttr_value) < 0:
        reasons.append("Negative TTR")
    elif float(ttr_value) == 0:
        if not open_status:
            reasons.append("Zero TTR")
    return reasons


def _build_mtbf_missing_reasons(row, duplicate_work_order_ids=None):
    duplicate_work_order_ids = duplicate_work_order_ids or set()
    reasons = []
    start_time = _get_work_order_start(row)
    end_time = _get_work_order_end(row)
    work_order_id = _clean_text(row.get("work_order_id"))
    open_status = _is_open_work_order_status(row.get("request_state"))

    if _is_missing_text(row.get("asset_id")):
        reasons.append("Missing Asset ID")
    if start_time is None:
        reasons.append("Missing start/request date")
    if end_time is None and not open_status:
        reasons.append("Missing end date")
    elif start_time and end_time and end_time < start_time:
        reasons.append("End date before start date")
    elif start_time and end_time and end_time == start_time:
        reasons.append("Start and end date are the same")
    if work_order_id and work_order_id in duplicate_work_order_ids:
        reasons.append("Duplicate Work Order ID")
    if not _is_mtbf_eligible_status(row.get("request_state")):
        reasons.append("Open or non-final work order status")
    return reasons


def _build_attention_reasons(row, duplicate_work_order_ids=None):
    duplicate_work_order_ids = duplicate_work_order_ids or set()
    reasons = []
    start_time = _get_work_order_start(row)
    end_time = _get_work_order_end(row)
    work_order_id = _clean_text(row.get("work_order_id"))
    raw_ttr, ttr_value = _get_ttr_value(row)
    open_status = _is_open_work_order_status(row.get("request_state"))

    if _is_missing_text(work_order_id):
        reasons.append("Missing Work Order ID")
    if start_time is None:
        reasons.append("Missing start date")
    if end_time is None and not open_status:
        reasons.append("Missing end date")
    if start_time is None:
        reasons.append("Missing request date")
    if _is_missing_text(row.get("asset_id")):
        reasons.append("Missing Asset ID")
    if _is_missing_text(row.get("machine_group") or row.get("machine_name_display")):
        reasons.append("Missing machine group")
    if raw_ttr is None or pd.isna(ttr_value):
        if not open_status:
            reasons.append("Invalid TTR")
    elif float(ttr_value) < 0:
        reasons.append("Negative TTR")
    elif float(ttr_value) == 0:
        if not open_status:
            reasons.append("Invalid TTR")
    if not row.get("has_assetlist_classification") or _is_missing_text(row.get("raw_criticality")):
        reasons.append("Missing classification")
    if _is_missing_text(row.get("location") or row.get("building")) or _clean_text(row.get("location") or row.get("building")).lower() in {"unassigned", "--"}:
        reasons.append("Missing location")
    if work_order_id and work_order_id in duplicate_work_order_ids:
        reasons.append("Duplicate Work Order ID")
    if start_time and end_time and end_time < start_time:
        reasons.append("End date before start date")

    deduped = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


def _build_reliability_badges(status_flag, has_attention):
    badges = []
    if status_flag == "critical":
        badges.append({"label": "CRITICAL", "level": "critical"})
    elif status_flag == "warning":
        badges.append({"label": "WARNING", "level": "warning"})
    elif not has_attention:
        badges.append({"label": "STABLE", "level": "stable"})
    if has_attention:
        badges.append({"label": "REQUIRES ATTENTION", "level": "requires_attention"})
    return badges


def _build_trend(rows, period_start, period_end):
    valid_rows = []
    for row in rows or []:
        timestamp = _parse_timestamp(row.get("end_time") or row.get("start_time"))
        if timestamp is None:
            continue
        valid_rows.append((timestamp, row))

    if not valid_rows:
        return {"labels": [], "downtime_hours": [], "work_order_counts": [], "bucket_mode": "day"}

    day_span = max((period_end - period_start).days, 0)
    if day_span >= 120:
        bucket_mode = "month"
        bucket_format = "%b %Y"
        bucket_key = lambda dt: datetime(dt.year, dt.month, 1)
    elif day_span >= 45:
        bucket_mode = "week"
        bucket_format = "%d %b"
        bucket_key = lambda dt: dt - pd.to_timedelta(dt.weekday(), unit="D")
    else:
        bucket_mode = "day"
        bucket_format = "%d %b"
        bucket_key = lambda dt: datetime(dt.year, dt.month, dt.day)

    buckets = defaultdict(lambda: {"hours": 0.0, "count": 0})
    for timestamp, row in valid_rows:
        key = bucket_key(timestamp)
        buckets[key]["hours"] += float(row.get("effective_ttr_hours") or row.get("ttr_hours") or row.get("duration_hours") or 0)
        buckets[key]["count"] += 1

    ordered_keys = sorted(buckets)
    return {
        "labels": [key.strftime(bucket_format) for key in ordered_keys],
        "downtime_hours": [round(buckets[key]["hours"], 3) for key in ordered_keys],
        "work_order_counts": [buckets[key]["count"] for key in ordered_keys],
        "bucket_mode": bucket_mode,
    }


def get_grouped_machine_mapping_meta(data_dir):
    return get_asset_mapping_meta(data_dir)


def _compute_mtbf_payload(rows, scope_label="Selected Period"):
    work_order_ids = [
        _clean_text(row.get("work_order_id"))
        for row in rows or []
        if _clean_text(row.get("work_order_id"))
    ]
    work_order_id_counts = Counter(work_order_ids)
    duplicate_work_order_ids = {work_order_id for work_order_id, count in work_order_id_counts.items() if count > 1}
    seen_work_order_ids = set()
    eligible_rows = []
    for row in rows:
        work_order_id = _clean_text(row.get("work_order_id"))
        if work_order_id and work_order_id in seen_work_order_ids:
            continue
        if work_order_id:
            seen_work_order_ids.add(work_order_id)
        actual_start = _get_work_order_start(row)
        actual_end = _get_work_order_end(row)
        if not row.get("asset_id"):
            continue
        if _is_mtbf_general_area(row):   # skip area/location placeholders
            continue
        if actual_start is None or actual_end is None:
            continue
        if actual_end <= actual_start:
            continue
        if not _is_mtbf_eligible_status(row.get("request_state")):
            continue
        if row.get("data_quality_flag") and row.get("data_quality_flag") != "Valid":
            continue
        eligible_rows.append(
            {
                **row,
                "_actual_start": actual_start,
                "_actual_end": actual_end,
            }
        )

    asset_rows = []
    group_rows_map = {}
    criticality_rows_map = {}
    mtbf_points = []

    rows_by_asset = defaultdict(list)
    for row in eligible_rows:
        rows_by_asset[row["asset_id"]].append(row)

    for asset_id, asset_items in rows_by_asset.items():
        asset_items.sort(key=lambda item: item["_actual_start"])
        mtbf_gaps = []
        invalid_gap_count = 0
        for prev_item, next_item in zip(asset_items, asset_items[1:]):
            # Strictly end-to-start: Next Actual Start − Previous Actual End.
            # Both dates are guaranteed present (eligible_rows filter above).
            gap_hours = (next_item["_actual_start"] - prev_item["_actual_end"]).total_seconds() / 3600
            if gap_hours <= 0:
                invalid_gap_count += 1
                continue
            # Exclude artefact gaps (≤ 1 minute) — not a real between-failure interval.
            if gap_hours < MTBF_MIN_GAP_HOURS:
                invalid_gap_count += 1
                continue
            mtbf_gaps.append(
                {
                    "gap_hours": round(gap_hours, 3),
                    "previous_work_order_id": prev_item.get("work_order_id"),
                    "next_work_order_id": next_item.get("work_order_id"),
                    "previous_end_time": (prev_item["_actual_end"].isoformat() if prev_item.get("_actual_end") else prev_item.get("actual_end_time")),
                    "next_start_time": (next_item["_actual_start"].isoformat() if next_item.get("_actual_start") else next_item.get("actual_start_time")),
                }
            )
            mtbf_points.append(
                {
                    "timestamp": next_item["_actual_start"],
                    "gap_hours": round(gap_hours, 3),
                }
            )

        total_ttr_hours = round(sum(float(item.get("effective_ttr_hours") or item.get("ttr_hours") or 0) for item in asset_items), 3)
        work_order_count = len(asset_items)
        average_mttr = round(total_ttr_hours / work_order_count, 3) if work_order_count else None
        average_mtbf = round(sum(item["gap_hours"] for item in mtbf_gaps) / len(mtbf_gaps), 3) if mtbf_gaps else None
        latest_item = max(asset_items, key=lambda item: item["_actual_end"])
        latest_gap = mtbf_gaps[-1]["gap_hours"] if mtbf_gaps else None
        repeated_failures = len(mtbf_gaps)

        if average_mtbf is None:
            reliability_status = "insufficient"
        elif average_mtbf < LOW_MTBF_THRESHOLD_HOURS or repeated_failures >= REPEATED_WORK_ORDER_THRESHOLD:
            reliability_status = "poor"
        elif average_mtbf >= HIGH_MTBF_THRESHOLD_HOURS and work_order_count <= 3:
            reliability_status = "good"
        else:
            reliability_status = "moderate"

        asset_row = {
            "asset_id": asset_id,
            "asset_name": latest_item.get("asset_display_name") or latest_item.get("machine_name") or asset_id,
            "machine_group": latest_item.get("machine_group") or latest_item.get("machine_name_display") or asset_id,
            "criticality": latest_item.get("criticality") or CRITICALITY_NON_CRITICAL,
            "raw_criticality": latest_item["raw_criticality"] if "raw_criticality" in latest_item else latest_item.get("criticality", ""),
            "normalized_criticality": latest_item.get("normalized_criticality") or latest_item.get("criticality") or CRITICALITY_NON_CRITICAL,
            "criticality_rank": latest_item.get("criticality_rank") or CRITICALITY_RANK["Unmapped"],
            "location": latest_item.get("location") or latest_item.get("building") or "Unassigned",
            "work_order_count": work_order_count,
            "average_mttr_hours": average_mttr,
            "average_mtbf_hours": average_mtbf,
            "last_failure_date": latest_item["_actual_end"].isoformat() if latest_item.get("_actual_end") else latest_item.get("actual_end_time"),
            "next_failure_gap_hours": latest_gap,
            "valid_mtbf_gap_count": len(mtbf_gaps),
            "invalid_mtbf_gap_count": invalid_gap_count,
            "reliability_status": reliability_status,
            "status_badge": {
                "poor": "critical",
                "moderate": "warning",
                "good": "ok",
                "insufficient": "offline",
            }.get(reliability_status, "offline"),
            "insight": (
                "Insufficient repeat work order data"
                if average_mtbf is None
                else (
                    "Lower reliability with repeated failures"
                    if reliability_status == "poor"
                    else ("Stable operating interval" if reliability_status == "good" else "Monitor repeat repair pattern")
                )
            ),
        }
        asset_rows.append(asset_row)

        group_key = f"{asset_row['machine_group']}__{asset_row['location']}"
        group_row = group_rows_map.setdefault(
            group_key,
            {
                "machine_group": asset_row["machine_group"],
                "location": asset_row["location"],
                "criticality": asset_row["criticality"],
                "criticality_rank": asset_row["criticality_rank"],
                "asset_count": 0,
                "work_order_count": 0,
                "total_mttr_hours": 0.0,
                "total_mtbf_hours": 0.0,
                "valid_mtbf_asset_count": 0,
            },
        )
        group_row["asset_count"] += 1
        group_row["work_order_count"] += asset_row["work_order_count"]
        group_row["total_mttr_hours"] += float(asset_row["average_mttr_hours"] or 0)
        if asset_row["average_mtbf_hours"] is not None:
            group_row["total_mtbf_hours"] += float(asset_row["average_mtbf_hours"])
            group_row["valid_mtbf_asset_count"] += 1

        crit_row = criticality_rows_map.setdefault(
            asset_row["criticality"],
            {
                "criticality": asset_row["criticality"],
                "criticality_rank": asset_row["criticality_rank"],
                "asset_count": 0,
                "work_order_count": 0,
                "total_mttr_hours": 0.0,
                "total_mtbf_hours": 0.0,
                "valid_mtbf_asset_count": 0,
            },
        )
        crit_row["asset_count"] += 1
        crit_row["work_order_count"] += asset_row["work_order_count"]
        crit_row["total_mttr_hours"] += float(asset_row["average_mttr_hours"] or 0)
        if asset_row["average_mtbf_hours"] is not None:
            crit_row["total_mtbf_hours"] += float(asset_row["average_mtbf_hours"])
            crit_row["valid_mtbf_asset_count"] += 1

    asset_rows.sort(
        key=lambda item: (
            item["criticality_rank"],
            float(item["average_mtbf_hours"]) if item["average_mtbf_hours"] is not None else float("inf"),
            -float(item["work_order_count"] or 0),
            item["asset_id"],
        )
    )

    group_rows = []
    for row in group_rows_map.values():
        row["average_mttr_hours"] = round(row["total_mttr_hours"] / row["asset_count"], 3) if row["asset_count"] else None
        row["average_mtbf_hours"] = round(row["total_mtbf_hours"] / row["valid_mtbf_asset_count"], 3) if row["valid_mtbf_asset_count"] else None
        group_rows.append(row)
    group_rows.sort(key=lambda item: (item["criticality_rank"], float(item["average_mtbf_hours"]) if item["average_mtbf_hours"] is not None else float("inf"), item["machine_group"]))

    criticality_rows = []
    for row in criticality_rows_map.values():
        row["average_mttr_hours"] = round(row["total_mttr_hours"] / row["asset_count"], 3) if row["asset_count"] else None
        row["average_mtbf_hours"] = round(row["total_mtbf_hours"] / row["valid_mtbf_asset_count"], 3) if row["valid_mtbf_asset_count"] else None
        criticality_rows.append(row)
    criticality_rows.sort(key=lambda item: item["criticality_rank"])

    mtbf_values = [row["average_mtbf_hours"] for row in asset_rows if row["average_mtbf_hours"] is not None]
    overall_average_mtbf = round(sum(mtbf_values) / len(mtbf_values), 3) if mtbf_values else None
    lowest_mtbf_asset = min((row for row in asset_rows if row["average_mtbf_hours"] is not None), key=lambda item: item["average_mtbf_hours"], default=None)
    highest_mtbf_asset = max((row for row in asset_rows if row["average_mtbf_hours"] is not None), key=lambda item: item["average_mtbf_hours"], default=None)
    repeated_failure_assets = [row for row in asset_rows if row["valid_mtbf_gap_count"] >= 1 and row["work_order_count"] >= REPEATED_WORK_ORDER_THRESHOLD]

    mtbf_points.sort(key=lambda item: item["timestamp"])
    trend = {"labels": [], "mtbf_hours": [], "pair_counts": [], "bucket_mode": "day"}
    if mtbf_points:
        overall_start = mtbf_points[0]["timestamp"]
        overall_end = mtbf_points[-1]["timestamp"]
        day_span = max((overall_end - overall_start).days, 0)
        if day_span >= 120:
            bucket_mode = "month"
            bucket_format = "%b %Y"
            bucket_key = lambda dt: datetime(dt.year, dt.month, 1)
        elif day_span >= 45:
            bucket_mode = "week"
            bucket_format = "%d %b"
            bucket_key = lambda dt: dt - pd.to_timedelta(dt.weekday(), unit="D")
        else:
            bucket_mode = "day"
            bucket_format = "%d %b"
            bucket_key = lambda dt: datetime(dt.year, dt.month, dt.day)

        buckets = defaultdict(lambda: {"hours": 0.0, "count": 0})
        for point in mtbf_points:
            key = bucket_key(point["timestamp"])
            buckets[key]["hours"] += float(point["gap_hours"])
            buckets[key]["count"] += 1
        ordered_keys = sorted(buckets)
        trend = {
            "labels": [key.strftime(bucket_format) for key in ordered_keys],
            "mtbf_hours": [round(buckets[key]["hours"] / buckets[key]["count"], 3) if buckets[key]["count"] else 0 for key in ordered_keys],
            "pair_counts": [buckets[key]["count"] for key in ordered_keys],
            "bucket_mode": bucket_mode,
        }

    return {
        "summary": {
            "scope_label": scope_label,
            "overall_average_mtbf_hours": overall_average_mtbf,
            "lowest_mtbf_asset_id": lowest_mtbf_asset["asset_id"] if lowest_mtbf_asset else None,
            "lowest_mtbf_asset_name": lowest_mtbf_asset["asset_name"] if lowest_mtbf_asset else None,
            "lowest_mtbf_hours": lowest_mtbf_asset["average_mtbf_hours"] if lowest_mtbf_asset else None,
            "highest_mtbf_asset_id": highest_mtbf_asset["asset_id"] if highest_mtbf_asset else None,
            "highest_mtbf_asset_name": highest_mtbf_asset["asset_name"] if highest_mtbf_asset else None,
            "highest_mtbf_hours": highest_mtbf_asset["average_mtbf_hours"] if highest_mtbf_asset else None,
            "repeated_failure_assets": len(repeated_failure_assets),
            "assets_with_valid_mtbf": len(mtbf_values),
            "duplicate_work_order_count": len(duplicate_work_order_ids),
        },
        "criticality_rows": criticality_rows,
        "machine_group_rows": group_rows,
        "asset_rows": asset_rows,
        "trend": trend,
    }


def _build_utilities_group_row(status_events):
    if not status_events:
        return None

    asset_rows_map = {}
    total_hours = 0.0
    latest_event_time = None
    locations = set()

    for event in status_events:
        duration_hours = float(event.get("duration_hours") or 0)
        if duration_hours <= 0:
            continue
        total_hours += duration_hours
        machine_code = _clean_text(event.get("machine_code"), "UTILITY")
        machine_name = _clean_text(event.get("machine_name"), machine_code)
        location = _clean_text(event.get("area"), "Utilities")
        locations.add(location)
        event_end = _parse_timestamp(event.get("end_time") or event.get("start_time"))
        if event_end and (latest_event_time is None or event_end > latest_event_time):
            latest_event_time = event_end

        asset_row = asset_rows_map.setdefault(
            machine_code,
            {
                "asset_id": machine_code,
                "asset_label": machine_code,
                "asset_display_name": machine_name,
                "work_order_count": 0,
                "total_ttr_hours": 0.0,
                "latest_work_order_time": None,
            },
        )
        asset_row["work_order_count"] += 1
        asset_row["total_ttr_hours"] += duration_hours
        latest_asset_time = _parse_timestamp(asset_row.get("latest_work_order_time"))
        if event_end and (latest_asset_time is None or event_end > latest_asset_time):
            asset_row["latest_work_order_time"] = event_end.isoformat()

    if total_hours <= 0:
        return None

    asset_ttr_rows = []
    for asset_row in asset_rows_map.values():
        asset_row["total_ttr_hours"] = round(asset_row["total_ttr_hours"], 3)
        asset_row["mttr_hours"] = round((asset_row["total_ttr_hours"] / asset_row["work_order_count"]), 3) if asset_row["work_order_count"] else None
        asset_ttr_rows.append(asset_row)
    asset_ttr_rows.sort(key=lambda item: (-float(item["total_ttr_hours"] or 0), -float(item["mttr_hours"] or 0), item["asset_id"]))

    location_label = "Utilities"
    if len(locations) == 1:
        location_label = next(iter(locations))

    row = {
        "criticality": CRITICALITY_NON_CRITICAL,
        "criticality_rank": CRITICALITY_RANK[CRITICALITY_NON_CRITICAL],
        "machine_group": "Utilities",
        "machine_name_display": "Utilities",
        "location": location_label,
        "building": location_label,
        "asset_ids": sorted(asset_rows_map),
        "asset_id_count": len(asset_rows_map),
        "work_order_count": sum(int(asset_row["work_order_count"]) for asset_row in asset_ttr_rows),
        "total_downtime_hours": round(total_hours, 3),
        "mttr_hours": round(total_hours / sum(int(asset_row["work_order_count"]) for asset_row in asset_ttr_rows), 3),
        "latest_work_order_time": latest_event_time.isoformat() if latest_event_time else None,
        "open_work_orders": 0,
        "mapping_source": "status_derived_downtime",
        "asset_ttr_rows": asset_ttr_rows,
    }
    row["alert_flags"] = _build_alert_flags(
        row["total_downtime_hours"],
        row["mttr_hours"],
        row["work_order_count"],
        row["criticality"],
        row["open_work_orders"],
    )
    row["status_flag"] = _build_status_flag(
        row["total_downtime_hours"],
        row["mttr_hours"],
        row["work_order_count"],
        row["criticality"],
        row["open_work_orders"],
    )
    row["reliability_badges"] = _build_reliability_badges(row["status_flag"], False)
    return row


def _select_mtbf_default(selected_payload, rolling_payload, historical_payload):
    for key, payload in (
        ("rolling_12_month", rolling_payload),
        ("selected_period", selected_payload),
        ("historical", historical_payload),
    ):
        if (payload.get("summary") or {}).get("assets_with_valid_mtbf"):
            return key, payload
    return "insufficient", selected_payload


def _build_mtbf_views(selected_rows, historical_rows, period_start, period_end):
    selected_payload = _compute_mtbf_payload(selected_rows or [], "Selected Period")
    rolling_start = (period_end or datetime.now()) - timedelta(days=365)
    rolling_end = period_end or datetime.now()
    rolling_rows = []
    for row in historical_rows or []:
        event_time = _get_work_order_start(row) or _get_work_order_end(row)
        if event_time and rolling_start <= event_time <= rolling_end:
            rolling_rows.append(row)
    rolling_payload = _compute_mtbf_payload(rolling_rows, "Rolling 12 Months")
    historical_payload = _compute_mtbf_payload(historical_rows or [], "Historical / All-Time")
    default_key, default_payload = _select_mtbf_default(selected_payload, rolling_payload, historical_payload)
    default_payload = {
        **default_payload,
        "selected_view": default_key,
        "views": {
            "selected_period": selected_payload,
            "rolling_12_month": rolling_payload,
            "historical": historical_payload,
        },
    }
    default_payload["summary"] = {
        **default_payload.get("summary", {}),
        "selected_view": default_key,
    }
    return default_payload


def _build_historical_trend(records):
    buckets = defaultdict(lambda: {
        "year": None,
        "ttr_logged_hours": 0.0,
        "work_order_count": 0,
        "valid_ttr_count": 0,
        "critical_work_order_count": 0,
        "asset_counts": defaultdict(int),
    })

    for row in records or []:
        event_time = _get_work_order_start(row) or _get_work_order_end(row)
        if event_time is None:
            continue
        year = int(event_time.year)
        bucket = buckets[year]
        bucket["year"] = year
        bucket["work_order_count"] += 1
        asset_id = _clean_text(row.get("asset_id"))
        if asset_id:
            bucket["asset_counts"][asset_id] += 1
        ttr_value = pd.to_numeric(row.get("ttr_hours") if row.get("ttr_hours") is not None else row.get("duration_hours"), errors="coerce")
        if pd.notna(ttr_value) and float(ttr_value) > 0:
            bucket["ttr_logged_hours"] += float(ttr_value)
            bucket["valid_ttr_count"] += 1
        if _normalize_display_criticality(row.get("criticality")) == "Critical":
            bucket["critical_work_order_count"] += 1

    rows = []
    for year in sorted(buckets):
        bucket = buckets[year]
        repeated_count = sum(1 for count in bucket["asset_counts"].values() if count >= 2)
        valid_count = bucket["valid_ttr_count"]
        rows.append({
            "year": bucket["year"],
            "ttr_logged_hours": round(bucket["ttr_logged_hours"], 3),
            "work_order_count": bucket["work_order_count"],
            "average_ttr_hours": round(bucket["ttr_logged_hours"] / valid_count, 3) if valid_count else None,
            "repeated_work_order_assets": repeated_count,
            "critical_work_order_count": bucket["critical_work_order_count"],
        })
    return rows


def build_management_downtime_payload(
    records,
    status_events,
    period_start,
    period_end,
    data_dir,
    mtbf_records=None,
    historical_records=None,
    mapping_meta=None,
):
    period_floor = period_start or _resolve_year_floor(period_start, period_end)
    work_order_ids = [
        _clean_text(row.get("work_order_id"))
        for row in records or []
        if _clean_text(row.get("work_order_id"))
    ]
    work_order_id_counts = Counter(work_order_ids)
    duplicate_work_order_ids = {work_order_id for work_order_id, count in work_order_id_counts.items() if count > 1}
    rows = []
    for row in records or []:
        ttr_hours = pd.to_numeric(row.get("ttr_hours") if row.get("ttr_hours") is not None else row.get("duration_hours"), errors="coerce")
        raw_criticality = row["raw_criticality"] if "raw_criticality" in row else row.get("criticality", "")
        classification_source = row.get("classification_source") or row.get("mapping_source") or ""
        has_assetlist_classification = row.get("has_assetlist_classification")
        if has_assetlist_classification is None:
            has_assetlist_classification = classification_source == "Asset_Master.xlsx"
        mapping_status = row.get("mappingStatus") or row.get("mapping_status") or ("Mapped" if has_assetlist_classification else "Unmapped")
        prepared = {
            **row,
            "raw_criticality": raw_criticality,
            "criticality": _normalize_display_criticality(row.get("criticality")),
            "normalized_criticality": _normalize_display_criticality(row.get("criticality")),
            "classification_source": classification_source,
            "has_assetlist_classification": bool(has_assetlist_classification),
            "has_asset_master_mapping": bool(row.get("has_asset_master_mapping") or mapping_status == "Mapped"),
            "mappingStatus": mapping_status,
            "mapping_status": mapping_status,
            "mappedStage": row.get("mappedStage") or row.get("mapped_stage") or mapping_status,
            "mapped_stage": row.get("mappedStage") or row.get("mapped_stage") or mapping_status,
            "mappedAssetName": row.get("mappedAssetName") or row.get("mapped_asset_name") or row.get("asset_display_name") or row.get("machine_name_display"),
            "mappedMainAssetGroup": row.get("mappedMainAssetGroup") or row.get("mapped_main_asset_group") or row.get("machine_group"),
            "mappedSubAssetGroup": row.get("mappedSubAssetGroup") or row.get("mapped_sub_asset_group") or "",
            "mappedLocation": row.get("mappedLocation") or row.get("mapped_location") or row.get("location") or row.get("building"),
            "mappedSystemArea": row.get("mappedSystemArea") or row.get("mapped_system_area") or "",
        }
        prepared["criticality_rank"] = CRITICALITY_RANK.get(prepared["criticality"], CRITICALITY_RANK["Unmapped"])
        if pd.notna(ttr_hours) and float(ttr_hours) > 0:
            prepared["ttr_hours"] = round(float(ttr_hours), 3)
            prepared["valid_ttr"] = True
            prepared["effective_ttr_hours"] = _calculate_bounded_hours(prepared, period_floor, period_end)
        else:
            prepared["ttr_hours"] = None if pd.isna(ttr_hours) else round(float(ttr_hours), 3)
            prepared["valid_ttr"] = False
            prepared["effective_ttr_hours"] = 0.0
        prepared["mttr_missing_reasons"] = _build_mttr_missing_reasons(prepared)
        prepared["mtbf_missing_reasons"] = _build_mtbf_missing_reasons(prepared, duplicate_work_order_ids)
        prepared["attention_reasons"] = _build_attention_reasons(prepared, duplicate_work_order_ids)
        prepared["requires_attention"] = bool(prepared["attention_reasons"])
        rows.append(prepared)

    valid_rows = [row for row in rows if row.get("valid_ttr")]
    total_hours = round(sum(float(row["effective_ttr_hours"]) for row in valid_rows), 3) if valid_rows else 0.0
    work_order_count = len(rows)
    valid_ttr_count = len(valid_rows)
    invalid_ttr_count = work_order_count - valid_ttr_count
    attention_record_count = sum(1 for row in rows if row.get("requires_attention"))
    overall_mttr = round(total_hours / valid_ttr_count, 3) if valid_ttr_count else None

    criticality_totals = {
        label: {"criticality": label, "criticality_rank": CRITICALITY_RANK[label], "total_downtime_hours": 0.0, "work_order_count": 0, "open_work_orders": 0}
        for label in CRITICALITY_ORDER
    }
    machine_group_map = {}
    location_map = {}

    for row in rows:
        criticality = row.get("criticality") or CRITICALITY_NON_CRITICAL
        criticality_row = criticality_totals.setdefault(
            criticality,
            {"criticality": criticality, "criticality_rank": CRITICALITY_RANK.get(criticality, CRITICALITY_RANK["Unmapped"]), "total_downtime_hours": 0.0, "work_order_count": 0, "open_work_orders": 0},
        )
        effective_hours = float(row.get("effective_ttr_hours") or 0)
        criticality_row["total_downtime_hours"] += effective_hours
        criticality_row["work_order_count"] += 1
        criticality_row["valid_ttr_count"] = criticality_row.get("valid_ttr_count", 0) + (1 if row.get("valid_ttr") else 0)
        criticality_row["requires_attention_count"] = criticality_row.get("requires_attention_count", 0) + (1 if row.get("requires_attention") else 0)
        criticality_row["open_work_orders"] += 1 if row.get("is_open") else 0

        location = row.get("location") or row.get("building") or "Unassigned"
        location_key = _clean_text(location, "Unassigned")
        location_row = location_map.setdefault(
            location_key,
            {"location": location_key, "building": location_key, "total_downtime_hours": 0.0, "work_order_count": 0, "mttr_hours": None},
        )
        location_row["total_downtime_hours"] += effective_hours
        location_row["work_order_count"] += 1

        group_name = row.get("machine_group") or row.get("machine_name_display") or row.get("asset_id") or "Unmapped Asset"
        group_key = f"{group_name}__{location_key}"
        group_row = machine_group_map.setdefault(
            group_key,
            {
                "criticality": criticality,
                "criticality_rank": CRITICALITY_RANK.get(criticality, CRITICALITY_RANK["Unmapped"]),
                "machine_group": group_name,
                "machine_name_display": row.get("machine_name_display") or group_name,
                "location": location_key,
                "building": location_key,
                "asset_ids": set(),
                "work_order_count": 0,
                "valid_ttr_count": 0,
                "invalid_ttr_count": 0,
                "total_downtime_hours": 0.0,
                "mttr_hours": None,
                "latest_work_order_time": None,
                "open_work_orders": 0,
                "mapping_source": row.get("mapping_source"),
                "asset_ttr_map": {},
                "attention_reasons": set(),
                "requires_attention_count": 0,
                "mttr_missing_count": 0,
                "mttr_missing_reasons": set(),
                "mtbf_missing_count": 0,
                "mtbf_missing_reasons": set(),
                "priority_values": set(),
            },
        )
        if row.get("requires_attention"):
            group_row["requires_attention_count"] += 1
            group_row["attention_reasons"].update(row.get("attention_reasons") or [])
        if row.get("mttr_missing_reasons"):
            group_row["mttr_missing_count"] += 1
            group_row["mttr_missing_reasons"].update(row.get("mttr_missing_reasons") or [])
        if row.get("mtbf_missing_reasons"):
            group_row["mtbf_missing_count"] += 1
            group_row["mtbf_missing_reasons"].update(row.get("mtbf_missing_reasons") or [])
        if row.get("asset_id"):
            group_row["asset_ids"].add(row["asset_id"])
            asset_row = group_row["asset_ttr_map"].setdefault(
                row["asset_id"],
                {
                    "asset_id": row["asset_id"],
                    "asset_label": row.get("asset_label") or row["asset_id"],
                    "asset_display_name": row.get("asset_display_name") or row.get("raw_machine_name") or row.get("machine_name_display") or row["asset_id"],
                    "work_order_count": 0,
                    "valid_ttr_count": 0,
                    "total_ttr_hours": 0.0,
                    "latest_work_order_time": None,
                    "mttr_missing_count": 0,
                    "mttr_missing_reasons": set(),
                    "mtbf_missing_count": 0,
                    "mtbf_missing_reasons": set(),
                },
            )
            asset_row["work_order_count"] += 1
            if row.get("valid_ttr"):
                asset_row["valid_ttr_count"] += 1
                asset_row["total_ttr_hours"] += float(row["ttr_hours"])
            if row.get("mttr_missing_reasons"):
                asset_row["mttr_missing_count"] += 1
                asset_row["mttr_missing_reasons"].update(row.get("mttr_missing_reasons") or [])
            if row.get("mtbf_missing_reasons"):
                asset_row["mtbf_missing_count"] += 1
                asset_row["mtbf_missing_reasons"].update(row.get("mtbf_missing_reasons") or [])
            asset_event_time = _parse_timestamp(row.get("latest_event_time"))
            latest_asset_time = _parse_timestamp(asset_row.get("latest_work_order_time"))
            if asset_event_time and (latest_asset_time is None or asset_event_time > latest_asset_time):
                asset_row["latest_work_order_time"] = asset_event_time.isoformat()
        group_row["work_order_count"] += 1
        group_row["valid_ttr_count"] += 1 if row.get("valid_ttr") else 0
        group_row["invalid_ttr_count"] += 0 if row.get("valid_ttr") else 1
        group_row["total_downtime_hours"] += effective_hours
        group_row["open_work_orders"] += 1 if row.get("is_open") else 0
        if row.get("priority") is not None:
            group_row["priority_values"].add(row["priority"])
        event_time = _parse_timestamp(row.get("latest_event_time"))
        latest_existing = _parse_timestamp(group_row.get("latest_work_order_time"))
        if event_time and (latest_existing is None or event_time > latest_existing):
            group_row["latest_work_order_time"] = event_time.isoformat()

    criticality_rows = []
    for row in criticality_totals.values():
        row["total_downtime_hours"] = round(row["total_downtime_hours"], 3)
        row["share_of_total_pct"] = round((row["total_downtime_hours"] / total_hours) * 100, 1) if total_hours else 0.0
        valid_count = row.get("valid_ttr_count", row["work_order_count"])
        row["average_mttr_hours"] = round((row["total_downtime_hours"] / valid_count), 3) if valid_count else None
        criticality_rows.append(row)
    criticality_rows.sort(key=lambda item: (item["criticality_rank"], -item["total_downtime_hours"], -item["work_order_count"]))

    machine_group_rows = []
    for row in machine_group_map.values():
        row["asset_ids"] = sorted(row["asset_ids"])
        row["asset_id_count"] = len(row["asset_ids"])
        row["total_downtime_hours"] = round(row["total_downtime_hours"], 3)
        row["mttr_hours"] = round((row["total_downtime_hours"] / row["valid_ttr_count"]), 3) if row["valid_ttr_count"] else None
        asset_ttr_rows = []
        for asset_row in row["asset_ttr_map"].values():
            asset_row["total_ttr_hours"] = round(asset_row["total_ttr_hours"], 3)
            valid_asset_count = asset_row.get("valid_ttr_count") or 0
            asset_row["mttr_hours"] = round((asset_row["total_ttr_hours"] / valid_asset_count), 3) if valid_asset_count else None
            asset_row["mttr_missing_reasons"] = sorted(asset_row["mttr_missing_reasons"])
            asset_row["mtbf_missing_reasons"] = sorted(asset_row["mtbf_missing_reasons"])
            asset_ttr_rows.append(asset_row)
        asset_ttr_rows.sort(key=lambda item: (-float(item["total_ttr_hours"] or 0), -float(item["mttr_hours"] or 0), item["asset_id"]))
        row["asset_ttr_rows"] = asset_ttr_rows
        del row["asset_ttr_map"]
        row["attention_reasons"] = sorted(row["attention_reasons"])
        row["mttr_missing_reasons"] = sorted(row["mttr_missing_reasons"])
        row["mtbf_missing_reasons"] = sorted(row["mtbf_missing_reasons"])
        row["priority_values"] = sorted(row["priority_values"])
        row["alert_flags"] = _build_alert_flags(
            row["total_downtime_hours"],
            row["mttr_hours"],
            row["work_order_count"],
            row["criticality"],
            row["open_work_orders"],
        )
        row["status_flag"] = _build_status_flag(
            row["total_downtime_hours"],
            row["mttr_hours"],
            row["work_order_count"],
            row["criticality"],
            row["open_work_orders"],
        )
        row["reliability_badges"] = _build_reliability_badges(row["status_flag"], bool(row["attention_reasons"]))
        machine_group_rows.append(row)
    machine_group_rows.sort(
        key=lambda item: (
            item["criticality_rank"],
            -float(item["total_downtime_hours"] or 0),
            -float(item["mttr_hours"] or 0),
            item["machine_group"],
        )
    )

    utilities_row = _build_utilities_group_row(status_events)
    if utilities_row:
        machine_group_rows.append(utilities_row)
        machine_group_rows.sort(
            key=lambda item: (
                item["criticality_rank"],
                -float(item["total_downtime_hours"] or 0),
                -float(item["mttr_hours"] or 0),
                item["machine_group"],
            )
        )

    location_rows = []
    for row in location_map.values():
        row["total_downtime_hours"] = round(row["total_downtime_hours"], 3)
        row["mttr_hours"] = round((row["total_downtime_hours"] / row["work_order_count"]), 3) if row["work_order_count"] else None
        location_rows.append(row)
    location_rows.sort(key=lambda item: (-float(item["total_downtime_hours"] or 0), -float(item["mttr_hours"] or 0), item["location"]))

    highest_mttr_group = max(machine_group_rows, key=lambda item: float(item["mttr_hours"] or 0), default=None)
    highest_downtime_group = max(machine_group_rows, key=lambda item: float(item["total_downtime_hours"] or 0), default=None)
    most_affected_location = max(location_rows, key=lambda item: float(item["total_downtime_hours"] or 0), default=None)

    detailed_rows = []
    for row in sorted(rows, key=lambda item: (_parse_timestamp(item.get("latest_event_time")) or datetime.min), reverse=True):
        asset_id = row.get("asset_id") or ""
        detail = {
            "work_order_id": row.get("work_order_id") or "--",
            "request_id": row.get("maintenance_order_id") or "--",
            "asset_id": asset_id,
            "machine_group": row.get("machine_group") or row.get("machine_name_display") or "--",
            "equipment_category": row.get("equipment_category") or group_to_category(row.get("machine_group")),
            "machine_name": row.get("machine_name_display") or row.get("machine_group") or "--",
            "asset_display_name": row.get("asset_display_name") or row.get("raw_machine_name") or row.get("machine_name_display") or "--",
            "machine_equipment_name": row.get("machine_equipment_name") or row.get("raw_machine_name") or row.get("asset_display_name") or "--",
            "criticality": row.get("criticality") or CRITICALITY_NON_CRITICAL,
            "raw_criticality": row["raw_criticality"] if "raw_criticality" in row else row.get("criticality", ""),
            "normalized_criticality": row.get("normalized_criticality") or row.get("criticality") or CRITICALITY_NON_CRITICAL,
            "criticality_rank": row.get("criticality_rank") or CRITICALITY_RANK["Unmapped"],
            "location": row.get("location") or row.get("building") or "Unassigned",
            "building": row.get("building") or row.get("location") or "Unassigned",
            "ttr_hours": row.get("effective_ttr_hours") if row.get("valid_ttr") else None,
            "original_ttr_hours": row.get("ttr_hours"),
            "ttr_source": row.get("ttr_source") or "",
            "duration_context": row.get("duration_context") or "",
            "request_state": row.get("request_state") or "--",
            "status_flag": "critical" if row.get("criticality") == "Critical" and row.get("is_open") else ("warning" if row.get("is_open") else "ok"),
            "is_open": bool(row.get("is_open")),
            "valid_ttr": bool(row.get("valid_ttr")),
            "requires_attention": bool(row.get("requires_attention")),
            "attention_reasons": row.get("attention_reasons") or [],
            "mttr_missing_reasons": row.get("mttr_missing_reasons") or [],
            "mtbf_missing_reasons": row.get("mtbf_missing_reasons") or [],
            "start_time": row.get("start_time"),
            "end_time": row.get("end_time"),
            "latest_event_time": row.get("latest_event_time"),
            "description": row.get("description_original") or row.get("description") or "",
            "translated_description": row.get("translated_description") or row.get("description_original") or row.get("description") or "",
            "job_trade": row.get("job_trade") or "",
            "maintenance_job_type": row.get("maintenance_job_type") or "",
            "raw_functional_location": row.get("raw_functional_location") or "",
            "refrigeration_group_match": bool(row.get("refrigeration_group_match")),
            "mapping_source": row.get("mapping_source"),
            "classification_source": row.get("classification_source"),
            "has_assetlist_classification": bool(row.get("has_assetlist_classification")),
            "priority": row.get("priority"),
            "service_level": row.get("service_level") or row.get("priority"),
            "request_created_time": row.get("request_created_time"),
            "started_by": row.get("started_by") or "",
            "created_by": row.get("created_by") or "",
            "acknowledgement_status": row.get("acknowledgement_status") or "",
            "data_quality_flag": row.get("data_quality_flag") or ("Valid" if row.get("valid_ttr") else ""),
            "data_quality_flags": row.get("data_quality_flags") or [],
            "valid_mttr_ttr": bool(row.get("valid_mttr_ttr") or row.get("valid_ttr")),
            "status_category": row.get("status_category") or ("Open" if row.get("is_open") else ("Closed" if _is_mtbf_eligible_status(row.get("request_state")) else "Review")),
            "actual_start_time": row.get("actual_start_time") or row.get("maintenance_start_time") or row.get("start_time"),
            "actual_end_time": row.get("actual_end_time") or row.get("maintenance_end_time") or row.get("end_time"),
        }
        detailed_rows.append(detail)

    alerts = []
    if highest_mttr_group and float(highest_mttr_group.get("mttr_hours") or 0) >= HIGH_MTTR_THRESHOLD_HOURS:
        alerts.append(
            {
                "level": "critical",
                "message": f"{highest_mttr_group['machine_group']} has the highest MTTR at {_format_hours(highest_mttr_group['mttr_hours'])}.",
            }
        )
    if most_affected_location and float(most_affected_location.get("total_downtime_hours") or 0) >= HIGH_DOWNTIME_THRESHOLD_HOURS:
        alerts.append(
            {
                "level": "warning",
                "message": f"{most_affected_location['location']} has the highest TTR logged at {_format_hours(most_affected_location['total_downtime_hours'])}.",
            }
        )
    open_critical_count = sum(1 for row in detailed_rows if row["criticality"] == "Critical" and row["is_open"])
    if open_critical_count:
        alerts.append(
            {
                "level": "critical",
                "message": f"{open_critical_count} critical work order(s) are still unresolved in the selected period.",
            }
        )

    filters = {
        "criticalities": [row["criticality"] for row in criticality_rows if row["work_order_count"] > 0],
        "machine_groups": sorted({row["machine_group"] for row in machine_group_rows}),
        "locations": sorted({row["location"] for row in machine_group_rows}),
        "asset_ids": sorted({row["asset_id"] for row in detailed_rows if row["asset_id"]}),
        "statuses": sorted({row["request_state"] for row in detailed_rows if row["request_state"]}),
    }

    mtbf_source_records = mtbf_records if mtbf_records is not None else rows
    historical_source_records = historical_records if historical_records is not None else mtbf_source_records
    mtbf_payload = _build_mtbf_views(rows, historical_source_records, period_start, period_end)
    historical_trend = _build_historical_trend(historical_source_records)

    summary = {
        "total_downtime_hours": total_hours,
        "total_ttr_logged_hours": total_hours,
        "total_work_orders": work_order_count,
        "valid_ttr_work_orders": valid_ttr_count,
        "invalid_missing_ttr_count": invalid_ttr_count,
        "requires_attention_count": attention_record_count,
        "overall_mttr_hours": overall_mttr,
        "critical_downtime_hours": round(sum(row["total_downtime_hours"] for row in criticality_rows if row["criticality"] == "Critical"), 3),
        "non_critical_facility_downtime_hours": round(
            sum(row["total_downtime_hours"] for row in criticality_rows if row["criticality"] == CRITICALITY_NON_CRITICAL),
            3,
        ),
        "open_work_orders": sum(1 for row in detailed_rows if row["is_open"]),
        "highest_mttr_machine_group": highest_mttr_group["machine_group"] if highest_mttr_group else None,
        "highest_mttr_hours": highest_mttr_group["mttr_hours"] if highest_mttr_group else None,
        "highest_mttr_location": highest_mttr_group["location"] if highest_mttr_group else None,
        "highest_downtime_machine_group": highest_downtime_group["machine_group"] if highest_downtime_group else None,
        "highest_downtime_hours": highest_downtime_group["total_downtime_hours"] if highest_downtime_group else None,
        "highest_downtime_location": highest_downtime_group["location"] if highest_downtime_group else None,
        "most_affected_location": most_affected_location["location"] if most_affected_location else None,
        "most_affected_location_hours": most_affected_location["total_downtime_hours"] if most_affected_location else None,
        "critical_machine_groups_with_repeats": sum(
            1 for row in machine_group_rows if row["criticality"] == "Critical" and row["work_order_count"] >= REPEATED_WORK_ORDER_THRESHOLD
        ),
    }

    return {
        "summary": summary,
        "mtbf": mtbf_payload,
        "criticality_rows": criticality_rows,
        "machine_group_rows": machine_group_rows,
        "location_rows": location_rows,
        "trend": _build_trend(detailed_rows, period_start, period_end),
        "work_orders": detailed_rows,
        "filters": filters,
        "alerts": alerts,
        "mapping_meta": mapping_meta if mapping_meta is not None else get_grouped_machine_mapping_meta(data_dir),
        "historical_trend": historical_trend,
    }
