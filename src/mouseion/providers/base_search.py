"""
BASE (Bielefeld Academic Search Engine) provider.

BASE harvests 400M+ records from 11k+ repositories over OAI-PMH -- above all
institutional-repository copies (theses, accepted manuscripts, reports) that
Unpaywall and OpenAlex often miss. That is exactly the PDF tail Mouseion is
stuck on, so the main product of this provider is a ranked list of repository
copies (URL + which repository holds it), used by pdf_manager as one more
legal, open-access strategy. It also works as an ordinary metadata provider
(DOI lookup, title search) for explicit use.

API (BASE Interface Guide v1.27, March 2023)
--------------------------------------------
* https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi
  ?func=PerformSearch&query=<Solr query>&format=json&hits=<=120
* Non-commercial use only; 1 request per second.
* Access is granted per registered IP address (checked together with the
  User-Agent) OR per apikey, requested through
  https://www.base-search.net/about/en/contact.php. An unregistered caller
  gets HTTP 200 with {"error": "Access denied for IP address ... and user
  agent ..."}; that is parked for an hour, never counted as "no copy".

Off unless ``base_search_enabled`` is set (config [pdf] base_search_enabled,
or MOUSEION_BASE_SEARCH_ENABLED=1). ``base_search_api_key`` is sent when set.
Not part of _make_providers(): at 1 QPS it would stall concurrent enrichment.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseProvider, clean_query_title

logger = logging.getLogger(__name__)

API_URL = "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi"

# BASE whitelists an IP *together with* a User-Agent. This exact string is what
# the registration names -- changing it silently revokes access.
USER_AGENT = "mouseion-base/1.0 (+https://github.com/outdatedcaveman/mouseion)"

_FIELDS = ",".join((
    "dctitle", "dccreator", "dcyear", "dcdoi", "dclink", "dcidentifier",
    "dcoa", "dccollection", "dcprovider", "dctypenorm", "dcformat",
    "dcpublisher", "dcsource", "dclang",
))

_DENIED_PARK_SECONDS = 3600.0
_ACCESS: Dict[str, Any] = {"denied_until": 0.0, "message": ""}

_TYPENORM = {
    "11": RefType.BOOK, "111": RefType.BOOK_CHAPTER, "121": RefType.JOURNAL,
    "13": RefType.CONFERENCE, "14": RefType.REPORT, "18": RefType.THESIS,
    "181": RefType.THESIS, "182": RefType.THESIS, "183": RefType.THESIS,
    "19": RefType.PREPRINT, "7": RefType.DATASET,
}

# Paths that usually serve the file itself rather than a landing page
# (DSpace bitstreams, EPrints/OJS downloads, Pure/HAL/Figshare files ...).
_FILE_HINT = re.compile(
    r"(\.pdf($|[?#])|/bitstream/|/bitstreams/|/download|/files/|/document|"
    r"/fulltext|/retrieve/|/content/|/pdf($|[/?])|getfile|/servlets/|/record/\d+/files/)",
    re.IGNORECASE,
)
_RESOLVER_HOSTS = ("doi.org", "dx.doi.org", "hdl.handle.net", "n2t.net")


class BaseAccessDenied(Exception):
    """BASE refused this caller (IP/User-Agent not registered, or bad apikey)."""


@dataclass
class RepositoryCopy:
    url: str
    collection: str = ""      # BASE internal repository name, e.g. "ftubbiepub"
    provider: str = ""        # human name, e.g. "PUB - Publications at Bielefeld University"
    oa: int = 2               # dcoa: 1 open access, 2 unknown (0 never returned)
    file_like: bool = False   # URL looks like the file, not a landing page
    matched_by: str = "doi"   # "doi" or "title"


# ---------------------------------------------------------------------------
# configuration / access state
# ---------------------------------------------------------------------------

def enabled(cfg=None) -> bool:
    if cfg is None:
        from ..config import get_config
        cfg = get_config()
    return bool(getattr(cfg, "base_search_enabled", False))


def access_parked() -> bool:
    return _ACCESS["denied_until"] > time.time()


def access_message() -> str:
    return _ACCESS["message"]


def _park(message: str) -> None:
    if not access_parked():
        logger.warning("BASE refused access (%s); parking it for %d min. Register the IP + "
                       "User-Agent %r or set base_search_api_key.", message,
                       _DENIED_PARK_SECONDS // 60, USER_AGENT)
    _ACCESS["denied_until"] = time.time() + _DENIED_PARK_SECONDS
    _ACCESS["message"] = message


# ---------------------------------------------------------------------------
# query building
# ---------------------------------------------------------------------------

def _phrase(s: str) -> str:
    return '"' + s.replace("\\", " ").replace('"', " ").strip() + '"'


def normalize_doi(doi: str) -> str:
    d = (doi or "").strip().lower()
    d = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d)
    return d


def doi_query(doi: str) -> str:
    return "dcdoi:" + _phrase(normalize_doi(doi))


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def title_query(title: str, authors: Optional[List[str]] = None, year: Optional[int] = None) -> str:
    t = clean_query_title(title) or title or ""
    parts = ["dctitle:" + _phrase(t[:300])]
    fam = next((a for a in (authors or []) if a and len(a.strip()) > 1), "")
    fam = re.sub(r"[^\w\s'-]", " ", fam).strip()
    if fam:
        parts.append("dccreator:" + _phrase(fam))
    if year:
        parts.append(f"dcyear:[{int(year) - 1} TO {int(year) + 1}]")
    return " ".join(parts)[:1000]


# ---------------------------------------------------------------------------
# response parsing (JSON requested; the documented XML form is accepted too)
# ---------------------------------------------------------------------------

def _as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if x is not None and str(x).strip()]
    s = str(v).strip()
    return [s] if s else []


def _first(v) -> str:
    items = _as_list(v)
    return items[0] if items else ""


def parse_response(body: bytes | str) -> List[dict]:
    """The docs of a PerformSearch response. Raises BaseAccessDenied on a refusal."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else (body or "")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        import json
        data = json.loads(stripped)
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            _raise_for_error(str(err))
            return []                      # "No results found!"
        resp = data.get("response") if isinstance(data, dict) else None
        docs = (resp or {}).get("docs") if isinstance(resp, dict) else None
        if docs is None and isinstance(data, dict):
            docs = data.get("docs")
        if not isinstance(docs, list):
            # An unknown shape must fail loudly: read as "no copies" it would mark
            # every ref as a miss (the JSON form is undocumented; the guide shows XML).
            raise ValueError(f"unrecognised BASE JSON: keys {sorted(data)[:8] if isinstance(data, dict) else type(data)}")
        return [d for d in docs if isinstance(d, dict)]
    if stripped.startswith("<"):
        root = ET.fromstring(stripped)
        if root.tag == "error":
            _raise_for_error(root.text or "")
        out = []
        for doc in root.iter("doc"):
            d: Dict[str, Any] = {}
            for el in doc:
                name = el.get("name")
                if not name:
                    continue
                if el.tag == "arr":
                    d[name] = [(c.text or "").strip() for c in el]
                else:
                    d[name] = (el.text or "").strip()
            out.append(d)
        return out
    return []


