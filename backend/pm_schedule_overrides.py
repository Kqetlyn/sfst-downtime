"""
Local, editable PM status overrides (manual completion only).

The source PM workbooks are read-only schedule plans (no completion column). This
module lets users record operational PM updates (Done, Backlog, Deferred, …)
WITHOUT touching the original workbooks: edits are persisted to a local override
file (``data/pm_schedule_updates.json``) keyed by the stable ``pmTaskId`` and
merged back when the dashboard builds the PM payload.

IMPORTANT — no auto-completion. A PM is treated as completed ONLY when a user
manually marks it Done and enters a completion date. The scheduled date is the
EXPECTED target week/date, not proof of completion. "Overdue" is computed
dynamically for display (scheduled week passed while still Scheduled); it is never
stored and never fills a completion date.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from maintenance_service import DATA_DIR

OVERRIDES_PATH = Path(DATA_DIR) / "pm_schedule_updates.json"

# Stored status vocabulary. The default/unedited status is "Pending" and changes
# ONLY when a user edits the task. "Overdue" is a derived metric (KPI/calendar),
# never the stored status.
STATUS_SCHEDULED = "Pending"
STATUS_DONE = "Done"
STATUS_BACKLOG = "Backlog"
STATUS_DEFERRED = "Deferred"
STATUS_NOT_APPLICABLE = "Not Applicable"
STATUS_CANCELLED = "Cancelled"
STATUS_OVERDUE = "Overdue"   # display only

ALLOWED_STATUSES = {
    STATUS_SCHEDULED, STATUS_DONE, STATUS_BACKLOG,
    STATUS_DEFERRED, STATUS_NOT_APPLICABLE, STATUS_CANCELLED,
}

# Legacy values that may exist in older saved files → map to the new vocabulary.
_LEGACY_STATUS_MAP = {
    "scheduled": STATUS_SCHEDULED,                          # renamed Scheduled → Pending
    "auto done / pending verification": STATUS_SCHEDULED,   # auto-done removed → revert to Pending
    "auto done": STATUS_SCHEDULED,
    "not done / backlog": STATUS_BACKLOG,
}

_MONTH_LABELS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

_cache = {"sig": None, "data": None}


# ── Persistence ─────────────────────────────────────────────────────────────────
def _file_sig():
    try:
        st = OVERRIDES_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_overrides() -> dict:
    """Return {pmTaskId: override_record}. Cached by file mtime+size."""
    sig = _file_sig()
    if sig is not None and _cache["sig"] == sig and _cache["data"] is not None:
        return _cache["data"]
    data = {}
    if OVERRIDES_PATH.exists():
        try:
            with open(OVERRIDES_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data = {str(k): v for k, v in raw.get("updates", raw).items() if isinstance(v, dict)}
        except (json.JSONDecodeError, OSError, ValueError):
            data = {}
    _cache.update(sig=sig, data=data)
    return data


def _atomic_write(payload: dict) -> None:
    OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(OVERRIDES_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, OVERRIDES_PATH)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    _cache.update(sig=_file_sig(), data=None)  # force reload next read


def _clean(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize_status(value) -> str:
    text = _clean(value)
    if text in ALLOWED_STATUSES:
        return text
    return _LEGACY_STATUS_MAP.get(text.lower(), text)


def _valid_date(value) -> bool:
    value = _clean(value)
    if not value:
        return True  # empty allowed; "required" is enforced separately
    try:
        datetime.fromisoformat(value[:10])
        return True
    except ValueError:
        return False


def validate_update(fields: dict) -> tuple[bool, str]:
    """Mirror the UI rules so the API is the source of truth for validity."""
    status = normalize_status(fields.get("status"))
    if status not in ALLOWED_STATUSES:
        return False, f"Invalid status '{status}'."
    for key in ("completionDate", "rescheduledDate", "scheduledDate"):
        if not _valid_date(fields.get(key)):
            return False, f"Invalid date for {key}."
    if status == STATUS_DONE and not _clean(fields.get("completionDate")):
        return False, "A completion date is required when status is Done."
    if status == STATUS_BACKLOG and not _clean(fields.get("reason")):
        return False, "A backlog reason is required when status is Backlog."
    if status == STATUS_DEFERRED:
        if not _clean(fields.get("rescheduledDate")):
            return False, "A rescheduled date is required when status is Deferred."
        if not (_clean(fields.get("reason")) or _clean(fields.get("remarks"))):
            return False, "A reason is required when status is Deferred."
    if status == STATUS_NOT_APPLICABLE and not _clean(fields.get("remarks")):
        return False, "Remarks are required when status is Not Applicable."
    if status == STATUS_CANCELLED and not _clean(fields.get("remarks")):
        return False, "Remarks are required when status is Cancelled."
    return True, ""


def save_override(task_id: str, fields: dict) -> dict:
    """Validate + persist one override record. Returns the stored record."""
    task_id = _clean(task_id)
    if not task_id:
        raise ValueError("A pmTaskId is required to save a PM update.")
    ok, message = validate_update(fields)
    if not ok:
        raise ValueError(message)

    status = normalize_status(fields.get("status"))
    record = {
        "pmTaskId": task_id,
        "status": status,
        # Only a Done task may carry a completion date — never auto-filled.
        "completionDate": _clean(fields.get("completionDate")) if status == STATUS_DONE else "",
        "contractorOrPIC": _clean(fields.get("contractorOrPIC")),
        "remarks": _clean(fields.get("remarks")),
        "rescheduledDate": _clean(fields.get("rescheduledDate")),
        "reason": _clean(fields.get("reason")),
        # Optional corrections to the imported plan (applied on display, source stays read-only).
        "scheduledDate": _clean(fields.get("scheduledDate")),
        "pmDescription": _clean(fields.get("pmDescription")),
        "updatedBy": _clean(fields.get("updatedBy")) or "dashboard-user",
        "lastUpdated": datetime.now().isoformat(timespec="seconds"),
    }

    store = dict(load_overrides())
    store[task_id] = record
    _atomic_write({
        "schemaVersion": 2,
        "note": "Local PM status overrides merged over read-only source schedules. Do not edit by hand while the app is running.",
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "updates": store,
    })
    return record


def clear_override(task_id: str) -> bool:
    """Remove a manual override (revert to plain Scheduled)."""
    task_id = _clean(task_id)
    store = dict(load_overrides())
    if task_id in store:
        del store[task_id]
        _atomic_write({"schemaVersion": 2, "updatedAt": datetime.now().isoformat(timespec="seconds"), "updates": store})
        return True
    return False


# ── Enrichment ──────────────────────────────────────────────────────────────────
def _parse_iso(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (ValueError, TypeError):
        return None


def _set_planned(task: dict, d: date) -> None:
    task["plannedDate"] = d.isoformat()
    task["plannedYear"] = d.year
    task["plannedMonth"] = d.month
    task["plannedMonthLabel"] = _MONTH_LABELS[d.month - 1]
    task["plannedQuarter"] = f"Q{(d.month - 1) // 3 + 1}"
    task["plannedDateLabel"] = d.strftime("%d %b %Y")


def derive_operational_fields(task: dict, today: date) -> None:
    """Compute display/aggregation fields from the (already-set) operational status.

    Completion is MANUAL ONLY. Overdue is dynamic. Sets: isDone, isOverdue,
    isOverdueOp, isBacklog, isDeferred, isLateCompleted, isOnTimeCompleted,
    isDueThisMonth, isDueSoon, displayStatus, daysOverdue.
    """
    status = normalize_status(task.get("status") or STATUS_SCHEDULED)
    task["status"] = status
    planned = _parse_iso(task.get("plannedDate"))
    week_end = (planned + timedelta(days=6)) if planned else None
    task["scheduledWeekEnd"] = week_end.isoformat() if week_end else None
    completion = _parse_iso(task.get("actualCompletionDate"))

    is_done = status == STATUS_DONE
    week_passed = bool(week_end is not None and week_end < today)
    is_overdue = bool(status == STATUS_SCHEDULED and week_passed)

    task["isDone"] = is_done
    task["isOverdue"] = is_overdue
    task["isOverdueOp"] = is_overdue
    task["isBacklog"] = status == STATUS_BACKLOG
    task["isDeferred"] = status == STATUS_DEFERRED
    task["isLateCompleted"] = bool(is_done and completion and week_end and completion > week_end)
    task["isOnTimeCompleted"] = bool(is_done and completion and week_end and completion <= week_end)
    task["isDueThisMonth"] = bool(
        planned and status == STATUS_SCHEDULED
        and planned.year == today.year and planned.month == today.month
    )
    task["isDueSoon"] = bool(
        planned and status == STATUS_SCHEDULED and today < planned <= today + timedelta(days=30)
    )
    task["daysOverdue"] = max((today - week_end).days, 0) if (is_overdue and week_end) else 0
    # Status stays as stored (Pending until a user edits it). Overdue is exposed
    # only as a derived metric (isOverdueOp), not as the task's status.
    task["displayStatus"] = status
    task["completionDate"] = task.get("actualCompletionDate")
    # never an inferred/auto completion
    task["autoUpdated"] = False


def apply_overrides(tasks: list[dict], today: date | None = None) -> dict:
    """Merge saved overrides into imported tasks (manual-only completion).

    No auto-done. Deferred tasks move to their rescheduled date (the original date
    is kept for traceability). Returns a small stats dict.
    """
    today = today or datetime.now().date()
    overrides = load_overrides()
    manual = 0

    for task in tasks:
        # Manual planner tasks manage their own status/source — never override-driven.
        if task.get("source") == "Manual":
            continue

        record = overrides.get(task.get("pmTaskId"))

        # An override may correct the imported scheduled date / description.
        if record and record.get("scheduledDate"):
            corrected = _parse_iso(record.get("scheduledDate"))
            if corrected:
                _set_planned(task, corrected)
        if record and record.get("pmDescription"):
            task["pmDescription"] = record["pmDescription"]

        if record:
            manual += 1
            task["source"] = "Edited Imported"
            status = normalize_status(record.get("status")) or STATUS_SCHEDULED
            task["status"] = status
            task["actualCompletionDate"] = record.get("completionDate") or None
            if record.get("contractorOrPIC"):
                task["contractorOrPIC"] = record["contractorOrPIC"]
            task["remarks"] = record.get("remarks") or ""
            task["reason"] = record.get("reason") or ""
            task["rescheduledDate"] = record.get("rescheduledDate") or None
            task["lastUpdated"] = record.get("lastUpdated")
            task["updatedBy"] = record.get("updatedBy")
            # Deferred: keep the original date, plan the task on the rescheduled date.
            if status == STATUS_DEFERRED and task["rescheduledDate"]:
                task.setdefault("originalScheduledDate", task.get("plannedDate"))
                new_date = _parse_iso(task["rescheduledDate"])
                if new_date:
                    _set_planned(task, new_date)
        else:
            task["source"] = "Imported"
            task["status"] = STATUS_SCHEDULED
            task["actualCompletionDate"] = None
            task.setdefault("remarks", "")
            task["reason"] = ""
            task["rescheduledDate"] = None
            task["lastUpdated"] = None
            task["updatedBy"] = None

        derive_operational_fields(task, today)

    return {"manualOverrides": manual, "overrideCount": len(overrides)}


# Backwards-compatible alias (old name used elsewhere).
def apply_overrides_and_autodone(tasks: list[dict], today: date | None = None) -> dict:
    return apply_overrides(tasks, today)
