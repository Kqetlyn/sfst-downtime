"""
MIRA — Maintenance Intelligence & Reporting Assistant.

A local/private assistant layer that sits ON TOP of the existing maintenance
dashboard. MIRA never re-implements KPI maths and never reads raw Excel / D365
data directly. It consumes the *same* KPI outputs the dashboard already produces,
passes them through a privacy guard, and hands the privacy-approved summary to a
local mock provider that writes human-readable, draft text.

Data flow
---------
imported MR/WO data
  -> existing dashboard processing / KPI calculation logic
  -> existing dashboard KPI outputs            (downtime_service / maintenance_service / pm_schedule_service)
  -> MIRA kpi_query_service                    (read-only adapter, this package)
  -> MIRA privacy_guard_service                (scrub / cap / label)
  -> MIRA provider (mock_provider default)     (rule-based, no external AI)
  -> MIRA response / report draft

NOTE
----
Training a full Transformer model from scratch is NOT included in this prototype.
MIRA is designed as a controlled local assistant layer using dashboard KPI tools.
A local LLM or approved AI service can be connected later if approved by IT
(see providers/local_llm_stub.py).
"""

__all__ = ["__version__"]
__version__ = "0.1.0-prototype"
