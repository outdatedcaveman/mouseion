"""
Notion integration.

Creates or updates pages in a Notion database for each reference.

Expected Notion database schema
--------------------------------
Create a database in Notion and add these properties:

  Title            — Title (required, built-in)
  Authors          — Rich Text
  Year             — Number
  Type             — Select
  Journal          — Rich Text
  Volume           — Rich Text
  Issue            — Rich Text
  Pages            — Rich Text
  DOI              — URL
  Abstract         — Rich Text
  Keywords         — Multi-select
  Open Access      — Checkbox
  Citation Count   — Number
  Completeness     — Number
  Cite Key         — Rich Text
  arXiv            — Rich Text
  PMID             — Rich Text
  URL              — URL
  PDF URL          — URL

The integration skips properties that don't exist in the database —
so you can start with a minimal schema and add properties over time.

API docs: https://developers.notion.com/reference/post-page
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import httpx

from ..models import Reference
from .base import BaseIntegration


_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


def _rich_text(s: str, limit: int = 2000) -> List[Dict]:
    """Build a Notion rich_text array from a plain string."""
    if not s:
        return []
    return [{"type": "text", "text": {"content": s[:limit]}}]


def _ref_to_notion_properties(ref: Reference) -> Dict[str, Any]:
    """Build the Notion page properties dict from a Reference."""
    props: Dict[str, Any] = {}

    # Title (required)
    props["Title"] = {
        "title": _rich_text(ref.title or "(untitled)")
    }

    # Authors
    author_str = "; ".join(a.full_name for a in ref.authors)
    if author_str:
        props["Authors"] = {"rich_text": _rich_text(author_str)}

    # Year
    if ref.year:
        props["Year"] = {"number": ref.year}

    # Type
    if ref.ref_type:
        props["Type"] = {"select": {"name": ref.ref_type.value}}

    # Journal / venue
    journal = ref.journal or ref.container_title or ""
    if journal:
        props["Journal"] = {"rich_text": _rich_text(journal)}

    # Volume / Issue / Pages
    if ref.volume:
        props["Volume"] = {"rich_text": _rich_text(ref.volume)}
    if ref.issue:
        props["Issue"]  = {"rich_text": _rich_text(ref.issue)}
    if ref.pages:
        props["Pages"]  = {"rich_text": _rich_text(ref.pages)}

    # DOI
    if ref.doi:
        props["DOI"] = {"url": f"https://doi.org/{ref.doi}"}

    # Abstract
    if ref.abstract:
        props["Abstract"] = {"rich_text": _rich_text(ref.abstract, limit=2000)}

    # Keywords as multi-select
    if ref.keywords:
        props["Keywords"] = {
            "multi_select": [{"name": kw[:100]} for kw in ref.keywords[:20]]
        }

    # Open Access
    if ref.open_access is not None:
        props["Open Access"] = {"checkbox": bool(ref.open_access)}

    # Citation Count
    if ref.citation_count is not None:
        props["Citation Count"] = {"number": ref.citation_count}

    # Completeness (as a percentage 0-100)
    props["Completeness"] = {"number": round(ref.completeness * 100)}

    # Cite Key
    cite_key = ref.cite_key or ref.auto_cite_key()
    props["Cite Key"] = {"rich_text": _rich_text(cite_key)}

    # arXiv
    if ref.arxiv_id:
        props["arXiv"] = {"rich_text": _rich_text(ref.arxiv_id)}

    # PMID
    if ref.pmid:
        props["PMID"] = {"rich_text": _rich_text(ref.pmid)}

    # URL
    url = ref.url or ref.oa_url or ""
    if url:
        props["URL"] = {"url": url}

    # PDF URL
    if ref.oa_url:
        props["PDF URL"] = {"url": ref.oa_url}

    return props


def _ref_to_notion_blocks(ref: Reference) -> List[Dict]:
    """Build the Notion page body (blocks) from a Reference."""
    blocks = []

    if ref.abstract:
        blocks.append({
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": _rich_text("Abstract")},
        })
        # Notion rich_text blocks have a 2000-char limit; split if needed
        abstract = ref.abstract
        while abstract:
            chunk, abstract = abstract[:2000], abstract[2000:]
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": _rich_text(chunk)},
            })

    if ref.keywords:
        blocks.append({
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": _rich_text("Keywords")},
        })
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(", ".join(ref.keywords))},
        })

    return blocks


def _notion_page_to_ref(page: Dict[str, Any]) -> Optional[Reference]:
    """
    Convert a Notion page dict (from the pages API) to a Reference.

    Expects the database schema described in this module's docstring.
    Returns ``None`` if the page has no usable title.
    """
    from ..models import Author, RefType, Reference

    props = page.get("properties", {})

    def _text(name: str) -> str:
        prop = props.get(name, {})
        rt = prop.get("rich_text") or prop.get("title") or []
        return "".join(t.get("plain_text", "") for t in rt).strip()

    def _number(name: str) -> Optional[int]:
        v = props.get(name, {}).get("number")
        return int(v) if v is not None else None

    def _select(name: str) -> Optional[str]:
        return (props.get(name, {}).get("select") or {}).get("name")

    def _url(name: str) -> Optional[str]:
        return props.get(name, {}).get("url") or None

    def _checkbox(name: str) -> Optional[bool]:
        v = props.get(name, {}).get("checkbox")
        return bool(v) if v is not None else None

    def _multi_select(name: str) -> List[str]:
        return [o["name"] for o in props.get(name, {}).get("multi_select", []) if o.get("name")]

    title = _text("Title")
    if not title:
        return None

    # DOI: strip the https://doi.org/ prefix we wrote during push
    doi_url = _url("DOI") or ""
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_url).strip() or None

    year_raw = _number("Year")

    type_str = _select("Type") or ""
    try:
        ref_type = RefType(type_str)
    except ValueError:
        ref_type = RefType.UNKNOWN

    # Authors: stored as "Family, Given; Family, Given; …"
    authors: List[Author] = []
    authors_text = _text("Authors")
    if authors_text:
        for part in authors_text.split(";"):
            part = part.strip()
            if not part:
                continue
            if ", " in part:
                fam, giv = part.split(", ", 1)
                authors.append(Author(family=fam.strip(), given=giv.strip()))
            else:
                authors.append(Author(family=part))

    ref = Reference(
        title         = title,
        authors       = authors,
        year          = year_raw,
        doi           = doi,
        arxiv_id      = _text("arXiv") or None,
        pmid          = _text("PMID") or None,
        url           = _url("URL"),
        oa_url        = _url("PDF URL"),
        abstract      = _text("Abstract") or None,
        ref_type      = ref_type,
        journal       = _text("Journal") or None,
        volume        = _text("Volume") or None,
        issue         = _text("Issue") or None,
        pages         = _text("Pages") or None,
        keywords      = _multi_select("Keywords"),
        open_access   = _checkbox("Open Access"),
        citation_count= _number("Citation Count"),
        sources       = {"notion": 1.0},
    )
    ref.normalize()
    return ref


class NotionIntegration(BaseIntegration):
    """Create / update pages in a Notion database."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        database_id: Optional[str] = None,
    ) -> None:
        from ..config import get_config
        cfg = get_config()
        self._api_key      = api_key      or cfg.notion_api_key
        self._database_id  = database_id  or cfg.notion_database_id
        self._client: Optional[httpx.AsyncClient] = None
        # Cache of known database properties (fetched on first use)
        self._db_properties: Optional[Dict[str, Any]] = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            headers=self._headers(),
            follow_redirects=True,
            timeout=30,
        )
        # Fetch database schema to know which properties exist
        await self._load_db_schema()

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()

    async def is_configured(self) -> bool:
        return bool(self._api_key and self._database_id)

    async def _load_db_schema(self) -> None:
        try:
            resp = await self._client.get(
                f"{_BASE}/databases/{self._database_id}"
            )
            if resp.status_code == 200:
                self._db_properties = resp.json().get("properties", {})
        except Exception:
            self._db_properties = {}

    def _filter_properties(self, props: Dict[str, Any]) -> Dict[str, Any]:
        """Only include properties that exist in the Notion database."""
        if not self._db_properties:
            return props
        return {k: v for k, v in props.items() if k in self._db_properties}

    async def push(self, refs: List[Reference]) -> List[str]:
        """Create Notion pages for each reference. Returns list of page IDs."""
        if not await self.is_configured():
            raise RuntimeError("Notion not configured — set api_key and database_id")

        page_ids: List[str] = []

        for ref in refs:
            props   = _ref_to_notion_properties(ref)
            props   = self._filter_properties(props)
            blocks  = _ref_to_notion_blocks(ref)

            payload = {
                "parent": {"database_id": self._database_id},
                "properties": props,
                "children": blocks,
            }

            try:
                resp = await self._client.post(f"{_BASE}/pages", json=payload)
                if resp.status_code in (200, 201):
                    page_ids.append(resp.json().get("id", ""))
                else:
                    page_ids.append("")
            except Exception:
                page_ids.append("")

        return page_ids

    async def update_page(self, page_id: str, ref: Reference) -> bool:
        """Update an existing Notion page with new reference data."""
        props = self._filter_properties(_ref_to_notion_properties(ref))
        try:
            resp = await self._client.patch(
                f"{_BASE}/pages/{page_id}",
                json={"properties": props},
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def pull(self) -> List[tuple]:
        """
        Pull all pages from the configured Notion database.

        Returns a list of ``(notion_page_id, Reference)`` tuples.
        Skips pages with no usable title.  Handles Notion's cursor-based
        pagination automatically.
        """
        if not await self.is_configured():
            raise RuntimeError("Notion not configured — set api_key and database_id")

        results: List[tuple] = []
        has_more = True
        start_cursor: Optional[str] = None

        while has_more:
            body: Dict[str, Any] = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor

            try:
                resp = await self._client.post(
                    f"{_BASE}/databases/{self._database_id}/query",
                    json=body,
                )
            except Exception:
                break

            if resp.status_code != 200:
                break

            data = resp.json()
            for page in data.get("results", []):
                ref = _notion_page_to_ref(page)
                if ref is not None:
                    results.append((page["id"], ref))

            has_more     = data.get("has_more", False)
            start_cursor = data.get("next_cursor")

        return results

    async def push_or_update(
        self,
        pairs: List[tuple],
    ) -> List[str]:
        """
        Idempotent upsert: POST new pages, PATCH existing ones.

        ``pairs`` is a list of ``(existing_page_id_or_none, ref)`` tuples.
        Returns a list of Notion page IDs in the same order.
        """
        if not await self.is_configured():
            raise RuntimeError("Notion not configured — set api_key and database_id")

        results: List[str] = [""] * len(pairs)

        for i, (page_id, ref) in enumerate(pairs):
            if page_id:
                # Update existing page — cheaper than creating a duplicate.
                success = await self.update_page(page_id, ref)
                results[i] = page_id if success else ""
            else:
                # Create new page.
                ids = await self.push([ref])
                results[i] = ids[0] if ids else ""

        return results
