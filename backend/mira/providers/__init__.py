"""
MIRA providers.

Provider selection is centralised here. Default is the safe, offline
``mock_provider``. The ``local_llm_stub`` is an inert interface for a future,
IT-approved local model runner and stays disabled unless explicitly enabled.
"""

from __future__ import annotations

from .. import config
from .mock_provider import MockMiraProvider
from .local_llm_stub import LocalLlmProviderStub
from .ollama_provider import (
    OllamaMiraProvider,
    generate_with_ollama,
    generate_structured_summary,
)


def get_provider():
    """Return the active provider instance.

    "auto" (default) / "ollama" -> local Ollama when it is running, else rule-based.
    "local" -> inert IT-gated stub. Anything else -> safe rule-based mock.
    """
    mode = config.PROVIDER_MODE
    if mode in ("auto", "ollama"):
        ollama = OllamaMiraProvider()
        if ollama.available():
            return ollama
        return MockMiraProvider()
    if mode == "local" and config.LOCAL_LLM_ENABLED:
        return LocalLlmProviderStub()
    return MockMiraProvider()


def get_provider_status() -> dict:
    """Report which wording layer is active, for the UI status row."""
    mode = config.PROVIDER_MODE
    if mode in ("auto", "ollama"):
        ollama = OllamaMiraProvider()
        model = ollama.resolve_model()
        if model:
            return {"provider": "ollama", "model": model,
                    "status": f"Ollama connected ({model})", "llm": True}
        if ollama.daemon_up():
            return {"provider": "mock", "model": None,
                    "status": "Ollama running but no model pulled — rule-based fallback active "
                              "(run: ollama pull llama3.1)", "llm": False}
        return {"provider": "mock", "model": None,
                "status": "Rule-based fallback active (Ollama not running)", "llm": False}
    if mode == "local" and config.LOCAL_LLM_ENABLED:
        return {"provider": "local", "model": None, "status": "Local LLM stub", "llm": False}
    return {"provider": "mock", "model": None, "status": "Rule-based summary", "llm": False}


__all__ = [
    "get_provider", "get_provider_status",
    "MockMiraProvider", "LocalLlmProviderStub", "OllamaMiraProvider",
    "generate_with_ollama", "generate_structured_summary",
]
