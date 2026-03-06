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

        # --- Semantic index (best-effort) ---
        from .config import get_config as _get_cfg
        if _get_cfg().semantic_auto_index:
            try:
                from .semantic import get_default_index
                idx = get_default_index()
                if idx.is_available():
                    idx.index_many(list(zip(ids, enriched)))
            except Exception:
                pass

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
            oa_only=oa,
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
        ids = db.upsert_many(final, tags_per_ref=tags_per_ref)

    avg = sum(r.completeness for r in final) / len(final) if final else 0
    console.print(
        f"[green]✓[/green] Re-enriched {len(final)} references — "
        f"avg completeness: {avg:.0%}"
    )

    # --- Semantic index (best-effort) ---
    from .config import get_config as _get_cfg
    if _get_cfg().semantic_auto_index and final:
        try:
            from .semantic import get_default_index
            idx = get_default_index()
            if idx.is_available():
                idx.index_many(list(zip(ids, final)))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------

_SYNC_TARGETS = click.Choice(
    ["zotero", "notion", "obsidian", "instapaper", "all"],
    case_sensitive=False,
)


@main.command()
@click.argument("target", type=_SYNC_TARGETS)
@click.option("--tag",        "tags",      multiple=True, help="Only sync refs with these tags")
@click.option("--year-from",  type=int,    default=None)
@click.option("--batch-size", type=int,    default=500, show_default=True,
              help="Number of refs per sync batch (set lower to reduce memory use)")
@click.option("--instapaper-oa-only", is_flag=True,
              help="Only send open-access papers to Instapaper")
def sync(target: str, tags, year_from, batch_size, instapaper_oa_only):
    """
    Push references to external tools.

    TARGET: zotero | notion | obsidian | instapaper | all

    Streams the database in pages of --batch-size so the full library can be
    synced regardless of size (hundreds of thousands of entries).
    """
    from .db import RefDatabase

    db_tags     = list(tags) or None
    targets_list = ["zotero", "notion", "obsidian", "instapaper"] if target == "all" else [target]

    # Count first for progress display
    with RefDatabase() as db:
        total = db.count()

    if total == 0:
        console.print("[yellow]No references to sync.[/yellow]")
        return

    console.print(
        f"[blue]→[/blue] Syncing up to {total} reference(s) to "
        f"[bold]{target}[/bold] in pages of {batch_size}…"
    )

    # Open connections to all integration targets once; stream pages through them.
    asyncio.run(_sync_all_paginated(
        targets_list,
        db_tags=db_tags,
        year_from=year_from,
        batch_size=batch_size,
        instapaper_oa_only=instapaper_oa_only,
    ))


async def _sync_all_paginated(
    targets: List[str],
    db_tags: Optional[List[str]],
    year_from: Optional[int],
    batch_size: int,
    instapaper_oa_only: bool,
) -> None:
    """Open all integration connections, stream DB pages, push each page."""
    from .db import RefDatabase

    # Pre-check configuration so we don't start streaming if nothing is set up.
    integrations = {}
    for t in targets:
        integration = _make_integration(t)
        if integration is not None:
            integrations[t] = integration

    if not integrations:
        console.print("[yellow]No integrations configured.[/yellow]")
        return

    # Connect all integrations
    for t, intg in integrations.items():
        await intg.connect()

    # Validate configuration before streaming
    unconfigured = []
    for t, intg in list(integrations.items()):
        if not await intg.is_configured():
            console.print(f"[yellow]⚠ {t} not configured — skipping[/yellow]")
            unconfigured.append(t)
    for t in unconfigured:
        del integrations[t]

    if not integrations:
        return

    # Counters per target
    ok_counts: Dict[str, int] = {t: 0 for t in integrations}
    total_counts: Dict[str, int] = {t: 0 for t in integrations}

    try:
        with RefDatabase() as db:
            for chunk in db.iter_all(
                chunk_size=batch_size,
                tags=db_tags,
                year_from=year_from,
            ):
                for t, intg in integrations.items():
                    ok, pushed = await _push_chunk(t, intg, chunk, instapaper_oa_only, db)
                    ok_counts[t]    += ok
                    total_counts[t] += pushed
    finally:
        for intg in integrations.values():
            await intg.disconnect()

    for t in integrations:
        console.print(
            f"[green]✓ {t}:[/green] {ok_counts[t]}/{total_counts[t]} pushed"
        )


