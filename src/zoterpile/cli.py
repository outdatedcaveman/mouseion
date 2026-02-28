"""
Command-line interface for zoterpile.

Commands
--------
  enrich   Enrich a RIS/BibTeX/HTML file with data from all providers.
  lookup   Look up a single reference by DOI, title, URL, or arXiv ID.
  stats    Show cache statistics.
  clear-cache  Clear the local API response cache.

Examples
--------
  # Enrich a BibTeX file and write cleaned BibTeX
  zoterpile enrich references.bib -o enriched.bib

  # Enrich a RIS file and export as Markdown for review
  zoterpile enrich input.ris --format markdown -o review.md

  # Look up a DOI and print as BibTeX
  zoterpile lookup --doi 10.1038/nature12373

  # Look up by title (fuzzy search across all providers)
  zoterpile lookup --title "Attention Is All You Need" --format ris

  # Look up from a URL (scrape + enrich)
  zoterpile lookup --url https://arxiv.org/abs/1706.03762 --format bibtex
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .exporters import (
    export_bibtex_file,
    export_markdown_file,
    export_ris_file,
    to_bibtex_string,
    to_markdown_string,
    to_ris_string,
)
from .lookup import enrich_batch, enrich_one
from .models import Reference
from .parsers import (
    parse_bibtex_file,
    parse_bibtex_string,
    parse_ris_file,
    parse_ris_string,
    parse_url,
)


console = Console(stderr=True)   # status output goes to stderr
out     = Console()              # data output goes to stdout


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

_FORMAT_CHOICES = click.Choice(["bibtex", "ris", "markdown"], case_sensitive=False)
_OUTPUT_HELP = "Output file path (default: stdout)"
_FORMAT_HELP = "Output format [bibtex|ris|markdown] (default: same as input)"


def _detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".bib", ".bibtex"):
        return "bibtex"
    if suffix == ".ris":
        return "ris"
    if suffix in (".html", ".htm"):
        return "html"
    return "bibtex"


def _format_output(refs: list, fmt: str) -> str:
    if fmt == "bibtex":
        return to_bibtex_string(refs)
    if fmt == "ris":
        return to_ris_string(refs)
    return to_markdown_string(refs)


def _write_output(refs: list, fmt: str, output: Optional[str]) -> None:
    content = _format_output(refs, fmt)
    if output:
        Path(output).write_text(content, encoding="utf-8")
        console.print(f"[green]✓[/green] Written to [bold]{output}[/bold]")
    else:
        out.print(content, highlight=False)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="zoterpile")
def main():
    """
    zoterpile — comprehensive reference lookup and enrichment.

    Queries CrossRef, OpenAlex, Semantic Scholar, PubMed, DBLP, and arXiv
    to complete and validate bibliographic references.
    """


# ---------------------------------------------------------------------------
# enrich command
# ---------------------------------------------------------------------------

@main.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--output", default=None, help=_OUTPUT_HELP)
@click.option("-f", "--format", "fmt", type=_FORMAT_CHOICES, default=None, help=_FORMAT_HELP)
@click.option("--concurrency", default=20, show_default=True,
              help="Max references to enrich simultaneously")
@click.option("--no-cache", is_flag=True, default=False,
              help="Bypass the local API response cache")
@click.option("--providers", default=None,
              help="Comma-separated provider names to use (default: all)")
@click.option("--min-score", default=0.0, type=float,
              help="Only output references with completeness ≥ this score (0.0-1.0)")
def enrich(
    input_file: Path,
    output: Optional[str],
    fmt: Optional[str],
    concurrency: int,
    no_cache: bool,
    providers: Optional[str],
    min_score: float,
):
    """
    Enrich all references in INPUT_FILE.

    Supports .bib, .ris, and .html input files.
    Queries all configured providers to fill in missing fields.
    """
    # --- Parse input ---
    detected_fmt = _detect_format(input_file)
    console.print(f"[blue]→[/blue] Parsing [bold]{input_file}[/bold] ({detected_fmt})")

    try:
        if detected_fmt == "bibtex":
            refs = parse_bibtex_file(input_file)
        elif detected_fmt == "ris":
            refs = parse_ris_file(input_file)
        elif detected_fmt == "html":
            # Parse HTML as a single reference
            html = input_file.read_text(encoding="utf-8", errors="replace")
            from .parsers.html import parse_html_string
            refs = [parse_html_string(html, source_url=input_file.as_uri())]
        else:
            console.print(f"[red]✗ Unsupported file type: {input_file.suffix}[/red]")
            sys.exit(1)
    except Exception as e:
        console.print(f"[red]✗ Parse error: {e}[/red]")
        sys.exit(1)

    console.print(f"[blue]→[/blue] Loaded {len(refs)} reference(s)")

    # --- Select providers ---
    selected_providers = None
    if providers:
        from .providers import (
            ArXivProvider, CrossRefProvider, DBLPProvider,
            OpenAlexProvider, PubMedProvider, SemanticScholarProvider,
        )
        provider_map = {
            "crossref": CrossRefProvider(),
            "openalex": OpenAlexProvider(),
            "semantic_scholar": SemanticScholarProvider(),
            "pubmed": PubMedProvider(),
            "dblp": DBLPProvider(),
            "arxiv": ArXivProvider(),
        }
        names = [p.strip().lower() for p in providers.split(",")]
        selected_providers = [provider_map[n] for n in names if n in provider_map]
        if not selected_providers:
            console.print("[red]✗ No valid providers selected[/red]")
            sys.exit(1)

    # --- Enrich with progress bar ---
    enriched: list[Reference] = [None] * len(refs)  # type: ignore

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("[cyan]Enriching…", total=len(refs))

        def on_done(idx: int, total: int, result: Reference):
            enriched[idx - 1] = result
            progress.advance(task)

        asyncio.run(
            enrich_batch(
                refs,
                providers=selected_providers,
                concurrency=concurrency,
                progress_callback=on_done,
            )
        )

    # Filter by min score
    final = [r for r in enriched if r is not None and r.completeness >= min_score]
    filtered = len(enriched) - len(final)

    # --- Report ---
    avg_score = sum(r.completeness for r in final) / len(final) if final else 0
    console.print(
        f"[green]✓[/green] Enriched {len(final)} reference(s)"
        + (f" (filtered {filtered} below {min_score:.0%})" if filtered else "")
        + f" — avg completeness: {avg_score:.0%}"
    )

    # --- Output ---
    out_fmt = fmt or detected_fmt
    if out_fmt == "html":
        out_fmt = "bibtex"
    _write_output(final, out_fmt, output)


# ---------------------------------------------------------------------------
# lookup command
# ---------------------------------------------------------------------------

@main.command()
@click.option("--doi",    default=None, help="Look up by DOI")
@click.option("--title",  default=None, help="Search by title")
@click.option("--url",    default=None, help="Scrape a URL and look up")
@click.option("--arxiv",  default=None, help="Look up by arXiv ID (e.g. 1706.03762)")
@click.option("--pmid",   default=None, help="Look up by PubMed ID")
@click.option("-f", "--format", "fmt", type=_FORMAT_CHOICES, default="bibtex", help=_FORMAT_HELP)
@click.option("-o", "--output", default=None, help=_OUTPUT_HELP)
@click.option("--author", default=None, help="Author name hint (for title search)")
@click.option("--year",   default=None, type=int, help="Publication year hint (for title search)")
def lookup(
    doi: Optional[str],
    title: Optional[str],
    url: Optional[str],
    arxiv: Optional[str],
    pmid: Optional[str],
    fmt: str,
    output: Optional[str],
    author: Optional[str],
    year: Optional[int],
):
    """
    Look up a single reference and print the result.

    Provide at least one of: --doi, --title, --url, --arxiv, --pmid.
    """
    if not any([doi, title, url, arxiv, pmid]):
        console.print("[red]✗ Provide at least one of: --doi, --title, --url, --arxiv, --pmid[/red]")
        sys.exit(1)

    ref = Reference()

    async def _run():
        nonlocal ref

        if url:
            console.print(f"[blue]→[/blue] Fetching {url}")
            ref = await parse_url(url)
        if doi:
            ref.doi = doi.strip()
        if arxiv:
            ref.arxiv_id = arxiv.strip()
        if pmid:
            ref.pmid = pmid.strip()
        if title:
            ref.title = title.strip()
        if author:
            from .models import Author
            ref.authors = [Author.from_bibtex_str(author)]
        if year:
            ref.year = year

        console.print("[blue]→[/blue] Querying providers…")
        enriched = await enrich_one(ref)
        return enriched

    result = asyncio.run(_run())

    if not result.title and not result.doi:
        console.print("[yellow]⚠ No matching reference found[/yellow]")
        sys.exit(1)

    console.print(
        f"[green]✓[/green] Found: [bold]{result.title or '(no title)'}[/bold] "
        f"({result.year or '?'}) — completeness: {result.completeness:.0%}"
    )

    _write_output([result], fmt, output)


# ---------------------------------------------------------------------------
# stats command
# ---------------------------------------------------------------------------

@main.command()
def stats():
    """Show cache statistics."""
    from .cache import get_default_cache
    cache = get_default_cache()
    s = cache.stats()

    table = Table(title="Cache Statistics")
    table.add_column("Metric",  style="cyan")
    table.add_column("Value",   style="green")
    table.add_row("Hits",       str(s["hits"]))
    table.add_row("Misses",     str(s["misses"]))
    table.add_row("Hit rate",   f"{s['hit_rate']:.1%}")
    table.add_row("Cache size", f"{s['size_bytes'] / 1024**2:.1f} MB")

    console.print(table)


# ---------------------------------------------------------------------------
# clear-cache command
# ---------------------------------------------------------------------------

@main.command("clear-cache")
@click.confirmation_option(prompt="This will delete all cached API responses. Continue?")
def clear_cache():
    """Clear the local API response cache."""
    from .cache import get_default_cache
    get_default_cache().clear()
    console.print("[green]✓[/green] Cache cleared.")


if __name__ == "__main__":
    main()
