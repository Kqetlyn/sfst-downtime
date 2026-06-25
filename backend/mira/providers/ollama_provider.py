"""
ollamaMiraProvider — local Ollama wording layer (chat API).

Ollama runs locally (loopback only). MIRA sends it the ALREADY-VERIFIED KPI JSON
and the system prompt; Ollama only writes the human-readable wording around those
numbers. It never fetches data and never produces numbers of its own.

Flow (per spec):
    Frontend MIRA request
    -> dashboard backend
    -> verified metrics function
    -> Ollama /api/chat
    -> structured response
    -> frontend render

If Ollama is not installed/running or errors/timeouts, MIRA falls back to a
deterministic rule-based summary built from the same verified metrics.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .. import config
from .base_provider import BaseMiraProvider
from .mock_provider import MockMiraProvider

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# Structured summary schema returned to the frontend (Overview / chat answers).
_SUMMARY_KEYS = (
    "executive_summary", "key_numbers_used", "key_observations", "main_concern",
    "recommended_follow_up", "one_line_summary", "data_notes",
)


def _is_loopback(host: str) -> bool:
    return any(h in host for h in _LOOPBACK_HOSTS)


# ── Low-level call (per spec: generate_with_ollama) ──────────────────────────────
def generate_with_ollama(system_prompt: str, user_prompt: str, *,
                         model: str | None = None, base_url: str | None = None,
                         timeout: int | None = None, format_json: bool = False) -> str:
    """POST to the local Ollama /api/chat and return the assistant message text.

    Raises on any transport/loopback/HTTP error so callers can fall back.
    When ``format_json`` is set, Ollama is constrained to emit valid JSON.
    """
    host = (base_url or config.OLLAMA_HOST).rstrip("/")
    if not _is_loopback(host):
        raise RuntimeError("Ollama host is not loopback; refusing external call")
    payload = {
        "model": model or config.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        # Low temperature: keep wording faithful to the verified numbers.
        "options": {"temperature": 0.1},
    }
    if format_json:
        payload["format"] = "json"
    req = urllib.request.Request(
        f"{host}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout or config.OLLAMA_TIMEOUT_SECONDS) as resp:
        parsed = json.loads(resp.read().decode("utf-8"))
    # /api/chat returns {"message": {"role": "assistant", "content": "..."}}
    return (parsed.get("message") or {}).get("content", "") or ""


def _build_user_prompt(*, question: str | None, filters: dict | None,
                       metrics: dict, warnings: list[str] | None) -> str:
    verified = json.dumps(metrics, default=str, ensure_ascii=False, indent=2)
    filt = json.dumps(filters or {}, default=str, ensure_ascii=False)
    warn = json.dumps(warnings or [], default=str, ensure_ascii=False)
    schema = json.dumps({k: ("" if k in ("executive_summary", "main_concern", "one_line_summary") else [])
                         for k in _SUMMARY_KEYS}, indent=2)
    ask = (question or "").strip() or "Produce the daily maintenance overview summary."
    return (
        f"USER_QUESTION:\n{ask}\n\n"
        f"SELECTED_FILTERS:\n{filt}\n\n"
        f"DATA_WARNINGS:\n{warn}\n\n"
        f"VERIFIED_METRICS_JSON (the only source of numbers — do not invent any):\n{verified}\n\n"
        "Return ONLY valid JSON (no prose, no markdown fences) matching exactly this schema. "
        "Every list field is an array of short plain strings. key_numbers_used must be strings "
        "in the form 'Label: value' (for example 'MR Raised: 199'), each quoting an exact value "
        "from the verified JSON — never invent or recompute a number. Leave a field empty if the "
        f"data does not support it:\n{schema}\n"
    )


def _coerce_summary(raw_text: str) -> dict | None:
    """Parse the model's JSON object, tolerating stray text/code fences."""
    if not raw_text:
        return None
    text = raw_text.strip()
    if "```" in text:
        # strip code fences
        parts = text.split("```")
        text = max(parts, key=len)
        text = text.replace("json", "", 1).strip() if text.lstrip().lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    out = {}
    for k in _SUMMARY_KEYS:
        v = obj.get(k)
        if k in ("executive_summary", "main_concern", "one_line_summary"):
            out[k] = str(v).strip() if v is not None else ""
        else:
            out[k] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else ([str(v)] if v else [])
    return out