def _make_integration(target: str):
    """Return the integration object for a target, or None if unknown."""
    if target == "zotero":
        from .integrations.zotero import ZoteroIntegration
        return ZoteroIntegration()
    elif target == "notion":
        from .integrations.notion import NotionIntegration
        return NotionIntegration()
    elif target == "obsidian":
        from .integrations.obsidian import ObsidianIntegration
        return ObsidianIntegration()
    elif target == "instapaper":
        from .integrations.instapaper import InstapaperIntegration
        return InstapaperIntegration()
    return None


async def _push_chunk(
    target: str,
    intg,
    chunk: List[Reference],
    instapaper_oa_only: bool,
    db,
) -> tuple:
    """Push one chunk to one integration. Returns (ok_count, attempted_count)."""
    from .db import _ref_id

    if target == "instapaper":
        chunk = [r for r in chunk if not instapaper_oa_only or r.open_access]
        if not chunk:
            return 0, 0
        try:
            ext_ids = await intg.push(chunk)
        except Exception as exc:
            console.print(f"[red]  {target} error: {exc}[/red]")
            return 0, len(chunk)
        return sum(1 for eid in ext_ids if eid), len(chunk)

    # For Zotero and Notion: load existing integration IDs in one query so we
    # can PATCH existing items instead of creating duplicates on repeated sync.
    if target in ("zotero", "notion"):
        rids = [_ref_id(r) for r in chunk]
        extras = db.get_extras_bulk(rids)
        pairs = []
        for ref, rid in zip(chunk, rids):
            extra = extras.get(rid, {})
            existing_key = (
                extra.get("zotero_item_key") if target == "zotero"
                else extra.get("notion_page_id")
            ) or None
            pairs.append((existing_key, ref))

        try:
            ext_ids = await intg.push_or_update(pairs)
        except Exception as exc:
            console.print(f"[red]  {target} error: {exc}[/red]")
            return 0, len(chunk)

        ok = sum(1 for eid in ext_ids if eid)
        # Persist only newly-created IDs (updates keep the same ID).
        for ref, eid, (existing_key, _) in zip(chunk, ext_ids, pairs):
            if not eid or existing_key:
                continue
            rid = _ref_id(ref)
            if target == "zotero":
                db.update_integration_ids(rid, zotero_item_key=eid)
            elif target == "notion":
                db.update_integration_ids(rid, notion_page_id=eid)
        return ok, len(chunk)

    # Obsidian and other integrations: no dedup needed (idempotent by file path).
    try:
        ext_ids = await intg.push(chunk)
    except Exception as exc:
        console.print(f"[red]  {target} error: {exc}[/red]")
        return 0, len(chunk)
    return sum(1 for eid in ext_ids if eid), len(chunk)


# ---------------------------------------------------------------------------
# pull-sync  (pull FROM external tools INTO local DB)
# ---------------------------------------------------------------------------

_PULL_SOURCES = click.Choice(["zotero", "notion"], case_sensitive=False)


@main.command("pull-sync")
@click.argument("source", type=_PULL_SOURCES)
@click.option("--full", is_flag=True,
              help="Ignore the stored library version and re-pull everything.")
@click.option("--enrich/--no-enrich", "do_enrich", default=True, show_default=True,
              help="Re-enrich pulled refs via OpenAlex/CrossRef after import.")
@click.option("--collection", "collection_id", default=None,
              help="Zotero collection key to restrict the pull to.")
@click.option("--dry-run", is_flag=True,
              help="Show what would be pulled without writing to the DB.")
def pull_sync(source, full, do_enrich, collection_id, dry_run):
    """
    Pull references FROM an external tool INTO the local database.

    SOURCE: zotero | notion

    Zotero pulls use incremental sync by default (only items changed since
    the last pull).  Pass --full to re-pull the entire library.

    Zotero example:
      zoterpile pull-sync zotero
      zoterpile pull-sync zotero --full --collection ABC123XY

    Notion example:
      zoterpile pull-sync notion
    """
    if source == "zotero":
        _run_pull_zotero(full=full, do_enrich=do_enrich,
                         collection_id=collection_id, dry_run=dry_run)
    elif source == "notion":
        _run_pull_notion(do_enrich=do_enrich, dry_run=dry_run)


