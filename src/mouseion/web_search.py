"""
Web search fallback for hard-to-find references.

When academic APIs fail (the paper was never indexed by CrossRef, OpenAlex,
or Semantic Scholar), a real web search is the only option. Many obscure
papers exist only on a professor's personal page, a seminar listing, or a
university repository.

Strategy:
  1. Search the title (+ optional author) on DuckDuckGo or Google CSE.
  2. Scan result URLs for DOIs, arXiv IDs, PMIDs.
  3. If identifiers found → return them for batch lookup.
  4. If no identifiers → fetch the top landing pages and scrape metadata
     from <meta> tags (Highwire Press, Dublin Core, OpenGraph, etc.).

DuckDuckGo HTML search is the default (no API key needed).
Google Custom Search JSON API is used when configured (better results,
100 free queries/day).
"""

from __future__ import annotations

import asyncio
import logging
import re
from .semaphore import SafeSemaphore
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus, unquote

import httpx
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from .models import Author, RefType, Reference
from .network_budget import bucket_from_url, network_slot
from .providers.base import clean_query_title

logger = logging.getLogger("mouseion.web_search")

_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Concurrency limits for web search. These fallbacks touch search engines and
# publisher landing pages, so reliability matters more than burst throughput.
_SEARCH_CONCURRENCY = 4
_PAGE_FETCH_CONCURRENCY = 8


# ---------------------------------------------------------------------------
# Identifier extraction from URLs
# ---------------------------------------------------------------------------

def _extract_doi_from_url(url: str) -> Optional[str]:
    for pat in [
        r'doi\.org/(10\.\d{4,}/[^\s"\'<>&#]+)',
        r'/doi/(?:abs|full|pdf)?/?(10\.\d{4,}/[^\s"\'<>&#]+)',
        r'doi[=:]\s*(10\.\d{4,}/[^\s"\'<>&]+)',
    ]:
        m = re.search(pat, url, re.I)
        if m:
            return m.group(1).rstrip('.,;)')
    return None


def _extract_arxiv_from_url(url: str) -> Optional[str]:
    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})', url, re.I)
    return m.group(1) if m else None


def _extract_pmid_from_url(url: str) -> Optional[str]:
    m = re.search(r'pubmed\.ncbi\.nlm\.nih\.gov/(\d+)', url, re.I)
    return m.group(1) if m else None


def _extract_ids_from_url(url: str) -> dict:
    """Extract all identifiers from a URL. Returns {doi, arxiv_id, pmid}."""
    return {
        "doi": _extract_doi_from_url(url),
        "arxiv_id": _extract_arxiv_from_url(url),
        "pmid": _extract_pmid_from_url(url),
    }


# ---------------------------------------------------------------------------
# DuckDuckGo HTML search (no API key)
# ---------------------------------------------------------------------------