def _raise_for_error(msg: str) -> None:
    if "access denied" in msg.lower() or "apikey" in msg.lower():
        raise BaseAccessDenied(msg)
    if "no results" in msg.lower():
        return
    raise ValueError(msg)


def _oa(doc: dict) -> int:
    try:
        return int(_first(doc.get("dcoa")) or 2)
    except ValueError:
        return 2


def doc_to_reference(doc: dict) -> Reference:
    ref = Reference()
    ref.title = _first(doc.get("dctitle")) or None
    for name in _as_list(doc.get("dccreator")):
        if "," in name:
            fam, giv = name.split(",", 1)
            ref.authors.append(Author(family=fam.strip(), given=giv.strip()))
        else:
            bits = name.split()
            ref.authors.append(Author(family=bits[-1], given=" ".join(bits[:-1])) if bits else Author(family=name))
    try:
        y = int(_first(doc.get("dcyear")) or 0)
        ref.year = y or None
    except ValueError:
        pass
    dois = [normalize_doi(d) for d in _as_list(doc.get("dcdoi"))]
    ref.doi = next((d for d in dois if d.startswith("10.")), None)
    ref.publisher = _first(doc.get("dcpublisher")) or None
    for code in _as_list(doc.get("dctypenorm")):
        if code in _TYPENORM:
            ref.ref_type = _TYPENORM[code]
            break
    copies = rank_copies(copies_from_doc(doc, matched_by=""))
    ref.url = _first(doc.get("dclink")) or None
    if _oa(doc) == 1:
        ref.open_access = True
        if copies:
            ref.oa_url = copies[0].url
    ref.extras["base_search"] = {
        "collection": _first(doc.get("dccollection")),
        "provider": _first(doc.get("dcprovider")),
        "oa": _oa(doc),
    }
    ref.sources["base_search"] = 0.6
    ref.normalize()
    return ref


