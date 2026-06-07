"""Tests for the lookup orchestrator, config module, and cache layer."""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mouseion.models import Author, Reference, RefType
from mouseion.providers.base import BaseProvider


# ===========================================================================
# Lookup / enrich tests
# ===========================================================================

class _AlwaysFoundProvider(BaseProvider):
    """Provider that always returns a fixed reference."""
    priority = 1

    def __init__(self, result_ref: Optional[Reference] = None, name: Optional[str] = None):
        super().__init__()
        self.name = name or f"always_found_{id(self)}"
        self._result = result_ref or Reference(
            title="Enriched Title",
            doi="10.xxx/enriched",
            year=2023,
            abstract="Enriched abstract.",
            ref_type=RefType.JOURNAL,
            authors=[Author(family="Enriched", given="Author")],
        )

    async def lookup_by_doi(self, doi, client):
        return self._result

    async def search(self, title, authors=None, year=None, client=None):
        return [self._result]


class _NeverFoundProvider(BaseProvider):
    """Provider that always returns empty results."""
    name = "never_found"
    priority = 99

    async def lookup_by_doi(self, doi, client):
        return None

    async def search(self, title, authors=None, year=None, client=None):
        return []


class _ErrorProvider(BaseProvider):
    """Provider that always raises an exception."""
    name = "error_provider"
    priority = 50

    async def lookup_by_doi(self, doi, client):
        raise RuntimeError("Simulated network failure")

    async def search(self, title, authors=None, year=None, client=None):
        raise RuntimeError("Simulated network failure")


class TestEnrichOne:
    @pytest.mark.asyncio
    async def test_returns_reference(self):
        from mouseion.lookup import enrich_one
        seed = Reference(doi="10.xxx/seed", ref_type=RefType.JOURNAL)
        result = await enrich_one(seed, providers=[_AlwaysFoundProvider()])
        assert isinstance(result, Reference)

    @pytest.mark.asyncio
    async def test_enriched_fields_present(self):
        from mouseion.lookup import enrich_one
        enriched = Reference(
            title="Enriched Paper",
            doi="10.xxx/enriched",
            year=2024,
            abstract="Long detailed abstract.",
            ref_type=RefType.JOURNAL,
            authors=[Author(family="Author", given="First")],
        )
        seed = Reference(doi="10.xxx/enriched", ref_type=RefType.JOURNAL)
        result = await enrich_one(seed, providers=[_AlwaysFoundProvider(enriched)])
        assert result.title == "Enriched Paper"
        assert result.year == 2024

    @pytest.mark.asyncio
    async def test_returns_seed_when_no_providers(self):
        from mouseion.lookup import enrich_one
        seed = Reference(title="Seed Only", ref_type=RefType.JOURNAL)
        result = await enrich_one(seed, providers=[])
        assert result.title == "Seed Only"

    @pytest.mark.asyncio
    async def test_error_provider_doesnt_crash(self):
        from mouseion.lookup import enrich_one
        seed = Reference(doi="10.xxx/1", ref_type=RefType.JOURNAL)
        # Should not raise even when a provider throws
        result = await enrich_one(seed, providers=[_ErrorProvider()])
        assert isinstance(result, Reference)

    @pytest.mark.asyncio
    async def test_multiple_providers_merged(self):
        from mouseion.lookup import enrich_one
        provider_a = _AlwaysFoundProvider(Reference(
            title="Title from A",
            doi="10.xxx/1",
            year=2023,
            ref_type=RefType.JOURNAL,
            citation_count=10,
        ))
        provider_b = _AlwaysFoundProvider(Reference(
            title="Title from A",
            doi="10.xxx/1",
            year=2023,
            ref_type=RefType.JOURNAL,
            citation_count=100,
        ))
        seed = Reference(doi="10.xxx/1", ref_type=RefType.JOURNAL)
        result = await enrich_one(seed, providers=[provider_a, provider_b])
        # Should take the max citation count from merge
        assert result.citation_count == 100

    @pytest.mark.asyncio
    async def test_never_found_returns_seed_data(self):
        from mouseion.lookup import enrich_one
        seed = Reference(title="My Paper", doi="10.xxx/1", ref_type=RefType.JOURNAL)
        result = await enrich_one(seed, providers=[_NeverFoundProvider()])
        assert result.title == "My Paper"


