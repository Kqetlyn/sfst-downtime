"""
localLlmProviderStub — DISABLED interface for a future IT-approved local model.

This is intentionally inert. It does NOT install, import, or connect to Ollama,
llama.cpp, transformers, or any model runner. It exists only to define the seam
where an approved, fully-local model could later be plugged in.

To enable (only after IT approval):
  1. Set env MIRA_PROVIDER=local and MIRA_LOCAL_LLM_ENABLED=true.
  2. Implement `_run_local_model()` to call the approved local runner
     (e.g. a localhost Ollama endpoint) using the privacy-approved `data` only.
  3. Keep all calls on-device / on-prem — never an external AI API.

NOTE: Training a full Transformer model from scratch is not included in this
prototype. MIRA is a controlled local assistant layer using dashboard KPI tools.
A local LLM or approved AI service can be connected later if approved by IT.
"""

from __future__ import annotations

from .base_provider import BaseMiraProvider
from .mock_provider import MockMiraProvider


class LocalLlmProviderStub(BaseMiraProvider):
    name = "local_llm_stub"
    is_local_only = True
    enabled = False  # hard-off until an approver wires a real local runner

    def available(self) -> bool:
        return False

    def generate(self, intent: str, data: dict, question: str | None = None) -> str:
        # Stub is inert: fall back to the safe mock narrative so the UI still works,
        # and make the disabled state explicit.
        narrative = MockMiraProvider().generate(intent, data, question)
        return (
            f"{narrative}\n\n[local LLM provider is a disabled stub — awaiting IT "
            f"approval; response produced by the safe mock provider]"
        )

    def _run_local_model(self, prompt: str) -> str:  # pragma: no cover - not wired
        raise NotImplementedError(
            "Local LLM runner not connected. Requires IT approval and explicit "
            "enablement (MIRA_LOCAL_LLM_ENABLED=true)."
        )
