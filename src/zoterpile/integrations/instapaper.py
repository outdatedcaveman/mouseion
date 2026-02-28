"""
Instapaper integration.

Adds article URLs to Instapaper for later reading.

Two API modes:
  Simple API  (no auth) — just POST a URL (rate-limited by IP)
  Full API    (XAUTH)   — requires username + password from config

Adds only references with a URL (landing page, DOI URL, or OA PDF).
Prefers DOI → landing page → OA PDF URL, in that order.
"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

import httpx

from ..models import Reference
from .base import BaseIntegration


_SIMPLE_API = "https://www.instapaper.com/api/add"


def _best_url(ref: Reference) -> Optional[str]:
    """Return the best URL to save to Instapaper."""
    if ref.doi:
        return f"https://doi.org/{ref.doi}"
    if ref.url:
        return ref.url
    if ref.arxiv_id:
        return f"https://arxiv.org/abs/{ref.arxiv_id}"
    if ref.oa_url:
        return ref.oa_url
    if ref.pmid:
        return f"https://pubmed.ncbi.nlm.nih.gov/{ref.pmid}/"
    return None


class InstapaperIntegration(BaseIntegration):
    """Add reference URLs to an Instapaper reading list."""

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        from ..config import get_config
        cfg = get_config()
        self._username = username or cfg.instapaper_username
        self._password = password or cfg.instapaper_password
        self._client: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=15,
        )

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()

    async def is_configured(self) -> bool:
        # Simple API works without credentials, but auth gives better reliability
        return True

    async def push(self, refs: List[Reference]) -> List[str]:
        """
        Add reference URLs to Instapaper.
        Returns list of status strings ("ok", "no_url", "error").
        """
        results: List[str] = []
        for ref in refs:
            url = _best_url(ref)
            if not url:
                results.append("no_url")
                continue

            params = {
                "url": url,
                "title": (ref.title or "")[:250],
                "description": (ref.abstract or "")[:500],
            }

            try:
                resp = await self._client.post(
                    _SIMPLE_API,
                    data=params,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code in (201, 200):
                    results.append("ok")
                else:
                    results.append(f"error:{resp.status_code}")
            except Exception as e:
                results.append(f"error:{e}")

        return results
