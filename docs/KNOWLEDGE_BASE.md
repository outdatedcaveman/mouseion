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

PDFs without a text layer are recorded as `empty`. `ocr-empty` reads them with Windows' built-in OCR engine
(`ocr.py`, every page) and indexes the text with its page numbers; the file is then recorded as `ocr`. A scan that
OCR cannot read stays `empty`. On a system without the engine, `ocr-empty` reports `unavailable` and changes nothing.
An unchanged `empty` file is not re-extracted at every build. Broken PDFs are recorded as `error:<type>`. Nothing is silently skipped.

## Use

```bash
python -m mouseion.kb build-fulltext            # all local PDFs -> passages
python -m mouseion.kb ocr-empty                 # scans without a text layer -> OCR -> passages (Windows)
python -m mouseion.kb build-citations           # all DOIs -> OpenAlex reference lists (~8 requests/s)
python -m mouseion.kb stats                     # coverage
python -m mouseion.kb search "strong lumpability causal states"
python -m mouseion.kb frontier -n 50            # most-cited works the library does not have: what to acquire next
python -m mouseion.kb backfill-notes --bib <synesism>/bibliography/references.bib --project synesism
python -m mouseion.kb backfill-notes --refs-table <semplex>/charter/REFERENCES.md --project semplex
python -m mouseion.kb eval docs/kb_eval/questions.jsonl -n 10
python -m mouseion.kb build-statements          # typed definitions/theorems/lemmas (incremental)
python -m mouseion.kb statements "univalent category" --kind definition
python -m mouseion.kb expand "question" --evidence 1203,877   # next hop via bridge terms
python -m mouseion.kb bridges "FI-modules" --with "univalent species"
```

**Why lexical, and how multi-hop and discovery work:** see [`KB_METHOD.md`](KB_METHOD.md). It covers the
evidence (BM25 beats dense and LLM-built graphs at scale), the measurements on 127 project papers, and how an agent
should use `kb_expand`, `kb_statements` and `kb_bridges`.

**Text normalisation (extraction version 2):** passages are NFKC-normalised, so LaTeX ligatures (ﬁ, ﬀ, ﬂ) match
ordinary queries. That was 50% of passages in the test corpus. Files indexed by version 1 are re-extracted by the
next `build-fulltext`, incrementally. Passages that notes cite are first copied to `chunks_archive`, and the notes
are re-anchored by their quote.

**Backfilled notes** (`backfill-notes`, add `--dry-run` to preview). Each source a project's bibliography records,
with a stated use, becomes one note "<project> uses this source for: ...", matched to the library by DOI, then arXiv
id, then exact title (case, punctuation and accents ignored). They carry no quote, so they have kind `project-use`
and author `backfill` and are never textual support. Rows at level MEMORY are skipped (semplex's own rule). Records
that match nothing are listed under `unmatched`: those are sources the library lacks. Running it again adds only
what is new.

**Retrieval evaluation** (`eval`). `docs/kb_eval/questions.jsonl` holds 28 real questions from semplex and
Synesism, paraphrased rather than copied from titles, each with the sources a good answer must surface. The report
gives hit@1/5/10 and MRR for passages and references and classifies every miss: `not_in_library`, `not_indexed` (in
the library, no text), or `retrieval_miss` (indexed text that BM25 did not rank). Only the last is evidence for
dense retrieval, and the verdict line says whether it dominates.

Always on (PC): run `python -m mouseion.kb daemon` (or `pythonw` for no window) at login. Every 6 h it indexes new
or changed PDFs, OCRs up to 50 scans, indexes typed statements, fetches reference lists for new DOIs, and writes `kb_status.json` next to `refs.db`. The health
report (`mouseion health`, `health.json`) then has a `knowledge_base` check, and `scripts/status_report.py` lists the
daemon among the always-on jobs.

Browse and check: open `http://<mouseion host>:7274/kb?api_key=<API key>` in the Mouseion web app. It searches
passages, notes and references; every hit shows its citation and page, and notes show their verbatim quote. The same
data is served as JSON under `/api/kb/` (search, stats, frontier, notes), behind the usual API key.

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

All of these run on the PC, where the library is.

- **Run it on the real library**: `build-fulltext`, `ocr-empty`, `build-citations`, then the daemon at login.
- **Backfill the notes** from both bibliographies (commands above). Review `unmatched`: those sources should be
  added to the library, then backfill again.
- **Measure**: run `eval`. Add the dense chunk index (the existing MiniLM + Chroma stack, hook in
  `search_passages(dense=...)`) only if the verdict says retrieval misses dominate.
