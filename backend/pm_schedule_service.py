"""
Unified Preventive Maintenance (PM) schedule tracking service.

This module sits on top of the existing Stage 1 (utility) and Stage 2 (equipment)
PM schedule loaders in ``maintenance_service`` and normalises both into a single
``pmScheduleTasks`` dataset. Stage is derived purely from ``Asset_Master.xlsx``
(via ``asset_mapping``) — it is never hard-coded per source file.

The single public entry point ``build_pm_schedule_payload`` returns a management
ready payload for the Overview / Equipment / Utilities tabs, filtered by a global
Stage selector (All / Stage 1 / Stage 2).

Completion handling: the source schedules carry no explicit completion column, so
"completed" is INFERRED (a PM whose planned week is already in the past is treated
as done). This matches the existing dashboard behaviour, so PM Compliance % is a
real planning-based number rather than "N/A".
"""

from __future__ import annotations

import calendar
import threading
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from maintenance_service import DATA_DIR, build_equipment_dataset_from_path, build_utility_dataset
from pm_schedule_sources import (
    get_pm_schedule_source_status,
    summarize_pm_schedule_sources,
)
from asset_mapping import load_asset_mapping
from pm_schedule_overrides import apply_overrides_and_autodone
from pm_planner_store import list_manual_tasks
import pm_feed_integration as pm_feed

# ── Functional-location mapping (Asset_Master: Asset Installation + Functional Locations) ──
# The "Asset Installation" sheet only holds D365 functional-location CODES for the
# Stage-2 equipment (ENPD) assets. Every asset, however, has a System/Area on the
# Asset_Master sheet, so we fall back to that (mapped to a zone where possible)
# instead of dumping the utility assets into "Unmapped".
UNMAPPED_FL = "Unmapped Functional Location"


def _zone_code_from_text(text: str) -> str:
    low = (text or "").lower()
    idx = low.find("zone")
    if idx >= 0:
        for ch in low[idx + 4: idx + 8]:
            if ch.isdigit():
                return "ZN" + ch
    return ""


def _attach_functional_location(tasks: list[dict]) -> None:
    """System/Area is the functional location for the PM workload chart — use it
    directly for every task (it is populated for all assets, equipment and utility)."""
    for task in tasks:
        sa = str(task.get("systemArea") or "").strip()
        if sa and sa.lower() not in {"unassigned", "general", "others", ""}:
            task["functionalLocationCode"] = _zone_code_from_text(sa)
            task["functionalLocationName"] = sa
            task["functionalLocationLabel"] = sa
        else:
            task["functionalLocationCode"] = ""
            task["functionalLocationName"] = UNMAPPED_FL
            task["functionalLocationLabel"] = UNMAPPED_FL

# ── Constants ──────────────────────────────────────────────────────────────────
MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

STAGE_VALUES = ("Stage 1", "Stage 2")
EQUIPMENT_GROUPS = {"Production Equipment"}
_PM_METRICS_PAYLOAD_CACHE = {}
# Full PM-page payload cache (the heavy ~13 MB payload). Keyed by request params +
# a signature of every data file it reads, so it is rebuilt only when the data
# actually changes (source workbooks, the D365 feed, the master, or the
# override/planner JSON stores) — not on every request.
_PM_PAGE_PAYLOAD_CACHE = {}


# ── Phase 4: SQL-backed PM data ───────────────────────────────────────────────

def _sql_has_pm_schedule_for_year(year: int) -> bool:
    """Return True if pm_schedule SQL table has at least one row for this year."""
    try:
        import db as _db
        with _db.get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM pm_schedule WHERE planned_year = ?", (year,)
            ).fetchone()[0]
        return count > 0
    except Exception:
        return False


def _sql_pm_row_to_task(row: dict, today: date) -> dict:
    """Reconstruct a full pre-override task dict from a SQL pm_schedule row.

    Date-sensitive booleans (isDone, isOverdue, etc.) are re-derived from
    planned_date so they stay correct regardless of when the row was synced.
    apply_overrides_and_autodone() will further update them from the JSON store.
    """
    planned_date_str = row.get("planned_date")
    mapping_status = row.get("mapping_status") or "Unmapped"
    main_group = row.get("main_asset_group") or "Unmapped"
    frequency = row.get("frequency") or ""
    stage = row.get("stage") or ""
    planned_month = row.get("planned_month")

    schedule_state = _derive_inferred_schedule_state({"scheduled_date": planned_date_str}, today)
    is_done = schedule_state["is_done"]
    is_due_this_month = schedule_state["is_due_this_month"]
    is_due_soon = schedule_state["is_due_soon"]
    is_overdue = schedule_state["is_overdue"]

    if mapping_status in {"Missing Asset ID", "Unmapped"}:
        schedule_status = STATUS_MISSING_MAPPING
    elif not planned_date_str:
        schedule_status = STATUS_MISSING_DATE
    elif stage not in STAGE_VALUES:
        schedule_status = STATUS_NEEDS_REVIEW
    elif is_done:
        schedule_status = STATUS_COMPLETED
    elif is_due_this_month:
        schedule_status = STATUS_DUE_THIS_MONTH
    elif is_due_soon:
        schedule_status = STATUS_DUE_SOON
    elif is_overdue:
        schedule_status = STATUS_OVERDUE
    else:
        schedule_status = STATUS_NOT_DUE

    needs_review = (
        schedule_status in {STATUS_NEEDS_REVIEW, STATUS_MISSING_DATE}
        or stage not in STAGE_VALUES
        or not main_group
        or not frequency
    )

    return {
        "pmTaskId": row.get("pm_task_id") or "",
        "stage": stage,
        "assetId": row.get("asset_id") or "",
        "assetName": row.get("asset_name") or "",
        "mainAssetGroup": main_group,
        "subAssetGroup": row.get("sub_asset_group") or "",
        "systemArea": row.get("system_area") or "Unassigned",
        "location": row.get("location") or "Unassigned",
        "pmDescription": row.get("pm_description") or "",
        "frequency": frequency,
        "plannedYear": row.get("planned_year"),
        "plannedMonth": planned_month,
        "plannedMonthLabel": (MONTH_LABELS[planned_month - 1] if planned_month else ""),
        "plannedQuarter": _quarter_label(planned_month),
        "plannedDate": planned_date_str,
        "plannedDateLabel": row.get("planned_date_label"),
        "contractorOrPIC": row.get("contractor_pic") or "",
        "provider": "",
        "scheduleStatus": schedule_status,
        "completionStatus": "Completed (inferred)" if is_done else "Open",
        "actualCompletionDate": None,
        "daysOverdue": schedule_state["days_overdue"],
        "sourceFile": row.get("source_file") or "",
        "sourceSlot": row.get("source_slot") or "",
        "sourceLabel": row.get("source_label") or row.get("source_slot") or "",
        "sourceSheet": row.get("source_sheet") or "",
        "mappingStatus": mapping_status,
        "domain": row.get("domain") or "",
        "scope": row.get("scope") or "",
        "isDone": is_done,
        "isDueThisMonth": is_due_this_month,
        "isDueSoon": is_due_soon,
        "isOverdue": is_overdue,
        "needsReview": needs_review,
    }


