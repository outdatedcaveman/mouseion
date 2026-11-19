# Mouseion knowledge base (`mouseion.kb`)

Goal: make the library's full texts and references usable by people and agents across all projects (semplex,
Synesism, ...), as a knowledge base that is **reliable** (every answer traceable to a page), **consistent**
(claims are versioned, not overwritten), **actionable** (it says what to read next) and **continually growing**.

## Why earlier attempts did not work, and what this does instead

| Earlier approach | Failure mode | Here |
|---|---|---|
| LLM-extracted knowledge graph (triples) | Extraction noise, entity drift, no way to check a triple against the text; answers cannot be cited | No extracted triples. The only graph is the **exact citation graph** from OpenAlex reference lists |
| Embeddings of titles and abstracts | Retrieves *papers*, not *statements*; abstracts omit most content; cosine scores have no provenance | **Page-anchored passages** of the full texts, ranked by BM25 (exact terms matter in science), with dense retrieval as an optional second ranking fused by reciprocal rank |
| Knowledge living in the model (e.g. AtlasKV) | Opaque, not citable, needs a GPU and fine-tuning | Knowledge stays in SQLite; agents query it through MCP and cite passages |

The knowledge itself, meaning what we learned from a source, lives in **notes**. A note is a claim tied to a
reference and, ideally, to the exact passage, with a verbatim quote that the store *checks* against the text. Notes
are never edited in place. A revision supersedes the old note, so the history of what we believed and why is kept.
This mirrors the semplex claim register: provenance-first, auditable, append-only.

## Layers (all in `kb.db`, next to `refs.db`; the library is opened read-only)

| Layer | Table(s) | Built by | Source |
|---|---|---|---|
| Full text | `pdf_files`, `chunks` + `chunks_fts` | `build-fulltext` (incremental: unchanged files are skipped) | local PDFs (`pdf_local`), PyMuPDF |
| Citation graph | `works`, `cites` | `build-citations` (cached: each DOI is fetched once) | OpenAlex `referenced_works` (no contact details sent) |
| Notes | `notes` + `notes_fts` | `add_note` (MCP tool or Python) | people and agents, with provenance |

PDFs without a text layer are recorded as `empty`. Those are the scans that need OCR, and `ocr.py` already exists
for them. Broken PDFs are recorded as `error:<type>`. Nothing is silently skipped.

## Use

```bash
python -m mouseion.kb build-fulltext            # all local PDFs -> passages
python -m mouseion.kb build-citations           # all DOIs -> OpenAlex reference lists (~8 requests/s)
python -m mouseion.kb stats                     # coverage
python -m mouseion.kb search "strong lumpability causal states"
python -m mouseion.kb frontier -n 50            # most-cited works the library does not have: what to acquire next
```

Agents, from any project:

```bash
claude mcp add mouseion -- python -m mouseion.kb serve
```

| Tool | What it returns |
|---|---|
| `search_refs` | references (title/abstract BM25) with citations |
| `search_fulltext` | passages with reference, page and citation, at most 2 per reference |
| `get_ref` | metadata, abstract, citation-graph counts |
| `citations` | references, cited-by, bibliographic coupling, co-citation |
| `reading_frontier` | works the library cites most but lacks |
| `search_notes` / `add_note` | the growing knowledge base (writes to `kb.db` only; provenance enforced) |
| `kb_stats` | coverage |

## How it grows

1. New PDFs are picked up by the next `build-fulltext`; new DOIs by the next `build-citations`. Both are
   incremental, so they can run nightly.
2. Every time a project reads a source (semplex's rule 7 "read before you cite", Synesism's citation audit), the
   finding goes in as a note with its passage. `charter/LITERATURE_REVIEW.md` entries then point to note ids, and
   the next session finds them with `search_notes` instead of re-reading.
3. `reading_frontier` turns the citation graph into an acquisition list. What the library's own papers cite most,
   and the library lacks, is the most likely gap.

## Not yet done (next steps, in order)

- **Measure before adding dense retrieval.** Run 20–30 real questions from semplex and Synesism against BM25
  passages and record which ones fail. Add the dense chunk index (the existing MiniLM + Chroma stack) only if
  paraphrase misses are the dominant failure. The fusion hook is in `search_passages(dense=...)`.
- **Backfill notes** from semplex `charter/LITERATURE_REVIEW.md` and Synesism `BIBLIOGRAPHY.md`, matching sources
  to library references by DOI or arXiv id.
- **OCR the `empty` PDFs** with `ocr.py`, then re-run `build-fulltext`.
