"""Abstract base class for all integrations."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import List
from ..models import Reference

class BaseIntegration(ABC):
    """Async context manager for an external tool integration."""

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.disconnect()

    async def connect(self) -> None:
        """Optional: set up connections / auth."""

    async def disconnect(self) -> None:
        """Optional: tear down connections."""

    @abstractmethod
    async def push(self, refs: List[Reference]) -> List[str]:
        """
        Push references to the external tool.
        Returns a list of external IDs (page IDs, item keys, etc.)
        in the same order as `refs`.  Returns "" for failures.
        """

    async def is_configured(self) -> bool:
        """Return True if the integration has the required credentials."""
        return True