def _load_pm_tasks_from_sql(sel_year: int, today: date) -> tuple:
    """Load PM tasks for sel_year from SQL. Returns (all_tasks, asset_map, source_meta).

    asset_map is built from SQL asset_master so _count_mapped_assets() works
    without reading Excel.  source_meta mirrors the shape returned by _build_tasks().
    """
    import db as _db

    sql_rows = _db.load_pm_schedule_from_sql(year=sel_year)
    all_tasks = [_sql_pm_row_to_task(r, today) for r in sql_rows]

    # Build asset_map from SQL for _count_mapped_assets()
    asset_map: dict = {}
    try:
        for row in _db.query_asset_master():
            aid = (row.get("asset_id") or "").strip().upper()
            if aid:
                asset_map[aid] = {
                    "mappedStage": row.get("stage") or "",
                    "mappedMainAssetGroup": row.get("category") or "",
                }
    except Exception:
        pass

    source_status = get_pm_schedule_source_status()
    tracked_counts = Counter(t["sourceSlot"] for t in all_tasks if t["sourceSlot"])
    source_summary = summarize_pm_schedule_sources(source_status, tracked_counts)
    mapping_meta = _db.get_asset_master_sync_meta()

    feed_files: list[str] = []
    try:
        feed_files = [
            Path(f["path"]).name
            for f in pm_feed.default_feeds(DATA_DIR)
            if Path(f["path"]).exists()
        ]
    except Exception:
        pass

    source_meta = {
        "utilityLastSynced": source_status.get("utility_stage1", {}).get("last_modified"),
        "utilityStage1LastSynced": source_status.get("utility_stage1", {}).get("last_modified"),
        "equipmentStage1LastSynced": source_status.get("equipment_stage1", {}).get("last_modified"),
        "utilitySource": source_status.get("utility_stage1", {}).get("file_name"),
        "utilityStage1Source": source_status.get("utility_stage1", {}).get("file_name"),
        "equipmentStage1Source": source_status.get("equipment_stage1", {}).get("file_name"),
        "stage2Source": "D365 PM feed (SQL)",
        "feedFiles": feed_files,
        "feedTaskCount": (
            tracked_counts.get("feed_production", 0)
            + tracked_counts.get("feed_utility", 0)
        ),
        "feedMasterErrors": [],
        "scheduleSources": list(source_status.values()),
        "sourceSummary": source_summary,
        "assetMasterAvailable": mapping_meta.get("available", False),
        "assetMasterSynced": mapping_meta.get("last_synced"),
        "errors": [],
        "dataSource": "sql",
    }

    return all_tasks, asset_map, source_meta


def sync_pm_schedule_to_sql(year: int | None = None) -> dict:
    """Build PM tasks from Excel for the given year and upsert into pm_schedule SQL table."""
    import db as _db
    today = datetime.now().date()
    sync_year = year if year is not None else today.year
    try:
        all_tasks, _, _ = _build_tasks(sync_year, today)
        result = _db.upsert_pm_schedule(all_tasks)
        _db.log_import(
            source_type="pm_schedule",
            source_file="all_sources",
            row_count=len(all_tasks),
            valid_count=len(all_tasks),
            invalid_count=0,
            notes=f"Auto-sync year={sync_year}",
        )
        return {
            "ok": True,
            "rows": result["rows"],
            "year": sync_year,
            "message": f"Synced {result['rows']} PM task(s) for {sync_year} into SQL.",
        }
    except Exception as exc:
        return {"ok": False, "message": f"PM schedule SQL sync failed: {exc}"}


def _sync_pm_to_db_background(year: int | None = None) -> None:
    try:
        result = sync_pm_schedule_to_sql(year)
        print(f"[db] {result['message']}")
    except Exception as exc:
        print(f"[db] pm_schedule sync error: {exc}")


def _pm_page_source_signature():
    """Cheap, stable signature = direct mtimes of every file the page reads.
    Avoids get_pm_schedule_source_status() in the hot path (it can be slow), and
    uses real file mtimes (not a volatile last_modified field) so identical
    requests always produce the same cache key."""
    paths = []
    try:
        for entry in get_pm_schedule_source_status().values():
            if entry.get("path"):
                paths.append(str(entry["path"]))
    except Exception:
        pass
    paths.append(str(pm_feed.default_master_path(DATA_DIR)))
    paths += [str(f["path"]) for f in pm_feed.default_feeds(DATA_DIR)]
    paths += [str(DATA_DIR / "pm_schedule_updates.json"), str(DATA_DIR / "pm_planner_tasks.json")]
    sig = []
    for p in sorted(set(paths)):
        try:
            sig.append((p, round(Path(p).stat().st_mtime, 2)))
        except OSError:
            sig.append((p, None))
    return tuple(sig)

# Forward-looking window (in days) used for "Due Soon".
DUE_SOON_WINDOW_DAYS = 30

# Status categories
STATUS_NOT_DUE = "Not Due"
STATUS_DUE_THIS_MONTH = "Due This Month"
STATUS_DUE_SOON = "Due Soon"
STATUS_COMPLETED = "Completed"
STATUS_COMPLETED_LATE = "Completed Late"
STATUS_OVERDUE = "Overdue"
STATUS_MISSING_DATE = "Missing Schedule Date"
STATUS_MISSING_MAPPING = "Missing Asset Mapping"
STATUS_NEEDS_REVIEW = "Needs Review"

_FREQ_LABELS = {
    "monthly": "Monthly",
    "weekly": "Weekly",
    "quarterly": "Quarterly",
    "biweekly": "Bi-weekly",
    "bi-weekly": "Bi-weekly",
    "daily": "Daily",
    "annual": "Annual",
    "annually": "Annual",
    "yearly": "Annual",
    "semiannual": "Semi-annual",
    "semi-annual": "Semi-annual",
    "scheduled": "Scheduled",
}


# ── Helpers ────────────────────────────────────────────────────────────────────
def _normalize_stage_filter(value) -> str:
    text = str(value or "all").strip().lower().replace(" ", "")
    if text in {"stage1", "s1", "1"}:
        return "Stage 1"
    if text in {"stage2", "s2", "2"}:
        return "Stage 2"
    return "all"


