"""
zoterpile CLI — comprehensive reference lookup, enrichment and sync.

Commands
--------
  add          Parse any input, enrich, auto-tag, and save to database.
  search       Full-text search the local database.
  list         List references with optional filters.
  show         Show full details for a single reference.
  tag          Add/remove tags on a reference.
  export       Export references from the database.
  enrich       Re-enrich (low-completeness) references already in the DB.
  sync         Push references to external tools (Zotero, Notion, Obsidian, …).
  fetch-pdfs   Try to download open-access PDFs.
  stats        Show database and cache statistics.
  init-config  Create a template config file.
  clear-cache  Clear the API response cache.

Examples
--------
  # Add from any input — DOI, URL, title, arXiv ID, file, …
  zoterpile add "10.1038/nature12373"
  zoterpile add "https://arxiv.org/abs/1706.03762"
  zoterpile add "Attention Is All You Need"
  zoterpile add bookmarks.html
  zoterpile add refs.bib --tags "AI,to-read"
  zoterpile add "10.1038/s41586-023-01, 10.1126/science.abc1234"

  # Search
  zoterpile search "transformer attention"
  zoterpile search "" --tag preprint --year-from 2022

  # Export
  zoterpile export --format bibtex -o all_refs.bib
  zoterpile export --tag ML --format ris -o ml_refs.ris

  # Sync to external tools
  zoterpile sync zotero
  zoterpile sync notion
  zoterpile sync obsidian
  zoterpile sync all

  # PDF fetching
  zoterpile fetch-pdfs --limit 100
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import List, Optional

import click
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.panel import Panel
from rich import box

from .exporters import to_bibtex_string, to_ris_string, to_markdown_string, export_bibtex_file, export_ris_file, export_markdown_file
from .models import Reference

console = Console(stderr=True)
out     = Console()

_FORMAT_CHOICES = click.Choice(["bibtex", "ris", "markdown"], case_sensitive=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_output(refs: List[Reference], fmt: str, output: Optional[str]) -> None:
    if fmt == "bibtex":
        content = to_bibtex_string(refs)
    elif fmt == "ris":
        content = to_ris_string(refs)
    else:
        content = to_markdown_string(refs)

    if output:
        Path(output).write_text(content, encoding="utf-8")
        console.print(f"[green]✓[/green] Written to [bold]{output}[/bold]")
    else:
        out.print(content, highlight=False)


def _make_progress() -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    )


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="zoterpile")
def main():
    """
    zoterpile — universal reference enrichment and management.

    Queries CrossRef, OpenAlex, Semantic Scholar, PubMed, DBLP, and arXiv.
    Syncs to Zotero, Notion, Obsidian, and Instapaper.
    """


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

@main.command()
@click.argument("input_data", metavar="INPUT")
@click.option("-t", "--tags",    default="", help="Comma-separated tags to apply")
@click.option("--no-enrich",     is_flag=True, help="Skip enrichment; only parse and save")
@click.option("--concurrency",   default=20,   show_default=True)
@click.option("--auto-tag/--no-auto-tag", default=True, show_default=True,
              help="Automatically assign tags from config rules")
@click.option("--no-db",         is_flag=True, help="Don't save to database")
@click.option("-f", "--format",  "fmt", type=_FORMAT_CHOICES, default=None,
              help="Also output the enriched references in this format")
@click.option("-o", "--output",  default=None, help="Output file (with -f)")
def add(
    input_data: str,
    tags: str,
    no_enrich: bool,
    concurrency: int,
    auto_tag: bool,
    no_db: bool,
    fmt: Optional[str],
    output: Optional[str],
):
    """
    Parse INPUT (any format), enrich, tag, and save to the database.

    INPUT can be: a DOI, URL, arXiv ID, PMID, title, comma/newline-separated
    list of any of the above, a file path (.bib/.ris/.html/.pdf), or a
    Chrome bookmarks HTML export.
    """
    from .input import parse_input
    from .lookup import enrich_batch
    from .tagger import auto_tag as compute_tags, tag_from_keywords
    from .db import RefDatabase

    # --- Parse ---
    console.print(f"[blue]→[/blue] Parsing input…")
    seeds = parse_input(input_data)
    if not seeds:
        console.print("[red]✗ Could not parse any references from input[/red]")
        sys.exit(1)
    console.print(f"[blue]→[/blue] Found {len(seeds)} reference(s)")

    # --- Enrich ---
    if no_enrich:
        enriched = seeds
    else:
        enriched: List[Reference] = [None] * len(seeds)
        with _make_progress() as progress:
            task = progress.add_task("[cyan]Enriching…", total=len(seeds))

            def on_done(idx, total, result):
                enriched[idx - 1] = result
                progress.advance(task)

            asyncio.run(enrich_batch(seeds, concurrency=concurrency, progress_callback=on_done))

    enriched = [r for r in enriched if r is not None]

    # --- Tag ---
    manual_tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags_per_ref: List[List[str]] = []
    for ref in enriched:
        these_tags = list(manual_tags)
        if auto_tag:
            these_tags += compute_tags(ref)
            these_tags += tag_from_keywords(ref)
        # Deduplicate
        seen: set = set()
        deduped = [t for t in these_tags if not (t in seen or seen.add(t))]
        tags_per_ref.append(deduped)

    # --- Save ---
    if not no_db:
        with RefDatabase() as db:
            ids = db.upsert_many(enriched, tags_per_ref=tags_per_ref)
        avg = sum(r.completeness for r in enriched) / len(enriched)
        console.print(
            f"[green]✓[/green] Saved {len(enriched)} reference(s) to database "
            f"(avg completeness: {avg:.0%})"
        )

    # --- Optional output ---
    if fmt:
        _write_output(enriched, fmt, output)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@main.command()
@click.argument("query", default="")
@click.option("--tag",        "tags",     multiple=True, help="Filter by tag (repeatable)")
@click.option("--year-from",  type=int,   default=None)
@click.option("--year-to",    type=int,   default=None)
@click.option("--type",       "ref_type", default=None, help="Reference type (e.g. journal-article)")
@click.option("--oa",         is_flag=True, help="Open-access only")
@click.option("--limit",      default=20,  show_default=True)
@click.option("-f", "--format", "fmt", type=_FORMAT_CHOICES, default=None)
@click.option("-o", "--output", default=None)
def search(query, tags, year_from, year_to, ref_type, oa, limit, fmt, output):
    """Full-text search the local database."""
    from .db import RefDatabase

    with RefDatabase() as db:
        results = db.search(
            query,
            tags=list(tags) or None,
            year_from=year_from,
            year_to=year_to,
            ref_type=ref_type,
            open_access_only=oa,
            limit=limit,
        )

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    if fmt:
        _write_output([r for r, _ in results], fmt, output)
        return

    # Pretty table
    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("Score", style="dim", width=5)
    table.add_column("Year",  style="cyan", width=5)
    table.add_column("Authors", width=20)
    table.add_column("Title", width=50)
    table.add_column("Journal", width=20)
    table.add_column("DOI", style="blue", width=25)
    table.add_column("%", width=5)

    for ref, score in results:
        auth = ref.authors[0].family if ref.authors else "?"
        if len(ref.authors) > 1:
            auth += " et al."
        table.add_row(
            f"{score:.1f}",
            str(ref.year or "?"),
            auth,
            (ref.title or "")[:50],
            (ref.journal or "")[:20],
            (ref.doi or "")[:25],
            f"{ref.completeness:.0%}",
        )

    console.print(table)
    console.print(f"[dim]{len(results)} result(s)[/dim]")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

@main.command("list")
@click.option("--tag",        "tags",     multiple=True)
@click.option("--year-from",  type=int,   default=None)
@click.option("--year-to",    type=int,   default=None)
@click.option("--type",       "ref_type", default=None)
@click.option("--limit",      default=50,  show_default=True)
@click.option("-f", "--format", "fmt", type=_FORMAT_CHOICES, default=None)
@click.option("-o", "--output", default=None)
def list_refs(tags, year_from, year_to, ref_type, limit, fmt, output):
    """List references from the database with optional filters."""
    from .db import RefDatabase

    with RefDatabase() as db:
        refs = db.list_all(
            tags=list(tags) or None,
            year_from=year_from,
            year_to=year_to,
            ref_type=ref_type,
            limit=limit,
        )

    if not refs:
        console.print("[yellow]No references found.[/yellow]")
        return

    if fmt:
        _write_output(refs, fmt, output)
        return

    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("Year",   style="cyan", width=5)
    table.add_column("Authors", width=22)
    table.add_column("Title",  width=55)
    table.add_column("Type",   style="dim", width=16)
    table.add_column("%",      width=5)
    for ref in refs:
        auth = ref.authors[0].family if ref.authors else "?"
        if len(ref.authors) > 1:
            auth += " et al."
        table.add_row(
            str(ref.year or "?"),
            auth,
            (ref.title or "")[:55],
            ref.ref_type.value[:16],
            f"{ref.completeness:.0%}",
        )
    console.print(table)
    console.print(f"[dim]{len(refs)} reference(s)[/dim]")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

@main.command()
@click.argument("ref_id")
@click.option("-f", "--format", "fmt", type=_FORMAT_CHOICES, default="markdown")
def show(ref_id: str, fmt: str):
    """Show full details for a single reference (by ID, DOI, or cite key)."""
    from .db import RefDatabase

    with RefDatabase() as db:
        ref = db.get(ref_id) or db.get_by_doi(ref_id)
        if ref is None:
            # Try by cite key
            results = db.search(ref_id, limit=1)
            if results:
                ref = results[0][0]

    if ref is None:
        console.print(f"[red]Reference not found: {ref_id}[/red]")
        sys.exit(1)

    _write_output([ref], fmt, None)


# ---------------------------------------------------------------------------
# tag
# ---------------------------------------------------------------------------

@main.command()
@click.argument("ref_id")
@click.argument("tags", nargs=-1, required=True)
@click.option("--remove", is_flag=True, help="Remove instead of add")
def tag(ref_id: str, tags: tuple, remove: bool):
    """Add or remove tags on a reference."""
    from .db import RefDatabase

    with RefDatabase() as db:
        if remove:
            for t in tags:
                db.remove_tag(ref_id, t)
            console.print(f"[green]✓[/green] Removed tags: {', '.join(tags)}")
        else:
            db.add_tags(ref_id, list(tags))
            console.print(f"[green]✓[/green] Added tags: {', '.join(tags)}")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@main.command()
@click.option("--tag",        "tags",     multiple=True)
@click.option("--year-from",  type=int,   default=None)
@click.option("--year-to",    type=int,   default=None)
@click.option("--type",       "ref_type", default=None)
@click.option("-f", "--format", "fmt", type=_FORMAT_CHOICES, default="bibtex",
              show_default=True)
@click.option("-o", "--output", required=True, help="Output file path")
@click.option("--limit",        default=10000, show_default=True)
def export(tags, year_from, year_to, ref_type, fmt, output, limit):
    """Export references from the database to a file."""
    from .db import RefDatabase

    with RefDatabase() as db:
        refs = db.list_all(
            tags=list(tags) or None,
            year_from=year_from,
            year_to=year_to,
            ref_type=ref_type,
            limit=limit,
        )

    if not refs:
        console.print("[yellow]No references to export.[/yellow]")
        return

    _write_output(refs, fmt, output)
    console.print(f"[green]✓[/green] Exported {len(refs)} references → [bold]{output}[/bold]")


# ---------------------------------------------------------------------------
# enrich  (re-enrich low-completeness refs)
# ---------------------------------------------------------------------------

@main.command()
@click.option("--threshold", default=0.5, show_default=True,
              help="Re-enrich references below this completeness score")
@click.option("--limit",     default=200, show_default=True)
@click.option("--concurrency", default=20, show_default=True)
def enrich(threshold: float, limit: int, concurrency: int):
    """Re-enrich references in the database that have low completeness."""
    from .db import RefDatabase
    from .lookup import enrich_batch
    from .tagger import auto_tag as compute_tags, tag_from_keywords

    with RefDatabase() as db:
        seeds = db.low_completeness(threshold=threshold, limit=limit)

    if not seeds:
        console.print(f"[green]✓[/green] All references are above {threshold:.0%} completeness!")
        return

    console.print(f"[blue]→[/blue] Re-enriching {len(seeds)} reference(s) below {threshold:.0%}…")

    enriched: List[Reference] = [None] * len(seeds)
    with _make_progress() as progress:
        task = progress.add_task("[cyan]Enriching…", total=len(seeds))

        def on_done(idx, total, result):
            enriched[idx - 1] = result
            progress.advance(task)

        asyncio.run(enrich_batch(seeds, concurrency=concurrency, progress_callback=on_done))

    with RefDatabase() as db:
        tags_per_ref = [
            list(set(compute_tags(r) + tag_from_keywords(r)))
            for r in enriched if r is not None
        ]
        final = [r for r in enriched if r is not None]
        db.upsert_many(final, tags_per_ref=tags_per_ref)

    avg = sum(r.completeness for r in final) / len(final) if final else 0
    console.print(
        f"[green]✓[/green] Re-enriched {len(final)} references — "
        f"avg completeness: {avg:.0%}"
    )


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

_SYNC_TARGETS = click.Choice(
    ["zotero", "notion", "obsidian", "instapaper", "all"],
    case_sensitive=False,
)


@main.command()
@click.argument("target", type=_SYNC_TARGETS)
@click.option("--tag",       "tags",     multiple=True, help="Only sync refs with these tags")
@click.option("--year-from", type=int,   default=None)
@click.option("--limit",     default=500, show_default=True)
@click.option("--instapaper-oa-only", is_flag=True,
              help="Only send open-access papers to Instapaper")
def sync(target: str, tags, year_from, limit, instapaper_oa_only):
    """
    Push references to external tools.

    TARGET: zotero | notion | obsidian | instapaper | all
    """
    from .db import RefDatabase

    with RefDatabase() as db:
        refs = db.list_all(
            tags=list(tags) or None,
            year_from=year_from,
            limit=limit,
        )

    if not refs:
        console.print("[yellow]No references to sync.[/yellow]")
        return

    console.print(f"[blue]→[/blue] Syncing {len(refs)} reference(s) to [bold]{target}[/bold]…")

    targets = ["zotero", "notion", "obsidian", "instapaper"] if target == "all" else [target]

    for t in targets:
        asyncio.run(_sync_one(t, refs, instapaper_oa_only))


async def _sync_one(target: str, refs: List[Reference], instapaper_oa_only: bool) -> None:
    from .db import RefDatabase

    if target == "zotero":
        from .integrations.zotero import ZoteroIntegration
        async with ZoteroIntegration() as z:
            if not await z.is_configured():
                console.print(f"[yellow]⚠ Zotero not configured — skipping[/yellow]")
                return
            keys = await z.push(refs)
            ok = sum(1 for k in keys if k)
            console.print(f"[green]✓ Zotero:[/green] pushed {ok}/{len(refs)}")
            if ok:
                with RefDatabase() as db:
                    for ref, key in zip(refs, keys):
                        if key:
                            from .db import _ref_id
                            db.update_integration_ids(_ref_id(ref), zotero_item_key=key)

    elif target == "notion":
        from .integrations.notion import NotionIntegration
        async with NotionIntegration() as n:
            if not await n.is_configured():
                console.print(f"[yellow]⚠ Notion not configured — skipping[/yellow]")
                return
            page_ids = await n.push(refs)
            ok = sum(1 for p in page_ids if p)
            console.print(f"[green]✓ Notion:[/green] created {ok}/{len(refs)} pages")
            if ok:
                with RefDatabase() as db:
                    for ref, pid in zip(refs, page_ids):
                        if pid:
                            from .db import _ref_id
                            db.update_integration_ids(_ref_id(ref), notion_page_id=pid)

    elif target == "obsidian":
        from .integrations.obsidian import ObsidianIntegration
        async with ObsidianIntegration() as o:
            if not await o.is_configured():
                console.print(f"[yellow]⚠ Obsidian vault not found — skipping[/yellow]")
                return
            paths = await o.push(refs)
            console.print(f"[green]✓ Obsidian:[/green] wrote {len(paths)} notes")

    elif target == "instapaper":
        from .integrations.instapaper import InstapaperIntegration
        to_send = [r for r in refs if not instapaper_oa_only or r.open_access]
        if not to_send:
            console.print("[yellow]⚠ No refs to send to Instapaper (try without --instapaper-oa-only)[/yellow]")
            return
        async with InstapaperIntegration() as i:
            results = await i.push(to_send)
            ok = sum(1 for r in results if r == "ok")
            console.print(f"[green]✓ Instapaper:[/green] added {ok}/{len(to_send)}")


# ---------------------------------------------------------------------------
# fetch-pdfs
# ---------------------------------------------------------------------------

@main.command("fetch-pdfs")
@click.option("--tag",    "tags",   multiple=True)
@click.option("--limit",           default=50, show_default=True)
@click.option("--oa-only", is_flag=True, default=True, show_default=True,
              help="Only attempt refs marked as open access")
@click.option("--email",           default=None, help="Email for Unpaywall (overrides config)")
def fetch_pdfs(tags, limit, oa_only, email):
    """Try to download open-access PDFs for stored references."""
    from .db import RefDatabase
    from .pdf_fetch import fetch_pdf

    with RefDatabase() as db:
        refs = db.list_all(tags=list(tags) or None, limit=limit)

    if oa_only:
        refs = [r for r in refs if r.open_access]

    if not refs:
        console.print("[yellow]No eligible references.[/yellow]")
        return

    console.print(f"[blue]→[/blue] Fetching PDFs for {len(refs)} reference(s)…")

    async def _run():
        ok = 0
        with RefDatabase() as db:
            for ref in refs:
                path = await fetch_pdf(ref, email=email)
                if path:
                    from .db import _ref_id
                    db.update_integration_ids(_ref_id(ref), pdf_local=str(path))
                    ok += 1
        return ok

    ok = asyncio.run(_run())
    console.print(f"[green]✓[/green] Downloaded {ok}/{len(refs)} PDFs")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@main.command()
def stats():
    """Show database and cache statistics."""
    from .db import RefDatabase
    from .cache import get_default_cache

    with RefDatabase() as db:
        s = db.stats()
        tag_list = db.all_tags()

    table = Table(title="Database", box=box.SIMPLE)
    table.add_column("Metric", style="cyan")
    table.add_column("Value",  style="green")
    table.add_row("Total references",    str(s["total"]))
    table.add_row("Avg completeness",    f"{s['avg_completeness']:.0%}")
    table.add_row("Open-access count",   str(s["open_access_count"]))
    for rtype, count in sorted(s["by_type"].items(), key=lambda x: -x[1]):
        table.add_row(f"  {rtype}", str(count))
    console.print(table)

    if tag_list:
        tag_table = Table(title="Top Tags", box=box.SIMPLE)
        tag_table.add_column("Tag",   style="magenta")
        tag_table.add_column("Count", style="green")
        for t in tag_list[:15]:
            tag_table.add_row(t["name"], str(t["count"]))
        console.print(tag_table)

    cache = get_default_cache()
    cs = cache.stats()
    console.print(
        f"\n[dim]Cache:[/dim] {cs['hits']} hits / {cs['misses']} misses "
        f"({cs['hit_rate']:.0%}) — {cs['size_bytes']/1024**2:.1f} MB"
    )


# ---------------------------------------------------------------------------
# init-config
# ---------------------------------------------------------------------------

@main.command("init-config")
@click.option("--force", is_flag=True, help="Overwrite existing config")
def init_config(force: bool):
    """Create a template configuration file."""
    from .config import _CONFIG_PATH, save_config, get_config

    if _CONFIG_PATH.exists() and not force:
        console.print(
            f"[yellow]Config already exists at {_CONFIG_PATH}[/yellow]\n"
            "Use --force to overwrite."
        )
        return

    save_config(get_config())
    console.print(f"[green]✓[/green] Config template written to [bold]{_CONFIG_PATH}[/bold]")
    console.print("\nEdit the file to add API keys for providers and integrations.")


# ---------------------------------------------------------------------------
# clear-cache
# ---------------------------------------------------------------------------

@main.command("clear-cache")
@click.confirmation_option(prompt="Delete all cached API responses?")
def clear_cache():
    """Clear the local API response cache."""
    from .cache import get_default_cache
    get_default_cache().clear()
    console.print("[green]✓[/green] Cache cleared.")


# ---------------------------------------------------------------------------
# gui
# ---------------------------------------------------------------------------

@main.command("gui")
def gui():
    """Launch the interactive terminal UI (TUI)."""
    try:
        from .tui import run
    except ImportError:
        console.print(
            "[red]Error:[/red] Textual is required for the GUI.\n"
            "Install it with:  pip install textual"
        )
        raise SystemExit(1)
    run()


# ---------------------------------------------------------------------------
# web
# ---------------------------------------------------------------------------

@main.command("web")
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=7274,        show_default=True, help="Bind port")
def web(host: str, port: int):
    """Launch the browser-based web UI."""
    try:
        from .web import run as web_run
    except ImportError as e:
        console.print(
            f"[red]Error:[/red] Flask is required for the web UI.\n"
            f"Install it with:  pip install flask\n({e})"
        )
        raise SystemExit(1)
    web_run(host=host, port=port)


if __name__ == "__main__":
    main()
