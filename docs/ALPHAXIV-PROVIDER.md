# alphaXiv provider (opt-in)

[alphaXiv](https://www.alphaxiv.org) indexes the full text of arXiv papers. Its public, keyless keyword-retrieval endpoint is the same one used by alphaXiv's [OpenResearch](https://github.com/alphaXiv/OpenResearch) (`orx discover keyword`, MIT). Mouseion uses it for one narrow job: **finding the arXiv id of a reference that has a title but no arXiv id**, along with its abstract and date. The authoritative arXiv API provider then fills in the rest.

## Turning it on

Set it in `config.toml`:

```toml
[providers]
alphaxiv_enabled = true
```

Or set the environment variable `MOUSEION_ALPHAXIV_ENABLED=1`.

When on, `AlphaXivProvider` is appended to the provider set used by enrichment (priority 8, after `arxiv_api`). No key or account is needed.

## Precision before recall

Keyword retrieval returns papers that *mention* the query anywhere in their text. A hit is accepted only when its title matches the reference's own full title:

- the normalized similarity is at least 0.92; or
- the record omits a subtitle, and the hit equals the reference's main title before the `:`. That main title must have at least 5 words.

A wrong arXiv id is worse than none, so near-misses are rejected. That includes a reference whose stored title is truncated.

## Rate limit and budget

1 request per second, 40 per minute, 10,000 per day. The limit is set in `api_router._DEFAULT_LIMITS["alphaxiv"]` and enforced by the shared router, with 429 cooldowns shared across enrichment.

## Tests

`tests/test_alphaxiv_provider.py` covers exact matching, rejection of full-text-only mentions, subtitle handling, failure paths, and opt-in registration.
