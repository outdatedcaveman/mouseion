"""
Core data model for a bibliographic reference.

Every field carries its own provenance (which provider supplied it) so the
merge engine can make per-field priority decisions.  The Reference dataclass
is intentionally plain (no ORM, no magic) to keep it fast and serialisable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Reference type vocabulary
# Aligned with CrossRef's type labels so conversion is trivial.
# ---------------------------------------------------------------------------

class RefType(str, Enum):
    JOURNAL        = "journal-article"
    BOOK           = "book"
    BOOK_CHAPTER   = "book-chapter"
    CONFERENCE     = "conference-paper"
    PREPRINT       = "preprint"
    THESIS         = "thesis"
    DATASET        = "dataset"
    REPORT         = "report"
    WEBSITE        = "website"
    OTHER          = "other"
    UNKNOWN        = "unknown"

    @classmethod
    def from_crossref(cls, ctype: str) -> "RefType":
        mapping = {
            "journal-article":         cls.JOURNAL,
            "book":                    cls.BOOK,
            "book-chapter":            cls.BOOK_CHAPTER,
            "proceedings-article":     cls.CONFERENCE,
            "posted-content":          cls.PREPRINT,  # arXiv etc.
            "dissertation":            cls.THESIS,
            "dataset":                 cls.DATASET,
            "report":                  cls.REPORT,
            "reference-entry":         cls.OTHER,
            "monograph":               cls.BOOK,
        }
        return mapping.get(ctype, cls.UNKNOWN)

    @classmethod
    def from_ris_type(cls, ty: str) -> "RefType":
        mapping = {
            "JOUR": cls.JOURNAL,
            "BOOK": cls.BOOK,
            "CHAP": cls.BOOK_CHAPTER,
            "CONF": cls.CONFERENCE,
            "THES": cls.THESIS,
            "RPRT": cls.REPORT,
            "ELEC": cls.WEBSITE,
            "GEN":  cls.OTHER,
            "ABST": cls.JOURNAL,
            "UNPB": cls.PREPRINT,
        }
        return mapping.get(ty.upper(), cls.UNKNOWN)

    def to_ris_type(self) -> str:
        mapping = {
            RefType.JOURNAL:      "JOUR",
            RefType.BOOK:         "BOOK",
            RefType.BOOK_CHAPTER: "CHAP",
            RefType.CONFERENCE:   "CONF",
            RefType.PREPRINT:     "UNPB",
            RefType.THESIS:       "THES",
            RefType.DATASET:      "DATA",
            RefType.REPORT:       "RPRT",
            RefType.WEBSITE:      "ELEC",
            RefType.OTHER:        "GEN",
            RefType.UNKNOWN:      "GEN",
        }
        return mapping.get(self, "GEN")

    def to_bibtex_type(self) -> str:
        mapping = {
            RefType.JOURNAL:      "article",
            RefType.BOOK:         "book",
            RefType.BOOK_CHAPTER: "incollection",
            RefType.CONFERENCE:   "inproceedings",
            RefType.PREPRINT:     "misc",
            RefType.THESIS:       "phdthesis",
            RefType.DATASET:      "misc",
            RefType.REPORT:       "techreport",
            RefType.WEBSITE:      "misc",
            RefType.OTHER:        "misc",
            RefType.UNKNOWN:      "misc",
        }
        return mapping.get(self, "misc")


# ---------------------------------------------------------------------------
# Author
# ---------------------------------------------------------------------------

@dataclass
class Author:
    family: str = ""
    given: str = ""
    orcid: Optional[str] = None
    affiliation: Optional[str] = None

    @property
    def full_name(self) -> str:
        parts = [p for p in (self.given, self.family) if p]
        return " ".join(parts)

    def to_bibtex_str(self) -> str:
        """Return 'Family, Given' for BibTeX author field."""
        if self.family and self.given:
            return f"{self.family}, {self.given}"
        return self.full_name

    @classmethod
    def from_bibtex_str(cls, s: str) -> "Author":
        """Parse 'Family, Given' or 'First Last' strings."""
        s = s.strip()
        if not s:
            return cls()
        if ", " in s:
            parts = s.split(", ", 1)
            return cls(family=parts[0].strip(), given=parts[1].strip())
        # Try 'First Last' — treat last token as family name
        tokens = s.rsplit(" ", 1)
        if len(tokens) == 2:
            return cls(family=tokens[1].strip(), given=tokens[0].strip())
        return cls(family=s)

    @classmethod
    def from_crossref(cls, data: dict) -> "Author":
        orcid_raw = data.get("ORCID", "")
        orcid = re.sub(r"^https?://orcid\.org/", "", orcid_raw) or None
        aff_list = data.get("affiliation", [])
        aff = aff_list[0].get("name") if aff_list else None
        return cls(
            family=data.get("family", ""),
            given=data.get("given", ""),
            orcid=orcid,
            affiliation=aff,
        )

    @classmethod
    def from_openalex(cls, data: dict) -> "Author":
        raw = data.get("author", {})
        name = raw.get("display_name", "")
        orcid_raw = raw.get("orcid", "") or ""
        orcid = re.sub(r"^https?://orcid\.org/", "", orcid_raw) or None
        inst_list = data.get("institutions", [])
        aff = inst_list[0].get("display_name") if inst_list else None
        # Split display_name into given/family best-effort
        parts = name.rsplit(" ", 1)
        if len(parts) == 2:
            return cls(family=parts[1], given=parts[0], orcid=orcid, affiliation=aff)
        return cls(family=name, orcid=orcid, affiliation=aff)


# ---------------------------------------------------------------------------
# FieldInfo — provenance tracking for a single field value
# ---------------------------------------------------------------------------

@dataclass
class FieldInfo:
    """Wraps a field value with its source and a confidence score."""
    value: Any
    source: str           # provider name, e.g. "crossref", "openalex"
    confidence: float = 1.0   # 0.0–1.0


# ---------------------------------------------------------------------------
# Reference — the central data model
# ---------------------------------------------------------------------------

@dataclass
class Reference:
    """
    A complete (or partial) bibliographic reference.

    All fields are Optional so we can represent incomplete records.
    The `sources` dict tracks which providers contributed data, keyed
    by provider name with a per-provider overall confidence score.

    Field naming follows BibTeX / CrossRef conventions where possible.
    """

    # --- Identifiers ---
    doi:      Optional[str] = None   # normalised: no https://doi.org/ prefix
    pmid:     Optional[str] = None
    pmcid:    Optional[str] = None
    arxiv_id: Optional[str] = None   # e.g. "1706.03762"
    isbn:     Optional[str] = None
    issn:     Optional[str] = None   # primary ISSN
    eissn:    Optional[str] = None   # electronic ISSN
    url:      Optional[str] = None   # canonical landing page

    # --- Core metadata ---
    title:    Optional[str] = None
    authors:  List[Author]  = field(default_factory=list)
    year:     Optional[int] = None
    month:    Optional[int] = None
    abstract: Optional[str] = None
    ref_type: RefType       = RefType.UNKNOWN

    # --- Journal / proceedings ---
    journal:        Optional[str] = None   # full journal name
    journal_abbrev: Optional[str] = None   # ISO abbreviated form
    container_title: Optional[str] = None  # generic container (journal or book title)
    volume:  Optional[str] = None
    issue:   Optional[str] = None
    pages:   Optional[str] = None          # "100-110" or "100–110"
    article_number: Optional[str] = None   # e1234567
    event_name: Optional[str] = None       # conference name

    # --- Book / publisher ---
    publisher: Optional[str] = None
    place:     Optional[str] = None        # city/country of publication
    edition:   Optional[str] = None
    editors:   List[Author] = field(default_factory=list)
    series:    Optional[str] = None
    num_pages: Optional[int] = None

    # --- Extras ---
    keywords:    List[str]     = field(default_factory=list)
    language:    Optional[str] = None
    open_access: Optional[bool] = None
    oa_url:      Optional[str] = None      # best open-access PDF URL
    license:     Optional[str] = None
    citation_count: Optional[int] = None

    # --- Provenance ---
    # Maps provider name → overall confidence score for this provider's data
    sources: Dict[str, float] = field(default_factory=dict)
    # Cite key (used in BibTeX export; auto-generated if absent)
    cite_key: Optional[str] = None

    # -----------------------------------------------------------------------
    # Convenience / computed properties
    # -----------------------------------------------------------------------

    @property
    def completeness(self) -> float:
        """
        Weighted completeness score 0.0–1.0.
        Useful for ranking results and highlighting gaps.
        Weights are adjusted per ref_type where appropriate.
        """
        is_book = self.ref_type in (RefType.BOOK, RefType.BOOK_CHAPTER)
        is_preprint = self.ref_type == RefType.PREPRINT

        # Primary identifier: DOI (or arXiv/ISBN/PMID as equivalent)
        has_id = bool(self.doi or self.arxiv_id or self.pmid or self.isbn)

        checks: Dict[str, tuple[bool, float]] = {
            "title":     (bool(self.title), 0.22),
            "authors":   (bool(self.authors), 0.15),
            "year":      (bool(self.year), 0.10),
            "identifier": (has_id, 0.15),
            # Journal/venue: not expected for books or preprints
            "venue":     (bool(self.journal or self.container_title or self.publisher),
                          0.08),
            "abstract":  (bool(self.abstract), 0.12),
            # Volume/issue/pages: less expected for books and preprints
            "volume":    (bool(self.volume),
                          0.03 if is_book or is_preprint else 0.05),
            "issue":     (bool(self.issue),
                          0.02 if is_book or is_preprint else 0.04),
            "pages":     (bool(self.pages or self.article_number),
                          0.04 if is_book or is_preprint else 0.05),
            # Citation count as bonus (indicates the ref is well-indexed)
            "cited":     (self.citation_count is not None and self.citation_count > 0,
                          0.04),
        }
        return min(1.0, sum(w for (present, w) in checks.values() if present))

    def has_identifier(self) -> bool:
        return bool(self.doi or self.pmid or self.arxiv_id or self.isbn)

    # -----------------------------------------------------------------------
    # Normalisation
    # -----------------------------------------------------------------------

    def normalize(self) -> None:
        """Clean up fields in-place (DOI format, whitespace, etc.)."""
        if self.doi:
            self.doi = _normalize_doi(self.doi)
        if self.title:
            self.title = " ".join(self.title.split())  # collapse whitespace
        if self.abstract:
            # Strip common junk prefixes added by some publishers
            self.abstract = re.sub(
                r"^(Abstract|ABSTRACT|Summary|SUMMARY)\s*[:\-]?\s*", "",
                self.abstract,
            ).strip()
        if self.arxiv_id:
            # Normalise to bare ID: "2310.01234" (no "arXiv:" prefix)
            self.arxiv_id = re.sub(r"^(?:arXiv:|arxiv:)", "", self.arxiv_id).strip()

    def auto_cite_key(self) -> str:
        """Generate a BibTeX-style cite key if none is set."""
        if self.cite_key:
            return self.cite_key
        family = ""
        if self.authors:
            family = re.sub(r"\W", "", self.authors[0].family).lower()
        yr = str(self.year) if self.year else "0000"
        word = ""
        if self.title:
            stop = {"a", "an", "the", "of", "in", "on", "for", "and", "with"}
            words = [
                w.lower() for w in re.split(r"\W+", self.title)
                if w and w.lower() not in stop
            ]
            word = words[0] if words else ""
        # Only include year if there's at least one other meaningful component
        if family or word:
            key = f"{family}{yr}{word}"
        else:
            key = ""
        return key or "ref"

    def __repr__(self) -> str:
        auth = self.authors[0].family if self.authors else "?"
        return f"<Reference {auth!r} {self.year} {self.title!r:.40}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_doi(doi: str) -> str:
    """Strip URL prefix and normalise to bare DOI string."""
    doi = doi.strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    return doi
