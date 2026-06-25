"""
Manual Preventive Maintenance planner store.

Lets engineers schedule/add PM tasks directly in the dashboard. Manual tasks are
saved to a separate editable file (``data/pm_planner_tasks.json``) and merged with
the read-only imported schedule when the PM payload is built — the original source
workbooks are never touched.

Each manual task is normalised to the SAME shape as an imported PM task (so the
calendar, KPIs, charts, and table treat them identically) and carries
``source = "Manual"``. Recurring schedules expand into individual dated tasks for
the planning year, with duplicate protection.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from maintenance_service import DATA_DIR
from asset_mapping import load_asset_mapping
from pm_schedule_overrides import (
    STATUS_SCHEDULED, STATUS_DONE, STATUS_BACKLOG, STATUS_DEFERRED,
    STATUS_NOT_APPLICABLE, STATUS_CANCELLED, ALLOWED_STATUSES,
    normalize_status, derive_operational_fields,
)

PLANNER_PATH = Path(DATA_DIR) / "pm_planner_tasks.json"
MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
ALLOWED_FREQUENCIES = {"One-time", "Weekly", "Monthly", "Quarterly", "Yearly", "Custom"}
DUE_SOON_WINDOW_DAYS = 30
_MAX_RECURRENCE = 60  # safety cap on generated occurrences

_cache = {"sig": None, "data": None}


# ── Persistence ─────────────────────────────────────────────────────────────────
def _file_sig():
    try:
        st = PLANNER_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_planner_tasks() -> dict:
    """Return {pmTaskId: stored_task}. Cached by file mtime+size."""
    sig = _file_sig()
    if sig is not None and _cache["sig"] == sig and _cache["data"] is not None:
        return _cache["data"]
    data = {}
    if PLANNER_PATH.exists():
        try:
            with open(PLANNER_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data = {str(k): v for k, v in raw.get("tasks", {}).items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError, ValueError):
            data = {}
    _cache.update(sig=sig, data=data)
    return data


def _write(store: dict) -> None:
    PLANNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PLANNER_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({
                "schemaVersion": 1,
                "note": "Manually planned PM tasks created in the dashboard. Merged with read-only imported schedules.",
                "updatedAt": datetime.now().isoformat(timespec="seconds"),
                "tasks": store,
            }, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, PLANNER_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    _cache.update(sig=_file_sig(), data=None)


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


def _parse_iso(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _quarter(month: int) -> str:
    return f"Q{(month - 1) // 3 + 1}"


# ── Asset catalogue (for the searchable planner dropdown) ────────────────────────
def get_asset_catalog() -> list[dict]:
    """Flat, searchable list of assets from Asset_Master for the planner form."""
    mapping = load_asset_mapping(str(DATA_DIR))
    catalog = []
    for asset_id, entry in (mapping.get("asset_map") or {}).items():
        group = entry.get("mappedMainAssetGroup") or ""
        catalog.append({
            "assetId": asset_id,
            "assetName": entry.get("mappedAssetName") or asset_id,
            "category": "Equipment" if group == "Production Equipment" else "Utility",
            "stage": entry.get("mappedStage") or "",
            "mainAssetGroup": group,
            "subAssetGroup": entry.get("mappedSubAssetGroup") or "",
            "systemArea": entry.get("mappedSystemArea") or "",
            "location": entry.get("mappedLocation") or "",
            "criticality": entry.get("criticality") or "",
        })
    catalog.sort(key=lambda a: a["assetId"])
    return catalog


# ── Validation + recurrence ──────────────────────────────────────────────────────
def validate_planner_fields(fields: dict) -> tuple[bool, str]:
    if not _clean(fields.get("assetId")):
        return False, "Asset ID is required."
    if not _parse_iso(fields.get("scheduledDate")):
        return False, "A valid scheduled date is required."
    if not _clean(fields.get("pmDescription")):
        return False, "A PM type / task description is required."
    freq = _clean(fields.get("frequency")) or "One-time"
    if freq not in ALLOWED_FREQUENCIES:
        return False, f"Unknown frequency '{freq}'."
    status = _clean(fields.get("status")) or STATUS_SCHEDULED
    if status not in ALLOWED_STATUSES:
        return False, f"Invalid status '{status}'."
    return True, ""


def _recurrence_dates(start: date, frequency: str, custom_days: int | None) -> list[date]:
    """Expand a frequency into individual dates within the planning year."""
    year_end = date(start.year, 12, 31)
    if frequency in ("One-time", "Yearly"):
        return [start]
    out = []
    cursor = start
    guard = 0
    while cursor <= year_end and guard < _MAX_RECURRENCE:
        out.append(cursor)
        guard += 1
        if frequency == "Weekly":
            cursor = cursor + timedelta(days=7)
        elif frequency == "Monthly":
            cursor = _add_months(cursor, 1)
        elif frequency == "Quarterly":
            cursor = _add_months(cursor, 3)
        elif frequency == "Custom":
            step = custom_days if (custom_days and custom_days > 0) else None
            if not step:
                break
            cursor = cursor + timedelta(days=step)
        else:
            break
    return out or [start]


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    # clamp day to month length
    for day in (d.day, 28, 29, 30, 31):
        try:
            return date(year, month, min(d.day, day))
        except ValueError:
            continue
    return date(year, month, 28)


def _dup_key(asset_id, pm_desc, planned_date) -> str:
    return f"{_clean(asset_id).upper()}|{_clean(pm_desc).lower()}|{_clean(planned_date)[:10]}"


def _new_task_id(asset_id: str, planned: date, salt: int) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "-", _clean(asset_id)).strip("-").lower() or "noid"
    return f"manual_{safe}_{planned.strftime('%Y%m%d')}_{int(time.time() * 1000)}{salt:02d}"


# ── Create / update / delete ─────────────────────────────────────────────────────
def create_tasks(fields: dict, confirm: bool = False) -> dict:
    ok, message = validate_planner_fields(fields)
    if not ok:
        raise ValueError(message)

    start = _parse_iso(fields.get("scheduledDate"))
    frequency = _clean(fields.get("frequency")) or "One-time"
    try:
        custom_days = int(fields.get("customIntervalDays")) if fields.get("customIntervalDays") else None
    except (TypeError, ValueError):
        custom_days = None

    dates = _recurrence_dates(start, frequency, custom_days)
    asset_id = _clean(fields.get("assetId"))
    pm_desc = _clean(fields.get("pmDescription"))

    store = dict(load_planner_tasks())
    existing_keys = {_dup_key(t.get("assetId"), t.get("pmDescription"), t.get("plannedDate")) for t in store.values()}

    collisions = [d for d in dates if _dup_key(asset_id, pm_desc, d.isoformat()) in existing_keys]
    if collisions and not confirm:
        return {
            "ok": False,
            "needsConfirm": True,
            "message": f"{len(collisions)} task(s) with the same asset, PM type and date already exist. Confirm to add anyway.",
        }

    series_id = f"series_{int(time.time() * 1000)}"
    created = []
    now_iso = datetime.now().isoformat(timespec="seconds")
    group = _clean(fields.get("mainAssetGroup"))
    category = _clean(fields.get("category")) or ("Equipment" if group == "Production Equipment" else "Utility")
    status = _clean(fields.get("status")) or STATUS_SCHEDULED

    for idx, planned in enumerate(dates):
        key = _dup_key(asset_id, pm_desc, planned.isoformat())
        if key in existing_keys and not confirm:
            continue
        existing_keys.add(key)
        task_id = _new_task_id(asset_id, planned, idx)
        stored = {
            "pmTaskId": task_id,
            "seriesId": series_id,
            "source": "Manual",
            "assetId": asset_id,
            "assetName": _clean(fields.get("assetName")) or asset_id,
            "category": category,
            "stage": _clean(fields.get("stage")) or "Stage 1",
            "mainAssetGroup": group or ("Production Equipment" if category == "Equipment" else "Utilities"),
            "subAssetGroup": _clean(fields.get("subAssetGroup")),
            "systemArea": _clean(fields.get("systemArea")) or "Unassigned",
            "location": _clean(fields.get("location")),
            "pmDescription": pm_desc,
            "frequency": frequency,
            "priority": _clean(fields.get("priority")),
            "contractorOrPIC": _clean(fields.get("contractorOrPIC")),
            "remarks": _clean(fields.get("remarks")),
            "plannedDate": planned.isoformat(),
            "status": status,
            "completionDate": _clean(fields.get("completionDate")),
            "rescheduledDate": _clean(fields.get("rescheduledDate")),
            "reason": _clean(fields.get("reason")),
            "autoUpdated": False,
            "updatedBy": _clean(fields.get("updatedBy")) or "dashboard-user",
            "lastUpdated": now_iso,
            "createdAt": now_iso,
        }
        store[task_id] = stored
        created.append(task_id)

    if created:
        _write(store)
    return {"ok": True, "created": created, "count": len(created), "seriesId": series_id}


def update_task(task_id: str, fields: dict) -> dict:
    task_id = _clean(task_id)
    store = dict(load_planner_tasks())
    stored = store.get(task_id)
    if not stored:
        raise ValueError("Manual PM task not found.")

    status = normalize_status(fields.get("status")) or stored.get("status") or STATUS_SCHEDULED
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Invalid status '{status}'.")
    # Manual completion only — mirror the imported-override validation rules.
    if status == STATUS_DONE and not _clean(fields.get("completionDate")):
        raise ValueError("A completion date is required when status is Done.")
    if status == STATUS_BACKLOG and not _clean(fields.get("reason")):
        raise ValueError("A backlog reason is required when status is Backlog.")
    if status == STATUS_DEFERRED:
        if not _clean(fields.get("rescheduledDate")):
            raise ValueError("A rescheduled date is required when status is Deferred.")
        if not (_clean(fields.get("reason")) or _clean(fields.get("remarks"))):
            raise ValueError("A reason is required when status is Deferred.")
    if status == STATUS_NOT_APPLICABLE and not _clean(fields.get("remarks")):
        raise ValueError("Remarks are required when status is Not Applicable.")
    if status == STATUS_CANCELLED and not _clean(fields.get("remarks")):
        raise ValueError("Remarks are required when status is Cancelled.")

    for key in ("assetName", "systemArea", "stage", "pmDescription", "frequency", "priority",
                "contractorOrPIC", "remarks", "reason", "rescheduledDate"):
        if key in fields:
            stored[key] = _clean(fields.get(key))
    if _clean(fields.get("scheduledDate")):
        new_date = _parse_iso(fields.get("scheduledDate"))
        if new_date:
            # Keep the original planned date for traceability the first time it moves.
            stored.setdefault("originalScheduledDate", stored.get("plannedDate"))
            stored["plannedDate"] = new_date.isoformat()
    stored["status"] = status
    stored["completionDate"] = _clean(fields.get("completionDate")) if status == STATUS_DONE else ""
    stored["autoUpdated"] = False
    stored["updatedBy"] = _clean(fields.get("updatedBy")) or "dashboard-user"
    stored["lastUpdated"] = datetime.now().isoformat(timespec="seconds")
    store[task_id] = stored
    _write(store)
    return stored


def delete_task(task_id: str) -> bool:
    task_id = _clean(task_id)
    store = dict(load_planner_tasks())
    if task_id in store:
        del store[task_id]
        _write(store)
        return True
    return False


# ── Normalisation (to imported task shape, with auto-done + internals) ────────────
def list_manual_tasks(today: date | None = None) -> list[dict]:
    today = today or datetime.now().date()
    out = []
    for stored in load_planner_tasks().values():
        out.append(_normalize(stored, today))
    return out


def _normalize(stored: dict, today: date) -> dict:
    status = normalize_status(stored.get("status")) or STATUS_SCHEDULED
    group = stored.get("mainAssetGroup") or ""
    scope = "equipment" if group == "Production Equipment" else "utility"

    # Deferred manual tasks plan on the rescheduled date; keep the original for traceability.
    planned_iso = stored.get("plannedDate")
    original_iso = stored.get("originalScheduledDate")
    rescheduled = stored.get("rescheduledDate")
    if status == STATUS_DEFERRED and rescheduled:
        original_iso = original_iso or planned_iso
        planned_iso = rescheduled
    planned = _parse_iso(planned_iso)
    planned_month = planned.month if planned else None
    planned_year = planned.year if planned else None

    # Completion is MANUAL only — never auto-filled.
    completion = stored.get("completionDate") if status == STATUS_DONE else ""
    completion = completion or None

    task = {
        "pmTaskId": stored.get("pmTaskId"),
        "seriesId": stored.get("seriesId"),
        "source": "Manual",
        "stage": stored.get("stage") or "Stage 1",
        "assetId": stored.get("assetId") or "",
        "assetName": stored.get("assetName") or stored.get("assetId") or "",
        "mainAssetGroup": group or "Utilities",
        "subAssetGroup": stored.get("subAssetGroup") or "",
        "systemArea": stored.get("systemArea") or "Unassigned",
        "location": stored.get("location") or "",
        "pmCategory": stored.get("category") or ("Equipment" if scope == "equipment" else "Utility"),
        "pmDescription": stored.get("pmDescription") or "Preventive Maintenance",
        "frequency": stored.get("frequency") or "One-time",
        "priority": stored.get("priority") or "",
        "plannedYear": planned_year,
        "plannedMonth": planned_month,
        "plannedMonthLabel": (MONTH_LABELS[planned_month - 1] if planned_month else ""),
        "plannedQuarter": (_quarter(planned_month) if planned_month else None),
        "plannedDate": planned_iso,
        "plannedDateLabel": (planned.strftime("%d %b %Y") if planned else ""),
        "originalScheduledDate": original_iso,
        "contractorOrPIC": stored.get("contractorOrPIC") or "",
        "provider": "Manual Planner",
        "scheduleStatus": "Manual",
        "actualCompletionDate": completion,
        "remarks": stored.get("remarks") or "",
        "rescheduledDate": rescheduled or None,
        "reason": stored.get("reason") or "",
        "sourceFile": "pm_planner_tasks.json",
        "sourceSlot": "manual",
        "sourceSheet": "",
        "mappingStatus": "Manual",
        "scope": scope,
        "status": status,
        "lastUpdated": stored.get("lastUpdated"),
        "updatedBy": stored.get("updatedBy"),
        "needsReview": False,
        "domain": scope,
    }
    # Dynamic display/aggregation fields (manual-only completion, dynamic overdue).
    derive_operational_fields(task, today)
    task["completionStatus"] = "Completed" if task["isDone"] else "Open"
    return task
