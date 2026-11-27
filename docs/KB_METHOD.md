# How the knowledge base finds non-trivial things (method decision, 2026-11-20)

**Question (Bruno):** RAG and embeddings may not be the best way to get non-trivial information out of the
consolidated mesh of text. What works best for us?

**Answer:** a lexical index with three cheap structural layers. The reasoning is done by an agent (Claude, through
the `kb_*` tools) **after** ranked retrieval, never in place of it. No embeddings and no LLM at indexing time.

## The constraints that decide it

- Library: ~262k references, ~27% with PDFs (tens of thousands of papers, millions of passages).
- Machine: 8 GB RAM, no usable GPU. Models: free tiers only (Gemini free, ~18 calls/day for extraction).
- Rule: every claim must be traceable to a verbatim passage.

## The evidence

| Source | Finding | Consequence |
|---|---|---|
| Wang et al. 2026, *BM25 Wins at Scale* (arXiv:2607.26497) | Questions held fixed, corpus grown 450-fold. Above ~10M corpus tokens BM25 beats dense retrieval, file-system agents and every LLM-built graph (GraphRAG, LightRAG, HippoRAG 2). Graph construction hits walls: LightRAG extrapolates to ~102B tokens (~4 instance-years) at 500k documents. **An agent working after BM25 ranking is best, +14.6 points over BM25.** | Lexical first; Claude as the reader and reasoner on top. LLM-built graphs are out. |
| Wu et al. 2026, *SAG2* (arXiv:2610.22588) | The FTS5 index already is a passage–term hypergraph. Judged seed passages activate rare "bridge" terms, which retrieve passages sharing no word with the question. Best R@10 on MuSiQue / 2Wiki / HotpotQA; lead over BM25 widens with corpus size; no indexing cost. | Implemented as `kb_expand`, with the calling agent as the judge, so the decision model costs nothing. |
| Our measurement (below) | Dense retrieval alone is clearly worse than BM25 on our own questions; fusion adds nothing at hit@10. Half of all passages had a text defect (ligatures) that cost far more recall than any retrieval method gains. | Fix the text first; no dense layer for now. |

## What was built (all in `kb.db`; `refs.db` is never written)

| Layer | Module / tool | What it answers | Cost |
|---|---|---|---|
| Text repair | `fulltext.normalize` (NFKC), extraction version 2 | Makes "definition", "finite", "fibration", "coefficient"... match in LaTeX PDFs, where the PDF stores ﬁ/ﬀ/ﬂ as one character | One re-extraction of old files (incremental, resumable) |
| Multi-hop | `expand.expand` · `kb_expand` · `python -m mouseion.kb expand` | "Which result connects X to Y?", where the second passage shares no word with the question | Query time only, ~0.25 s |
| Discovery | `expand.bridges` · `kb_bridges` | Closed: which ideas link literature A and literature C, and whether anyone has joined them (co-occurrence count). Open: what lies two steps from A but never appears with it (Swanson ABC). **Candidates to check, not findings.** | Query time only |
| Mathematical structure | `statements` · `kb_statements` · `build-statements` | "Where is X defined?", "Which theorems are stated about X?" Typed, numbered and named definitions, theorems, lemmas, conjectures... with page | Pattern-based, incremental, seconds per 100 papers |

Safeguards: re-extraction archives every passage a note points to (`chunks_archive`) and re-anchors the note to
the new passage containing its quote. Nothing a note cites is lost.

## Measurements (127 arXiv papers cited by Synesism and semplex, 22,563 passages)

Questions: 58 derived from the bibliographies' own "why we use this source" notes, with the source's title words and
author names removed (so a hit cannot come from matching the title), plus the hand-written `kb_eval/questions.jsonl`.
Metric: rank of the expected paper among retrieved references.