def _run_pull_zotero(*, full, do_enrich, collection_id, dry_run):
    from .integrations.zotero import ZoteroIntegration
    from .db import RefDatabase, _ref_id as _make_id
    from .tagger import auto_tag, tag_from_keywords
    from .config import get_config
    import anyio

    with RefDatabase() as db:
        stored = db.get_setting("zotero_library_version")

    since: Optional[int] = None if full else (int(stored) if stored else None)
    label = "full pull" if since is None else f"incremental pull since version {since}"
    console.print(f"[blue]→[/blue] Zotero {label}…")

    async def _fetch():
        async with ZoteroIntegration() as intg:
            if not await intg.is_configured():
                return None
            return await intg.pull(since=since, collection_id=collection_id)

    result = anyio.run(_fetch)
    if result is None:
        console.print("[red]Zotero not configured — set zotero_api_key and zotero_user_id[/red]")
        return

    refs, keys, new_version = result

    if not refs:
        console.print("[yellow]No new or updated items found.[/yellow]")
        if new_version:
            with RefDatabase() as db:
                db.set_setting("zotero_library_version", str(new_version))
        return

    console.print(f"  Retrieved {len(refs)} item(s) (library version: {new_version}).")

    if dry_run:
        for ref in refs[:20]:
            console.print(f"  [dim]· {(ref.title or '(untitled)')[:72]}[/dim]")
        if len(refs) > 20:
            console.print(f"  [dim]  … and {len(refs) - 20} more[/dim]")
        return

    if do_enrich:
        from .lookup import enrich_batch
        console.print("  Enriching with CrossRef / OpenAlex…")
        refs = anyio.run(lambda: enrich_batch(refs))

    cfg = get_config()
    with RefDatabase() as db:
        tags_per_ref = [
            list(set(auto_tag(r, cfg) + tag_from_keywords(r))) for r in refs
        ]
        ids = db.upsert_many(refs, tags_per_ref=tags_per_ref)
        # Persist Zotero item keys for refs that don't have one yet.
        for ref_id, key in zip(ids, keys):
            if key and not db.get_extra(ref_id).get("zotero_item_key"):
                db.update_integration_ids(ref_id, zotero_item_key=key)
        if new_version:
            db.set_setting("zotero_library_version", str(new_version))

    console.print(f"[green]✓[/green] Imported/updated {len(ids)} reference(s). "
                  f"Library version now: {new_version}")


def _run_pull_notion(*, do_enrich, dry_run):
    from .integrations.notion import NotionIntegration
    from .db import RefDatabase
    from .tagger import auto_tag, tag_from_keywords
    from .config import get_config
    import anyio

    console.print("[blue]→[/blue] Pulling from Notion database…")

    async def _fetch():
        async with NotionIntegration() as intg:
            if not await intg.is_configured():
                return None
            return await intg.pull()

    pairs = anyio.run(_fetch)
    if pairs is None:
        console.print("[red]Notion not configured — set notion_api_key and notion_database_id[/red]")
        return

    if not pairs:
        console.print("[yellow]No pages found in Notion database.[/yellow]")
        return

    page_ids, refs = zip(*pairs)
    refs = list(refs)
    console.print(f"  Retrieved {len(refs)} page(s).")

    if dry_run:
        for ref in refs[:20]:
            console.print(f"  [dim]· {(ref.title or '(untitled)')[:72]}[/dim]")
        if len(refs) > 20:
            console.print(f"  [dim]  … and {len(refs) - 20} more[/dim]")
        return

    if do_enrich:
        from .lookup import enrich_batch
        console.print("  Enriching with CrossRef / OpenAlex…")
        refs = anyio.run(lambda: enrich_batch(refs))

    cfg = get_config()
    with RefDatabase() as db:
        tags_per_ref = [
            list(set(auto_tag(r, cfg) + tag_from_keywords(r))) for r in refs
        ]
        ids = db.upsert_many(refs, tags_per_ref=tags_per_ref)
        for ref_id, pid in zip(ids, page_ids):
            if pid and not db.get_extra(ref_id).get("notion_page_id"):
                db.update_integration_ids(ref_id, notion_page_id=pid)

    console.print(f"[green]✓[/green] Imported/updated {len(ids)} reference(s) from Notion.")


# ---------------------------------------------------------------------------
# stream  (long-running real-time sync via Zotero streaming API)
# ---------------------------------------------------------------------------

@main.command("stream")
@click.argument("source", type=click.Choice(["zotero"], case_sensitive=False))
@click.option("--enrich/--no-enrich", "do_enrich", default=False, show_default=True,
              help="Re-enrich each incoming ref (slower, more API calls).")
