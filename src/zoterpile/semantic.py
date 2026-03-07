"""
Semantic index — embedding-based search and analysis for references.

Provides dense-vector search and topic clustering that go far beyond the
SQLite FTS5 keyword index.  Designed to feed downstream tools (classifiers,
recommender systems, UMAP visualisations, etc.) via the export_embeddings()
interface.

Backend
-------
* Embeddings : sentence-transformers  ``all-MiniLM-L6-v2``  (384-d, ~80 MB)
  Optionally overridden via ``Config.semantic_model``.
* Vector store: ChromaDB (persistent, embedded, no server required)
  Supports cosine-similarity search and optional metadata filtering by year,
  ref_type, and open_access.

Both dependencies are optional — importing this module succeeds even if they
are not installed; individual methods raise a clear RuntimeError instead.

Scale
-----
ChromaDB with HNSW can comfortably handle 500k+ vectors on a laptop. Batch
encoding with sentence-transformers processes ~512 refs/s on a modern CPU
and ~2000/s with a GPU.

Usage
-----
    from zoterpile.semantic import SemanticIndex

    idx = SemanticIndex()
    idx.index(ref_id, ref)
    idx.index_many([(ref_id, ref), ...])
    results = idx.search("attention transformer", n=10)
    similar = idx.find_similar(ref_id, n=5)
    topics  = idx.cluster_topics(n_clusters=10)
    ids, matrix = idx.export_embeddings()   # numpy array for external tools
    idx.reindex_all(db)
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .db import RefDatabase
    from .models import Reference

log = logging.getLogger(__name__)

_DEFAULT_CHROMA_DIR = Path.home() / ".local" / "share" / "zoterpile" / "semantic"
_DEFAULT_MODEL = "all-MiniLM-L6-v2"
_COLLECTION_NAME = "references"

# Stop-words for top-term extraction
_STOP = frozenset(
    "the a an and or of in for to with on at by is are was were be has have "
    "that this it as we our from can not also using based used data paper "
    "study show result method approach results using these which".split()
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _ref_text(ref: "Reference") -> str:
    """Produce the text to embed for a reference (title + abstract + keywords)."""
    parts: List[str] = []
    if ref.title:
        # Title weighted 2x by repeating it
        parts.append(ref.title)
        parts.append(ref.title)
    if ref.abstract:
        parts.append(ref.abstract[:1500])
    if ref.keywords:
        parts.append(" ".join(ref.keywords[:15]))
    if ref.journal:
        parts.append(ref.journal)
    if ref.authors:
        parts.append(" ".join(a.family for a in ref.authors[:3] if a.family))
    return " ".join(parts).strip()


def _ref_to_meta(ref: "Reference") -> Dict[str, Any]:
    """Flat metadata dict for ChromaDB (values must be str/int/float/bool)."""
    return {
        "year":        ref.year or 0,
        "ref_type":    ref.ref_type.value if ref.ref_type else "unknown",
        "open_access": bool(ref.open_access),
        "doi":         (ref.doi or "")[:200],
        "journal":     (ref.journal or "")[:200],
    }


def _build_chroma_where(
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    ref_type: Optional[str] = None,
    oa_only: bool = False,
) -> Optional[Dict]:
    conditions: List[Dict] = []
    if year_from:
        conditions.append({"year": {"$gte": year_from}})
    if year_to:
        conditions.append({"year": {"$lte": year_to}})
    if ref_type:
        conditions.append({"ref_type": {"$eq": ref_type}})
    if oa_only:
        conditions.append({"open_access": {"$eq": True}})
    if not conditions:
        return None
    return conditions[0] if len(conditions) == 1 else {"$and": conditions}


def _extract_top_terms(documents: List[str], n: int = 8) -> List[str]:
    """Most frequent non-stop terms across a list of documents."""
    counter: Counter = Counter()
    for doc in documents:
        for word in re.findall(r"\b[a-z]{3,}\b", doc.lower()):
            if word not in _STOP:
                counter[word] += 1
    return [w for w, _ in counter.most_common(n)]


# ---------------------------------------------------------------------------
# SemanticIndex
# ---------------------------------------------------------------------------

class SemanticIndex:
    """
    Persistent semantic index backed by ChromaDB + sentence-transformers.

    Public methods
    --------------
    index(ref_id, ref)
    index_many(pairs, batch_size, progress_callback)
    delete(ref_id)
    count() -> int

    search(query, n, year_from, year_to, ref_type, oa_only)
        -> [(ref_id, score), ...]

    find_similar(ref_id, n)
        -> [(ref_id, score), ...]

    cluster_topics(n_clusters, max_refs)
        -> [{"cluster_id", "size", "ref_ids", "top_terms"}, ...]

    export_embeddings(ref_ids=None)
        -> (ref_ids: list[str], matrix: np.ndarray)   # for external tools

    reindex_all(db, chunk_size, progress_callback) -> int
    """

    def __init__(
        self,
        persist_dir: Optional[Path | str] = None,
        model_name: str = _DEFAULT_MODEL,
    ) -> None:
        self._dir        = Path(persist_dir) if persist_dir else _DEFAULT_CHROMA_DIR
        self._model_name = model_name
        self._embedder   = None   # lazy-loaded
        self._collection = None   # lazy-loaded

    # -----------------------------------------------------------------------
    # Availability check
    # -----------------------------------------------------------------------

    def is_available(self) -> bool:
        """True if sentence-transformers and chromadb are both installed."""
        try:
            import sentence_transformers  # noqa: F401
            import chromadb               # noqa: F401
            return True
        except ImportError:
            return False

    # -----------------------------------------------------------------------
    # Lazy initialisation
    # -----------------------------------------------------------------------

    def _get_embedder(self):
        if self._embedder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers is required for the semantic index.\n"
                    "Install:  pip install sentence-transformers"
                )
            self._embedder = SentenceTransformer(self._model_name)
        return self._embedder

    def _get_collection(self):
        if self._collection is None:
            try:
                import chromadb
            except ImportError:
                raise RuntimeError(
                    "chromadb is required for the semantic index.\n"
                    "Install:  pip install chromadb"
                )
            self._dir.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(path=str(self._dir))
            self._collection = client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    # -----------------------------------------------------------------------
    # Embedding helpers
    # -----------------------------------------------------------------------

    def _embed(self, texts: List[str]) -> List[List[float]]:
        embedder = self._get_embedder()
        vecs = embedder.encode(
            texts,
            batch_size=64,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        return vecs.tolist()

    # -----------------------------------------------------------------------
    # Core write operations
    # -----------------------------------------------------------------------

    def index(self, ref_id: str, ref: "Reference") -> None:
        """Index (or re-index) a single reference."""
        text = _ref_text(ref)
        if not text:
            return
        col       = self._get_collection()
        embedding = self._embed([text])[0]
        col.upsert(
            ids=[ref_id],
            embeddings=[embedding],
            documents=[text],
            metadatas=[_ref_to_meta(ref)],
        )

    def index_many(
        self,
        pairs: List[Tuple[str, "Reference"]],
        batch_size: int = 128,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """
        Batch-index (ref_id, ref) pairs using vectorised encoding.
        Returns the count of successfully indexed references.

        ``progress_callback(done, total)`` is called after each batch.
        """
        col = self._get_collection()
        indexed = 0
        total = len(pairs)

        for start in range(0, total, batch_size):
            batch = pairs[start : start + batch_size]

            # Build valid (non-empty text) entries only
            valid: List[Tuple[str, str, Dict]] = []
            for ref_id, ref in batch:
                text = _ref_text(ref)
                if text.strip():
                    valid.append((ref_id, text, _ref_to_meta(ref)))

            if valid:
                v_ids, v_texts, v_metas = zip(*valid)
                embeddings = self._embed(list(v_texts))
                col.upsert(
                    ids=list(v_ids),
                    embeddings=embeddings,
                    documents=list(v_texts),
                    metadatas=list(v_metas),
                )
                indexed += len(valid)

            if progress_callback:
                progress_callback(min(start + batch_size, total), total)

        return indexed

    def delete(self, ref_id: str) -> None:
        """Remove a reference from the index."""
        try:
            self._get_collection().delete(ids=[ref_id])
        except Exception:
            pass

    def count(self) -> int:
        """Number of references currently in the index."""
        try:
            return self._get_collection().count()
        except Exception:
            return 0

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    def search(
        self,
        query: str,
        n: int = 10,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        ref_type: Optional[str] = None,
        oa_only: bool = False,
    ) -> List[Tuple[str, float]]:
        """
        Semantic search by free-text query.

        Returns ``[(ref_id, score), ...]`` sorted by descending similarity,
        where 1.0 = identical and 0.0 = completely unrelated.
        """
        if not query.strip():
            return []

        col  = self._get_collection()
        n_db = col.count()
        if n_db == 0:
            return []

        embedding = self._embed([query])[0]
        where     = _build_chroma_where(year_from, year_to, ref_type, oa_only)

        kwargs: Dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results":        min(n, n_db),
            "include":          ["distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            result = col.query(**kwargs)
        except Exception as exc:
            log.warning("Semantic search failed: %s", exc)
            return []

        ids       = result["ids"][0]
        distances = result["distances"][0]   # cosine distance: 0=identical, 2=opposite
        return [(rid, 1.0 - d / 2.0) for rid, d in zip(ids, distances)]

    def find_similar(
        self,
        ref_id: str,
        n: int = 5,
    ) -> List[Tuple[str, float]]:
        """
        Find the n most semantically similar references to the one with ref_id.
        Returns ``[(ref_id, score), ...]``, excluding the query ref itself.
        """
        col = self._get_collection()
        try:
            existing = col.get(ids=[ref_id], include=["embeddings"])
        except Exception:
            return []

        if not existing.get("embeddings"):
            return []

        embedding = existing["embeddings"][0]
        n_db = col.count()
        result = col.query(
            query_embeddings=[embedding],
            n_results=min(n + 1, n_db),
            include=["distances"],
        )

        return [
            (rid, 1.0 - d / 2.0)
            for rid, d in zip(result["ids"][0], result["distances"][0])
            if rid != ref_id
        ][:n]

    # -----------------------------------------------------------------------
    # Topic clustering
    # -----------------------------------------------------------------------

    def cluster_topics(
        self,
        n_clusters: int = 10,
        max_refs: int = 50_000,
    ) -> List[Dict[str, Any]]:
        """
        Cluster all indexed references into ``n_clusters`` topic groups using
        MiniBatch k-means on the embedding matrix.

        Requires ``numpy`` and ``scikit-learn``.

        Returns a list of dicts sorted by cluster size (descending):
            {
                "cluster_id": int,
                "size":       int,
                "ref_ids":    [str, ...],
                "top_terms":  [str, ...],
            }
        """
        try:
            import numpy as np
            from sklearn.cluster import MiniBatchKMeans
        except ImportError:
            raise RuntimeError(
                "numpy and scikit-learn are required for topic clustering.\n"
                "Install:  pip install numpy scikit-learn"
            )

        col   = self._get_collection()
        total = col.count()
        if total == 0:
            return []

        data = col.get(
            limit=min(total, max_refs),
            include=["embeddings", "documents", "ids"],
        )
        if not data.get("embeddings"):
            return []

        embeddings = np.array(data["embeddings"], dtype=np.float32)
        ref_ids    = data["ids"]
        documents  = data.get("documents") or [""] * len(ref_ids)

        k      = min(n_clusters, len(ref_ids))
        kmeans = MiniBatchKMeans(n_clusters=k, n_init=3, random_state=42)
        labels = kmeans.fit_predict(embeddings)

        clusters:    Dict[int, List[str]]  = defaultdict(list)
        docs_by_cls: Dict[int, List[str]]  = defaultdict(list)
        for rid, label, doc in zip(ref_ids, labels, documents):
            clusters[int(label)].append(rid)
            docs_by_cls[int(label)].append(doc)

        return sorted(
            [
                {
                    "cluster_id": cid,
                    "size":       len(members),
                    "ref_ids":    members,
                    "top_terms":  _extract_top_terms(docs_by_cls[cid]),
                }
                for cid, members in clusters.items()
            ],
            key=lambda c: -c["size"],
        )

    # -----------------------------------------------------------------------
    # Export for external tools
    # -----------------------------------------------------------------------

    def export_embeddings(
        self,
        ref_ids: Optional[List[str]] = None,
    ) -> Tuple[List[str], Any]:
        """
        Export embeddings as ``(ref_ids, numpy_array)`` for downstream use in
        UMAP/t-SNE visualisations, custom classifiers, or other tools.

        If ``ref_ids`` is None, all indexed references are exported.
        Requires ``numpy``.
        """
        try:
            import numpy as np
        except ImportError:
            raise RuntimeError("numpy is required for export_embeddings.")

        col = self._get_collection()
        if ref_ids:
            data = col.get(ids=ref_ids, include=["embeddings", "ids"])
        else:
            data = col.get(include=["embeddings", "ids"])

        if not data.get("embeddings"):
            return [], np.array([], dtype=np.float32)

        return data["ids"], np.array(data["embeddings"], dtype=np.float32)

    # -----------------------------------------------------------------------
    # Bulk reindex from database
    # -----------------------------------------------------------------------

    def reindex_all(
        self,
        db: "RefDatabase",
        chunk_size: int = 500,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> int:
        """
        Rebuild the entire semantic index from a RefDatabase.

        Streams references in chunks via ``db.iter_all()`` to avoid loading
        the entire database into memory.  Safe to call on a live database.

        Returns the total number of references indexed.
        """
        from .db import _ref_id as compute_ref_id

        total   = db.count()
        indexed = 0

        for chunk in db.iter_all(chunk_size=chunk_size):
            pairs = [(compute_ref_id(ref), ref) for ref in chunk]
            self.index_many(
                pairs,
                progress_callback=(
                    (lambda done, _t: progress_callback(indexed + done, total))
                    if progress_callback else None
                ),
            )
            indexed += len(pairs)

        return indexed


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_index: Optional[SemanticIndex] = None


def get_default_index(model: Optional[str] = None) -> SemanticIndex:
    global _default_index
    if _default_index is None:
        from .config import get_config
        cfg   = get_config()
        path  = getattr(cfg, "semantic_index_path", "") or None
        model = model or getattr(cfg, "semantic_model", _DEFAULT_MODEL)
        _default_index = SemanticIndex(persist_dir=path, model_name=model)
    return _default_index