class OllamaMiraProvider(BaseMiraProvider):
    name = "ollama"
    is_local_only = True

    def __init__(self) -> None:
        self._fallback = MockMiraProvider()
        self.host = config.OLLAMA_HOST
        self.model = config.OLLAMA_MODEL
        self.timeout = config.OLLAMA_TIMEOUT_SECONDS

    # ── Availability (loopback only) ────────────────────────────────────────────
    def daemon_up(self) -> bool:
        return self._installed_models() is not None

    def _installed_models(self) -> list[str] | None:
        if not _is_loopback(self.host):
            return None
        try:
            req = urllib.request.Request(f"{self.host}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    return None
                tags = json.loads(resp.read().decode("utf-8"))
            return [m.get("name", "") for m in tags.get("models", []) if m.get("name")]
        except Exception:
            return None

    def resolve_model(self) -> str | None:
        models = self._installed_models()
        if not models:
            return None
        for name in models:
            if name == self.model or name.split(":")[0] == self.model.split(":")[0]:
                return name
        return models[0]

    def available(self) -> bool:
        return self.resolve_model() is not None

    # ── Free-text wording (chat answers) ────────────────────────────────────────
    def generate(self, intent: str, data: dict, question: str | None = None) -> str:
        model = self.resolve_model()
        if not model:
            return self._fallback.generate(intent, data, question)
        try:
            text = generate_with_ollama(
                config.MIRA_SYSTEM_PROMPT,
                self._build_text_prompt(intent, data, question),
                model=model,
            ).strip()
            if text:
                return text
        except Exception:
            pass
        return self._fallback.generate(intent, data, question)

    def _build_text_prompt(self, intent: str, data: dict, question: str | None) -> str:
        verified = json.dumps(data, default=str, ensure_ascii=False, indent=2)
        ask = (question or "").strip() or f"Summarise the verified metrics for intent: {intent}."
        return (
            f'USER_QUESTION: "{ask}"\n\n'
            "Use ONLY the verified metrics in the JSON below. Do not introduce any number "
            "not present in it; if a value is null/missing, say it is unavailable. Reply in "
            f"short professional sentences or bullet points.\n\nVERIFIED_METRICS_JSON:\n{verified}\n"
        )


# ── Structured summary (Overview / chat) with rule-based fallback ────────────────
def generate_structured_summary(metrics: dict, *, question: str | None = None,
                                filters: dict | None = None,
                                warnings: list[str] | None = None,
                                timeout: int | None = None) -> dict:
    """Return the structured summary dict; use Ollama if available, else rule-based.

    Always includes a `provider` field so the UI can show the LLM status.
    """
    provider = OllamaMiraProvider()
    model = provider.resolve_model()
    if model:
        try:
            raw = generate_with_ollama(
                config.MIRA_SYSTEM_PROMPT,
                _build_user_prompt(question=question, filters=filters,
                                   metrics=metrics, warnings=warnings),
                model=model,
                timeout=timeout,
                format_json=True,
            )
            parsed = _coerce_summary(raw)
            if parsed and parsed.get("executive_summary"):
                parsed["provider"] = "ollama"
                parsed["model"] = model
                return parsed
        except Exception:
            pass
    fallback = _rule_based_summary(metrics, warnings)
    fallback["provider"] = "rule_based"
    fallback["model"] = None
    return fallback


def _rule_based_summary(metrics: dict, warnings: list[str] | None) -> dict:
    """Deterministic structured summary from verified metrics (no LLM)."""
    dt = (metrics.get("downtime_summary") or metrics.get("downtime") or {})
    pm = (metrics.get("pm_schedule") or {})
    wo = (metrics.get("work_orders") or {})
    period = metrics.get("window") or "the selected period"

    def g(d, *keys):
        for k in keys:
            if d.get(k) is not None:
                return d.get(k)
        return None

    raised = g(wo, "total") if g(wo, "total") is not None else g(dt, "total_work_orders")
    open_c = g(wo, "open") if g(wo, "open") is not None else g(dt, "open_work_orders")
    closed_c = g(wo, "closed") if g(wo, "closed") is not None else g(dt, "closed_work_orders")
    closure = g(wo, "closure_rate_pct") or g(dt, "closure_rate_pct")
    carry = g(dt, "carry_over_open_mr", "opening_backlog_count")
    active = g(dt, "total_active_workload", "total_with_backlog_count")
    prev = g(dt, "preventive_count")
    corr = g(dt, "corrective_count")
    compliance = g(pm, "compliance_pct")
    overdue = g(pm, "overdue")
    backlog = g(pm, "backlog")

    def fmt(v):
        return "unavailable" if v is None else (f"{v:.1f}%" if isinstance(v, float) else f"{v:,}" if isinstance(v, int) else str(v))

    key_numbers = []
    for label, val in [
        ("MR Raised", raised), ("Open / In Progress", open_c), ("Closed / Confirmed", closed_c),
        ("Closure Rate", f"{closure}%" if closure is not None else None),
        ("Carry-over Open MR", carry), ("Total Active Workload", active),
        ("Preventive / Corrective", f"{fmt(prev)} / {fmt(corr)}" if (prev is not None or corr is not None) else None),
        ("PM Compliance", f"{compliance}%" if compliance is not None else None),
        ("PM Overdue", overdue), ("PM Backlog", backlog),
    ]:
        if val is not None:
            key_numbers.append(f"{label}: {fmt(val) if not isinstance(val, str) else val}")

    exec_summary = (
        f"{fmt(raised)} MR were raised in {period}, with {fmt(closed_c)} closed/confirmed and "
        f"{fmt(open_c)} still open or in progress (closure rate {fmt(closure) if closure is not None else 'unavailable'}). "
        f"Carry-over open MR from before the period was {fmt(carry)}, giving a total active workload of {fmt(active)}."
    )
    observations = []
    if prev is not None and corr is not None:
        observations.append(f"Maintenance mix was {fmt(prev)} preventive vs {fmt(corr)} corrective MR.")
    if compliance is not None:
        observations.append(f"PM compliance is {fmt(compliance)}% with {fmt(overdue)} overdue and {fmt(backlog)} backlog PM tasks.")
    main_concern = ""
    if compliance is not None and isinstance(compliance, (int, float)) and compliance < 75:
        main_concern = f"PM compliance is low at {fmt(compliance)}, which warrants attention."
    elif corr is not None and prev is not None and isinstance(corr, int) and isinstance(prev, int) and corr > prev:
        main_concern = "Corrective maintenance dominates the period, indicating reactive workload."
    follow_up = []
    if open_c:
        follow_up.append(f"Review the {fmt(open_c)} open/in-progress MR.")
    if overdue:
        follow_up.append(f"Action {fmt(overdue)} overdue PM tasks.")
    if dt.get("missing_asset_count"):
        follow_up.append(f"Correct {fmt(dt.get('missing_asset_count'))} MR records missing Asset ID.")
    one_line = (
        f"{period}: {fmt(raised)} MR raised, {fmt(closed_c)} closed ({fmt(closure) if closure is not None else 'n/a'} closure), "
        f"{fmt(active)} total active workload."
    )
    return {
        "executive_summary": exec_summary,
        "key_numbers_used": key_numbers,
        "key_observations": observations,
        "main_concern": main_concern,
        "recommended_follow_up": follow_up,
        "one_line_summary": one_line,
        "data_notes": list(warnings or []),
    }