def copies_from_doc(doc: dict, matched_by: str = "doi") -> List[RepositoryCopy]:
    oa = _oa(doc)
    if oa == 0:
        return []
    coll, prov = _first(doc.get("dccollection")), _first(doc.get("dcprovider"))
    urls = _as_list(doc.get("dclink")) + _as_list(doc.get("dcidentifier"))
    out: List[RepositoryCopy] = []
    seen = set()
    for u in urls:
        if not u.lower().startswith(("http://", "https://")) or u in seen:
            continue
        host = u.split("/")[2].lower()
        if host.startswith("www."):
            host = host[4:]
        if host in _RESOLVER_HOSTS:          # the publisher route is tried elsewhere
            continue
        seen.add(u)
        out.append(RepositoryCopy(url=u, collection=coll, provider=prov, oa=oa,
                                  file_like=bool(_FILE_HINT.search(u)), matched_by=matched_by))
    return out


def rank_copies(copies: List[RepositoryCopy], limit: int = 6) -> List[RepositoryCopy]:
    uniq: Dict[str, RepositoryCopy] = {}
    for c in copies:
        uniq.setdefault(c.url, c)
    return sorted(uniq.values(), key=lambda c: (c.oa != 1, not c.file_like))[:limit]


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

def doc_matches_doi(doc: dict, doi: str) -> bool:
    want = normalize_doi(doi)
    return any(normalize_doi(d) == want for d in _as_list(doc.get("dcdoi")))


def doc_matches_title(doc: dict, title: str, authors: Optional[List[str]] = None,
                      year: Optional[int] = None) -> bool:
    from rapidfuzz import fuzz
    t = _first(doc.get("dctitle"))
    if not t or not title:
        return False
    a, b = _fold(clean_query_title(title) or title), _fold(t)
    if fuzz.token_sort_ratio(a, b) < 92:
        return False
    fams = [_fold(x) for x in (authors or []) if x and len(x) > 1]
    creators = _fold(" ".join(_as_list(doc.get("dccreator"))))
    if fams and creators and not any(f in creators for f in fams):
        return False
    try:
        dy = int(_first(doc.get("dcyear")) or 0)
    except ValueError:
        dy = 0
    if year and dy and abs(int(year) - dy) > 1:
        return False
    return True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _params(query: str, hits: int, api_key: str = "") -> dict:
    p = {"func": "PerformSearch", "query": query, "format": "json",
         "hits": str(max(1, min(hits, 120))), "boost": "oa", "fields": _FIELDS}
    if api_key:
        p["apikey"] = api_key
    return p


