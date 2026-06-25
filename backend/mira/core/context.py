"""
Filter / context resolution for MIRA.

Normalises the public MIRA filter object and maps it onto the parameters the
existing dashboard builders already expect. This is the ONLY place that decides
how a (stage, year, month) filter becomes a downtime "period", a PM month, etc.,
so every MIRA function targets the same window the dashboard would.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime

# Public filter keys MIRA accepts (camelCase, matching the dashboard / spec).
FILTER_KEYS = (
    "stage", "year", "month",
    "period_mode", "start", "end",
    "assetId", "assetName", "mainAssetGroup", "subAssetGroup",
    "maintenanceType", "status", "mappingStatus",
)

# Period modes (default is YTD).
_PERIOD_MODES = {"ytd", "monthly", "full_year", "financial_year", "custom"}


def _parse_iso_date(value):
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None

_STAGE_ALIASES = {
    "all": "all", "": "all", "none": "all",
    "stage1": "stage1", "stage 1": "stage1", "s1": "stage1", "1": "stage1",
    "stage2": "stage2", "stage 2": "stage2", "s2": "stage2", "2": "stage2",
}


def _to_int(value):
    try:
        if value in (None, "", "all"):
            return None
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_month(value):
    """Accept 6, '6', '06', 'June', or '2026-06' -> month int 1..12 (or None)."""
    if value in (None, "", "all"):
        return None
    text = str(value).strip()
    if "-" in text:  # 'YYYY-MM'
        parts = text.split("-")
        if len(parts) >= 2:
            return _to_int(parts[1])
    as_int = _to_int(text)
    if as_int and 1 <= as_int <= 12:
        return as_int
    for idx, name in enumerate(calendar.month_name):
        if name and name.lower().startswith(text.lower()[:3]):
            return idx
    return None


def normalize_filters(raw: dict | None) -> dict:
    """Return a clean filter dict with safe defaults. Never raises."""
    raw = raw or {}
    stage = _STAGE_ALIASES.get(str(raw.get("stage", "all")).strip().lower(), "all")
    year = _to_int(raw.get("year"))
    month = _parse_month(raw.get("month"))
    today = datetime.now()
    if year is None:
        year = today.year

    def clean(key):
        val = raw.get(key)
        if val in (None, "", "all"):
            return None
        return str(val).strip()

    # Period mode: explicit wins; else infer (month -> monthly, no month -> ytd).
    mode = str(raw.get("period_mode") or "").strip().lower()
    if mode not in _PERIOD_MODES:
        mode = "monthly" if month else "ytd"
    if mode == "monthly" and not month:
        mode = "ytd"

    return {
        "stage": stage,
        "year": year,
        "month": month,                      # int 1..12 or None
        "period_mode": mode,
        "start": _parse_iso_date(raw.get("start")),
        "end": _parse_iso_date(raw.get("end")),
        "assetId": clean("assetId"),
        "assetName": clean("assetName"),
        "mainAssetGroup": clean("mainAssetGroup"),
        "subAssetGroup": clean("subAssetGroup"),
        "maintenanceType": clean("maintenanceType"),
        "status": clean("status"),
        "mappingStatus": clean("mappingStatus"),
    }


def resolved_window(filters: dict) -> dict:
    """Resolved calendar window honouring period_mode (default YTD)."""
    year = int(filters["year"])
    month = filters.get("month")
    mode = filters.get("period_mode") or ("monthly" if month else "ytd")
    today = datetime.now().date()

    if mode == "monthly" and month:
        last_day = calendar.monthrange(year, month)[1]
        return {"mode": "month", "label": f"{calendar.month_name[month]} {year}",
                "start_date": date(year, month, 1), "end_date": date(year, month, last_day)}

    if mode == "custom":
        start = filters.get("start")
        end = filters.get("end")
        if start and end and start <= end:
            return {"mode": "custom", "label": f"{start:%d %b %Y} – {end:%d %b %Y}",
                    "start_date": start, "end_date": end}
        # fall through to YTD if a custom range was not fully provided.

    if mode == "financial_year":
        # FY label = the calendar year the FY ends in (Apr year-1 .. Mar year).
        start = date(year - 1, 4, 1)
        end = date(year, 3, 31)
        if end > today:
            end = today
        return {"mode": "financial_year", "label": f"FY{year}", "start_date": start, "end_date": end}

    if mode == "full_year":
        return {"mode": "full_year", "label": f"Full Year {year}",
                "start_date": date(year, 1, 1), "end_date": date(year, 12, calendar.monthrange(year, 12)[1])}

    # ytd (default). Current year -> Jan 1 to today; past year -> full year.
    if year == today.year:
        return {"mode": "ytd", "label": f"YTD {year}", "start_date": date(year, 1, 1), "end_date": today}
    return {"mode": "full_year", "label": f"Full Year {year}",
            "start_date": date(year, 1, 1), "end_date": date(year, 12, calendar.monthrange(year, 12)[1])}


def month_label(filters: dict) -> str:
    """Human label for the resolved window, e.g. 'June 2026' or 'Full Year 2025'."""
    return resolved_window(filters)["label"]


def month_value(filters: dict) -> str | None:
    """'YYYY-MM' string for builders that key on a month, else None."""
    if filters.get("month"):
        return f"{filters['year']}-{filters['month']:02d}"
    return None


def resolve_downtime_period(filters: dict) -> dict:
    """Map MIRA filters onto build_downtime_payload(period, month, start, end).

    Derived from the resolved window so every period_mode is honoured consistently.
    """
    window = resolved_window(filters)
    if window["mode"] == "month":
        return {"period": "this_month", "month": month_value(filters), "start": None, "end": None}
    if window["mode"] == "ytd":
        return {"period": "ytd", "month": None, "start": None, "end": None}
    # full_year / financial_year / custom -> an explicit custom date window.
    return {
        "period": "custom",
        "month": None,
        "start": window["start_date"].isoformat(),
        "end": window["end_date"].isoformat(),
    }