async def _search_duckduckgo(
    query: str, client: httpx.AsyncClient, max_results: int = 15
) -> List[dict]:
    """
    Search DuckDuckGo HTML and return list of {title, url, snippet}.
    Uses the lite (HTML-only) version for reliability.
    """
    results = []
    try:
        await _acquire_domain_lock("https://lite.duckduckgo.com")
        async with network_slot("metadata", bucket="duckduckgo", min_interval=1.0):
            resp = await client.post(
                "https://lite.duckduckgo.com/lite/",
                data={"q": query, "kl": ""},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            logger.debug("DDG search HTTP %d for: %s", resp.status_code, query[:50])
            return results

        soup = BeautifulSoup(resp.text, "html.parser")

        # DuckDuckGo lite results are in table rows with class "result-link"
        for link in soup.select("a.result-link")[:max_results]:
            href = link.get("href", "")
            title = link.get_text(strip=True)
            if href and title:
                results.append({"title": title, "url": href, "snippet": ""})

        # Fallback: parse any <a> tags that point to external sites
        if not results:
            for link in soup.find_all("a", href=True):
                href = link["href"]
                if href.startswith("http") and "duckduckgo.com" not in href:
                    title = link.get_text(strip=True)
                    if title and len(title) > 5:
                        results.append({"title": title, "url": href, "snippet": ""})
                        if len(results) >= max_results:
                            break

    except Exception as e:
        logger.debug("DDG search error: %s", e)

    return results


def _looks_academic_url(url: str) -> bool:
    low = (url or "").lower()
    domains = [
        "doi.org", "arxiv.org", "pubmed", "ncbi.nlm.nih.gov", "semanticscholar",
        "crossref", "openalex", "philpapers", "philarchive", "jstor", "springer",
        "wiley", "elsevier", "sciencedirect", "nature.com", "science.org",
        "pnas.org", "ams.org", "ieee", "acm.org", "hal.science", "ssrn",
        "nber", "repec", "tandfonline", "oxfordacademic", "cambridge.org",
        "degruyter", "muse.jhu", "projecteuclid", "dblp.org", "biorxiv",
        "medrxiv", ".edu", ".ac.", "university", "repository", "handle.net",
        "researchgate", "academia.edu", "worldscientific", "wspc",
        "scienceconnect",
    ]
    return low.endswith(".pdf") or any(d in low for d in domains)


def _search_result_candidate(seed: Reference, result: dict) -> Optional[Tuple[Reference, float]]:
    """Use a search result as low-confidence URL evidence when title is tight."""
    url = result.get("url") or ""
    title = clean_query_title(result.get("title") or "")
    seed_title = clean_query_title(seed.title or "")
    if not url or not title or not seed_title or not _looks_academic_url(url):
        return None

    sim = fuzz.token_sort_ratio(seed_title.lower(), title.lower())
    seed_has_context = bool(seed.year or seed.authors or seed.doi or seed.pmid or seed.arxiv_id or seed.isbn)
    threshold = 88 if seed_has_context else 94 if len(seed_title) < 45 else 90
    if sim < threshold:
        return None

    ref = Reference(title=title, url=url)
    if seed.year:
        ref.year = seed.year
    if seed.authors:
        ref.authors = list(seed.authors)
    ref.sources["web_search_result"] = 0.48
    return ref, 0.48


# ---------------------------------------------------------------------------
# Google Custom Search (optional, needs API key + CSE ID)
# ---------------------------------------------------------------------------

async def _search_google_cse(
    query: str, client: httpx.AsyncClient,
    api_key: str = "", cse_id: str = "", max_results: int = 10,
) -> List[dict]:
    """Search via Google Custom Search JSON API."""
    if not api_key or not cse_id:
        return []

    results = []
    try:
        async with network_slot("metadata", bucket="google_cse", min_interval=0.2):
            resp = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": api_key,
                    "cx": cse_id,
                    "q": query,
                    "num": min(max_results, 10),
                },
            )
        if resp.status_code != 200:
            logger.debug("Google CSE HTTP %d", resp.status_code)
            return results

        for item in resp.json().get("items", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            })
    except Exception as e:
        logger.debug("Google CSE error: %s", e)

    return results


# ---------------------------------------------------------------------------
# Page metadata scraper
# ---------------------------------------------------------------------------

# Academic sites use these <meta> tags:
# Highwire Press: citation_title, citation_author, citation_doi, citation_date
# Dublin Core: DC.title, DC.creator, DC.identifier, DC.date
# OpenGraph: og:title, og:description
# PRISM: prism.doi
_META_DOI_NAMES = [
    "citation_doi", "dc.identifier", "prism.doi", "doi",
    "DC.Identifier", "DC.identifier.doi",
]
_META_TITLE_NAMES = [
    "citation_title", "dc.title", "DC.Title", "og:title",
    "twitter:title", "eprints.title",
]
_META_AUTHOR_NAMES = [
    "citation_author", "dc.creator", "DC.Creator",
    "citation_authors", "author", "eprints.creators_name",
]
_META_YEAR_NAMES = [
    "citation_date", "citation_publication_date", "dc.date",
    "DC.Date", "citation_year", "eprints.date",
]
_META_ARXIV_NAMES = ["citation_arxiv_id"]
_META_PMID_NAMES = ["citation_pmid"]
_META_ABSTRACT_NAMES = [
    "citation_abstract", "dc.description", "DC.Description",
    "og:description", "description",
]



