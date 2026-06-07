"""
Academic database providers.

Each provider can look up references by identifier (DOI/PMID/arXiv ID)
or search by title + optional author.  All calls are async.

Priority order (highest reliability first):
  1. CrossRef        — DOI registry, gold standard for journal articles
  2. DOI.org         — content negotiation, authoritative for DOI metadata
  3. OpenAlex        — broad coverage, fully open, very fast
  4. Semantic Scholar — strong in CS/AI, good citation graph data
  5. PubMed          — best for biomedical
  6. arXiv API       — authoritative for arXiv papers
  7. DBLP            — best for CS conferences
  8. arXiv           — preprints (legacy)
  9. OpenLibrary     — books via ISBN (archive.org)
 10. Google Books    — books via ISBN (broader coverage)
 11. Unpaywall       — open access PDF URLs via DOI
"""

from .crossref         import CrossRefProvider
from .semantic_scholar import SemanticScholarProvider
from .openalex         import OpenAlexProvider
from .pubmed           import PubMedProvider
from .dblp             import DBLPProvider
from .arxiv            import ArXivProvider
from .openlibrary      import OpenLibraryProvider
from .doi_org          import DOIOrgProvider
from .unpaywall        import UnpaywallProvider
from .arxiv_api        import ArXivAPIProvider
from .google_books     import GoogleBooksProvider

def _make_providers():
    """Create fresh provider instances.

    IMPORTANT: Provider instances hold httpx AsyncClients with asyncio locks
    that are bound to the event loop in which they were first used.  If you
    reuse providers across separate ``anyio.run()`` calls (each of which
    creates a new event loop) those locks will raise
    "is bound to a different event loop".  Always call this factory to get
    fresh providers for each enrichment cycle.
    """
    return [
        CrossRefProvider(),
        DOIOrgProvider(),
        OpenAlexProvider(),
        SemanticScholarProvider(),
        PubMedProvider(),
        ArXivAPIProvider(),
        DBLPProvider(),
        ArXivProvider(),
        OpenLibraryProvider(),
        GoogleBooksProvider(),
        UnpaywallProvider(),
    ]


# Kept for backwards compatibility but callers should prefer _make_providers()
DEFAULT_PROVIDERS = _make_providers()

__all__ = [
    "CrossRefProvider",
    "DOIOrgProvider",
    "OpenAlexProvider",
    "SemanticScholarProvider",
    "PubMedProvider",
    "ArXivAPIProvider",
    "DBLPProvider",
    "ArXivProvider",
    "OpenLibraryProvider",
    "GoogleBooksProvider",
    "UnpaywallProvider",
    "DEFAULT_PROVIDERS",
    "_make_providers",
]
