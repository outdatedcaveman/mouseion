"""
alphaXiv provider (opt-in).

alphaXiv indexes the FULL TEXT of arXiv papers and exposes a public, keyless
retrieval endpoint — the one behind alphaXiv/OpenResearch's `orx discover
keyword` (MIT; https://github.com/alphaXiv/OpenResearch, src/client.rs):

    GET https://api.alphaxiv.org/search/v2/paper/discover/keyword?q=<query>&prioritize=default
    -> [{paperId, title, abstract, publicationDate, votes, snippets: [...]}, ...]

Its job here is narrow: give an untitled-arXiv reference (a title with no arXiv
id) its arXiv id, abstract and date, so arxiv_api can then fill the rest.

Precision matters more than recall. Keyword retrieval returns papers that MENTION
the query anywhere in their text, so a hit is accepted only when its title matches
the reference title almost exactly (normalized similarity >= 0.92). A wrong arXiv
id is worse than none.

Off unless ``alphaxiv_enabled`` is set (config [providers] alphaxiv_enabled, or
MOUSEION_ALPHAXIV_ENABLED=1).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import List, Optional

import httpx

from ..models import RefType, Reference
from .base import BaseProvider, clean_query_title

API_URL = "https://api.alphaxiv.org/search/v2/paper/discover/keyword"
MATCH_THRESHOLD = 0.92
_ARXIV_ID = re.compile(r"^(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$")


def enabled(cfg=None) -> bool:
    if cfg is None:
        from ..config import get_config
        cfg = get_config()
    return bool(getattr(cfg, "alphaxiv_enabled", False))


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def title_similarity(a: str, b: str) -> float:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def title_match(hit_title: str, ref_title: str) -> float:
    """Similarity score, with one narrow allowance: a record may drop the subtitle.
    If the hit equals the reference's main title (the part before ':'), and that
    main title has >= 5 words, it counts as a match (score 0.95)."""
    sim = title_similarity(hit_title, ref_title)
    if sim >= MATCH_THRESHOLD:
        return sim
    for longer, shorter in ((ref_title, hit_title), (hit_title, ref_title)):
        if ":" in longer:
            main = _norm(longer.split(":", 1)[0])
            if len(main.split()) >= 5 and main == _norm(shorter):
                return 0.95
    return sim


class AlphaXivProvider(BaseProvider):
    name = "alphaxiv"
    priority = 8          # after arxiv_api: it locates arXiv ids, the arXiv API stays authoritative
    _max_concurrent = 1
    _min_interval = 1.0

    @staticmethod
    def _parse_hit(hit: dict, query_title: str) -> Optional[Reference]:
        pid = re.sub(r"v\d+$", "", str(hit.get("paperId") or ""))
        title = " ".join(str(hit.get("title") or "").split())
        if not pid or not _ARXIV_ID.match(pid) or not title:
            return None
        sim = title_match(title, query_title)
        if sim < MATCH_THRESHOLD:
            return None
        ref = Reference()
        ref.ref_type = RefType.PREPRINT
        ref.arxiv_id = pid
        ref.title = title
        abstract = " ".join(str(hit.get("abstract") or "").split())
        ref.abstract = abstract or None
        m = re.match(r"(\d{4})-(\d{2})", str(hit.get("publicationDate") or ""))
        if m:
            ref.year = int(m.group(1))
            ref.month = int(m.group(2))
        ref.url = f"https://www.alphaxiv.org/abs/{pid}"
        ref.open_access = True
        ref.oa_url = f"https://arxiv.org/pdf/{pid}"
        ref.sources["alphaxiv"] = round(sim, 3)
        ref.normalize()
        return ref

    async def lookup_by_doi(self, doi: str, client: httpx.AsyncClient) -> Optional[Reference]:
        return None                                    # alphaXiv is not indexed by DOI

    async def search(
        self,
        title: str,
        authors: Optional[List[str]] = None,
        year: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> List[Reference]:
        # The cleaned title is only the API query; acceptance is judged against the
        # reference's own full title (clean_query_title may shorten it, e.g. at a hyphen).
        q = clean_query_title(title)
        if not q or len(q) < 12 or len(_norm(title)) < 12:
            return []
        full = " ".join(title.split())
        if len(q) < 0.8 * len(full) and not re.search(r"https?://|\b10\.\d{4,}/", full):
            q = full                                      # cleaning cut a real title short (e.g. at a hyphen)
        params = {"q": q, "prioritize": "default"}

        async def _do(c: httpx.AsyncClient) -> List[Reference]:
            resp = await self._get(c, API_URL, params=params)
            if resp is None:
                return []
            try:
                hits = resp.json()
            except ValueError:
                return []
            if not isinstance(hits, list):
                return []
            out = [r for r in (self._parse_hit(h, title) for h in hits if isinstance(h, dict)) if r is not None]
            out.sort(key=lambda r: r.sources.get("alphaxiv", 0.0), reverse=True)
            return out[:3]

        if client is not None:
            return await _do(client)
        async with self._make_client() as c:
            return await _do(c)