def stream_cmd(source, do_enrich):
    """
    Connect to a streaming API and sync changes in real-time.

    SOURCE: zotero

    Maintains a persistent websocket connection to the Zotero streaming API
    (wss://stream.zotero.org) and pulls changed items automatically whenever
    the library is modified.  Press Ctrl+C to stop.

    Requires:  pip install websockets
    """
    from .integrations.zotero import ZoteroIntegration
    from .db import RefDatabase
    from .tagger import auto_tag, tag_from_keywords
    from .config import get_config

    console.print("[blue]→[/blue] Connecting to Zotero streaming API…  (Ctrl+C to stop)")

    async def _run():
        async with ZoteroIntegration() as intg:
            if not await intg.is_configured():
                console.print("[red]Zotero not configured[/red]")
                return

            async for _topic, version in intg.stream_changes():
                with RefDatabase() as db:
                    stored = db.get_setting("zotero_library_version")
                since = int(stored) if stored else None
                console.print(
                    f"  [yellow]⚡[/yellow] Library changed → version {version}. "
                    f"Pulling since {since}…"
                )

                refs, keys, new_version = await intg.pull(since=since)
                if not refs:
                    with RefDatabase() as db:
                        db.set_setting("zotero_library_version", str(new_version))
                    continue

                if do_enrich:
                    from .lookup import enrich_batch
                    refs = await enrich_batch(refs)

                cfg = get_config()
                with RefDatabase() as db:
                    tags_per_ref = [
                        list(set(auto_tag(r, cfg) + tag_from_keywords(r))) for r in refs
                    ]
                    ids = db.upsert_many(refs, tags_per_ref=tags_per_ref)
                    for ref_id, key in zip(ids, keys):
                        if key and not db.get_extra(ref_id).get("zotero_item_key"):
                            db.update_integration_ids(ref_id, zotero_item_key=key)
                    db.set_setting("zotero_library_version", str(new_version))

                console.print(
                    f"  [green]✓[/green] Synced {len(refs)} item(s). "
                    f"Version: {new_version}"
                )

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]Streaming stopped.[/dim]")


# ---------------------------------------------------------------------------
# fetch-pdfs
# ---------------------------------------------------------------------------

@main.command("fetch-pdfs")
@click.option("--tag",    "tags",   multiple=True)
@click.option("--limit",           default=50, show_default=True)
@click.option("--oa-only", is_flag=True, default=True, show_default=True,
              help="Only attempt refs marked as open access")
@click.option("--email",           default=None, help="Email for Unpaywall (overrides config)")
@click.option("--concurrency", default=5, show_default=True,
              help="Number of simultaneous download connections")
def fetch_pdfs(tags, limit, oa_only, email, concurrency):
    """Try to download open-access PDFs for stored references."""
    import asyncio as _aio
    from .db import RefDatabase, _ref_id
    from .pdf_fetch import fetch_pdf

    with RefDatabase() as db:
        refs = db.list_all(tags=list(tags) or None, limit=limit)
        # Load extras in one query to skip already-downloaded refs.
        rids = [_ref_id(r) for r in refs]
        extras = db.get_extras_bulk(rids)

    if oa_only:
        refs = [r for r in refs if r.open_access]

    if not refs:
        console.print("[yellow]No eligible references.[/yellow]")
        return

    console.print(f"[blue]→[/blue] Fetching PDFs for {len(refs)} reference(s) "
                  f"(concurrency={concurrency})…")

    async def _run():
        sem = _aio.Semaphore(concurrency)

        async def _one(ref):
            rid = _ref_id(ref)
            # Skip if already recorded as downloaded.
            local = extras.get(rid, {}).get("pdf_local")
            if local:
                from pathlib import Path as _P
                if _P(local).exists():
                    return None  # already have it
            async with sem:
                return await fetch_pdf(ref, email=email)

        tasks = [_one(r) for r in refs]
        paths = await _aio.gather(*tasks)

        ok = 0
        with RefDatabase() as db2:
            for ref, path in zip(refs, paths):
                if path:
                    db2.update_integration_ids(_ref_id(ref), pdf_local=str(path))
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

    # Semantic index size
    try:
        from .semantic import get_default_index
        idx = get_default_index()
        if idx.is_available():
            console.print(f"[dim]Semantic index:[/dim] {idx.count()} vectors indexed")
        else:
            console.print("[dim]Semantic index:[/dim] not available (install chromadb + sentence-transformers)")
    except Exception:
        pass

    # Provider quota status
    try:
        from .quota import get_default_quota_manager
        qm = get_default_quota_manager()
        status = qm.get_status()
        if status:
            qtable = Table(title="Provider Quota (last 24 h)", box=box.SIMPLE)
            qtable.add_column("Provider", style="cyan")
            qtable.add_column("/ min", justify="right")
            qtable.add_column("/ hour", justify="right")
            qtable.add_column("/ day", justify="right")
            for provider, info in sorted(status.items()):
                qtable.add_row(
                    provider,
                    f"{info['last_minute']}/{info['limits']['per_minute'] or '∞'}",
                    f"{info['last_hour']}/{info['limits']['per_hour'] or '∞'}",
                    f"{info['last_day']}/{info['limits']['per_day'] or '∞'}",
                )
            console.print(qtable)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# semantic-search