import time
_domain_locks = {}
_domain_cache_lock = asyncio.Lock()

async def _acquire_domain_lock(url: str):
    from urllib.parse import urlparse
    domain = urlparse(url).netloc
    if not domain:
        return
    
    async with _domain_cache_lock:
        if domain not in _domain_locks:
            _domain_locks[domain] = 0.0
    
    now = time.time()
    last_hit = _domain_locks[domain]
    if now - last_hit < 1.0:
        await asyncio.sleep(1.0 - (now - last_hit))
    
    _domain_locks[domain] = time.time()

async def _scrape_page_metadata(
    url: str, client: httpx.AsyncClient
) -> Optional[Reference]:
    """
    Fetch a page and extract bibliographic metadata from <meta> tags.
    Returns a Reference if meaningful metadata was found, else None.
    """
    try:
        await _acquire_domain_lock(url)
        async with network_slot("metadata", bucket=bucket_from_url(url), min_interval=0.1):
            resp = await client.get(url, follow_redirects=True)
        
        # --- Wayback Machine Fallback ---
        if resp.status_code in (404, 403, 410, 500, 502, 503, 504):
            try:
                async with network_slot("metadata", bucket="archive.org", min_interval=0.2):
                    wb_resp = await client.get(f"http://archive.org/wayback/available?url={url}")
                if wb_resp.status_code == 200:
                    wb_data = wb_resp.json()
                    archived = wb_data.get("archived_snapshots", {}).get("closest")
                    if archived and archived.get("available") and archived.get("url"):
                        # Swap to the historical snapshot!
                        async with network_slot("metadata", bucket=bucket_from_url(archived["url"]), min_interval=0.1):
                            resp = await client.get(archived["url"], follow_redirects=True)
            except Exception:
                pass
                
        if resp.status_code != 200:
            return None

        ct = resp.headers.get("content-type", "").lower()

        # --- Direct PDF Analyzer ---
        if "application/pdf" in ct or url.lower().endswith(".pdf") or url.lower().endswith(".pdf/"):
            try:
                import io
                from pdfminer.high_level import extract_text
                # Extract text from just the first 2 pages (the title page / header)
                text = extract_text(io.BytesIO(resp.content), maxpages=2)
                
                ref = Reference()
                ref.url = url
                
                # Hunt for DOI in the raw text
                import re
                m = re.search(r'10\.\d{4,}/[^\s"\'<>]+', text)
                if m:
                    ref.doi = m.group(0).rstrip('.,;)')
                    return ref
            except Exception as e:
                logger.debug("PDF parse failed %s: %s", url[:60], e)
            return None

        # Only parse HTML
        if "html" not in ct and "xhtml" not in ct:
            return None

        soup = BeautifulSoup(resp.text[:200_000], "html.parser")  # limit parse size
    except Exception as e:
        logger.debug("Scrape failed %s: %s", url[:60], e)
        return None

    def _meta(names: list) -> Optional[str]:
        for name in names:
            tag = soup.find("meta", attrs={"name": name})
            if not tag:
                tag = soup.find("meta", attrs={"property": name})
            if tag:
                val = tag.get("content", "").strip()
                if val:
                    return val
        return None

    def _meta_all(names: list) -> List[str]:
        vals = []
        for name in names:
            for tag in soup.find_all("meta", attrs={"name": name}):
                val = tag.get("content", "").strip()
                if val:
                    vals.append(val)
            for tag in soup.find_all("meta", attrs={"property": name}):
                val = tag.get("content", "").strip()
                if val:
                    vals.append(val)
        return vals

    ref = Reference()

    # Identifiers
    doi_str = _meta(_META_DOI_NAMES)
    if doi_str:
        # Clean DOI: may be a full URL
        doi_str = doi_str.replace("https://doi.org/", "").replace("http://doi.org/", "")
        doi_str = doi_str.replace("doi:", "").strip()
        if doi_str.startswith("10."):
            ref.doi = doi_str

    arxiv_str = _meta(_META_ARXIV_NAMES)
    if arxiv_str:
        ref.arxiv_id = arxiv_str.strip()

    pmid_str = _meta(_META_PMID_NAMES)
    if pmid_str:
        ref.pmid = pmid_str.strip()

    # Also try extracting from the URL itself
    if not ref.doi:
        ref.doi = _extract_doi_from_url(url)
    if not ref.arxiv_id:
        ref.arxiv_id = _extract_arxiv_from_url(url)
    if not ref.pmid:
        ref.pmid = _extract_pmid_from_url(url)

    # Title
    ref.title = _meta(_META_TITLE_NAMES)
    if not ref.title:
        title_tag = soup.find("title")
        if title_tag:
            ref.title = title_tag.get_text(strip=True)

    # Authors
    author_strs = _meta_all(_META_AUTHOR_NAMES)
    for a in author_strs[:20]:
        # Handle "Last, First" or "First Last" formats
        if "," in a:
            parts = a.split(",", 1)
            ref.authors.append(Author(family=parts[0].strip(), given=parts[1].strip()))
        else:
            parts = a.rsplit(" ", 1)
            if len(parts) == 2:
                ref.authors.append(Author(family=parts[1].strip(), given=parts[0].strip()))
            else:
                ref.authors.append(Author(family=a.strip()))

    # Year
    date_str = _meta(_META_YEAR_NAMES)
    if date_str:
        m = re.search(r"(\d{4})", date_str)
        if m:
            ref.year = int(m.group(1))

    # Abstract
    abstract = _meta(_META_ABSTRACT_NAMES)
    if abstract and len(abstract) > 30:
        ref.abstract = abstract

    # URL
    ref.url = url

    # Check if we got anything useful
    has_id = ref.doi or ref.arxiv_id or ref.pmid
    has_title = ref.title and len(ref.title) >= 5
    if not has_id and not has_title:
        return None

    ref.sources["web_search"] = 0.6  # lower confidence from web scraping
    return ref


