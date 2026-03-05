"""
Zotero integration.

Pushes enriched References to a Zotero library (user or group) via the
Zotero Web API v3.

Setup
-----
1. Generate an API key at https://www.zotero.org/settings/keys
2. Set in config:
     [zotero]
     api_key      = "YOUR_KEY"
     user_id      = "123456"
     library_type = "user"   # or "group"
     library_id   = "123456" # same as user_id for personal libraries

API docs: https://www.zotero.org/support/dev/web_api/v3/write_requests
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import httpx

from ..models import Author, RefType, Reference
from .base import BaseIntegration


_BASE = "https://api.zotero.org"
_API_VERSION = "3"


# Map RefType → Zotero itemType
_TYPE_MAP = {
    RefType.JOURNAL:      "journalArticle",
    RefType.BOOK:         "book",
    RefType.BOOK_CHAPTER: "bookSection",
    RefType.CONFERENCE:   "conferencePaper",
    RefType.PREPRINT:     "preprint",
    RefType.THESIS:       "thesis",
    RefType.REPORT:       "report",
    RefType.DATASET:      "dataset",
    RefType.WEBSITE:      "webpage",
    RefType.OTHER:        "document",
    RefType.UNKNOWN:      "document",
}

# Map RefType → Zotero "extra" note prefix (for types not in the map above)
_CREATOR_ROLE = {
    RefType.BOOK:         "author",
    RefType.BOOK_CHAPTER: "author",
    RefType.THESIS:       "author",
    RefType.REPORT:       "author",
}


def _ref_to_zotero_item(ref: Reference) -> Dict[str, Any]:
    item_type = _TYPE_MAP.get(ref.ref_type, "document")

    creators = [
        {
            "creatorType": "author",
            "lastName": a.family or a.full_name,
            "firstName": a.given,
        }
        for a in ref.authors
    ]
    creators += [
        {
            "creatorType": "editor",
            "lastName": e.family or e.full_name,
            "firstName": e.given,
        }
        for e in ref.editors
    ]

    item: Dict[str, Any] = {
        "itemType": item_type,
        "title": ref.title or "",
        "creators": creators,
        "abstractNote": ref.abstract or "",
        "date": str(ref.year) if ref.year else "",
        "url": ref.url or ref.oa_url or "",
        "accessDate": "",
        "language": ref.language or "",
        "tags": [{"tag": t} for t in ref.keywords[:10]],
        "relations": {},
        "extra": "",
    }

    # Identifiers in 'extra' field (Zotero recognises these)
    extra_parts = []
    if ref.doi:
        item["DOI"] = ref.doi
    if ref.isbn:
        item["ISBN"] = ref.isbn
    if ref.issn:
        item["ISSN"] = ref.issn
    if ref.pmid:
        extra_parts.append(f"PMID: {ref.pmid}")
    if ref.arxiv_id:
        extra_parts.append(f"arXiv: {ref.arxiv_id}")
    if extra_parts:
        item["extra"] = "\n".join(extra_parts)

    # Type-specific fields
    if item_type in ("journalArticle",):
        item["publicationTitle"] = ref.journal or ref.container_title or ""
        item["journalAbbreviation"] = ref.journal_abbrev or ""
        item["volume"] = ref.volume or ""
        item["issue"] = ref.issue or ""
        item["pages"] = ref.pages or ""

    elif item_type in ("bookSection", "conferencePaper"):
        item["bookTitle"] = ref.container_title or ""
        item["publisher"] = ref.publisher or ""
        item["place"] = ref.place or ""
        item["pages"] = ref.pages or ""

    elif item_type == "book":
        item["publisher"] = ref.publisher or ""
        item["place"] = ref.place or ""
        item["edition"] = ref.edition or ""
        item["series"] = ref.series or ""
        item["numPages"] = str(ref.num_pages) if ref.num_pages else ""

    elif item_type == "conferencePaper":
        item["conferenceName"] = ref.event_name or ""
        item["proceedingsTitle"] = ref.container_title or ""

    elif item_type == "preprint":
        item["repository"] = "arXiv" if ref.arxiv_id else ""

    elif item_type == "thesis":
        item["university"] = ref.publisher or ""
        item["thesisType"] = ""

    elif item_type == "report":
        item["institution"] = ref.publisher or ""
        item["reportType"] = ""

    elif item_type == "webpage":
        item["websiteTitle"] = ref.journal or ref.container_title or ""

    return item


class ZoteroIntegration(BaseIntegration):
    """Push References to a Zotero library."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        user_id: Optional[str] = None,
        library_type: Optional[str] = None,
        library_id: Optional[str] = None,
        collection_id: Optional[str] = None,
    ) -> None:
        from ..config import get_config
        cfg = get_config()
        self._api_key      = api_key      or cfg.zotero_api_key
        self._user_id      = user_id      or cfg.zotero_user_id
        self._lib_type     = library_type or cfg.zotero_library_type or "user"
        self._lib_id       = library_id   or cfg.zotero_library_id or self._user_id
        self._collection   = collection_id or cfg.zotero_collection_id
        self._client: Optional[httpx.AsyncClient] = None

    def _lib_url(self) -> str:
        return f"{_BASE}/{self._lib_type}s/{self._lib_id}"

    def _headers(self) -> Dict[str, str]:
        return {
            "Zotero-API-Key": self._api_key,
            "Zotero-API-Version": _API_VERSION,
            "Content-Type": "application/json",
        }

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(
            headers=self._headers(),
            follow_redirects=True,
            timeout=30,
        )

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()

    async def is_configured(self) -> bool:
        return bool(self._api_key and self._lib_id)

    async def push(self, refs: List[Reference]) -> List[str]:
        """
        Upload refs to Zotero in batches of 50 (API limit).
        Returns list of Zotero item keys.
        """
        if not await self.is_configured():
            raise RuntimeError("Zotero not configured — set api_key and user_id")

        all_keys: List[str] = [""] * len(refs)

        # Zotero accepts max 50 items per request
        BATCH = 50
        for batch_start in range(0, len(refs), BATCH):
            batch = refs[batch_start : batch_start + BATCH]
            items = []
            for ref in batch:
                item = _ref_to_zotero_item(ref)
                if self._collection:
                    item["collections"] = [self._collection]
                items.append(item)

            resp = await self._client.post(
                f"{self._lib_url()}/items",
                json=items,
            )
            if resp.status_code not in (200, 201):
                # Partial failure — fill with empty strings
                continue

            data = resp.json()
            success = data.get("success", {})
            for local_idx_str, key in success.items():
                global_idx = batch_start + int(local_idx_str)
                if global_idx < len(all_keys):
                    all_keys[global_idx] = key

        return all_keys

    async def push_or_update(
        self,
        pairs: List[tuple],
    ) -> List[str]:
        """
        Idempotent upsert: POST new items, PATCH existing ones.

        ``pairs`` is a list of ``(existing_key_or_none, ref)`` tuples.
        Returns a list of Zotero item keys in the same order.
        """
        if not await self.is_configured():
            raise RuntimeError("Zotero not configured — set api_key and user_id")

        results: List[str] = [""] * len(pairs)

        # Separate new refs (no existing key) from refs to update.
        new_indices  = [i for i, (k, _) in enumerate(pairs) if not k]
        upd_indices  = [i for i, (k, _) in enumerate(pairs) if k]

        # POST new items in batches of 50 (Zotero API limit).
        BATCH = 50
        for batch_start in range(0, len(new_indices), BATCH):
            batch_idxs = new_indices[batch_start : batch_start + BATCH]
            items = []
            for i in batch_idxs:
                item = _ref_to_zotero_item(pairs[i][1])
                if self._collection:
                    item["collections"] = [self._collection]
                items.append(item)

            resp = await self._client.post(
                f"{self._lib_url()}/items", json=items
            )
            if resp.status_code not in (200, 201):
                continue
            success = resp.json().get("success", {})
            for local_idx_str, key in success.items():
                global_i = batch_idxs[int(local_idx_str)]
                results[global_i] = key

        # PATCH existing items individually (Zotero has no bulk PATCH).
        for i in upd_indices:
            key, ref = pairs[i]
            item = _ref_to_zotero_item(ref)
            try:
                resp = await self._client.patch(
                    f"{self._lib_url()}/items/{key}", json=item
                )
                results[i] = key if resp.status_code in (200, 204) else ""
            except Exception:
                results[i] = ""

        return results

    async def get_collections(self) -> List[Dict[str, Any]]:
        """Return the user's Zotero collections (for collection selection)."""
        if not await self.is_configured():
            return []
        resp = await self._client.get(f"{self._lib_url()}/collections")
        if resp.status_code != 200:
            return []
        return [
            {"key": c["key"], "name": c["data"]["name"]}
            for c in resp.json()
        ]