| Method | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|
| BM25, before the ligature fix | .638 | .862 | .897 | .733 |
| **BM25** | **.672** | .879 | .897 | **.766** |
| dense (model2vec potion-base-8M) | .517 | .724 | .810 | .616 |
| BM25 + dense (RRF) | .690 | .879 | .897 | .755 |
| BM25 + bridge expansion (no judge; see the scale table below) | .655 | .862 | .948 | .746 |
| **BM25, function words dropped from the query** (2026-11-27) | **.690** | **.914** | .931 | **.779** |

Dropping function words from queries ("of", "the", "in"...) also fixed speed on the real library. Each of those
words has millions of postings among 3.6M passages, and one query took 66 s.

The ligature fix alone: a search for "definition" went from 1,638 to 4,598 matching passages. Statements found:
8,664, e.g. *Definition 4.3: a wild category C is univalent if for all A, B : Ob(C), the canonical function
(A = B) → (A ≃ B) is an equivalence* (Hart 2026), and the global and equivariant Barratt–Priddy–Quillen theorems
(Schwede; Barwick et al.).

Closed discovery, examples:
- FI-modules ↔ univalent species: *finite sets, permutations, configuration spaces, moduli, congruence*.
- 𝔽₁ ↔ sphere spectrum: *cyclotomic, Morava K-theory, absolute geometry, topological modular (forms)*.

Open discovery picks good intermediates (for "cohesion modality": crisp, codiscrete, comodality, parametricity,
Lawvere, comonad). Its final candidates are still noisy on a corpus this small; treat them as prompts for a human or
agent to check.

**At 4.6× the corpus** (587 papers, 65,784 passages: the same 127 plus 460 recent arXiv papers from the same
fields as distractors, 2026-11-27):

| Method | hit@1 | hit@5 | hit@10 | MRR |
|---|---|---|---|---|
| **BM25** | **.672** | **.862** | **.914** | **.765** |
| dense (model2vec) | .431 | .655 | .724 | .536 |
| BM25 + dense (RRF) | .655 | .828 | .862 | .718 |
| BM25 + bridge expansion, **no judge** | .655 | .828 | .879 | .737 |

BM25 holds; dense collapses, and fusing it in now hurts. **Expansion without a judge falls below BM25 once
distractors are present**, as SAG2's ablation predicts: the judging step is what makes expansion work. So use
`kb_expand` with `evidence` (the chunk ids the agent judged relevant), not as a blind re-ranker. Its no-evidence
mode is for exploration only.

**Limits of this measurement.** 127 papers is small, and the published differences between methods grow with corpus
size. A distractor corpus of ~2,000 recent arXiv papers in the same fields is being added. Run
`python -m mouseion.kb eval` on the real library to settle it. The questions are mostly single-hop; multi-hop gains
rest on SAG2's benchmarks.

## How an agent should use it

1. `kb_search_fulltext` (BM25) for the question.
2. Read the passages and decide which are evidence. Then `kb_expand(query, evidence=[chunk ids])` reaches the next
   hop through bridge terms. Repeat once more for three-hop chains.
3. `kb_statements(term, kind="definition")` for exact definitions and theorem statements.
4. `kb_bridges(a, c)` to ask whether two literatures meet, and through what.
5. Record what was learned with `kb_add_note`, with a verbatim quote, which is checked against the text.

## What was not adopted, and why

Recorded in the tool registry (`ai-ensemble/registry`, 2026-11-20):
- **LightRAG, RAG-Anything, PIKE-RAG, sift-kg, NexusRAG: REJECT.** LLM extraction over every passage hits a cost
  wall, its triples cannot be checked against text, and it is dominated by BM25 at scale.
- **HippoRAG 2, graphiti: REFERENCE.** The ideas are sound, but the goals are met lexically.
- **PageIndex: TRIAL, scoped to one long book** (HoTT book, Rijke). It needs an LLM pass per document.
- **model2vec dense: kept out of the KB.** Measured worse (above); re-measure on the full library.
- **PaperQA2: TRIAL, only if** Claude over the `kb_*` tools misses evidence the KB holds.
- **Frontier Graph's idea** (research questions from missing links) is implemented as `kb_bridges`.