# ---------------------------------------------------------------------------
# Main search function
# ---------------------------------------------------------------------------

async def web_search_refs(
    refs: List[Reference],
    google_cse_key: str = "",
    google_cse_id: str = "",
    concurrency: int = _SEARCH_CONCURRENCY,
) -> Dict[str, List[Tuple[Reference, float]]]:
    """
    Search the web for hard-to-find references.

    For each ref, search by title, scan result URLs for identifiers,
    and scrape metadata from landing pages.

    Args:
        refs: References to search for (must have _batch_id set).
        google_cse_key: Optional Google Custom Search API key.
        google_cse_id: Optional Google CSE engine ID.

    Returns:
        Dict mapping ref._batch_id -> list of (Reference, confidence) pairs.
    """
    results: Dict[str, List[Tuple[Reference, float]]] = {}

    if not refs:
        return results

    search_sem = SafeSemaphore(concurrency)
    fetch_sem = SafeSemaphore(_PAGE_FETCH_CONCURRENCY)

    async with httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
        follow_redirects=True,
    ) as client:

        async def _process_one(ref: Reference):
            async with search_sem:
                if not ref.title or len(ref.title) < 5:
                    return

                # Build search query — bias toward academic results
                base_title = clean_query_title(ref.title) or ref.title
                queries = []
                if ref.authors:
                    first_author = ref.authors[0].family or ref.authors[0].given or ""
                    if first_author:
                        queries.append(f'"{base_title}" {first_author}')
                elif len(ref.title) < 60:
                    # Short titles: wrap in quotes for exact match, add academic hint
                    queries.append(f'"{base_title}" paper OR DOI')
                queries.append(f'"{base_title}"')
                if len(base_title) < 80:
                    queries.append(f'"{base_title}" filetype:pdf')

                # Search
                use_google = bool(google_cse_key and google_cse_id)
                search_results = []
                seen_urls = set()
                for query in queries[:3]:
                    if use_google:
                        found = await _search_google_cse(
                            query, client, google_cse_key, google_cse_id
                        )
                    else:
                        found = await _search_duckduckgo(query, client)
                    for sr in found:
                        url = sr.get("url", "")
                        if url and url not in seen_urls:
                            search_results.append(sr)
                            seen_urls.add(url)
                    if len(search_results) >= 10:
                        break

                if not search_results:
                    return

                for sr in search_results[:8]:
                    candidate = _search_result_candidate(ref, sr)
                    if candidate:
                        results.setdefault(ref._batch_id, []).append(candidate)
                        break

                # Phase 1: Extract identifiers from result URLs
                found_ids = False
                for sr in search_results:
                    ids = _extract_ids_from_url(sr["url"])
                    if ids["doi"] or ids["arxiv_id"] or ids["pmid"]:
                        scraped = Reference()
                        scraped.doi = ids["doi"]
                        scraped.arxiv_id = ids["arxiv_id"]
                        scraped.pmid = ids["pmid"]
                        scraped.title = sr.get("title")
                        scraped.url = sr["url"]
                        scraped.sources["web_search"] = 0.7

                        if ref._batch_id not in results:
                            results[ref._batch_id] = []
                        results[ref._batch_id].append((scraped, 0.7))
                        found_ids = True
                        break  # one good identifier is enough

                # Phase 2: If no identifiers found, scrape top pages for metadata
                if not found_ids:
                    # Filter to likely academic pages
                    academic_domains = [
                        "edu", "ac.uk", "ac.jp", "uni-", "university",
                        "scholar", "academia", "researchgate", "ssrn",
                        "philpapers", "philarchive", "jstor", "springer", "wiley",
                        "elsevier", "sciencedirect", "nature.com",
                        "pnas.org", "ams.org", "ieee", "acm.org",
                        "hal.science", "hal.archives", "repec", "nber",
                        "plato.stanford", "sep.", "iep.utm",  # philosophy encyclopedias
                        "tandfonline", "oxfordacademic", "cambridge.org",
                        "degruyter", "muse.jhu", "projecteuclid",
                        "dblp.org", "arxiv.org", "biorxiv",
                    ]
                    candidates = []
                    for sr in search_results[:8]:
                        url_lower = sr["url"].lower()
                        is_academic = any(d in url_lower for d in academic_domains)
                        # Also accept any .pdf link
                        is_pdf = url_lower.endswith(".pdf")
                        if is_academic or is_pdf:
                            candidates.append(sr)
                    # Also try the top 2 results regardless
                    for sr in search_results[:2]:
                        if sr not in candidates:
                            candidates.append(sr)

                    for sr in candidates[:3]:
                        async with fetch_sem:
                            scraped = await _scrape_page_metadata(sr["url"], client)
                            if scraped and (scraped.doi or scraped.arxiv_id or scraped.pmid
                                           or (scraped.title and scraped.authors)):
                                if ref._batch_id not in results:
                                    results[ref._batch_id] = []
                                conf = 0.7 if (scraped.doi or scraped.arxiv_id) else 0.5
                                results[ref._batch_id].append((scraped, conf))
                                break  # found something useful

                # Small delay to be polite
                await asyncio.sleep(0.5)

        tasks = [_process_one(ref) for ref in refs]
        await asyncio.gather(*tasks, return_exceptions=True)

    resolved = sum(1 for v in results.values() if v)
    logger.info("Web search: resolved %d/%d refs", resolved, len(refs))

    return results
