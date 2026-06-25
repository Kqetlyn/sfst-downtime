"""
mockMiraProvider — the default, safe, offline provider.

Generates AI-style narrative text from KPI outputs using deterministic
rule-based templates. NO external AI calls, NO API keys, NO model. This is the
safe local prototype mode described in the MIRA spec.
"""

from __future__ import annotations

from .base_provider import BaseMiraProvider


def _num(value, suffix="", dash="not available"):
    if value is None:
        return dash
    if isinstance(value, float):
        value = round(value, 2)
    return f"{value}{suffix}"


def _hours(value):
    return _num(value, " h")


def _pct(value):
    return _num(value, "%")


class MockMiraProvider(BaseMiraProvider):
    name = "mock"
    is_local_only = True

    def generate(self, intent: str, data: dict, question: str | None = None) -> str:
        handler = getattr(self, f"_say_{intent}", None)
        if handler is None:
            handler = self._say_monthly_summary
        try:
            return handler(data)
        except Exception:
            # Never fail a chatbot reply on a template edge case.
            return self._fallback(data)

    # ── Intent templates ────────────────────────────────────────────────────
    def _say_monthly_summary(self, d: dict) -> str:
        wo = d.get("work_orders", {})
        pm = d.get("pm_schedule", {})
        lines = [
            f"Maintenance performance summary — {d.get('window', 'selected period')} "
            f"({_stage(d.get('stage'))}):",
            f"• Work orders: {_num(wo.get('total'))} total — "
            f"{_num(wo.get('open'))} open, {_num(wo.get('closed'))} closed.",
            f"• MTTR: {_hours(d.get('mttr_hours'))}; MTBF: {_hours(d.get('mtbf_hours'))}.",
            f"• Maintenance mix: {_num(d.get('preventive_count'))} preventive vs "
            f"{_num(d.get('corrective_count'))} corrective "
            f"(status: {d.get('performance_status') or 'n/a'}).",
            f"• PM schedule: {_num(pm.get('total_scheduled'))} scheduled, "
            f"{_num(pm.get('due_this_month'))} due this month, "
            f"{_num(pm.get('overdue'))} overdue, compliance {_pct(pm.get('compliance_pct'))}.",
            f"• Data reliability issues flagged: {_num(d.get('data_reliability_issue_count'))}.",
        ]
        return "\n".join(lines)

    def _say_mttr(self, d: dict) -> str:
        txt = (f"MTTR (Mean Time To Repair) for {d.get('window')} ({_stage(d.get('stage'))}) "
               f"is {_hours(d.get('overall_mttr_hours'))}, based on "
               f"{_num(d.get('valid_ttr_work_orders'))} valid work orders "
               f"out of {_num(d.get('total_work_orders'))} total.")
        if d.get("highest_mttr_machine_group"):
            txt += (f" Longest repairs are in {d['highest_mttr_machine_group']} "
                    f"({_hours(d.get('highest_mttr_hours'))}).")
        return txt

    def _say_mtbf(self, d: dict) -> str:
        txt = (f"MTBF (Mean Time Between Failures) for {d.get('window')} "
               f"({_stage(d.get('stage'))}) is {_hours(d.get('overall_average_mtbf_hours'))}, "
               f"across {_num(d.get('assets_with_valid_mtbf'))} assets with enough repeat data.")
        if d.get("lowest_mtbf_asset_name"):
            txt += (f" Lowest interval: {d['lowest_mtbf_asset_name']} "
                    f"({_hours(d.get('lowest_mtbf_hours'))}).")
        return txt

    def _say_open_work_orders(self, d: dict) -> str:
        return (f"For {d.get('window')} ({_stage(d.get('stage'))}): "
                f"{_num(d.get('open_work_orders'))} open and "
                f"{_num(d.get('closed_work_orders'))} closed work orders, "
                f"out of {_num(d.get('total_work_orders'))} total. "
                f"{_num(d.get('requires_attention_count'))} need data attention.")

    def _say_preventive_corrective(self, d: dict) -> str:
        return (f"Maintenance mix for {d.get('window')}: "
                f"{_num(d.get('preventive_count'))} preventive ({_pct(d.get('preventive_ratio_pct'))}) "
                f"vs {_num(d.get('corrective_count'))} corrective ({_pct(d.get('corrective_ratio_pct'))}). "
                f"Performance flag: {d.get('performance_status') or 'n/a'}.")

    def _say_data_quality(self, d: dict) -> str:
        return (f"Data reliability for {d.get('window')} ({_stage(d.get('stage'))}): "
                f"{_num(d.get('requires_attention_count'))} work orders need attention; "
                f"{_num(d.get('invalid_missing_ttr_count'))} have missing/invalid repair time; "
                f"MTTR-missing {_num(d.get('mttr_missing_total'))}, "
                f"MTBF-missing {_num(d.get('mtbf_missing_total'))}, "
                f"duplicates {_num(d.get('duplicate_work_order_count'))}. "
                f"Valid: {_num(d.get('valid_ttr_work_orders'))}/{_num(d.get('total_work_orders'))}.")

    def _say_pm_schedule(self, d: dict) -> str:
        cov = d.get("coverage") or {}
        cov_pct = cov.get("pct") if isinstance(cov, dict) else None
        return (f"PM schedule status for {d.get('window')} ({_stage(d.get('stage'))}): "
                f"{_num(d.get('total_scheduled'))} scheduled, "
                f"{_num(d.get('due_this_month'))} due this month, "
                f"{_num(d.get('due_soon'))} due soon, "
                f"{_num(d.get('overdue'))} overdue, "
                f"backlog {_num(d.get('backlog'))}, "
                f"compliance {_pct(d.get('compliance_pct'))}, "
                f"coverage {_pct(cov_pct)}. "
                f"Data quality: {_num(d.get('missing_mapping'))} unmapped, "
                f"{_num(d.get('needs_review'))} need review.")

    def _say_stage_compare(self, d: dict) -> str:
        s1, s2 = d.get("stage1", {}), d.get("stage2", {})

        def line(label, s):
            wo = s.get("open_work_orders", {})
            mt = s.get("mttr", {})
            mb = s.get("mtbf", {})
            pm = s.get("pm_schedule", {})
            return (f"{label}: WO {_num(wo.get('total_work_orders'))} "
                    f"({_num(wo.get('open_work_orders'))} open), "
                    f"MTTR {_hours(mt.get('overall_mttr_hours'))}, "
                    f"MTBF {_hours(mb.get('overall_average_mtbf_hours'))}, "
                    f"PM {_num(pm.get('total_scheduled'))} "
                    f"(overdue {_num(pm.get('overdue'))}, "
                    f"compliance {_pct(pm.get('compliance_pct'))}).")

        return (f"Stage comparison — {d.get('window')}:\n"
                f"• {line('Stage 1', s1)}\n• {line('Stage 2', s2)}")

    def _say_work_order_search(self, d: dict) -> str:
        n = d.get("returned_rows", len(d.get("rows", [])))
        total = d.get("total_matched", n)
        txt = (f"Found {total} matching work orders for {d.get('window')} "
               f"({_stage(d.get('stage'))}); showing {n} (field-reduced).")
        if d.get("truncated"):
            txt += " Result list was capped — narrow the filter for fewer rows."
        return txt

    def _fallback(self, d: dict) -> str:
        return ("Here is the requested maintenance information based on the current "
                "dashboard KPI outputs. See the structured data for details.")


def _stage(stage):
    return {"all": "All stages", "stage1": "Stage 1", "stage2": "Stage 2"}.get(stage, stage or "All stages")
