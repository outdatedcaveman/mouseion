"""
Academic database providers.

Each provider can look up references by identifier (DOI/PMID/arXiv ID)
or search by title + optional author.  All calls are async.

Priority order (highest reliability first):
  1. CrossRef        — DOI registry, gold standard for journal articles
  2. OpenAlex        — broad coverage, fully open, very fast
  3. Semantic Scholar — strong in CS/AI, good citation graph data
  4. PubMed          — best for biomedical
  5. DBLP            — best for CS conferences
  6. arXiv           — preprints
"""

from .crossref         import CrossRefProvider
from .semantic_scholar import SemanticScholarProvider
from .openalex         import OpenAlexProvider
from .pubmed           import PubMedProvider
from .dblp             import DBLPProvider
from .arxiv            import ArXivProvider

# Default ordered set used by the lookup engine
DEFAULT_PROVIDERS = [
    CrossRefProvider(),
    OpenAlexProvider(),
    SemanticScholarProvider(),
    PubMedProvider(),
    DBLPProvider(),
    ArXivProvider(),
]

__all__ = [
    "CrossRefProvider",
    "OpenAlexProvider",
    "SemanticScholarProvider",
    "PubMedProvider",
    "DBLPProvider",
    "ArXivProvider",
    "DEFAULT_PROVIDERS",
]
