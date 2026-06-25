"""
Base provider interface.

A provider turns an (intent, privacy-approved KPI data) pair into human-readable
text. It must NEVER fetch data itself and NEVER call the network — it only
verbalises data MIRA already gathered and scrubbed.
"""

from __future__ import annotations

import abc


class BaseMiraProvider(abc.ABC):
    name = "base"
    is_local_only = True          # contract: no external network egress

    @abc.abstractmethod
    def generate(self, intent: str, data: dict, question: str | None = None) -> str:
        """Return a natural-language answer for the given intent + KPI data."""
        raise NotImplementedError

    def available(self) -> bool:
        return True