def _quarter_label(month: int | None) -> str | None:
    if not month:
        return None
    return f"Q{(int(month) - 1) // 3 + 1}"


def _freq_label(occ) -> str:
    explicit_label = str(occ.get("frequency_label") or "").strip()
    if explicit_label:
        return explicit_label
    ft = str(occ.get("frequency_type") or "").strip().lower()
    if ft in _FREQ_LABELS:
        return _FREQ_LABELS[ft]
    return ft.title() if ft else ""


def _pm_description(occ) -> str:
    freq = _freq_label(occ)
    base = f"{freq} Preventive Maintenance".strip() if freq else "Preventive Maintenance"
    if occ.get("inspection_required"):
        return f"{base} + additional checks"
    return base


def _parse_iso_date(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _derive_inferred_schedule_state(occ, today: date) -> dict:
    planned_date = _parse_iso_date(occ.get("scheduled_date"))
    scheduled_week_end = _parse_iso_date(occ.get("scheduled_week_end"))
    if planned_date and scheduled_week_end is None:
        scheduled_week_end = planned_date + timedelta(days=6)

    explicit_done = occ.get("is_done")
    if explicit_done is None:
        is_done = bool(scheduled_week_end and scheduled_week_end < today)
    else:
        is_done = bool(explicit_done)

    explicit_due_this_month = occ.get("is_due_this_month")
    if explicit_due_this_month is None:
        is_due_this_month = bool(
            (not is_done)
            and planned_date is not None
            and planned_date.year == today.year
            and planned_date.month == today.month
        )
    else:
        is_due_this_month = bool(explicit_due_this_month)

    is_due_soon = bool(
        (not is_done)
        and planned_date is not None
        and today < planned_date <= today.fromordinal(today.toordinal() + DUE_SOON_WINDOW_DAYS)
    )

    explicit_overdue = occ.get("is_overdue")
    if explicit_overdue is None:
        is_overdue = bool(
            (not is_done)
            and scheduled_week_end is not None
            and scheduled_week_end < today
            and not is_due_this_month
        )
    else:
        is_overdue = bool(explicit_overdue)

    try:
        explicit_days_overdue = int(occ.get("days_overdue"))
    except (TypeError, ValueError):
        explicit_days_overdue = None
    if explicit_days_overdue is not None:
        days_overdue = explicit_days_overdue if is_overdue else 0
    elif is_overdue and scheduled_week_end is not None:
        days_overdue = max((today - scheduled_week_end).days, 0)
    else:
        days_overdue = 0

    return {
        "planned_date": planned_date,
        "is_done": is_done,
        "is_due_this_month": is_due_this_month,
        "is_due_soon": is_due_soon,
        "is_overdue": is_overdue,
        "days_overdue": days_overdue,
    }


def _scope_for_group(main_group: str | None) -> str:
    return "equipment" if (main_group or "") in EQUIPMENT_GROUPS else "utility"


_REFRIGERATION_KEYWORDS = (
    "refriger", "condens", "evapor", "chiller", "cold room", "freezer", "cooling", "ammonia",
)


def _derive_group_from_schedule(occ, domain):
    """Derive Main Asset Group / System-Area from the schedule itself when the
    Asset ID cannot be joined to Asset_Master (the utility schedule uses its own
    UL- code scheme that does not exist in the master)."""
    cat = str(occ.get("category") or "").strip()
    sub = str(occ.get("subcategory") or "").strip()
    loc = str(occ.get("location_display") or occ.get("location_raw") or "").strip()
    blob = " ".join([cat, sub, loc]).lower()
    if any(k in blob for k in _REFRIGERATION_KEYWORDS):
        main_group = "Refrigeration"
    elif domain == "equipment":
        main_group = "Production Equipment"
    else:
        main_group = "Utilities"
    if cat and cat.lower() != "others":
        system_area = cat
    else:
        system_area = loc or cat or "General"
    return main_group, system_area


# ── Normalisation ──────────────────────────────────────────────────────────────
def _normalize_occurrence(occ, *, domain, source_file, source_slot, source_label, default_stage, asset_map, today):
    asset_id_raw = str(occ.get("asset_code") or "").strip()
    asset_id = asset_id_raw.upper()
    mapping = asset_map.get(asset_id) if asset_id else None
    mapped_from_master = bool(mapping and mapping.get("mappedStage") in STAGE_VALUES)

    if mapped_from_master:
        # Asset ID matched the master and carries a valid stage.
        mapping_status = mapping.get("mappingStatus") or "Mapped"
        stage = mapping.get("mappedStage")
        asset_name = mapping.get("mappedAssetName") or occ.get("asset_name") or asset_id_raw
        main_group = mapping.get("mappedMainAssetGroup") or ""
        sub_group = mapping.get("mappedSubAssetGroup") or occ.get("subcategory") or ""
        system_area = mapping.get("mappedSystemArea") or ""
        location = mapping.get("mappedLocation") or occ.get("location_display") or occ.get("location_raw") or ""
    else:
        # No master match. Stage follows the source slot and groups come from the schedule.
        asset_name = occ.get("asset_name") or asset_id_raw
        main_group, system_area = _derive_group_from_schedule(occ, domain)
        sub_group = occ.get("subcategory") or ""
        location = occ.get("location_display") or occ.get("location_raw") or ""
        if not asset_id_raw:
            stage = "Needs Stage Review"
            mapping_status = "Missing Asset ID"
        elif source_slot == "utility_stage1":
            stage = default_stage if default_stage in STAGE_VALUES else "Stage 1"
            mapping_status = "Schedule-defined"
        else:
            stage = default_stage if default_stage in STAGE_VALUES else "Stage 2"
            mapping_status = "Unmapped"

    schedule_state = _derive_inferred_schedule_state(occ, today)
    planned_date = schedule_state["planned_date"]
    planned_month = occ.get("planned_month") or (planned_date.month if planned_date else None)
    planned_year = planned_date.year if planned_date else None
    frequency = _freq_label(occ)
    is_done = schedule_state["is_done"]
    is_due_this_month = schedule_state["is_due_this_month"]
    is_due_soon = schedule_state["is_due_soon"]
    is_overdue = schedule_state["is_overdue"]

    # ── Schedule status (mapping issues take priority, then time-based) ──
    if mapping_status in {"Missing Asset ID", "Unmapped"}:
        schedule_status = STATUS_MISSING_MAPPING
    elif planned_date is None:
        schedule_status = STATUS_MISSING_DATE
    elif stage not in STAGE_VALUES:
        schedule_status = STATUS_NEEDS_REVIEW
    elif is_done:
        schedule_status = STATUS_COMPLETED
    elif is_due_this_month:
        schedule_status = STATUS_DUE_THIS_MONTH
    elif is_due_soon:
        schedule_status = STATUS_DUE_SOON
    elif is_overdue:
        schedule_status = STATUS_OVERDUE
    else:
        schedule_status = STATUS_NOT_DUE

    needs_review = (
        schedule_status in {STATUS_NEEDS_REVIEW, STATUS_MISSING_DATE}
        or stage not in STAGE_VALUES
        or not main_group
        or not frequency
    )

    return {
        "pmTaskId": f"{source_slot}-{asset_id or 'NOID'}-{occ.get('scheduled_date') or 'NODATE'}-{occ.get('source_sheet') or ''}",
        "stage": stage,
        "assetId": asset_id_raw,
        "assetName": asset_name,
        "mainAssetGroup": main_group or "Unmapped",
        "subAssetGroup": sub_group,
        "systemArea": system_area or "Unassigned",
        "location": location or "Unassigned",
        "pmDescription": _pm_description(occ),
        "frequency": frequency,
        "plannedYear": planned_year,
        "plannedMonth": planned_month,
        "plannedMonthLabel": (MONTH_LABELS[planned_month - 1] if planned_month else ""),
        "plannedQuarter": _quarter_label(planned_month),
        "plannedDate": occ.get("scheduled_date"),
        "plannedDateLabel": occ.get("scheduled_date_label"),
        "contractorOrPIC": occ.get("assigned_technician") or "",
        "provider": "",
        "scheduleStatus": schedule_status,
        "completionStatus": "Completed (inferred)" if is_done else "Open",
        "actualCompletionDate": None,
        "daysOverdue": schedule_state["days_overdue"],
        "sourceFile": source_file,
        "sourceSlot": source_slot,
        "sourceLabel": source_label or source_slot,
        "sourceSheet": occ.get("source_sheet") or "",
        "mappingStatus": mapping_status,
        "domain": domain,
        "scope": _scope_for_group(main_group),
        # internal booleans (kept for aggregation; harmless in JSON)
        "isDone": is_done,
        "isDueThisMonth": is_due_this_month,
        "isDueSoon": is_due_soon,
        "isOverdue": is_overdue,
        "needsReview": needs_review,
    }


def _build_tasks(year, today):
    """Normalise the active PM schedule sources into one list."""
    mapping = load_asset_mapping(str(DATA_DIR))
    asset_map = mapping.get("asset_map", {}) if mapping.get("available") else {}
    source_status = get_pm_schedule_source_status()
    utility_stage1_source = source_status["utility_stage1"]
    equipment_stage1_source = source_status["equipment_stage1"]

    def source_path(source_entry):
        path_value = source_entry.get("path")
        return Path(path_value) if path_value else None

    tasks = []
    utility_stage1 = build_utility_dataset(year)
    for occ in utility_stage1.get("occurrences", []):
        tasks.append(_normalize_occurrence(
            occ,
            domain="utility",
            source_file=utility_stage1_source.get("file_name") or "",
            source_slot="utility_stage1",
            source_label=utility_stage1_source.get("label"),
            default_stage=utility_stage1_source.get("default_stage"),
            asset_map=asset_map,
            today=today,
        ))

    equipment_stage1 = build_equipment_dataset_from_path(
        source_path(equipment_stage1_source),
        year,
        cache_key_prefix="equipment_stage1_dataset",
        source_cache_key="equipment_stage1_asset_source",
        # Persist the parsed workbook (~87s to read the 4.4MB / 84-sheet file) to a
        # signature-keyed disk cache so it survives process restarts. It auto-rebuilds
        # only when the source file changes; a separate file from the 17MB equipment
        # cache so the two never collide.
        disk_cache_path=DATA_DIR / "equipment_stage1_maintenance_cache.json",
    )
    for occ in equipment_stage1.get("occurrences", []):
        tasks.append(_normalize_occurrence(
            occ,
            domain="equipment",
            source_file=equipment_stage1_source.get("file_name") or "",
            source_slot="equipment_stage1",
            source_label=equipment_stage1_source.get("label"),
            default_stage=equipment_stage1_source.get("default_stage"),
            asset_map=asset_map,
            today=today,
        ))

    # Stage 2 = live D365 PM feed (production + utility workbooks). This replaces
    # the old hard-coded Stage 2 generators (week-token equipment workbook + the
    # S2U "modern utility" fabricator). Stage / Scope / System-Area / Location /
    # PIC are all resolved from Asset_Master + PM_Feed_Map (nothing hard-coded);
    # both sheets classify every feed line as Stage 2. Stage 1 (utility_stage1 +
    # equipment_stage1) is left untouched.
    feed_master = pm_feed.read_master(pm_feed.default_master_path(DATA_DIR))
    feed_tasks = pm_feed.build_feed_tasks_internal(
        pm_feed.default_feeds(DATA_DIR),
        feed_master,
        {
            "year": year,
            "today": today,
            "win_start": date(year, 1, 1),
            "win_end": date(year, 12, 31),
        },
    )
    tasks.extend(feed_tasks)

    tracked_counts = Counter(task.get("sourceSlot") for task in tasks if task.get("sourceSlot"))
    source_summary = summarize_pm_schedule_sources(source_status, tracked_counts)

    meta = {
        "utilityLastSynced": utility_stage1.get("meta", {}).get("last_synced"),
        "utilityStage1LastSynced": utility_stage1.get("meta", {}).get("last_synced"),
        "equipmentStage1LastSynced": equipment_stage1.get("meta", {}).get("last_synced"),
        "utilitySource": utility_stage1_source.get("file_name"),
        "utilityStage1Source": utility_stage1_source.get("file_name"),
        "equipmentStage1Source": equipment_stage1_source.get("file_name"),
        # Stage 2 now comes from the D365 PM feed (production + utility workbooks).
        "stage2Source": "D365 PM feed",
        "feedFiles": [Path(f["path"]).name for f in pm_feed.default_feeds(DATA_DIR)],
        "feedTaskCount": len(feed_tasks),
        "feedMasterErrors": feed_master.get("errors", []),
        "scheduleSources": list(source_status.values()),
        "sourceSummary": source_summary,
        "assetMasterAvailable": mapping.get("available", False),
        "assetMasterSynced": mapping.get("last_synced"),
        "errors": (
            (utility_stage1.get("meta", {}).get("errors") or [])
            + (equipment_stage1.get("meta", {}).get("errors") or [])
            + list(feed_master.get("errors", []))
        ),
    }
    return tasks, asset_map, meta


# ── Aggregations ───────────────────────────────────────────────────────────────
def _public_task(task):
    """Strip internal-only boolean fields for the JSON table rows."""
    drop = {"isDone", "isDueThisMonth", "isDueSoon", "isOverdue", "needsReview", "domain"}
    public = {k: v for k, v in task.items() if k not in drop}
    public["pmCategory"] = "Equipment" if task.get("scope") == "equipment" else "Utility"
    public["completionDate"] = task.get("actualCompletionDate")
    public["remarks"] = task.get("remarks") or ""
    return public


def _counter_to_chart(counter, *, top=None, order=None):
    items = list(counter.items())
    if order is not None:
        items = [(k, counter.get(k, 0)) for k in order]
    else:
        items.sort(key=lambda kv: (-kv[1], str(kv[0])))
    if top:
        items = items[:top]
    return {"labels": [str(k) for k, _ in items], "data": [v for _, v in items]}


def _kpis(tasks, *, today, sel_year, sel_month, mapped_asset_total):
    total = len(tasks)
    completed = sum(1 for t in tasks if t["isDone"])
    due_this_month = sum(
        1 for t in tasks
        if t["plannedMonth"] == sel_month and t["plannedYear"] == sel_year
    )
    due_soon = sum(1 for t in tasks if t["isDueSoon"])
    overdue = sum(1 for t in tasks if t["isOverdue"])
    backlog = sum(
        1 for t in tasks
        if (not t["isDone"]) and (t["isOverdue"] or t["isDueThisMonth"])
    )
    missing_mapping = sum(1 for t in tasks if t["mappingStatus"] in {"Missing Asset ID", "Unmapped"})
    needs_review = sum(1 for t in tasks if t["needsReview"])

    assets_with_pm = {t["assetId"].upper() for t in tasks if t["assetId"] and t["mappingStatus"] not in {"Missing Asset ID", "Unmapped"}}
    # The utility schedule uses a different code scheme than the master, so the
    # numerator can exceed the master count for that stage — clamp to 100%.
    coverage_pct = round(min(len(assets_with_pm), mapped_asset_total) / mapped_asset_total * 100, 1) if mapped_asset_total else None

    compliance = round(completed / total * 100, 1) if total else None

    return {
        "totalScheduled": total,
        "dueThisMonth": due_this_month,
        "dueSoon": due_soon,
        "completed": completed,
        "compliancePct": compliance,            # number (completion inferred) or None
        "overdue": overdue,
        "backlog": backlog,
        "coverage": {
            "assetsWithPm": len(assets_with_pm),
            "totalMappedAssets": mapped_asset_total,
            "pct": coverage_pct,
        },
        "missingMapping": missing_mapping,
        "needsReview": needs_review,
    }


def _task_planned_date(task):
    raw = task.get("plannedDate")
    if raw:
        try:
            return datetime.fromisoformat(str(raw)[:10]).date()
        except Exception:
            pass
    y, m = task.get("plannedYear"), task.get("plannedMonth")
    if y and m:
        try:
            return date(int(y), int(m), 1)
        except Exception:
            return None
    return None


def _completion_date(task):
    """Manual completion date (date), or None."""
    raw = task.get("completionDate") or task.get("actualCompletionDate")
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text[:19].replace("Z", "")).date()
    except Exception:
        pass
    if len(text) >= 10:
        try:
            return date(int(text[:4]), int(text[5:7]), int(text[8:10]))
        except (ValueError, IndexError):
            return None
    return None


