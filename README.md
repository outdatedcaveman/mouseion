# Mouseion

A self-hosted academic reference manager built for large personal libraries (250k+ refs tested). Standalone `.exe` on Windows, web UI accessible from any browser.

## What it does

- **Import anything**: BibTeX, RIS, CSL-JSON, Markdown, HTML bookmarks, plain text with DOIs/URLs/arXiv IDs/PMIDs. Drag and drop.
- **Enrich automatically**: 11 academic API providers (CrossRef, OpenAlex, Semantic Scholar, PubMed, DBLP, arXiv, OpenLibrary, DOI.org, Unpaywall, Google Books) fill in missing metadata. Background daemon with adaptive priority queue processes your whole library while you work.
- **Search everything**: Full-text search across titles, abstracts, authors, keywords, journals, DOIs, URLs, ISBNs, PMIDs, arXiv IDs. Instant results on 250k+ libraries via FTS5.
- **Export anywhere**: BibTeX, RIS, CSL-JSON, CSV, Markdown, Zotero RDF.
- **Organize**: Tags (manual + auto-assigned from 30-topic taxonomy), collections, reading status (unread/reading/read), Kanban board view.
- **Stay complete**: Completeness scoring per reference. Filter by completion status. Background enrichment daemon continuously improves your library using escalating strategies.

## Architecture

```
mouseion/
  web.py           Flask app with embedded single-page dark-mode UI
  db.py            SQLite + FTS5, batch upsert, enrichment queue
  lookup.py        Enrichment orchestrator (provider selection, retry, merge)
  merge.py         Net-positive merge (never loses data, weighted conflict resolution)
  enrich_daemon.py Background enrichment with tiered strategies (L0-L4)
  providers/       11 academic API providers
  parsers/         BibTeX, RIS, JSON, Markdown, HTML, plain text
  exporters/       BibTeX, RIS, JSON, CSV, Markdown, Zotero RDF
  tagger.py        Auto-tagging via keyword rules + topic taxonomy
  integrations/    Zotero, Notion, Obsidian, Google Drive, Instapaper
  pdf_manager.py   PDF download/storage (Google Drive sync)
```

## Quick start

### Windows (.exe)

Download `mouseion.exe` from [Releases](https://github.com/outdatedcaveman/mouseion/releases), double-click it. A browser window opens at `http://127.0.0.1:7274`. No installation, no dependencies.

### From source

```bash
git clone https://github.com/outdatedcaveman/mouseion.git
cd mouseion
pip install -e .
mouseion-web          # starts the web UI
```

### Build the .exe

```bash
pip install pyinstaller pywebview
pyinstaller mouseion.spec
# Output: dist/mouseion.exe (~25 MB)
```

## Enrichment daemon

The background enrichment daemon runs automatically while the app is open. It processes incomplete references using five escalating strategy tiers:

| Level | Strategy | When used |
|-------|----------|-----------|
| L0 | Single best provider for the identifier (DOI->CrossRef, PMID->PubMed) | First attempt, refs with identifiers |
| L1 | All relevant providers in parallel | After L0 doesn't improve enough |
| L2 | Clean title search on fast providers | Title-only refs |
| L3 | Aggressive title normalization + author/year combinations | After L2 fails |
| L4 | URL resolution, exhaustive provider search, fuzzy matching | Last resort |

Priority scoring: `gap x ease / (1 + attempts)`. Refs with DOIs get processed first (high ease score), garbage titles sink to the bottom. Failed attempts reduce priority so easy wins are always prioritized.

Control the daemon from the toolbar: toggle on/off, view the queue monitor.

## Performance

Designed for large libraries:

- **Import**: 250k refs inserted in ~2 minutes (pure DB, no enrichment blocking)
- **Search**: FTS5 returns results on 254k refs in milliseconds
- **UI**: Virtual scrolling renders only visible items. Server-side pagination loads 5k refs per page.
- **DB**: SQLite WAL mode, batch upserts with deferred FTS triggers, 65 MB page cache

## Configuration

Config file: `~/.config/mouseion/config.toml`

```toml
[database]
path = "~/.local/share/mouseion/refs.db"

[pdf]
directory = "~/Google Drive/Mouseion PDFs"

[providers]
unpaywall_email = "your@email.com"
semantic_scholar_api_key = ""
```

Environment variables: `MOUSEION_DB_PATH`, `MOUSEION_API_KEY`

## License

AGPL-3.0
