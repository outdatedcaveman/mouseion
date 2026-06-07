"""Input format parsers — RIS, BibTeX, HTML/URL, bookmarks, PDF."""

from .ris       import parse_ris_file, parse_ris_string
from .bibtex    import parse_bibtex_file, parse_bibtex_string
from .html      import parse_html_string, parse_url
from .bookmarks import parse_bookmarks_file, parse_bookmarks_string
from .pdf       import parse_pdf_file

__all__ = [
    "parse_ris_file",
    "parse_ris_string",
    "parse_bibtex_file",
    "parse_bibtex_string",
    "parse_html_string",
    "parse_url",
    "parse_bookmarks_file",
    "parse_bookmarks_string",
    "parse_pdf_file",
]