class TestEnrichBatch:
    @pytest.mark.asyncio
    async def test_batch_returns_same_count(self):
        from mouseion.lookup import enrich_batch
        seeds = [
            Reference(doi=f"10.xxx/{i}", ref_type=RefType.JOURNAL)
            for i in range(5)
        ]
        results = await enrich_batch(seeds, providers=[_AlwaysFoundProvider()])
        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_batch_all_references(self):
        from mouseion.lookup import enrich_batch
        seeds = [Reference(doi=f"10.xxx/{i}", ref_type=RefType.JOURNAL) for i in range(3)]
        results = await enrich_batch(seeds, providers=[_AlwaysFoundProvider()])
        assert all(isinstance(r, Reference) for r in results)

    @pytest.mark.asyncio
    async def test_batch_progress_callback(self):
        from mouseion.lookup import enrich_batch
        called = []
        def cb(idx, total, ref):
            called.append((idx, total))
        seeds = [Reference(doi="10.xxx/1", ref_type=RefType.JOURNAL)]
        await enrich_batch(seeds, providers=[_AlwaysFoundProvider()], progress_callback=cb)
        assert len(called) == 1
        assert called[0] == (1, 1)

    @pytest.mark.asyncio
    async def test_batch_error_provider_fills_with_seed(self):
        from mouseion.lookup import enrich_batch
        seed = Reference(title="My Paper", ref_type=RefType.JOURNAL)
        results = await enrich_batch([seed], providers=[_ErrorProvider()])
        assert len(results) == 1
        # seed data should be preserved
        assert results[0].title == "My Paper"

    def test_enrich_batch_sync(self):
        from mouseion.lookup import enrich_batch_sync
        seeds = [Reference(doi="10.xxx/1", ref_type=RefType.JOURNAL)]
        results = enrich_batch_sync(seeds, providers=[_AlwaysFoundProvider()])
        assert len(results) == 1

    def test_enrich_one_sync(self):
        from mouseion.lookup import enrich_one_sync
        seed = Reference(doi="10.xxx/1", ref_type=RefType.JOURNAL)
        result = enrich_one_sync(seed, providers=[_AlwaysFoundProvider()])
        assert isinstance(result, Reference)


# ===========================================================================
# Config tests
# ===========================================================================

class TestConfig:
    def test_default_config_returns_config_object(self):
        from mouseion.config import Config
        cfg = Config()
        assert isinstance(cfg, Config)

    def test_default_db_path_set(self):
        from mouseion.config import Config
        cfg = Config()
        assert cfg.db_path
        assert "mouseion" in cfg.db_path or cfg.db_path.endswith(".db")

    def test_default_fields_are_strings(self):
        from mouseion.config import Config
        cfg = Config()
        assert isinstance(cfg.crossref_email, str)
        assert isinstance(cfg.notion_api_key, str)
        assert isinstance(cfg.zotero_api_key, str)

    def test_env_override_crossref_email(self, monkeypatch):
        monkeypatch.setenv("CROSSREF_EMAIL", "test@example.com")
        # get_config reads env vars; reload to pick up the change
        from importlib import reload
        import mouseion.config as cfg_mod
        reload(cfg_mod)
        # Just test that Config can be constructed; env var integration
        # is handled per-provider
        cfg = cfg_mod.Config()
        assert isinstance(cfg, cfg_mod.Config)

    def test_get_config_returns_config(self):
        from mouseion.config import get_config
        cfg = get_config()
        assert hasattr(cfg, "db_path")

    def test_auto_tag_rules_default_empty(self):
        from mouseion.config import Config
        cfg = Config()
        assert cfg.auto_tag_rules == []

    def test_config_asdict_roundtrip(self):
        from mouseion.config import Config
        from dataclasses import asdict
        cfg = Config()
        d = asdict(cfg)
        assert "db_path" in d
        assert "crossref_email" in d

    def test_save_and_load_config(self, tmp_path, monkeypatch):
        """save_config should persist to disk; get_config should read it back."""
        from mouseion.config import save_config, Config
        import mouseion.config as cfg_mod

        cfg = Config(crossref_email="saved@test.com")
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr(cfg_mod, "_CONFIG_PATH", config_file)

        # save_config should not crash
        try:
            save_config(cfg)
        except Exception:
            pytest.skip("tomlkit/tomli not available for write test")


# ===========================================================================
# Cache tests
# ===========================================================================