def _period_kpis(tasks, *, win_start, win_end):
    """PM KPIs scoped to a date WINDOW (YTD / month / FY / full year / custom).

    Completion is MANUAL ONLY (isDone). Compliance = on-time completed / scheduled
    in the window. Overdue / backlog are scoped to the window's tasks (planned date
    inside the window). Scheduled date/week is only the target period.
    """
    def in_win(d):
        return bool(d and win_start <= d <= win_end)

    period_tasks = [t for t in tasks if in_win(_task_planned_date(t))]
    scheduled = len(period_tasks)
    completed = sum(1 for t in period_tasks if t.get("isDone") and in_win(_completion_date(t)))
    on_time = sum(1 for t in period_tasks if t.get("isOnTimeCompleted"))
    overdue = sum(1 for t in period_tasks if t.get("isOverdueOp"))
    backlog = sum(1 for t in period_tasks if t.get("isBacklog"))
    deferred = sum(1 for t in period_tasks if t.get("isDeferred"))
    late = sum(1 for t in period_tasks if t.get("isLateCompleted"))
    no_pic = sum(1 for t in period_tasks if not str(t.get("contractorOrPIC") or "").strip())
    compliance = round(on_time / scheduled * 100, 1) if scheduled else None
    return {
        "scheduledInMonth": scheduled,
        "dueThisMonth": scheduled,
        "completedInMonth": completed,
        "onTimeInMonth": on_time,
        "compliancePct": compliance,
        "overdueInMonth": overdue,
        "backlogInMonth": backlog,
        "deferredInMonth": deferred,
        "lateInMonth": late,
        "noPicInMonth": no_pic,
        "yearTaskCount": scheduled,
        "byStage": _counter_to_chart(Counter(t["stage"] for t in period_tasks), order=["Stage 1", "Stage 2"]),
        "byAssetCategory": _counter_to_chart(Counter(t["mainAssetGroup"] for t in period_tasks), top=10),
        "byFunctionalLocation": _counter_to_chart(
            Counter(t.get("functionalLocationLabel") or "Unassigned" for t in period_tasks), top=10),
    }


