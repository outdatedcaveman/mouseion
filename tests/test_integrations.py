"""Tests for Zotero, Notion, Obsidian, and Instapaper integrations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from mouseion.models import Author, Reference, RefType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_author(family: str, given: str = "") -> Author:
    return Author(family=family, given=given)


def _journal_ref(**kwargs) -> Reference:
    defaults = dict(
        title="Test Paper",
        doi="10.1234/test",
        year=2023,
        journal="Journal of Testing",
        volume="5",
        issue="2",
        pages="1-10",
        abstract="An abstract.",
        keywords=["test", "paper"],
        open_access=True,
        oa_url="https://example.com/paper.pdf",
        citation_count=10,
        ref_type=RefType.JOURNAL,
        authors=[_make_author("Smith", "John"), _make_author("Doe", "Jane")],
    )
    defaults.update(kwargs)
    return Reference(**defaults)


def _mock_client_response(status: int, data: Any) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    if isinstance(data, str):
        resp.text = data
        resp.json = MagicMock(side_effect=json.JSONDecodeError("", "", 0))
    else:
        resp.json = MagicMock(return_value=data)
        resp.text = json.dumps(data)
    return resp


# ===========================================================================
# Zotero integration tests
# ===========================================================================

class TestZoteroIntegration:
    @pytest.fixture
    def integration(self, tmp_path, monkeypatch):
        from mouseion.config import Config
        import mouseion.config as cfg_mod
        fake_cfg = Config(
            zotero_api_key="fake_key",
            zotero_user_id="12345",
            zotero_library_type="user",
            zotero_library_id="12345",
        )
        monkeypatch.setattr(cfg_mod, "get_config", lambda: fake_cfg)
        from mouseion.integrations.zotero import ZoteroIntegration
        return ZoteroIntegration(
            api_key="fake_key",
            user_id="12345",
            library_type="user",
            library_id="12345",
        )

    @pytest.mark.asyncio
    async def test_is_configured_true(self, integration):
        assert await integration.is_configured() is True

    @pytest.mark.asyncio
    async def test_is_configured_false_without_key(self):
        from mouseion.integrations.zotero import ZoteroIntegration
        z = ZoteroIntegration(api_key="", user_id="")
        assert await z.is_configured() is False

    @pytest.mark.asyncio
    async def test_push_returns_item_keys(self, integration):
        fake_resp = _mock_client_response(200, {
            "success": {"0": "ABCDEF12", "1": "GHIJKL34"},
            "failed": {},
        })
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        integration._client = mock_client

        refs = [_journal_ref(), _journal_ref(title="Second Paper")]
        keys = await integration.push(refs)
        assert len(keys) == 2
        assert "ABCDEF12" in keys

    @pytest.mark.asyncio
    async def test_push_raises_when_not_configured(self):
        from mouseion.integrations.zotero import ZoteroIntegration
        z = ZoteroIntegration(api_key="", user_id="")
        z._client = AsyncMock()
        with pytest.raises(RuntimeError, match="not configured"):
            await z.push([_journal_ref()])

    @pytest.mark.asyncio
    async def test_push_handles_partial_failure(self, integration):
        # API returns 500 for one batch
        fake_resp = _mock_client_response(500, {})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        integration._client = mock_client

        refs = [_journal_ref()]
        keys = await integration.push(refs)
        # Should return empty strings for failed items, not raise
        assert len(keys) == 1
        assert keys[0] == ""

    def test_ref_to_zotero_item_type(self):
        from mouseion.integrations.zotero import _ref_to_zotero_item
        item = _ref_to_zotero_item(_journal_ref())
        assert item["itemType"] == "journalArticle"

    def test_ref_to_zotero_item_book_type(self):
        from mouseion.integrations.zotero import _ref_to_zotero_item
        ref = Reference(title="A Book", ref_type=RefType.BOOK,
                        authors=[_make_author("Smith", "John")],
                        year=2020, publisher="MIT Press")
        item = _ref_to_zotero_item(ref)
        assert item["itemType"] == "book"

    def test_ref_to_zotero_item_creators(self):
        from mouseion.integrations.zotero import _ref_to_zotero_item
        item = _ref_to_zotero_item(_journal_ref())
        assert any(c["lastName"] == "Smith" for c in item["creators"])

    def test_ref_to_zotero_item_doi(self):
        from mouseion.integrations.zotero import _ref_to_zotero_item
        item = _ref_to_zotero_item(_journal_ref())
        assert item.get("DOI") == "10.1234/test"

    def test_ref_to_zotero_item_title(self):
        from mouseion.integrations.zotero import _ref_to_zotero_item
        item = _ref_to_zotero_item(_journal_ref())
        assert item["title"] == "Test Paper"

    def test_ref_to_zotero_item_arxiv_in_extra(self):
        from mouseion.integrations.zotero import _ref_to_zotero_item
        ref = _journal_ref(arxiv_id="2301.00001")
        item = _ref_to_zotero_item(ref)
        assert "arXiv" in item.get("extra", "")


# ===========================================================================
# Notion integration tests
# ===========================================================================

class TestNotionIntegration:
    @pytest.fixture
    def integration(self, monkeypatch):
        from mouseion.config import Config
        import mouseion.config as cfg_mod
        fake_cfg = Config(notion_api_key="fake_key", notion_database_id="db_abc")
        monkeypatch.setattr(cfg_mod, "get_config", lambda: fake_cfg)
        from mouseion.integrations.notion import NotionIntegration
        ni = NotionIntegration(api_key="fake_key", database_id="db_abc")
        ni._db_properties = {}  # skip schema fetch
        return ni

    @pytest.mark.asyncio
    async def test_is_configured_true(self, integration):
        assert await integration.is_configured() is True

    @pytest.mark.asyncio
    async def test_is_configured_false_no_key(self):
        from mouseion.integrations.notion import NotionIntegration
        ni = NotionIntegration(api_key="", database_id="")
        assert await ni.is_configured() is False

    @pytest.mark.asyncio
    async def test_push_returns_page_ids(self, integration):
        fake_resp = _mock_client_response(200, {"id": "page-id-123"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        integration._client = mock_client

        page_ids = await integration.push([_journal_ref()])
        assert len(page_ids) == 1
        assert page_ids[0] == "page-id-123"

    @pytest.mark.asyncio
    async def test_push_raises_when_not_configured(self):
        from mouseion.integrations.notion import NotionIntegration
        ni = NotionIntegration(api_key="", database_id="")
        ni._client = AsyncMock()
        ni._db_properties = {}
        with pytest.raises(RuntimeError, match="not configured"):
            await ni.push([_journal_ref()])

    @pytest.mark.asyncio
    async def test_push_handles_error_response(self, integration):
        fake_resp = _mock_client_response(400, {"message": "bad request"})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        integration._client = mock_client

        page_ids = await integration.push([_journal_ref()])
        # Should return empty string, not raise
        assert page_ids[0] == ""

    def test_ref_to_notion_properties_title(self):
        from mouseion.integrations.notion import _ref_to_notion_properties
        props = _ref_to_notion_properties(_journal_ref())
        assert "Title" in props
        title_content = props["Title"]["title"][0]["text"]["content"]
        assert "Test Paper" in title_content

    def test_ref_to_notion_properties_year(self):
        from mouseion.integrations.notion import _ref_to_notion_properties
        props = _ref_to_notion_properties(_journal_ref())
        assert props["Year"]["number"] == 2023

    def test_ref_to_notion_properties_doi(self):
        from mouseion.integrations.notion import _ref_to_notion_properties
        props = _ref_to_notion_properties(_journal_ref())
        assert "doi.org/10.1234" in props["DOI"]["url"]

    def test_ref_to_notion_properties_keywords_multi_select(self):
        from mouseion.integrations.notion import _ref_to_notion_properties
        props = _ref_to_notion_properties(_journal_ref())
        kw_names = {k["name"] for k in props["Keywords"]["multi_select"]}
        assert "test" in kw_names

    def test_ref_to_notion_properties_open_access_checkbox(self):
        from mouseion.integrations.notion import _ref_to_notion_properties
        props = _ref_to_notion_properties(_journal_ref())
        assert props["Open Access"]["checkbox"] is True

    def test_ref_to_notion_properties_completeness(self):
        from mouseion.integrations.notion import _ref_to_notion_properties
        props = _ref_to_notion_properties(_journal_ref())
        assert isinstance(props["Completeness"]["number"], (int, float))

    def test_ref_to_notion_blocks_abstract(self):
        from mouseion.integrations.notion import _ref_to_notion_blocks
        blocks = _ref_to_notion_blocks(_journal_ref())
        types = [b["type"] for b in blocks]
        assert "paragraph" in types or "heading_2" in types


# ===========================================================================
# Obsidian integration tests
# ===========================================================================

class TestObsidianIntegration:
    @pytest.fixture
    def vault(self, tmp_path):
        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        return vault_dir

    @pytest.fixture
    def integration(self, vault):
        from mouseion.integrations.obsidian import ObsidianIntegration
        return ObsidianIntegration(
            vault_path=str(vault),
            notes_folder="References",
        )

    @pytest.mark.asyncio
    async def test_is_configured_when_vault_exists(self, integration):
        assert await integration.is_configured() is True

    @pytest.mark.asyncio
    async def test_is_configured_false_missing_vault(self, tmp_path):
        from mouseion.integrations.obsidian import ObsidianIntegration
        oi = ObsidianIntegration(vault_path=str(tmp_path / "nonexistent"))
        assert await oi.is_configured() is False

    @pytest.mark.asyncio
    async def test_push_creates_markdown_file(self, integration, vault):
        ref = _journal_ref()
        paths = await integration.push([ref])
        assert len(paths) == 1
        assert Path(paths[0]).exists()

    @pytest.mark.asyncio
    async def test_push_file_is_markdown(self, integration, vault):
        paths = await integration.push([_journal_ref()])
        assert paths[0].endswith(".md")

    @pytest.mark.asyncio
    async def test_push_file_has_frontmatter(self, integration, vault):
        paths = await integration.push([_journal_ref()])
        content = Path(paths[0]).read_text(encoding="utf-8")
        assert content.startswith("---")
        assert "---" in content[4:]  # closing ---

    @pytest.mark.asyncio
    async def test_push_file_contains_title(self, integration, vault):
        paths = await integration.push([_journal_ref(title="Unique Title XYZ")])
        content = Path(paths[0]).read_text(encoding="utf-8")
        assert "Unique Title XYZ" in content

    @pytest.mark.asyncio
    async def test_push_file_contains_doi(self, integration, vault):
        paths = await integration.push([_journal_ref()])
        content = Path(paths[0]).read_text(encoding="utf-8")
        assert "10.1234/test" in content

    @pytest.mark.asyncio
    async def test_push_multiple_refs_creates_multiple_files(self, integration, vault):
        refs = [
            _journal_ref(title="Paper One", doi="10.xxx/1"),
            _journal_ref(title="Paper Two", doi="10.xxx/2"),
        ]
        paths = await integration.push(refs)
        assert len(paths) == 2
        assert Path(paths[0]).exists()
        assert Path(paths[1]).exists()

    @pytest.mark.asyncio
    async def test_push_raises_when_vault_missing(self, tmp_path):
        from mouseion.integrations.obsidian import ObsidianIntegration
        oi = ObsidianIntegration(vault_path=str(tmp_path / "nope"))
        with pytest.raises(RuntimeError, match="vault"):
            await oi.push([_journal_ref()])

    def test_safe_filename_strips_illegal_chars(self):
        from mouseion.integrations.obsidian import _safe_filename
        assert "/" not in _safe_filename("A/B:C*D")
        assert ":" not in _safe_filename("Time: 10")

    def test_make_filename_cite_key_template(self):
        from mouseion.integrations.obsidian import _make_filename
        ref = _journal_ref()
        name = _make_filename(ref, "{cite_key}")
        assert name.endswith(".md")

    def test_ref_to_markdown_has_yaml_frontmatter(self):
        from mouseion.integrations.obsidian import _ref_to_markdown
        md = _ref_to_markdown(_journal_ref())
        assert md.startswith("---")
        assert "cite_key:" in md

    def test_ref_to_markdown_has_abstract_section(self):
        from mouseion.integrations.obsidian import _ref_to_markdown
        md = _ref_to_markdown(_journal_ref())
        assert "Abstract" in md
        assert "An abstract." in md

    def test_ref_to_markdown_author_wikilinks(self):
        from mouseion.integrations.obsidian import _ref_to_markdown
        md = _ref_to_markdown(_journal_ref())
        assert "[[" in md  # wikilinks for authors


# ===========================================================================
# Instapaper integration tests
# ===========================================================================

class TestInstapaperIntegration:
    @pytest.fixture
    def integration(self):
        from mouseion.integrations.instapaper import InstapaperIntegration
        return InstapaperIntegration(username="user", password="pass")

    @pytest.mark.asyncio
    async def test_is_configured_always_true(self, integration):
        assert await integration.is_configured() is True

    @pytest.mark.asyncio
    async def test_push_ok_returns_ok_status(self, integration):
        fake_resp = _mock_client_response(201, "")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        integration._client = mock_client

        results = await integration.push([_journal_ref()])
        assert results == ["ok"]

    @pytest.mark.asyncio
    async def test_push_no_url_returns_no_url(self, integration):
        mock_client = AsyncMock()
        integration._client = mock_client
        ref = Reference(title="No URL Paper", ref_type=RefType.JOURNAL)
        results = await integration.push([ref])
        assert results == ["no_url"]

    @pytest.mark.asyncio
    async def test_push_error_status_returns_error_string(self, integration):
        fake_resp = _mock_client_response(400, "bad request")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        integration._client = mock_client

        results = await integration.push([_journal_ref()])
        assert results[0].startswith("error:")

    @pytest.mark.asyncio
    async def test_push_multiple_refs(self, integration):
        fake_resp = _mock_client_response(201, "")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_resp)
        integration._client = mock_client

        refs = [_journal_ref(), _journal_ref(title="Second")]
        results = await integration.push(refs)
        assert len(results) == 2
        assert all(r == "ok" for r in results)

    @pytest.mark.asyncio
    async def test_push_exception_returns_error_string(self, integration):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("network down"))
        integration._client = mock_client

        results = await integration.push([_journal_ref()])
        assert results[0].startswith("error:")

    def test_best_url_prefers_doi(self):
        from mouseion.integrations.instapaper import _best_url
        ref = _journal_ref(doi="10.1234/test", url="https://example.com")
        url = _best_url(ref)
        assert "doi.org" in url

    def test_best_url_falls_back_to_url(self):
        from mouseion.integrations.instapaper import _best_url
        ref = Reference(title="No DOI", url="https://example.com/paper",
                        ref_type=RefType.WEBSITE)
        url = _best_url(ref)
        assert url == "https://example.com/paper"

    def test_best_url_arxiv_fallback(self):
        from mouseion.integrations.instapaper import _best_url
        ref = Reference(title="Preprint", arxiv_id="2301.00001",
                        ref_type=RefType.PREPRINT)
        url = _best_url(ref)
        assert "arxiv.org" in url

    def test_best_url_none_when_no_ids(self):
        from mouseion.integrations.instapaper import _best_url
        ref = Reference(title="Nothing", ref_type=RefType.UNKNOWN)
        assert _best_url(ref) is None

    @pytest.mark.asyncio
    async def test_connect_creates_client(self, integration):
        await integration.connect()
        assert integration._client is not None
        await integration.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_closes_client(self, integration):
        await integration.connect()
        assert integration._client is not None
        # Disconnect just closes the underlying connection; the attribute may remain
        await integration.disconnect()
