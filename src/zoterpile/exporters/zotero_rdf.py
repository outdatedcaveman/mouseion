"""
Zotero RDF exporter.

Produces Zotero-compatible RDF/XML for direct import into Zotero via
File → Import.  Uses the Zotero bibliographic ontology (bib:, dc:, dcterms:,
foaf:, link:, z:).

Ref: https://www.zotero.org/support/dev/data_formats
"""

from __future__ import annotations

import re
import html
from typing import List

from ..models import Reference, RefType


_ZOTERO_ITEM_TYPE = {
    RefType.JOURNAL:      "bib:Article",
    RefType.BOOK:         "bib:Book",
    RefType.BOOK_CHAPTER: "bib:BookSection",
    RefType.CONFERENCE:   "bib:ConferencePaper",
    RefType.PREPRINT:     "bib:Document",
    RefType.THESIS:       "bib:Thesis",
    RefType.DATASET:      "bib:Document",
    RefType.REPORT:       "bib:Report",
    RefType.WEBSITE:      "bib:Webpage",
    RefType.OTHER:        "bib:Document",
    RefType.UNKNOWN:      "bib:Document",
}


def _e(s: str) -> str:
    """XML-escape a string."""
    return html.escape(str(s or ""), quote=True)


def _item_uri(ref: Reference) -> str:
    if ref.doi:
        return f"https://doi.org/{ref.doi}"
    if ref.url:
        return ref.url
    return f"urn:zoterpile:{ref.id or id(ref)}"


def _author_elements(authors, role: str = "z:Author") -> str:
    parts = []
    for a in authors:
        given  = _e(a.given or "")
        family = _e(a.family or "")
        parts.append(
            f'    <bib:authors><rdf:Seq><rdf:li>'
            f'<foaf:Person><foaf:surname>{family}</foaf:surname>'
            f'<foaf:firstName>{given}</foaf:firstName></foaf:Person>'
            f'</rdf:li></rdf:Seq></bib:authors>'
        )
    return "\n".join(parts)


def _ref_to_rdf_item(ref: Reference) -> str:
    item_type = _ZOTERO_ITEM_TYPE.get(ref.ref_type, "bib:Document")
    uri       = _item_uri(ref)

    lines = [f'  <{item_type} rdf:about="{_e(uri)}">']

    if ref.title:
        lines.append(f"    <dc:title>{_e(ref.title)}</dc:title>")

    if ref.authors:
        lines.append(_author_elements(ref.authors))

    if ref.year:
        date_str = str(ref.year)
        if ref.month:
            date_str = f"{ref.year}-{ref.month:02d}"
        lines.append(f"    <dc:date>{_e(date_str)}</dc:date>")

    journal = ref.journal or ref.container_title
    if journal:
        lines.append(
            f"    <dcterms:isPartOf><bib:Journal>"
            f"<dc:title>{_e(journal)}</dc:title>"
            f"</bib:Journal></dcterms:isPartOf>"
        )

    if ref.volume:
        lines.append(f"    <prism:volume>{_e(ref.volume)}</prism:volume>")
    if ref.issue:
        lines.append(f"    <prism:number>{_e(ref.issue)}</prism:number>")
    if ref.pages:
        lines.append(f"    <bib:pages>{_e(ref.pages)}</bib:pages>")
    if ref.publisher:
        lines.append(f"    <dc:publisher>{_e(ref.publisher)}</dc:publisher>")
    if ref.abstract:
        lines.append(f"    <dcterms:abstract>{_e(ref.abstract)}</dcterms:abstract>")
    if ref.doi:
        lines.append(f"    <dc:identifier>DOI {_e(ref.doi)}</dc:identifier>")
    if ref.isbn:
        lines.append(f"    <dc:identifier>ISBN {_e(ref.isbn)}</dc:identifier>")
    if ref.url:
        lines.append(f"    <dc:identifier>{_e(ref.url)}</dc:identifier>")
    if ref.language:
        lines.append(f"    <z:language>{_e(ref.language)}</z:language>")
    for kw in (ref.keywords or []):
        lines.append(f"    <dc:subject>{_e(kw)}</dc:subject>")

    lines.append(f"  </{item_type}>")
    return "\n".join(lines)


_RDF_HEADER = """\
<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:z="http://www.zotero.org/namespaces/export#"
  xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:dcterms="http://purl.org/dc/terms/"
  xmlns:bib="http://purl.org/net/biblio#"
  xmlns:foaf="http://xmlns.com/foaf/0.1/"
  xmlns:prism="http://prismstandard.org/namespaces/1.2/basic/"
  xmlns:link="http://purl.org/rss/1.0/modules/link/">
"""

_RDF_FOOTER = "</rdf:RDF>\n"


def to_zotero_rdf_string(refs: List[Reference]) -> str:
    """Convert a list of References to a Zotero-importable RDF string."""
    items = [_ref_to_rdf_item(r) for r in refs]
    return _RDF_HEADER + "\n".join(items) + "\n" + _RDF_FOOTER