def _pm_resolve_window(sel_year, sel_month, period_mode, today, start=None, end=None):
    """Date window for the PM period KPIs, honouring period_mode (default YTD)."""
    mode = (period_mode or ("monthly" if sel_month else "ytd")).lower()
    if mode == "monthly" and sel_month:
        last = calendar.monthrange(sel_year, sel_month)[1]
        return date(sel_year, sel_month, 1), date(sel_year, sel_month, last)
    if mode == "custom" and start and end:
        s = start if isinstance(start, date) else _coerce_date(start)
        e = end if isinstance(end, date) else _coerce_date(end)
        if s and e and s <= e:
            return s, e
    if mode == "financial_year":
        return date(sel_year - 1, 4, 1), min(date(sel_year, 3, 31), today)
    if mode == "full_year":
        return date(sel_year, 1, 1), date(sel_year, 12, 31)
    # ytd (default): current year -> Jan 1..today; past year -> full year.
    if sel_year == today.year:
        return date(sel_year, 1, 1), today
    return date(sel_year, 1, 1), date(sel_year, 12, 31)


def _coerce_date(value):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except Exception:
        return None


def _data_quality(tasks):
    seen = set()
    duplicates = 0
    for t in tasks:
        key = (t["assetId"].upper(), t["plannedDate"], t["pmDescription"])
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    counts = {
        "missingAssetId": sum(1 for t in tasks if not t["assetId"]),
        "unmappedAssetId": sum(1 for t in tasks if t["mappingStatus"] == "Unmapped"),
        "missingPlanned": sum(1 for t in tasks if not t["plannedDate"]),
        "missingFrequency": sum(1 for t in tasks if not t["frequency"]),
        "missingGroup": sum(1 for t in tasks if t["mainAssetGroup"] in {"", "Unmapped", "Unknown / Review"}),
        "missingStage": sum(1 for t in tasks if t["stage"] not in STAGE_VALUES),
        "duplicates": duplicates,
        "needsReview": sum(1 for t in tasks if t["needsReview"]),
    }
    # Compact review rows (capped) for the Overview expandable table.
    rows = [
        _public_task(t) for t in tasks
        if t["needsReview"] or t["mappingStatus"] in {"Missing Asset ID", "Unmapped"}
    ][:200]
    return counts, rows


