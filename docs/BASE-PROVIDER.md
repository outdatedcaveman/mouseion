# BASE (Bielefeld Academic Search Engine) — repository copies for the PDF tail

Status: **built and tested, switched off, waiting for BASE access** (refused on 2026-10-18, 10-31 and 11-04; the public IP alternated between two addresses).

## Why BASE

The PDF tail is the part of the library the existing chain cannot reach: 85,412
of 280,828 refs have a DOI and still no PDF (143,299 have no PDF at all). The
open indexes Mouseion already asks (Unpaywall, OpenAlex locations, Europe PMC,
PMC, Semantic Scholar, CORE) mostly list publisher copies and the large
repositories. BASE harvests 400M+ records over OAI-PMH from 11k+ repositories:
the university repositories that hold theses, accepted manuscripts and reports
those indexes often lack. It is free for non-commercial use and legal (the copies are
the repositories' own open-access files).

## What was built

| Piece | Where |
|---|---|
| Provider + API client | `src/mouseion/providers/base_search.py` (`BaseSearchProvider`, `repository_copies()`) |
| PDF strategy 3.6 (after the repository copies, before Semantic Scholar) | `src/mouseion/pdf_manager.py` |
| Flag + key | `[pdf] base_search_enabled` (default `false`), `[providers] base_search_api_key`; env `MOUSEION_BASE_SEARCH_ENABLED`, `MOUSEION_BASE_SEARCH_API_KEY` |
| Budget | `api_router` limit `base_search`: 55/min, 40k/day, 1.05 s spacing (BASE asks for 1 request per second) |
| Probe + measurement | `scripts/base_search_measure.py` (`--probe`, or `N` refs) |
| Tests | `tests/test_base_search.py` (22 tests, mocked HTTP) |

How it searches:

1. DOI: `dcdoi:"<doi>"`; a record counts only if its `dcdoi` equals the ref's DOI.
2. If the DOI gives nothing: `dctitle:"<clean title>" dccreator:"<first author>" dcyear:[y-1 TO y+1]`.
   A record counts only with title similarity ≥ 92 (token sort), a matching author
   family name, a year within ±1, and no conflicting DOI.
3. URLs come from `dclink` + `dcidentifier`. Closed records (`dcoa=0`) and resolver
   links (doi.org, handle.net) are dropped; open (`dcoa=1`) and file-looking
   URLs (`/bitstream/`, `.pdf`, `/download` ...) come first; landing pages are
   followed through their `citation_pdf_url`.
4. A downloaded PDF records where it came from in `extras.pdf_provenance`
   (`repository` = BASE's internal name, `repository_name`, `url`, `matched_by`).

Failure handling (so a refusal is never read as "no PDF"):

* BASE refuses an unregistered caller with HTTP 200 and
  `{"error": "Access denied for IP address … and user agent …"}`. That parks
  BASE for an hour (logged once) and the ref is **not** written to the attempt
  ledger as a miss. The same applies when the 1 QPS budget makes BASE
  unreachable within the wait.
* An unrecognised JSON response is an error, not an empty result. The guide
  documents only the XML form. The documented XML form is parsed too.
* The attempt-ledger key gets a `_base` suffix while the flag is on, so earlier
  misses do not hide refs from BASE. Side effect: switching the flag on opens
  the whole tail to the whole chain once, not just to BASE.
* BASE is **not** in `_make_providers()`. At 1 QPS it would stall concurrent
  metadata enrichment, so it serves the PDF chain only (and works as a provider
  when called explicitly).

## The blocker: access

The API (BASE Interface Guide v1.27, March 2023) is "IP controlled or with an
apikey". An IP grant is tied to the caller's User-Agent. Probes on 2026-10-18
and 2026-10-31 were both refused. The public IP **changed between the two
probes**: it is a residential line with a dynamic address. So the request to
make is for an **apikey**. An IP grant would break at the next address change.

Access is requested via BASE's contact form, https://www.base-search.net/about/en/contact.php.
The page is behind Anubis bot protection, so it opens in a normal browser only.
The owner submits it by hand. The form, as archived from Nov 2024 to Jun 2025,
has exactly these fields:

| Field | What to enter |
|---|---|
| "Please leave the following field blank:" | nothing (a spam trap) |
| Your Email Address (required) | the address BASE should reply to |
| Your Question (required) | the text below |
| "I hereby consent to the processing …" checkbox (required) | tick it |

There is no name or subject field, so the subject line goes at the top of the question. Suggested text:

> HTTP API access request (non-commercial) — apikey
>
> I maintain Mouseion, an open-source (AGPL-3.0) personal reference manager
> (https://github.com/outdatedcaveman/mouseion). I would like to use the BASE
> HTTP API (PerformSearch) for non-commercial purposes: finding open-access
> repository copies (theses, accepted manuscripts) of works in my own research
> library. It makes DOI lookups with a title + author fallback, at most 1
> request per second and a few thousand requests per day, and does not
> redistribute BASE data.
>
> My internet connection is residential with a dynamic IP address, so I would
> be grateful for an **apikey** (Interface Guide v1.27). If IP registration is
> the only option: my current IP is `<run --probe>`, User-Agent
> `mouseion-base/1.0 (+https://github.com/outdatedcaveman/mouseion)`.

Do not change `USER_AGENT` in `base_search.py` after an IP grant: the grant names it.

## Once access is granted

```bash
# 1. key (if BASE sent one) — or skip if they registered the IP
setx MOUSEION_BASE_SEARCH_API_KEY "<key>"
# 2. check
python scripts/base_search_measure.py --probe          # exit 0 = BASE answers
# 3. measure on 200 refs with a DOI and no PDF (~10–15 min at 1 QPS; read-only, PDFs go to a scratch folder)
python scripts/base_search_measure.py 200
# 4. if the yield is worth it: [pdf] base_search_enabled = true
```

## Measurement (200 refs with a DOI and no PDF)

**Not run yet: BASE refuses this machine (see above).** `base_search_measure.py`
prints this table. Overlap is measured against the sources the chain already
asks for the same ref (OpenAlex locations, Unpaywall `oa_locations`,
Europe PMC, PMC, a known `oa_url`).

| measure | n | % of asked |
|---|---|---|
| Sample (DOI, no PDF) | 200 | |
| BASE asked / could not ask | — | |
| Refs with ≥1 BASE repository copy | — | — |
| matched by DOI / by title+author | — | |
| Valid PDFs downloaded (pypdf opens them) | — | — |
| PDF URL not listed by existing sources | — | |
| Refs where existing sources list any copy | — | — |
| BASE hits that existing sources also cover | — | |
| BASE hits sharing an exact URL with them | — | |

Unverified until access works: the exact JSON shape of `format=json` (the
parser fails loudly if it differs) and whether `dcdoi` phrase queries match
case-insensitively. The DOI is lowercased before querying.
