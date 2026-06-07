"""
Integration layer — push enriched references to external tools.

Each integration is a separate module that can be used independently.
All integrations share the same base class and are async.

Available integrations:
  - Zotero      (zotero.py)   — push to a Zotero library via API
  - Notion      (notion.py)   — create pages in a Notion database
  - Obsidian    (obsidian.py) — write markdown files to a vault
  - Instapaper  (instapaper.py) — add URLs to reading list

Usage:
    from mouseion.integrations.zotero import ZoteroIntegration
    async with ZoteroIntegration() as z:
        await z.push(refs)
"""

from .zotero    import ZoteroIntegration
from .notion    import NotionIntegration
from .obsidian  import ObsidianIntegration
from .instapaper import InstapaperIntegration

__all__ = [
    "ZoteroIntegration",
    "NotionIntegration",
    "ObsidianIntegration",
    "InstapaperIntegration",
]