def _monthly_series(tasks, sel_year):
    scheduled = [0] * 12
    completed = [0] * 12
    overdue = [0] * 12
    for t in tasks:
        m = t["plannedMonth"]
        if not m or not (1 <= m <= 12):
            continue
        if t["plannedYear"] and t["plannedYear"] != sel_year:
            continue
        scheduled[m - 1] += 1
        if t["isDone"]:
            completed[m - 1] += 1
        if t["isOverdue"]:
            overdue[m - 1] += 1
    return {
        "labels": MONTH_LABELS,
        "scheduled": scheduled,
        "completed": completed,
        "overdue": overdue,
    }


def _stage_breakdown(tasks):
    counter = Counter(t["stage"] for t in tasks)
    order = ["Stage 1", "Stage 2"] + sorted(k for k in counter if k not in {"Stage 1", "Stage 2"})
    return _counter_to_chart(counter, order=[k for k in order if counter.get(k)])


def _overview_section(tasks, *, today, sel_year, sel_month, mapped_asset_total, win_start, win_end):
    kpis = _kpis(tasks, today=today, sel_year=sel_year, sel_month=sel_month,
                 mapped_asset_total=mapped_asset_total)
    dq_counts, dq_rows = _data_quality(tasks)

    overdue_tasks = [t for t in tasks if t["isOverdue"]]
    due_soon_tasks = [t for t in tasks if t["isDueSoon"]]

    charts = {
        "scheduledByStage": _stage_breakdown(tasks),
        "scheduledByMonth": _monthly_series(tasks, sel_year),
        "overdueByStage": _stage_breakdown(overdue_tasks),
        "dueSoonByStage": _stage_breakdown(due_soon_tasks),
        "workloadByMainGroup": _counter_to_chart(Counter(t["mainAssetGroup"] for t in tasks), top=10),
        "workloadBySystemArea": _counter_to_chart(
            Counter(t["systemArea"] for t in tasks if t["systemArea"] and t["systemArea"] != "Unassigned"),
            top=10,
        ),
        "stageWorkload": _counter_to_chart(
            Counter(t["stage"] for t in tasks),
            order=["Stage 1", "Stage 2"],
        ),
        "dataQuality": {
            "labels": ["Missing Asset ID", "Unmapped", "Missing Date", "Missing Freq.", "Missing Group", "Duplicates", "Needs Review"],
            "data": [
                dq_counts["missingAssetId"], dq_counts["unmappedAssetId"], dq_counts["missingPlanned"],
                dq_counts["missingFrequency"], dq_counts["missingGroup"], dq_counts["duplicates"],
                dq_counts["needsReview"],
            ],
        },
    }
    return {
        "kpis": kpis,
        "periodKpis": _period_kpis(tasks, win_start=win_start, win_end=win_end),
        "charts": charts,
        "dataQuality": {"counts": dq_counts, "rows": dq_rows},
    }


def _sort_table(rows):
    status_rank = {
        STATUS_OVERDUE: 0, STATUS_DUE_THIS_MONTH: 1, STATUS_DUE_SOON: 2,
        STATUS_NEEDS_REVIEW: 3, STATUS_MISSING_MAPPING: 3, STATUS_MISSING_DATE: 3,
        STATUS_NOT_DUE: 4, STATUS_COMPLETED: 5, STATUS_COMPLETED_LATE: 5,
    }
    return sorted(
        rows,
        key=lambda t: (status_rank.get(t["scheduleStatus"], 9), -(t["daysOverdue"] or 0), t["plannedDate"] or "9999"),
    )


def _scope_section(tasks, *, scope, today, sel_year, sel_month, mapped_asset_total, group_chart_key, win_start, win_end):
    scoped = [t for t in tasks if t["scope"] == scope]
    kpis = _kpis(scoped, today=today, sel_year=sel_year, sel_month=sel_month,
                 mapped_asset_total=mapped_asset_total)

    if group_chart_key == "systemArea":
        secondary = _counter_to_chart(
            Counter(t["systemArea"] for t in scoped if t["systemArea"] and t["systemArea"] != "Unassigned"),
            top=12,
        )
        top_label = "topSystems"
        top_counter = Counter(t["systemArea"] for t in scoped if t["systemArea"] and t["systemArea"] != "Unassigned")
    else:
        secondary = _counter_to_chart(Counter(t["subAssetGroup"] for t in scoped if t["subAssetGroup"]), top=12)
        top_label = "topAssets"
        top_counter = Counter(f"{t['assetId']} — {t['assetName']}" for t in scoped if t["assetId"])

    charts = {
        "byMainGroup": _counter_to_chart(Counter(t["mainAssetGroup"] for t in scoped), top=10),
        "bySecondary": secondary,
        "byMonth": _monthly_series(scoped, sel_year),
        "byStage": _stage_breakdown(scoped),
    }

    all_rows = [_public_task(t) for t in _sort_table(scoped)]
    overdue = [_public_task(t) for t in _sort_table([t for t in scoped if t["isOverdue"]])]
    due_soon = [_public_task(t) for t in _sort_table([t for t in scoped if t["isDueSoon"]])]
    needs_review = [_public_task(t) for t in scoped if t["needsReview"]]

    return {
        "kpis": kpis,
        "periodKpis": _period_kpis(scoped, win_start=win_start, win_end=win_end),
        "charts": charts,
        top_label: _counter_to_chart(top_counter, top=10),
        "tables": {
            "all": all_rows[:1000],
            "allCount": len(all_rows),
            "overdue": overdue[:300],
            "dueSoon": due_soon[:300],
            "needsReview": needs_review[:300],
        },
    }


