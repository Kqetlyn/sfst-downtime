"""Daily MR triage backend — scope-aware, local-Ollama only.

For the selected scope (Stage 1 / Stage 2 / All) this:
  1. Pulls the review-day's Maintenance Requests for that scope (imported MR data,
     filtered by the asset's Stage — see downtime_service.filter_work_orders_by_stage).
  2. Pulls recent history (default 90d) for those same assets, WITHIN THE SAME SCOPE.
  3. Sends both to the LOCAL Ollama model with the scope-injected triage prompt and
     gets back a RAG verdict (JSON).
  4. Stores the verdict keyed by (scope, date).
  5. Serves it from GET /api/mira/verdict?scope=<scope> (wired in mira/api.py).

Hard rules honoured here:
  * Scope is a runtime parameter — never hardcoded. Storage key, prompt, pull filter
    and the returned `scope` all reflect it.
  * Scopes stay separated: a single-scope verdict never pulls another scope's assets
    or history. "All" is synthesised by MERGING the stored single-scope verdicts, so
    recurrence is always evaluated within each item's own scope.
  * No external AI. The model call goes to the local Ollama loopback only
    (mira.providers.ollama_provider). No company data leaves the machine.
  * Fail safe: no MRs -> Green "No MRs raised"; a failed run keeps the last good
    verdict and the endpoint never returns malformed JSON.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time as _time
from datetime import datetime, timedelta, date as _date
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

import downtime_service as _dt

# ── Config (env / App Settings; sensible local defaults) ──────────────────────
_BASE_DIR = Path(__file__).resolve().parent
_DATA_DIR = _BASE_DIR.parent / "data"
VERDICT_DIR = _DATA_DIR / "mr_triage_verdicts"          # gitignored (data/*)
PROMPT_PATH = _BASE_DIR / "mr_triage_prompt.md"

# Scopes the morning job precomputes. Single scopes only — "All" is merged on read.
SCOPES = [s.strip() for s in os.environ.get("SCOPES", "Stage 1, Stage 2").split(",") if s.strip()]
MR_HISTORY_DAYS = int(os.environ.get("MR_HISTORY_DAYS", "90"))
RUN_TIME = os.environ.get("RUN_TIME", "06:00")           # HH:MM, facility tz
RUN_TIMEZONE = os.environ.get("RUN_TIMEZONE", "Asia/Bangkok")
RECURRENCE_MIN = int(os.environ.get("MR_TRIAGE_RECURRENCE_MIN", "2"))  # >= this many prior MRs = recurrence
TRIAGE_TIMEOUT = int(os.environ.get("MR_TRIAGE_TIMEOUT", "150"))  # daily batch — not latency-sensitive
# Optional: when the review day has no MRs (e.g. a static imported file whose data
# ends weeks ago), fall back to the latest day that DOES have MRs so the widget is
# demonstrable. Off by default — production with live data should use the real day.
FALLBACK_LATEST = os.environ.get("MR_TRIAGE_FALLBACK_LATEST", "0") not in {"0", "false", "no"}

_RAG_VALUES = {"Red", "Amber", "Green"}
_SEV_VALUES = {"S1", "S2", "S3", "S4"}

# ── tz / date helpers ─────────────────────────────────────────────────────────
def _facility_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(RUN_TIMEZONE))
        except Exception:
            pass
    return datetime.now()


def _yesterday() -> _date:
    return (_facility_now() - timedelta(days=1)).date()


def _parse_dt(value):
    if not value:
        return None
    try:
        return _dt.parse_iso_datetime(value)
    except Exception:
        try:
            return datetime.fromisoformat(str(value)[:19])
        except Exception:
            return None


def _row_date(row) -> _date | None:
    d = _parse_dt(row.get("request_created_time") or row.get("created_date") or row.get("start_time"))
    return d.date() if d else None


def _scope_slug(scope: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(scope or "").lower()).strip("-") or "scope"


def _normalize_scope_label(scope: str) -> str:
    norm = _dt.normalize_stage_filter(scope)
    if norm:
        return norm                       # "Stage 1" / "Stage 2" / ...
    key = re.sub(r"[^a-z0-9]+", "", str(scope or "").lower())
    return "All" if key in {"", "all", "allstages"} else str(scope).strip()


# ── MR field extraction (from management.work_orders rows) ───────────────────
def _mr_id(row):
    return str(row.get("request_id") or row.get("maintenance_order_id") or row.get("work_order_id") or "").strip()


def _wo_id(row):
    return str(row.get("work_order_id") or "").strip()


def _asset_id(row):
    return str(row.get("asset_id") or row.get("machine_code") or "").strip()


def _asset_name(row):
    return str(
        row.get("asset_display_name") or row.get("machine_name_display") or row.get("machine_name")
        or row.get("machine_group") or _asset_id(row) or "Unknown asset"
    ).strip()


def _severity(row):
    return str(row.get("service_level") or row.get("priority") or row.get("severity") or "").strip()


def _description(row):
    return str(row.get("description") or row.get("description_original") or "").strip()


def _translated(row):
    t = str(row.get("translated_description") or "").strip()
    return t if t and t != _description(row) else ""


# ── Data pulls (per scope, by stage) ──────────────────────────────────────────
def _scope_work_orders(scope_label: str) -> list[dict]:
    """All-years work orders for a scope, filtered by the asset's Stage.

    Reuses the same builder the downtime dashboard uses, so the stage scoping and
    field shape are identical to the rest of the app. For 'All' no stage filter is
    applied (every scope included)."""
    stage = "" if scope_label == "All" else scope_label
    payload = _dt.build_downtime_payload(period="all_years", work_orders_only=True, stage=stage)
    mgmt = payload.get("management") or {}
    return list(mgmt.get("work_orders") or [])


def _latest_mr_date(rows) -> _date | None:
    dates = [d for d in (_row_date(r) for r in rows) if d]
    return max(dates) if dates else None


def pull_review_day_mrs(scope_label: str, review_date: _date, rows: list[dict] | None = None):
    """MRs raised on `review_date` for the scope. Returns (mrs, asset_ids)."""
    rows = rows if rows is not None else _scope_work_orders(scope_label)
    mrs, asset_ids = [], set()
    for r in rows:
        if _row_date(r) != review_date:
            continue
        aid = _asset_id(r)
        mrs.append({
            "mr_id": _mr_id(r),
            "wo_id": _wo_id(r),
            "asset_id": aid,
            "asset_name": _asset_name(r),
            "severity": _severity(r),
            "description": _description(r),
            "translated_description": _translated(r),
            "created": str(r.get("request_created_time") or ""),
        })
        if aid:
            asset_ids.add(aid)
    return mrs, asset_ids


def pull_history(scope_label: str, asset_ids: set[str], review_date: _date,
                 days: int = MR_HISTORY_DAYS, rows: list[dict] | None = None) -> dict[str, list[dict]]:
    """Prior MRs (window before review_date) for the given assets, same scope.

    Returns {asset_id: [ {mr_id, asset_name, description, created}, ... newest first ]}."""
    if not asset_ids:
        return {}
    rows = rows if rows is not None else _scope_work_orders(scope_label)
    window_start = review_date - timedelta(days=days)
    history: dict[str, list[dict]] = {aid: [] for aid in asset_ids}
    for r in rows:
        aid = _asset_id(r)
        if aid not in history:
            continue
        d = _row_date(r)
        if d is None or d >= review_date or d < window_start:
            continue
        history[aid].append({
            "mr_id": _mr_id(r),
            "asset_name": _asset_name(r),
            "description": _description(r) or _translated(r),
            "created": str(r.get("request_created_time") or ""),
        })
    for aid in history:
        history[aid].sort(key=lambda x: x.get("created") or "", reverse=True)
    return history


# ── Prompt + Ollama call ──────────────────────────────────────────────────────
def _load_system_prompt(scope_label: str) -> str:
    try:
        text = PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        text = "You are MIRA, a local maintenance triage assistant. Return only valid JSON."
    marker = "## System prompt"
    if marker in text:
        text = text.split(marker, 1)[1]
    return text.replace("{{SCOPE}}", scope_label).strip()


def _build_user_message(scope_label: str, review_date: _date, mrs, history) -> str:
    enriched = []
    for mr in mrs:
        aid = mr["asset_id"]
        prior = history.get(aid, [])
        enriched.append({
            "asset_name": mr["asset_name"],
            "asset_id": aid,
            "mr_id": mr["mr_id"],
            "severity": mr["severity"],
            "description": mr["description"],
            "translated_description": mr["translated_description"],
            "created": mr["created"],
            "prior_mr_count_in_window": len(prior),
            "recent_prior_descriptions": [p["description"] for p in prior[:3] if p.get("description")],
        })
    payload = {
        "scope": scope_label,
        "date_reviewed": review_date.isoformat(),
        "history_window_days": MR_HISTORY_DAYS,
        "mrs_raised_on_review_date": enriched,
    }
    return (
        f"Scope: {scope_label}\n"
        f"Review date: {review_date.isoformat()}\n"
        f"There are {len(mrs)} MR(s) raised on the review date for this scope.\n"
        "Use prior_mr_count_in_window and recent_prior_descriptions to judge recurrence "
        "(recurrence is true when the same asset has prior MRs in the window).\n"
        "Return ONLY the verdict JSON in the required schema.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _call_ollama(system_prompt: str, user_message: str) -> str:
    from mira.providers.ollama_provider import generate_with_ollama
    return generate_with_ollama(system_prompt, user_message, format_json=True, timeout=TRIAGE_TIMEOUT)


def _parse_verdict_json(raw_text: str) -> dict | None:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```"))
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group())
        except Exception:
            return None


# ── Verdict shaping + validation ──────────────────────────────────────────────
def _green_verdict(scope_label: str, review_date: _date, summary: str) -> dict:
    return {
        "scope": scope_label,
        "date_reviewed": review_date.isoformat(),
        "overall_verdict": "Green",
        "summary": summary,
        "items": [],
        "watchlist": [],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provider": "n/a",
    }


def _coerce_item(item: dict, scope_label: str, recurrence_lookup: dict[str, int]) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = str(item.get("asset_name") or "").strip()
    if not name:
        return None
    rag = str(item.get("rag") or "Green").title()
    if rag not in _RAG_VALUES:
        rag = "Green"
    sev = str(item.get("suggested_severity") or "S4").upper().strip()
    if sev not in _SEV_VALUES:
        sev = "S4"
    item_scope = str(item.get("scope") or scope_label).strip() or scope_label
    rec_count = recurrence_lookup.get(name.lower())
    recurrence = bool(item.get("recurrence"))
    if rec_count is not None and rec_count >= RECURRENCE_MIN:
        recurrence = True
    return {
        "asset_name": name,
        "scope": item_scope,
        "rag": rag,
        "suggested_severity": sev,
        "recurrence": recurrence,
        "recurrence_note": str(item.get("recurrence_note") or (f"{rec_count} prior MR(s) in {MR_HISTORY_DAYS}d" if rec_count else "")).strip(),
        "escalation_flag": bool(item.get("escalation_flag")),
        "reason": str(item.get("reason") or "").strip(),
    }


def _validate_verdict(v: dict) -> bool:
    if not isinstance(v, dict):
        return False
    if v.get("overall_verdict") not in _RAG_VALUES:
        return False
    if not isinstance(v.get("items"), list):
        return False
    if not isinstance(v.get("scope"), str) or not v.get("date_reviewed"):
        return False
    for it in v["items"]:
        if not isinstance(it, dict) or it.get("rag") not in _RAG_VALUES:
            return False
        if it.get("suggested_severity") not in _SEV_VALUES:
            return False
    return True


def _shape_verdict(parsed: dict, scope_label: str, review_date: _date, mrs, history, provider: str) -> dict:
    recurrence_lookup = {}
    for mr in mrs:
        recurrence_lookup[mr["asset_name"].lower()] = len(history.get(mr["asset_id"], []))
    items = [_coerce_item(i, scope_label, recurrence_lookup) for i in (parsed.get("items") or [])]
    items = [i for i in items if i]
    overall = str(parsed.get("overall_verdict") or "Green").title()
    if overall not in _RAG_VALUES:
        overall = "Red" if any(i["rag"] == "Red" for i in items) else "Amber" if any(i["rag"] == "Amber" for i in items) else "Green"
    watchlist = parsed.get("watchlist")
    if not isinstance(watchlist, list) or not watchlist:
        watchlist = [i["asset_name"] for i in items if i["rag"] in {"Red", "Amber"}]
    return {
        "scope": scope_label,
        "date_reviewed": review_date.isoformat(),
        "overall_verdict": overall,
        "summary": str(parsed.get("summary") or "").strip() or f"{len(items)} item(s) flagged for review.",
        "items": items,
        "watchlist": [str(w) for w in watchlist][:25],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provider": provider,
    }


# ── Storage ───────────────────────────────────────────────────────────────────
def _verdict_path(scope_label: str, review_date: _date) -> Path:
    return VERDICT_DIR / f"{_scope_slug(scope_label)}_{review_date.isoformat()}.json"


def save_verdict(verdict: dict) -> None:
    try:
        VERDICT_DIR.mkdir(parents=True, exist_ok=True)
        scope_label = verdict.get("scope") or "scope"
        review_date = verdict.get("date_reviewed") or _yesterday().isoformat()
        path = VERDICT_DIR / f"{_scope_slug(scope_label)}_{review_date}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        print(f"[MR triage] save failed: {exc}")


def load_latest_verdict(scope_label: str) -> dict | None:
    try:
        slug = _scope_slug(scope_label)
        files = sorted(VERDICT_DIR.glob(f"{slug}_*.json"))
        if not files:
            return None
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


# ── Run one scope ─────────────────────────────────────────────────────────────
def run_triage_for_scope(scope_input: str, review_date: _date | None = None) -> dict:
    """Pull -> Ollama -> validate -> store -> return the verdict for one scope."""
    scope_label = _normalize_scope_label(scope_input)
    rows = _scope_work_orders(scope_label)

    if review_date is None:
        review_date = _yesterday()
        if FALLBACK_LATEST:
            latest = _latest_mr_date(rows)
            # if yesterday has no MRs but the (static) data has earlier MRs, use the latest day
            if latest and not any(_row_date(r) == review_date for r in rows):
                review_date = latest

    mrs, asset_ids = pull_review_day_mrs(scope_label, review_date, rows=rows)
    if not mrs:
        verdict = _green_verdict(scope_label, review_date, "No MRs raised for this scope on the review date.")
        save_verdict(verdict)
        return verdict

    history = pull_history(scope_label, asset_ids, review_date, rows=rows)
    system_prompt = _load_system_prompt(scope_label)
    user_message = _build_user_message(scope_label, review_date, mrs, history)

    try:
        raw = _call_ollama(system_prompt, user_message)
        parsed = _parse_verdict_json(raw)
        if parsed is None:
            raise ValueError("model returned non-JSON")
        verdict = _shape_verdict(parsed, scope_label, review_date, mrs, history, provider="ollama")
        if not _validate_verdict(verdict):
            raise ValueError("verdict failed schema validation")
    except Exception as exc:
        print(f"[MR triage] {scope_label} {review_date}: model run failed ({exc}); keeping last good verdict.")
        last = load_latest_verdict(scope_label)
        if last:
            return last
        # No prior verdict — degrade to a deterministic Amber/Green built from recurrence only.
        items = []
        for mr in mrs:
            prior = len(history.get(mr["asset_id"], []))
            if prior >= RECURRENCE_MIN:
                items.append({
                    "asset_name": mr["asset_name"], "scope": scope_label, "rag": "Amber",
                    "suggested_severity": "S3", "recurrence": True,
                    "recurrence_note": f"{prior} prior MR(s) in {MR_HISTORY_DAYS}d",
                    "escalation_flag": False, "reason": "Recurring MRs on this asset (AI unavailable; rule-based flag).",
                })
        verdict = {
            "scope": scope_label, "date_reviewed": review_date.isoformat(),
            "overall_verdict": "Amber" if items else "Green",
            "summary": f"AI unavailable — {len(items)} recurring asset(s) flagged by rule-based fallback." if items else "AI unavailable; no recurring assets detected.",
            "items": items, "watchlist": [i["asset_name"] for i in items],
            "generated_at": datetime.now().isoformat(timespec="seconds"), "provider": "rule_based_fallback",
        }
    save_verdict(verdict)
    return verdict


def run_all_scopes(review_date: _date | None = None) -> dict[str, dict]:
    out = {}
    for scope in SCOPES:
        try:
            out[scope] = run_triage_for_scope(scope, review_date)
        except Exception as exc:
            print(f"[MR triage] scope {scope} failed: {exc}")
    return out


# ── Read API (single scope or merged 'All') ───────────────────────────────────
def get_verdict(scope_input: str) -> dict:
    """Latest stored verdict for the scope. 'All' merges the stored single-scope
    verdicts so items keep their own scope and recurrence stays within-scope."""
    scope_label = _normalize_scope_label(scope_input)
    if scope_label != "All":
        v = load_latest_verdict(scope_label)
        return v or _green_verdict(scope_label, _yesterday(),
                                   "No triage has run yet for this scope. It will populate on the next morning run.")
    # Merge every configured single scope.
    merged_items, watch, dates, providers = [], [], [], []
    overall_rank = {"Green": 0, "Amber": 1, "Red": 2}
    overall = "Green"
    any_found = False
    for scope in SCOPES:
        v = load_latest_verdict(scope)
        if not v:
            continue
        any_found = True
        merged_items.extend(v.get("items") or [])
        watch.extend(v.get("watchlist") or [])
        dates.append(v.get("date_reviewed"))
        providers.append(v.get("provider"))
        if overall_rank.get(v.get("overall_verdict"), 0) > overall_rank[overall]:
            overall = v.get("overall_verdict")
    if not any_found:
        return _green_verdict("All", _yesterday(), "No triage has run yet. It will populate on the next morning run.")
    return {
        "scope": "All",
        "date_reviewed": max([d for d in dates if d], default=_yesterday().isoformat()),
        "overall_verdict": overall,
        "summary": f"Combined view across {len([s for s in SCOPES if load_latest_verdict(s)])} scope(s); {len(merged_items)} item(s) flagged.",
        "items": merged_items,
        "watchlist": sorted(set(str(w) for w in watch))[:25],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provider": ",".join(sorted(set(p for p in providers if p))) or "n/a",
    }


# ── Daily scheduler (background thread) ───────────────────────────────────────
_scheduler_started = False


def _seconds_until_run() -> float:
    now = _facility_now()
    try:
        hh, mm = (int(x) for x in RUN_TIME.split(":")[:2])
    except Exception:
        hh, mm = 6, 0
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


def start_scheduler(run_on_start: bool = True) -> None:
    """Start the once-a-morning triage thread. Idempotent."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def loop():
        # Optional: ensure today's verdict exists shortly after boot (so the widget
        # isn't empty until 06:00). Delayed so it doesn't fight the cache warm-up.
        if run_on_start:
            _time.sleep(float(os.environ.get("MR_TRIAGE_BOOT_DELAY", "90")))
            try:
                for scope in SCOPES:
                    if load_latest_verdict(scope) is None:
                        run_triage_for_scope(scope)
            except Exception as exc:
                print(f"[MR triage] boot run failed: {exc}")
        while True:
            _time.sleep(_seconds_until_run())
            try:
                run_all_scopes()
                print(f"[MR triage] daily run complete for scopes={SCOPES}")
            except Exception as exc:
                print(f"[MR triage] daily run failed: {exc}")
            _time.sleep(60)  # avoid double-fire within the same minute

    threading.Thread(target=loop, name="mr-triage-scheduler", daemon=True).start()