async def perform_search(client: httpx.AsyncClient, query: str, hits: int = 10,
                         api_key: Optional[str] = None, max_wait: float = 20.0) -> Optional[List[dict]]:
    """Run one PerformSearch. Returns docs, [] for no hits, None when BASE could
    not be asked right now (budget wait, network/server error). Raises
    BaseAccessDenied when BASE refuses this caller."""
    if access_parked():
        raise BaseAccessDenied(access_message())
    if api_key is None:
        from ..config import get_config
        api_key = getattr(get_config(), "base_search_api_key", "") or ""
    from ..api_router import get_router
    from ..network_budget import network_slot
    router = get_router()
    if not await router.acquire("base_search", max_wait=max_wait):
        return None
    try:
        async with network_slot("pdf_lookup", bucket="base_search", min_interval=1.0):
            resp = await client.get(API_URL, params=_params(query, hits, api_key),
                                    headers={"User-Agent": USER_AGENT}, timeout=30.0)
    except httpx.RequestError:
        router.report("base_search", None, ok=False)
        return None
    if resp.status_code == 403:
        router.report("base_search", 403, ok=False)
        _park(f"HTTP 403 {resp.text[:200]}")
        raise BaseAccessDenied(access_message())
    if resp.status_code != 200:
        router.report("base_search", resp.status_code, ok=False)
        return None
    try:
        docs = parse_response(resp.content)
    except BaseAccessDenied as e:
        router.report("base_search", 403, ok=False)
        _park(str(e))
        raise BaseAccessDenied(str(e))
    except (ValueError, ET.ParseError):
        router.report("base_search", resp.status_code, ok=False)
        return None
    router.report("base_search", 200, ok=True)
    return docs


async def repository_copies(client: httpx.AsyncClient, ref: Reference,
                            api_key: Optional[str] = None) -> Optional[List[RepositoryCopy]]:
    """Repository copies of `ref` BASE knows of: DOI match first, then title +
    first author (+ year +/-1). None means BASE could not be asked (do not count
    as a miss); [] means BASE has no open copy."""
    copies: List[RepositoryCopy] = []
    asked = False
    if ref.doi:
        docs = await perform_search(client, doi_query(ref.doi), hits=20, api_key=api_key)
        if docs is not None:
            asked = True
            for d in docs:
                if doc_matches_doi(d, ref.doi):
                    copies.extend(copies_from_doc(d, matched_by="doi"))
    if not copies and ref.title and len(ref.title.strip()) >= 15:
        fams = [a.family for a in ref.authors if a.family]
        docs = await perform_search(client, title_query(ref.title, fams, ref.year), hits=10, api_key=api_key)
        if docs is not None:
            asked = True
            for d in docs:
                if ref.doi and _as_list(d.get("dcdoi")) and not doc_matches_doi(d, ref.doi):
                    continue      # a different work that happens to share the title
                if doc_matches_title(d, ref.title, fams, ref.year):
                    copies.extend(copies_from_doc(d, matched_by="title"))
    if not asked:
        return None
    return rank_copies(copies)


class BaseSearchProvider(BaseProvider):
    name = "base_search"
    priority = 9               # supplementary: repository copies, thin metadata
    _max_concurrent = 1
    _min_interval = 1.05

    def __init__(self, api_key: Optional[str] = None) -> None:
        super().__init__()
        self._api_key = api_key

    async def lookup_by_doi(self, doi: str, client: httpx.AsyncClient) -> Optional[Reference]:
        try:
            docs = await perform_search(client, doi_query(doi), hits=20, api_key=self._api_key)
        except BaseAccessDenied:
            return None
        for d in docs or []:
            if doc_matches_doi(d, doi):
                return doc_to_reference(d)
        return None

    async def search(self, title: str, authors: Optional[List[str]] = None,
                     year: Optional[int] = None,
                     client: Optional[httpx.AsyncClient] = None) -> List[Reference]:
        if not title:
            return []
        client = client or await self._get_shared_client()
        try:
            docs = await perform_search(client, title_query(title, authors, year), hits=10,
                                        api_key=self._api_key)
        except BaseAccessDenied:
            return []
        return [doc_to_reference(d) for d in (docs or []) if doc_matches_title(d, title, authors, year)][:5]