# ---------------------------------------------------------------------------

@main.command("semantic-search")
@click.argument("query")
@click.option("-n", "--results", "n", default=10, show_default=True,
              help="Number of results")
@click.option("--year-from",  type=int,  default=None)
@click.option("--year-to",    type=int,  default=None)
@click.option("--type",       "ref_type", default=None)
@click.option("--oa",         is_flag=True, help="Open-access only")
@click.option("-f", "--format", "fmt", type=_FORMAT_CHOICES, default=None)
@click.option("-o", "--output", default=None)
def semantic_search(query, n, year_from, year_to, ref_type, oa, fmt, output):
    """
    Semantic (embedding-based) search — finds conceptually related references
    even when the exact keywords are absent.

    Requires:  pip install chromadb sentence-transformers
    """
    from .semantic import get_default_index
    from .db import RefDatabase

    idx = get_default_index()
    if not idx.is_available():
        console.print(
            "[red]Semantic index not available.[/red]\n"
            "Install dependencies:  pip install chromadb sentence-transformers\n"
            "Then rebuild the index:  zoterpile index-semantic"
        )
        raise SystemExit(1)

    if idx.count() == 0:
        console.print(
            "[yellow]Semantic index is empty.[/yellow]\n"
            "Build it with:  zoterpile index-semantic"
        )
        return

    with console.status("[cyan]Searching…"):
        hits = idx.search(
            query, n=n,
            year_from=year_from, year_to=year_to,
            ref_type=ref_type, oa_only=oa,
        )

    if not hits:
        console.print("[yellow]No results.[/yellow]")
        return

    ref_ids = [rid for rid, _ in hits]
    scores  = {rid: score for rid, score in hits}

    with RefDatabase() as db:
        refs = [db.get(rid) for rid in ref_ids]
    refs = [r for r in refs if r is not None]

    if fmt:
        _write_output(refs, fmt, output)
        return

    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("Sim",    style="dim",  width=5)
    table.add_column("Year",   style="cyan", width=5)
    table.add_column("Authors", width=20)
    table.add_column("Title",  width=55)
    table.add_column("DOI",    style="blue", width=25)

    for ref in refs:
        from .db import _ref_id
        score = scores.get(_ref_id(ref), 0.0)
        auth  = ref.authors[0].family if ref.authors else "?"
        if len(ref.authors) > 1:
            auth += " et al."
        table.add_row(
            f"{score:.2f}",
            str(ref.year or "?"),
            auth,
            (ref.title or "")[:55],
            (ref.doi or "")[:25],
        )
    console.print(table)
    console.print(f"[dim]{len(refs)} result(s)[/dim]")


# ---------------------------------------------------------------------------
# find-similar
# ---------------------------------------------------------------------------