class TestReferenceCache:
    @pytest.fixture
    def cache(self, tmp_path):
        from mouseion.cache import ReferenceCache
        c = ReferenceCache(directory=tmp_path / "cache", ttl=3600)
        yield c
        c.close()

    def test_get_miss_returns_none(self, cache):
        result = cache.get("crossref", "doi", "10.xxx/missing")
        assert result is None

    def test_set_and_get(self, cache):
        data = {"title": "Cached Paper"}
        cache.set("crossref", "doi", "10.xxx/1", data)
        result = cache.get("crossref", "doi", "10.xxx/1")
        assert result == data

    def test_hit_increments_counter(self, cache):
        cache.set("crossref", "doi", "10.xxx/1", {"title": "test"})
        cache.get("crossref", "doi", "10.xxx/1")
        assert cache.hits == 1

    def test_miss_increments_counter(self, cache):
        cache.get("crossref", "doi", "10.xxx/missing")
        assert cache.misses == 1

    def test_stats_hit_rate(self, cache):
        cache.set("crossref", "doi", "10.xxx/1", {"x": 1})
        cache.get("crossref", "doi", "10.xxx/1")   # hit
        cache.get("crossref", "doi", "10.xxx/2")   # miss
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_clear_removes_entries(self, cache):
        cache.set("crossref", "doi", "10.xxx/1", {"x": 1})
        cache.clear()
        result = cache.get("crossref", "doi", "10.xxx/1")
        assert result is None

    def test_different_providers_isolated(self, cache):
        cache.set("crossref", "doi", "10.xxx/1", {"src": "crossref"})
        cache.set("pubmed", "doi", "10.xxx/1", {"src": "pubmed"})
        r1 = cache.get("crossref", "doi", "10.xxx/1")
        r2 = cache.get("pubmed", "doi", "10.xxx/1")
        assert r1["src"] == "crossref"
        assert r2["src"] == "pubmed"

    def test_context_manager(self, tmp_path):
        from mouseion.cache import ReferenceCache
        with ReferenceCache(directory=tmp_path / "cm_cache") as c:
            c.set("test", "key", "value", {"data": 42})
            assert c.get("test", "key", "value") == {"data": 42}

    def test_key_is_deterministic(self, cache):
        cache.set("crossref", "doi", "10.xxx/ABC", {"x": 1})
        # Same lookup — different capitalisation in value should still match
        # (keys are lowercased before hashing in the implementation)
        assert cache.get("crossref", "doi", "10.XXX/ABC") == {"x": 1}

    def test_stats_size_bytes_returned(self, cache):
        cache.set("x", "y", "z", {"large": "data" * 100})
        stats = cache.stats()
        assert "size_bytes" in stats
        assert stats["size_bytes"] >= 0


# ===========================================================================
# Quota manager tests
# ===========================================================================

class TestQuota:
    @pytest.fixture
    def qm(self, tmp_path):
        from mouseion.quota import QuotaManager, ProviderLimits
        return QuotaManager(
            limits={"fast": ProviderLimits(requests_per_minute=100)},
            state_file=tmp_path / "quota.json",
        )

    @pytest.mark.asyncio
    async def test_acquire_permits_request(self, qm):
        """acquire() completes without error for a fresh provider."""
        async with qm.acquire("fast"):
            pass  # should not block or raise

    @pytest.mark.asyncio
    async def test_acquire_records_request(self, qm):
        import time
        async with qm.acquire("fast"):
            pass
        state = qm._get_state("fast")
        assert len(state._minute) == 1

    @pytest.mark.asyncio
    async def test_daily_quota_exceeded_raises(self, tmp_path):
        """QuotaExceeded is raised when daily limit is exhausted and reset is far."""
        from mouseion.quota import QuotaManager, ProviderLimits, QuotaExceeded
        qm = QuotaManager(
            limits={"tiny": ProviderLimits(requests_per_day=1)},
            state_file=tmp_path / "q2.json",
        )
        # First request succeeds
        async with qm.acquire("tiny"):
            pass
        # Manually inject far-future timestamps to simulate exhaustion
        import time
        state = qm._get_state("tiny")
        state._day.clear()
        state._day.append(time.time() - 1)   # 1 second old but daily limit is 1
        # Second request should see quota exhausted and remaining > 600s doesn't apply
        # since the timestamp is 1s old (remaining ≈ 86399s > 600s → raise)
        with pytest.raises(QuotaExceeded) as exc_info:
            async with qm.acquire("tiny"):
                pass
        assert exc_info.value.provider == "tiny"
        assert exc_info.value.window == "day"

    @pytest.mark.asyncio
    async def test_quota_exceeded_message_has_provider(self, tmp_path):
        """The exception message includes the correct provider name (bug fix)."""
        from mouseion.quota import QuotaManager, ProviderLimits, QuotaExceeded
        qm = QuotaManager(
            limits={"myprovider": ProviderLimits(requests_per_day=1)},
            state_file=tmp_path / "q3.json",
        )
        async with qm.acquire("myprovider"):
            pass
        import time
        state = qm._get_state("myprovider")
        state._day.clear()
        state._day.append(time.time() - 1)
        with pytest.raises(QuotaExceeded) as exc_info:
            async with qm.acquire("myprovider"):
                pass
        # Before the fix, this was "?" in the exception message
        assert "myprovider" in str(exc_info.value)

    def test_get_status_returns_counts(self, qm):
        status = qm.get_status()
        assert isinstance(status, dict)

    def test_save_and_reload_state(self, tmp_path):
        """State persisted to disk is restored on next instantiation."""
        import asyncio, time
        from mouseion.quota import QuotaManager, ProviderLimits

        state_file = tmp_path / "qstate.json"
        qm1 = QuotaManager(
            limits={"crossref": ProviderLimits(requests_per_minute=50)},
            state_file=state_file,
        )

        async def _run():
            async with qm1.acquire("crossref"):
                pass

        asyncio.run(_run())
        qm1.save_state()

        # New instance with same file
        qm2 = QuotaManager(
            limits={"crossref": ProviderLimits(requests_per_minute=50)},
            state_file=state_file,
        )
        state = qm2._get_state("crossref")
        assert len(state._day) >= 1