def _schedule_filter_options(tasks) -> dict:
    def unique_values(key):
        return sorted({
            str(task.get(key) or "").strip()
            for task in tasks
            if str(task.get(key) or "").strip()
        }, key=str.lower)

    return {
        "categories": ["All", "Utility", "Equipment"],
        "systems": unique_values("systemArea"),
        "assetGroups": unique_values("mainAssetGroup"),
        "statuses": unique_values("scheduleStatus"),
        "pics": unique_values("contractorOrPIC"),
    }


def _schedule_section(tasks) -> dict:
    rows = [_public_task(t) for t in _sort_table(tasks)]
    overdue = [_public_task(t) for t in _sort_table([t for t in tasks if t["isOverdue"]])]
    due_soon = [_public_task(t) for t in _sort_table([t for t in tasks if t["isDueSoon"]])]
    needs_review = [_public_task(t) for t in tasks if t["needsReview"]]
    return {
        "tasks": rows,
        "filterOptions": _schedule_filter_options(tasks),
        "tables": {
            "all": rows,
            "allCount": len(rows),
            "overdue": overdue,
            "dueSoon": due_soon,
            "needsReview": needs_review,
        },
    }


def _count_mapped_assets(asset_map, *, stage, scope=None):
    total = 0
    for entry in asset_map.values():
        entry_stage = entry.get("mappedStage")
        if entry_stage not in STAGE_VALUES:
            continue
        if stage != "all" and entry_stage != stage:
            continue
        if scope is not None and _scope_for_group(entry.get("mappedMainAssetGroup")) != scope:
            continue
        total += 1
    return total


# ── Public entry point ─────────────────────────────────────────────────────────
def build_pm_schedule_payload(stage="all", year=None, month=None, period_mode=None, start=None, end=None):
    today = datetime.now().date()
    sel_year = int(year) if year else today.year
    stage_filter = _normalize_stage_filter(stage)

    try:
        sel_month = int(month) if month else None
    except (TypeError, ValueError):
        sel_month = None
    if sel_month is not None and not (1 <= sel_month <= 12):
        sel_month = None

    # Serve a cached payload (keyed by request params + date). Data mutations
    # (edits/uploads) explicitly clear this cache via the API layer, so the key
    # deliberately excludes volatile file mtimes — the build itself touches some
    # of those files, which made every request a cache miss (and a ~97s rebuild).
    page_cache_key = (
        today.isoformat(), stage_filter, sel_year, sel_month,
        str(period_mode or ("monthly" if sel_month else "ytd")), str(start), str(end),
    )
    cached_page = _PM_PAGE_PAYLOAD_CACHE.get(page_cache_key)
    if cached_page is not None:
        return cached_page

    # Period window for the period-scoped KPIs (default YTD when no month/mode).
    win_start, win_end = _pm_resolve_window(sel_year, sel_month, period_mode, today, start, end)
    # For the legacy single-month _kpis/charts and the meta label, keep a month value.
    label_month = sel_month or today.month

    # Phase 4: SQL-first — prefer SQL over reading Excel. Fall back to Excel if SQL
    # has no tasks for the requested year (e.g. first startup or a different year),
    # then kick off a background sync so subsequent loads are fast.
    if _sql_has_pm_schedule_for_year(sel_year):
        all_tasks, asset_map, source_meta = _load_pm_tasks_from_sql(sel_year, today)
    else:
        all_tasks, asset_map, source_meta = _build_tasks(sel_year, today)
        if sel_year == today.year:
            threading.Thread(
                target=_sync_pm_to_db_background, args=(sel_year,),
                name="db-pm-sync", daemon=True,
            ).start()

    # Merge local PM status overrides and apply the auto-assumed-done rule so the
    # operational `status` (Done / Auto Done / Backlog / Deferred / …) is available
    # to every KPI, chart, table, and calendar cell downstream.
    override_stats = apply_overrides_and_autodone(all_tasks, today)
    source_meta["overrideStats"] = override_stats

    # Merge manually planned PM tasks (saved in data/pm_planner_tasks.json). These
    # carry source="Manual" and are already normalised to the imported task shape.
    manual_tasks = list_manual_tasks(today)
    all_tasks.extend(manual_tasks)
    source_meta["manualCount"] = len(manual_tasks)

    # Attach functional-location (asset installation → zone) for the FL workload chart.
    _attach_functional_location(all_tasks)

    # Available years across both schedules (for the year selector).
    years = sorted({t["plannedYear"] for t in all_tasks if t["plannedYear"]})
    if sel_year not in years:
        years = sorted(set(years) | {sel_year})

    if stage_filter == "all":
        tasks = all_tasks
    else:
        tasks = [t for t in all_tasks if t["stage"] == stage_filter]

    mapped_total_all = _count_mapped_assets(asset_map, stage=stage_filter)
    mapped_total_equipment = _count_mapped_assets(asset_map, stage=stage_filter, scope="equipment")
    mapped_total_utility = _count_mapped_assets(asset_map, stage=stage_filter, scope="utility")

    overview = _overview_section(
        tasks, today=today, sel_year=sel_year, sel_month=label_month,
        mapped_asset_total=mapped_total_all, win_start=win_start, win_end=win_end,
    )
    equipment = _scope_section(
        tasks, scope="equipment", today=today, sel_year=sel_year, sel_month=label_month,
        mapped_asset_total=mapped_total_equipment, group_chart_key="subAssetGroup",
        win_start=win_start, win_end=win_end,
    )
    utility = _scope_section(
        tasks, scope="utility", today=today, sel_year=sel_year, sel_month=label_month,
        mapped_asset_total=mapped_total_utility, group_chart_key="systemArea",
        win_start=win_start, win_end=win_end,
    )
    schedule = _schedule_section(tasks)

    payload = {
        "meta": {
            "stageFilter": stage_filter,
            "year": sel_year,
            "month": sel_month,
            "monthLabel": MONTH_LABELS[label_month - 1],
            "periodMode": (period_mode or ("monthly" if sel_month else "ytd")),
            "periodStart": win_start.isoformat(),
            "periodEnd": win_end.isoformat(),
            "availableYears": years,
            "availableStages": list(STAGE_VALUES),
            "today": today.isoformat(),
            "completionBasis": "inferred",
            "dueSoonWindowDays": DUE_SOON_WINDOW_DAYS,
            "taskCountAllStages": len(all_tasks),
            "taskCount": len(tasks),
            "generatedAt": datetime.now().isoformat(),
            **source_meta,
        },
        "overview": overview,
        "schedule": schedule,
        "equipment": equipment,
        "utility": utility,
    }
    if len(_PM_PAGE_PAYLOAD_CACHE) > 64:
        _PM_PAGE_PAYLOAD_CACHE.clear()
    _PM_PAGE_PAYLOAD_CACHE[page_cache_key] = payload
    return payload