@main.command("find-similar")
@click.argument("ref_id")
@click.option("-n", "--results", "n", default=5, show_default=True)
def find_similar(ref_id: str, n: int):
    """Find references similar to an existing one (by ID or DOI)."""
    from .semantic import get_default_index
    from .db import RefDatabase

    idx = get_default_index()
    if not idx.is_available():
        console.print("[red]Semantic index not available.[/red]")
        raise SystemExit(1)

    # Resolve ref_id (might be a DOI)
    with RefDatabase() as db:
        seed = db.get(ref_id) or db.get_by_doi(ref_id)

    if seed is None:
        console.print(f"[red]Reference not found: {ref_id}[/red]")
        raise SystemExit(1)

    from .db import _ref_id as compute_ref_id
    rid = compute_ref_id(seed)

    with console.status("[cyan]Finding similar references…"):
        hits = idx.find_similar(rid, n=n)

    if not hits:
        console.print("[yellow]No similar references found (index may be incomplete).[/yellow]")
        return

    result_ids = [r for r, _ in hits]
    scores     = {r: s for r, s in hits}

    with RefDatabase() as db:
        refs = [db.get(r) for r in result_ids]
    refs = [r for r in refs if r is not None]

    table = Table(title=f"Similar to: {seed.title[:50] if seed.title else ref_id}", box=box.SIMPLE)
    table.add_column("Sim",    style="dim",  width=5)
    table.add_column("Year",   style="cyan", width=5)
    table.add_column("Authors", width=20)
    table.add_column("Title",  width=55)
    for ref in refs:
        score = scores.get(compute_ref_id(ref), 0.0)
        auth  = ref.authors[0].family if ref.authors else "?"
        if len(ref.authors) > 1:
            auth += " et al."
        table.add_row(
            f"{score:.2f}",
            str(ref.year or "?"),
            auth,
            (ref.title or "")[:55],
        )
    console.print(table)


# ---------------------------------------------------------------------------
# index-semantic  (build / rebuild the semantic index)
# ---------------------------------------------------------------------------

@main.command("index-semantic")
@click.option("--chunk-size", default=500, show_default=True,
              help="References to embed per batch")
@click.option("--rebuild",    is_flag=True,
              help="Delete existing index and rebuild from scratch")
def index_semantic(chunk_size: int, rebuild: bool):
    """
    Build or rebuild the semantic (embedding) index from the database.

    Safe to run on a live database — uses streaming pagination.
    Requires:  pip install chromadb sentence-transformers
    """
    from .semantic import get_default_index, SemanticIndex
    from .db import RefDatabase

    idx = get_default_index()
    if not idx.is_available():
        console.print(
            "[red]Dependencies not installed.[/red]\n"
            "Run:  pip install chromadb sentence-transformers"
        )
        raise SystemExit(1)

    if rebuild:
        # Reset by creating a fresh collection
        try:
            import chromadb as _chroma
            from pathlib import Path
            from .config import get_config
            cfg  = get_config()
            path = cfg.semantic_index_path or str(
                Path.home() / ".local" / "share" / "zoterpile" / "semantic"
            )
            client = _chroma.PersistentClient(path=path)
            try:
                client.delete_collection("references")
            except Exception:
                pass
            # Reset the singleton so _get_collection re-creates it
            import zoterpile.semantic as _sem_mod
            _sem_mod._default_index = None
            idx = get_default_index()
        except Exception as exc:
            console.print(f"[red]Could not reset index: {exc}[/red]")
            raise SystemExit(1)
        console.print("[yellow]Old index deleted.[/yellow]")

    with RefDatabase() as db:
        total = db.count()

    if total == 0:
        console.print("[yellow]Database is empty — nothing to index.[/yellow]")
        return

    console.print(f"[blue]→[/blue] Indexing {total} references…")

    with _make_progress() as progress:
        task = progress.add_task("[cyan]Embedding…", total=total)

        def on_progress(done: int, _total: int) -> None:
            progress.update(task, completed=done)

        with RefDatabase() as db:
            indexed = idx.reindex_all(db, chunk_size=chunk_size, progress_callback=on_progress)

    console.print(f"[green]✓[/green] Indexed {indexed} references into the semantic store.")


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
@click.option("--host", default=None,  show_default=False, help="Bind host (default: 127.0.0.1, or 0.0.0.0 with --public)")
@click.option("--port", default=7274,  show_default=True,  help="Bind port")
@click.option("--public", is_flag=True, default=False,
              help="Bind to 0.0.0.0 to allow remote access (overrides --host default)")
def web(host: Optional[str], port: int, public: bool):
    """Launch the browser-based web UI.

    Use --public to bind to all interfaces (needed for remote access from
    other devices, Docker containers, or the browser extension).
    The API key is printed at startup — save it in the ⚙ Settings modal.
    """
    try:
        from .web import run as web_run
    except ImportError as e:
        console.print(
            f"[red]Error:[/red] Flask is required for the web UI.\n"
            f"Install it with:  pip install flask\n({e})"
        )
        raise SystemExit(1)
    effective_host = host or ("0.0.0.0" if public else "127.0.0.1")
    web_run(host=effective_host, port=port)


if __name__ == "__main__":
    main()