def build_pm_schedule_metrics_payload(stage="all", year=None, month=None, period_mode=None, start=None, end=None, allow_excel_fallback=True):
    """Lightweight PM payload for assistant / KPI consumers.

    This avoids building the full page tables and charts when a caller only needs
    period-scoped KPIs plus a small overdue list.
    """
    today = datetime.now().date()
    sel_year = int(year) if year else today.year
    stage_filter = _normalize_stage_filter(stage)

    try:
        sel_month = int(month) if month else None
    except (TypeError, ValueError):
        sel_month = None
    if sel_month is not None and not (1 <= sel_month <= 12):
        sel_month = None

    win_start, win_end = _pm_resolve_window(sel_year, sel_month, period_mode, today, start, end)
    label_month = sel_month or today.month
    source_status = get_pm_schedule_source_status()
    source_signature = tuple(
        (
            slot,
            str(entry.get("path") or ""),
            str(entry.get("last_modified") or ""),
            bool(entry.get("active")),
        )
        for slot, entry in sorted(source_status.items())
    )
    cache_key = (
        today.isoformat(),
        stage_filter,
        sel_year,
        sel_month,
        str(period_mode or ("monthly" if sel_month else "ytd")),
        str(start),
        str(end),
        bool(allow_excel_fallback),
        source_signature,
    )
    cached_payload = _PM_METRICS_PAYLOAD_CACHE.get(cache_key)
    if cached_payload is not None:
        return cached_payload

    # Phase 4: SQL-first path (same logic as build_pm_schedule_payload).
    if _sql_has_pm_schedule_for_year(sel_year):
        all_tasks, asset_map, source_meta = _load_pm_tasks_from_sql(sel_year, today)
    elif allow_excel_fallback:
        all_tasks, asset_map, source_meta = _build_tasks(sel_year, today)
        if sel_year == today.year:
            threading.Thread(
                target=_sync_pm_to_db_background, args=(sel_year,),
                name="db-pm-sync", daemon=True,
            ).start()
    else:
        try:
            import db as _db
            mapping_meta = _db.get_asset_master_sync_meta()
        except Exception:
            mapping_meta = {"available": False, "last_synced": None}
        source_status = get_pm_schedule_source_status()
        source_meta = {
            "utilityLastSynced": source_status.get("utility_stage1", {}).get("last_modified"),
            "utilityStage1LastSynced": source_status.get("utility_stage1", {}).get("last_modified"),
            "equipmentStage1LastSynced": source_status.get("equipment_stage1", {}).get("last_modified"),
            "utilitySource": source_status.get("utility_stage1", {}).get("file_name"),
            "utilityStage1Source": source_status.get("utility_stage1", {}).get("file_name"),
            "equipmentStage1Source": source_status.get("equipment_stage1", {}).get("file_name"),
            "stage2Source": "D365 PM feed (SQL)",
            "feedFiles": [],
            "feedTaskCount": 0,
            "feedMasterErrors": [],
            "scheduleSources": list(source_status.values()),
            "sourceSummary": summarize_pm_schedule_sources(source_status, Counter()),
            "assetMasterAvailable": mapping_meta.get("available", False),
            "assetMasterSynced": mapping_meta.get("last_synced"),
            "errors": [f"PM schedule SQL data is not available for {sel_year}."],
            "dataSource": "sql_unavailable",
        }
        all_tasks = []
        asset_map = {}

    override_stats = apply_overrides_and_autodone(all_tasks, today)
    source_meta["overrideStats"] = override_stats

    manual_tasks = list_manual_tasks(today)
    all_tasks.extend(manual_tasks)
    source_meta["manualCount"] = len(manual_tasks)

    _attach_functional_location(all_tasks)

    tasks = all_tasks if stage_filter == "all" else [t for t in all_tasks if t["stage"] == stage_filter]
    equipment_tasks = [t for t in tasks if t["scope"] == "equipment"]
    utility_tasks = [t for t in tasks if t["scope"] == "utility"]

    mapped_total_all = _count_mapped_assets(asset_map, stage=stage_filter)
    mapped_total_equipment = _count_mapped_assets(asset_map, stage=stage_filter, scope="equipment")
    mapped_total_utility = _count_mapped_assets(asset_map, stage=stage_filter, scope="utility")

    overview_kpis = _kpis(tasks, today=today, sel_year=sel_year, sel_month=label_month, mapped_asset_total=mapped_total_all)
    overview_period = _period_kpis(tasks, win_start=win_start, win_end=win_end)
    dq_counts, _dq_rows = _data_quality(tasks)

    equipment = {
        "kpis": _kpis(
            equipment_tasks,
            today=today,
            sel_year=sel_year,
            sel_month=label_month,
            mapped_asset_total=mapped_total_equipment,
        ),
        "periodKpis": _period_kpis(equipment_tasks, win_start=win_start, win_end=win_end),
    }
    utility = {
        "kpis": _kpis(
            utility_tasks,
            today=today,
            sel_year=sel_year,
            sel_month=label_month,
            mapped_asset_total=mapped_total_utility,
        ),
        "periodKpis": _period_kpis(utility_tasks, win_start=win_start, win_end=win_end),
    }

    overdue_rows = [_public_task(t) for t in _sort_table([t for t in tasks if t["isOverdue"]])]

    years = sorted({t["plannedYear"] for t in all_tasks if t["plannedYear"]})
    if sel_year not in years:
        years = sorted(set(years) | {sel_year})

    payload = {
        "meta": {
            "stageFilter": stage_filter,
            "year": sel_year,
            "month": sel_month,
            "monthLabel": MONTH_LABELS[label_month - 1],
            "periodMode": (period_mode or ("monthly" if sel_month else "ytd")),
            "periodStart": win_start.isoformat(),
            "periodEnd": win_end.isoformat(),
            "availableYears": years,
            "availableStages": list(STAGE_VALUES),
            "today": today.isoformat(),
            "completionBasis": "inferred",
            "dueSoonWindowDays": DUE_SOON_WINDOW_DAYS,
            "taskCountAllStages": len(all_tasks),
            "taskCount": len(tasks),
            "generatedAt": datetime.now().isoformat(),
            **source_meta,
        },
        "overview": {
            "kpis": overview_kpis,
            "periodKpis": overview_period,
            "dataQuality": {"counts": dq_counts},
        },
        "equipment": equipment,
        "utility": utility,
        "schedule": {
            "tables": {
                "overdue": overdue_rows[:300],
            },
        },
    }
    _PM_METRICS_PAYLOAD_CACHE[cache_key] = payload
    return payload
