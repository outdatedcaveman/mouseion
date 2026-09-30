"""A paper trail on the open web for entries the scholarly indexes don't know.

Owner's requirement (2026-09-30): every entry gets at least a full web search, so
that each has a URL to it -- a personal page, a small journal, a library record --
and "rich strings" are understood before they are searched ('LNCS 3796' is volume
3796 of Lecture Notes in Computer Science, i.e. a specific book).

Pipeline per entry:
  1. understand the string: expand series/journal abbreviations, join words split
     by lost ligatures ('Artif icial'), strip LaTeX debris;
  2. search the web (DuckDuckGo's HTML endpoint, politely paced; a throttled answer
     pauses the run -- nothing is ever solved or bypassed);
  3. keep only results that ARE the entry: the entry's title words (or series +
     volume) and an author's surname appear in the result's title/snippet/URL;
     shadow libraries never count as a trail;
  4. harvest identifiers from the kept results (DOI, arXiv, JSTOR stable id, ISBN in
     Amazon/Google Books/WorldCat links) and resolve them to a full record, which
     must agree with the entry again; otherwise the best kept page becomes the
     entry's URL, with its title/snippet/query saved as evidence in extras.web_trail.
"""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

# ------------------------------------------------------------------ 1. understand
SERIES = {
    "lncs": "Lecture Notes in Computer Science", "lnai": "Lecture Notes in Artificial Intelligence",
    "lnbi": "Lecture Notes in Bioinformatics", "lnm": "Lecture Notes in Mathematics",
    "lnp": "Lecture Notes in Physics", "lnl": "Lecture Notes in Logic", "lnee": "Lecture Notes in Electrical Engineering",
    "lnbip": "Lecture Notes in Business Information Processing", "ccis": "Communications in Computer and Information Science",
    "gtm": "Graduate Texts in Mathematics", "utm": "Undergraduate Texts in Mathematics",
    "eptcs": "Electronic Proceedings in Theoretical Computer Science",
    "lipics": "Leibniz International Proceedings in Informatics",
    "entcs": "Electronic Notes in Theoretical Computer Science", "bsps": "Boston Studies in the Philosophy of Science",
    "sl": "Synthese Library", "lms": "London Mathematical Society Lecture Note Series",
    "ams": "American Mathematical Society", "cup": "Cambridge University Press", "oup": "Oxford University Press",
}
JOURNALS = {
    "jsl": "Journal of Symbolic Logic", "bsl": "Bulletin of Symbolic Logic", "ndjfl": "Notre Dame Journal of Formal Logic",
    "bjps": "British Journal for the Philosophy of Science", "jpl": "Journal of Philosophical Logic",
    "psa": "PSA: Proceedings of the Biennial Meeting of the Philosophy of Science Association",
    "apal": "Annals of Pure and Applied Logic", "mlq": "Mathematical Logic Quarterly", "rsl": "Review of Symbolic Logic",
    "shpmp": "Studies in History and Philosophy of Modern Physics", "shps": "Studies in History and Philosophy of Science",
    "jmp": "Journal of Mathematical Physics", "prl": "Physical Review Letters", "tcs": "Theoretical Computer Science",
}
SERIES_VOL = re.compile(r"^\s*(" + "|".join(sorted(SERIES, key=len, reverse=True)) + r")\.?\s*(?:vol(?:ume)?\.?\s*)?(\d{1,5})\b",
                        re.I)
LATEX = re.compile(r"\b(textbraceleft|textbraceright|textbackslash|mathbf|mathrm|mathcal|mathbb|textit|textbf|emph|"
                   r"textasciicircum|textunderscore|textendash|textemdash)\b|[{}$\\^]")
LATEX_FONT = re.compile(r"(?:textbraceleft|\\|\{)\s*(?:rm|bf|it|sf|tt|cal|sl|sc|sharp)\b")
# lost ligatures: "Artif icial", "Classif ication", "ef fect", "suf ficient"
LIGATURE = re.compile(r"\b(\w*(?:f|ff))\s+((?:i|l|fi|fl)[a-z]+)\b")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def fix_ligatures(s: str, known: Optional[set] = None) -> str:
    def join(m):
        w = m.group(1) + m.group(2)
        if known is None or norm(w) in known:
            return w
        return m.group(0)
    return LIGATURE.sub(join, s)


def understand(title: str, known: Optional[set] = None) -> Dict[str, str]:
    """{'title': cleaned title, 'series': full series name, 'volume': n} for a raw stored title."""
    t = LATEX_FONT.sub(" ", title or "")        # "textbraceleft rm S textbraceright": the font switch goes too
    t = LATEX.sub(" ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = fix_ligatures(" ".join(t.split()), known)
    t = re.sub(r"^\[([^\]]{4,80})\]\s*", r"\1 ", t).strip()        # "[Lecture Notes in ... 1838] LNCS 1838"
    out = {"title": t}
    m = SERIES_VOL.match(t)
    if m:
        out["series"] = SERIES[m.group(1).lower()]
        out["volume"] = m.group(2)
        rest = t[m.end():].strip(" :-,.")
        out["title"] = rest
    else:
        for full in SERIES.values():                 # "Lecture Notes in Artificial Intelligence 2831"
            m = re.match(re.escape(full) + r"\s*(?:vol(?:ume)?\.?\s*)?(\d{1,5})\b\s*[:,.-]?\s*(.*)$", t, re.I)
            if m:
                out.update(series=full, volume=m.group(1), title=m.group(2).strip())
                break
    return out


def expand_journal(j: str) -> str:
    k = norm(j).replace(" ", "")
    return JOURNALS.get(k, j or "")


# ------------------------------------------------------------------ 2. search
SHADOW = re.compile(r"(libgen|library\.lol|genesis|booksee|z-?lib|1lib|sci-hub|scihub|annas-archive|pdfdrive|dokumen\.pub|"
                    r"vdoc\.pub|epdf\.|ebin\.pub|doku\.pub|pdfcoffee|dokumen\.tips|idoc\.pub|kupdf|silo\.pub|"
                    r"pdfroom|freebookcentre|dlscrib|studylib|documents\.pub|vdocuments|b-ok\.|bookfi|ebooksclub)", re.I)
NOISE = re.compile(r"(pinterest\.|facebook\.com|twitter\.com|x\.com/|instagram\.com|tiktok\.com|youtube\.com|"
                   r"reddit\.com|quora\.com|linkedin\.com/posts)", re.I)
RANK = [  # higher is better
    (re.compile(r"doi\.org/"), 10), (re.compile(r"(jstor\.org|philpapers\.org|projecteuclid|ams\.org|springer|"
     r"cambridge\.org|oup\.com|academic\.oup|wiley\.com|tandfonline|sciencedirect|muse\.jhu|degruyter|"
     r"journals\.|pdcnet\.org|persee\.fr|cairn\.info|erudit\.org|scielo)"), 9),
    (re.compile(r"(arxiv\.org|hal\.science|hal\.archives|ssrn\.com|zenodo|osf\.io|philarchive|core\.ac\.uk|"
                r"semanticscholar|openalex|europepmc|ncbi\.nlm)"), 8),
    (re.compile(r"(\.edu/|\.ac\.[a-z]{2}/|\.edu\.[a-z]{2}/|uni-[a-z]+\.de|\.univ-|repositor|dspace|eprints)"), 7),
    (re.compile(r"(worldcat\.org|openlibrary\.org|books\.google|archive\.org|catalog|biblio|library)"), 6),
    (re.compile(r"(researchgate\.net|academia\.edu)"), 5),
    (re.compile(r"(amazon\.|goodreads|abebooks|bookfinder)"), 3),
]


def rank(url: str) -> int:
    for rx, r in RANK:
        if rx.search(url):
            return r
    return 4


class Throttled(Exception):
    pass


@dataclass
class Hit:
    title: str
    url: str
    snippet: str


class Searcher:
    """DuckDuckGo's HTML endpoint, paced. Throttling raises Throttled: the caller waits,
    it never works around a check."""

    def __init__(self, min_gap: float = 2.5) -> None:
        self.gap = min_gap
        self.last = 0.0
        self.client = httpx.Client(timeout=25, headers=_UA, follow_redirects=True)

    def search(self, q: str) -> List[Hit]:
        wait = self.last + self.gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        try:
            r = self.client.post("https://html.duckduckgo.com/html/", data={"q": q})
        except httpx.HTTPError as e:
            raise Throttled(f"network: {e}") from e
        if r.status_code in (202, 403, 429) or "anomaly-modal" in r.text:
            raise Throttled(f"HTTP {r.status_code}")
        from bs4 import BeautifulSoup
        s = BeautifulSoup(r.text, "html.parser")
        out = []
        for res in s.select("div.result"):
            a = res.select_one("a.result__a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            if "duckduckgo.com/l/" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
            if href.startswith("//"):
                href = "https:" + href
            sn = res.select_one(".result__snippet")
            out.append(Hit(a.get_text(" ", strip=True), href, sn.get_text(" ", strip=True) if sn else ""))
        return out


# Search APIs the owner signed up for. Each has an allowance; the pool spends them
# evenly and never past the free amount (usage kept in web_search_budget.json).
class Exhausted(Exception):
    pass


class _Backend:
    name = ""
    limit = 0            # searches per period
    period = "month"     # "month" | "lifetime"

    def __init__(self, key: str) -> None:
        self.key = key
        self.client = httpx.Client(timeout=40, follow_redirects=True)

    def _check(self, r: httpx.Response) -> None:
        if r.status_code in (401, 402, 403) or (r.status_code == 429 and "quota" in r.text.lower()):
            raise Exhausted(f"{self.name}: HTTP {r.status_code}")
        if r.status_code == 429 or r.status_code >= 500:
            raise Throttled(f"{self.name}: HTTP {r.status_code}")
        r.raise_for_status()


class Serper(_Backend):
    name, limit, period = "serper", 2450, "lifetime"          # 2,500 free, once

    def search(self, q: str) -> Tuple[List[Hit], int]:
        r = self.client.post("https://google.serper.dev/search", headers={"X-API-KEY": self.key},
                             json={"q": q, "num": 10})
        self._check(r)
        return [Hit(x.get("title", ""), x.get("link", ""), x.get("snippet", "")) for x in r.json().get("organic", [])], 1


class Tavily(_Backend):
    name, limit = "tavily", 980                                # 1,000 free credits a month

    def search(self, q: str) -> Tuple[List[Hit], int]:
        r = self.client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.key}"},
                             json={"query": q, "max_results": 10, "search_depth": "basic"})
        self._check(r)
        return [Hit(x.get("title", ""), x.get("url", ""), (x.get("content") or "")[:500])
                for x in r.json().get("results", [])], 1


class Brave(_Backend):
    # $5 monthly credit = 1,000 searches. Brave's billing month need not start on the 1st: half per
    # calendar month keeps ANY 30-day window under the credit (owner: not a single extra cent)
    name, limit = "brave", 450

    def search(self, q: str) -> Tuple[List[Hit], int]:
        r = self.client.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": 10},
                            headers={"X-Subscription-Token": self.key, "Accept": "application/json"})
        self._check(r)
        return [Hit(x.get("title", ""), x.get("url", ""), x.get("description", ""))
                for x in (r.json().get("web") or {}).get("results", [])], 1


class GeminiGoogle(_Backend):
    """Gemini with Grounding with Google Search: Google's own results. The model's prose is
    ignored -- only the pages Google returned (grounding chunks) are used, and each is
    checked like any other hit. Billed per search the model runs: counted from
    webSearchQueries, capped at the 5,000 free a month."""
    name, limit = "gemini-google", 2400          # 5,000 free a month; half per calendar month (see Brave)
    MODEL = "gemini-3.5-flash-lite"
    USD_CAP = 0.45          # a calendar month of model fees from the prepaid credit (owner's $1 cap, halved)

    def search(self, q: str) -> Tuple[List[Hit], int]:
        body = {"contents": [{"parts": [{"text": f"Find web pages about this scholarly work: {q}. "
                                                   f"Search the web and name the sites."}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"thinkingConfig": {"thinkingLevel": "minimal"}, "maxOutputTokens": 256}}
        r = self.client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{self.MODEL}:generateContent",
                             params={"key": self.key}, json=body)
        self._check(r)
        data = r.json()
        um = data.get("usageMetadata") or {}
        # model fees (the search itself is free up to 5,000 a month): $0.30/M in, $2.50/M out
        self.last_cost = (um.get("promptTokenCount", 0) * 0.30 +
                          (um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)) * 2.50) / 1e6
        cand = (data.get("candidates") or [{}])[0]
        gm = cand.get("groundingMetadata") or {}
        used = len(gm.get("webSearchQueries") or [])
        hits = []
        for ch in (gm.get("groundingChunks") or [])[:8]:
            url = (ch.get("web") or {}).get("uri", "")
            try:                          # grounding links are Google redirects: follow to the real page
                h = self.client.get(url, follow_redirects=False, timeout=15)
                url = h.headers.get("location", url)
            except Exception:
                continue
            title, desc = page_title(self.client, url)
            if title:
                hits.append(Hit(title, url, desc))
        return hits, used


def page_title(client: httpx.Client, url: str) -> Tuple[str, str]:
    """(title, description) of a page: citation_title / og:title / <title>."""
    if not url.startswith("http") or SHADOW.search(url):
        return "", ""
    try:
        r = client.get(url, headers=_UA, timeout=20, follow_redirects=True)
        html = r.text[:200000] if "html" in r.headers.get("content-type", "") else ""
    except Exception:
        return "", ""
    if not html:
        return "", ""

    def meta(name):
        m = re.search(r'<meta[^>]+(?:name|property)=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']+)', html, re.I) or \
            re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']' + re.escape(name) + r'["\']', html, re.I)
        return m.group(1).strip() if m else ""
    import html as _h
    t = meta("citation_title") or meta("dc.title") or meta("og:title")
    if not t:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        t = m.group(1).strip() if m else ""
    authors = " ".join(re.findall(r'<meta[^>]+name=["\']citation_author["\'][^>]+content=["\']([^"\']+)', html, re.I)[:6])
    desc = meta("description") or meta("og:description")
    return _h.unescape(" ".join(t.split()))[:300], _h.unescape(f"{authors} {desc}".strip())[:500]


class DuckDuckGo(_Backend):
    name, limit = "duckduckgo", 10 ** 9

    def __init__(self, key: str = "") -> None:
        super().__init__(key)
        self.s = Searcher(min_gap=8.0)

    def search(self, q: str) -> Tuple[List[Hit], int]:
        return self.s.search(q), 1


class SearchPool:
    """Spend every configured allowance evenly; a throttled engine rests an hour; when
    every allowance is used up, Exhausted stops the run (resumable next period)."""

    def __init__(self, backends: List[_Backend], budget_file) -> None:
        from pathlib import Path as _P
        self.backends = backends
        self.file = _P(budget_file)
        self.resting: Dict[str, float] = {}
        try:
            import json as _j
            self.used = _j.loads(self.file.read_text(encoding="utf-8"))
        except Exception:
            self.used = {}

    # OWNER'S RULE (2026-09-30): "not a single extra cent" -- "just stay within the free usage".
    # Serper and Tavily: free plans, no card (past the allowance they refuse). Brave: metered
    # plan with a $5 monthly credit -> 900 searches a month, under the credit. Gemini: search
    # free to 5,000 a month -> 4,900; its small model fee comes from the PREPAID credit
    # (auto-reload off: the card is never charged), metered from usageMetadata, $0.90 a month.
    FREE_ONLY = (("serper_api_key", Serper), ("tavily_api_key", Tavily))
    METERED = (("gemini_search_api_key", GeminiGoogle), ("brave_api_key", Brave))

    @classmethod
    def from_config(cls, cfg, budget_file, use_ddg: bool = True, allow_metered: bool = True) -> "SearchPool":
        bs: List[_Backend] = []
        for attr, klass in cls.FREE_ONLY + (cls.METERED if allow_metered else ()):
            key = (getattr(cfg, attr, "") or "").strip()
            if key:
                bs.append(klass(key))
        if use_ddg:
            bs.append(DuckDuckGo())
        return cls(bs, budget_file)

    def _slot(self, b: _Backend) -> str:
        return "lifetime" if b.period == "lifetime" else time.strftime("%Y-%m")

    def remaining(self, b: _Backend) -> int:
        left = b.limit - self.used.get(b.name, {}).get(self._slot(b), 0)
        cap = getattr(b, "USD_CAP", None)
        if cap is not None and self.used.get(b.name + "-usd", {}).get(self._slot(b), 0.0) >= cap:
            return 0
        return left

    def _spend(self, b: _Backend, n: int) -> None:
        import json as _j
        slot = self._slot(b)
        self.used.setdefault(b.name, {})[slot] = self.used.get(b.name, {}).get(slot, 0) + n
        try:
            self.file.write_text(_j.dumps(self.used, indent=1), encoding="utf-8")
        except Exception:
            pass

    def status(self) -> Dict[str, int]:
        return {b.name: self.remaining(b) for b in self.backends if b.name != "duckduckgo"}

    LAST_RESORT = ("gemini-google",)       # costs a little prepaid credit: only when the others found nothing

    def has(self, names) -> bool:
        now = time.time()
        return any(b.name in names and self.remaining(b) > 0 and self.resting.get(b.name, 0) < now
                   for b in self.backends)

    def search(self, q: str, only=None) -> List[Hit]:
        now = time.time()
        live = [b for b in self.backends if self.remaining(b) > 0 and self.resting.get(b.name, 0) < now
                and (b.name in only if only else b.name not in self.LAST_RESORT)]
        if not live:
            raise Exhausted("every search allowance is used up (or resting)")
        keyed = [b for b in live if b.name != "duckduckgo"]
        b = max(keyed, key=lambda x: self.remaining(x) / max(1, x.limit)) if keyed else live[0]
        try:
            hits, n = b.search(q)
        except Exhausted:
            self.used.setdefault(b.name, {})[self._slot(b)] = b.limit
            return self.search(q, only)
        except Exception:                  # throttled, network, or an unexpected answer: rest it an hour
            self.resting[b.name] = now + 3600
            return self.search(q, only)
        self._spend(b, n)
        cost = getattr(b, "last_cost", 0.0) or 0.0
        if cost:
            import json as _j
            slot = self._slot(b)
            d = self.used.setdefault(b.name + "-usd", {})
            d[slot] = round(d.get(slot, 0.0) + cost, 6)
            try:
                self.file.write_text(_j.dumps(self.used, indent=1), encoding="utf-8")
            except Exception:
                pass
        return hits


# ------------------------------------------------------------------ 3. is it the entry?
def coverage(title: str, text: str) -> float:
    words = [w for w in norm(title).split() if len(w) > 3] or [w for w in norm(title).split() if len(w) > 1]
    if not words:
        return 0.0
    hay = norm(text)
    hay_words = set(hay.split())
    return sum(1 for w in words if w in hay_words) / len(words)


GENERIC_PAGE = re.compile(r"^\s*(browse|search( results)?|home|index|category|tag|archives?|issue|volume|table of contents|"
                          r"all issues|current issue|results for)\b", re.I)


def _title_candidates(hit_title: str) -> List[str]:
    """The work's title inside a result title: 'Logic Is Not Occultism - A. Kootte - PhilPapers',
    'John R. Searle, Reply to Jacquette's ...', 'SCIENCE AS DISCOVERY | JSTOR'."""
    t = re.sub(r"<[^>]+>", " ", hit_title or "")
    t = re.sub(r"^\s*\((?:pdf|doc|html?)\)\s*", "", t, flags=re.I)
    segs = [x.strip() for x in re.split(r"\s+[|–—]\s+|\s+-\s+|\s+::\s+", t) if x.strip()]
    out = list(segs)
    for x in segs:                      # "Firstname Lastname, Title" (PhilPapers) / "Title by Author"
        m = re.match(r"^(?:[A-Z][\w.'’\-]*\s){1,4}[A-Z][\w'’\-]+,\s+(.{8,})$", x)
        if m:
            out.append(m.group(1))
        m = re.match(r"^(.{8,}?)\s+by\s+[A-Z]", x)
        if m:
            out.append(m.group(1))
    return out or [t]


def title_score(entry_title: str, hit_title: str) -> float:
    """How surely the result's title IS the entry's title (0..1)."""
    ne = norm(entry_title)
    if len(ne.split()) < 2:
        return 0.0
    best = 0.0
    for c in _title_candidates(hit_title):
        trunc = c.rstrip().endswith(("...", "…"))
        nc = norm(c)
        if not nc or GENERIC_PAGE.match(c):
            continue
        sim = difflib.SequenceMatcher(None, ne, nc).ratio()
        if len(ne.split()) >= 3 and len(ne) < len(nc) and ne in nc:       # stored title cut off
            sim = max(sim, 0.9 if len(ne) >= 0.5 * len(nc) else 0.8)
        if len(ne.split()) >= 3 and nc.startswith(ne):                    # stored title cut off at the end
            sim = max(sim, 0.9)
        if trunc and len(nc.split()) >= 4 and ne.startswith(nc):           # the engine cut the result title
            sim = max(sim, 0.9)
        best = max(best, sim)
    return best


def matches(entry: Dict, hit: Hit) -> float:
    """0..1 confidence that the page is about this entry (0 = reject)."""
    if SHADOW.search(hit.url) or NOISE.search(hit.url):
        return 0.0
    text = f"{hit.title} {hit.snippet} {unquote(hit.url)}"
    surnames = [norm(s).split()[-1] for s in entry.get("surnames", []) if norm(s)]
    found = [s for s in surnames if re.search(rf"\b{re.escape(s)}\b", norm(f"{hit.title} {hit.snippet}"))]
    if surnames and not found:
        found = [s for s in surnames if re.search(rf"\b{re.escape(s)}\b", norm(unquote(hit.url)))]
    if surnames and not found:
        return 0.0
    # a surname that is also an ordinary word ('Home', 'Young') proves little: then the title must be near-exact
    weak = bool(found) and all(s in entry.get("common_words", ()) for s in found)
    if entry.get("series") and entry.get("volume"):
        ser_ok = coverage(entry["series"], text) >= 0.75
        vol_ok = re.search(rf"(?<!\d){entry['volume']}(?!\d)", text) is not None
        if ser_ok and vol_ok and not GENERIC_PAGE.match(hit.title or ""):
            return 0.8 + (0.2 * coverage(entry["title"], text) if entry.get("title") else 0.0)
        if not entry.get("title"):
            return 0.0
    sc = title_score(entry.get("title") or "", hit.title)
    return sc if sc >= (0.95 if weak else 0.85) else 0.0


# ------------------------------------------------------------------ 4. identifiers
DOI_IN_URL = re.compile(r"(?:doi\.org/|/doi/(?:abs/|full/|pdf/|epdf/)?|doi=)(10\.\d{4,9}/[^\s?#&\"'<>]+)", re.I)
ARXIV_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", re.I)
JSTOR_IN_URL = re.compile(r"jstor\.org/stable/(?:10\.2307/)?(\d{4,9})\b")
ISBN_IN_URL = re.compile(r"(?:amazon\.[a-z.]+/(?:[^/]+/)?(?:dp|gp/product)/|isbn[=/:]|/isbn/)(\d{9}[\dXx]|\d{13})\b")


def identifiers(hit: Hit) -> Dict[str, str]:
    url = unquote(hit.url)
    out = {}
    m = DOI_IN_URL.search(url)
    if m:
        out["doi"] = m.group(1).rstrip(".,;)").lower()
    m = ARXIV_IN_URL.search(url)
    if m:
        out["arxiv"] = m.group(1)
    m = JSTOR_IN_URL.search(url)
    if m and "doi" not in out:
        out["jstor"] = m.group(1)
    m = ISBN_IN_URL.search(url)
    if m:
        out["isbn"] = m.group(1).upper()
    return out


@dataclass
class Trail:
    result: str                      # record | url | none | throttled
    url: str = ""
    evidence: Dict[str, str] = field(default_factory=dict)
    ids: Dict[str, str] = field(default_factory=dict)


def queries(entry: Dict) -> List[str]:
    """Most specific first. Entry: title, surnames, year, journal, series, volume."""
    s0 = entry.get("surnames", [])[:1]
    qs = []
    if entry.get("series") and entry.get("volume"):
        base = f'"{entry["series"]}" {entry["volume"]}'
        qs.append(f'{base} {entry["title"]}' if entry.get("title") else base)
        if entry.get("title"):
            qs.append(f'"{entry["title"]}" {" ".join(s0)}')
    else:
        t = entry.get("title", "")
        if len(norm(t).split()) >= 3:
            qs.append(f'"{t}" {" ".join(s0)}'.strip())
        qs.append(" ".join([t, " ".join(entry.get("surnames", [])[:2]), expand_journal(entry.get("journal", "")),
                            str(entry.get("year") or "")]).strip())
    return [" ".join(q.split())[:300] for q in dict.fromkeys(qs) if q.strip()]


def find_trail(entry: Dict, searcher: Searcher, min_conf: float = 0.7) -> Trail:
    best: Optional[Tuple[float, int, Hit, str]] = None
    ids: Dict[str, str] = {}
    for q in queries(entry):
        hits = searcher.search(q)
        for h in hits[:10]:
            conf = matches(entry, h)
            if conf < min_conf:
                continue
            for k, v in identifiers(h).items():
                ids.setdefault(k, v)
            key = (conf, rank(h.url))
            if best is None or key > (best[0], best[1]):
                best = (conf, rank(h.url), h, q)
        if best is not None and (best[1] >= 7 or ids):
            break                     # a scholarly page or an identifier: no second query needed
    if best is None and hasattr(searcher, "has") and searcher.has(SearchPool.LAST_RESORT):
        q = queries(entry)[-1]
        try:
            for h in searcher.search(q, only=SearchPool.LAST_RESORT)[:8]:
                conf = matches(entry, h)
                if conf >= min_conf and (best is None or (conf, rank(h.url)) > (best[0], best[1])):
                    best = (conf, rank(h.url), h, q + " [gemini]")
                    for k, v in identifiers(h).items():
                        ids.setdefault(k, v)
        except Exhausted:
            pass
    if best is None:
        return Trail("none")
    conf, rk, h, q = best
    return Trail("url", h.url, {"title": h.title[:300], "snippet": h.snippet[:500], "query": q,
                                "confidence": f"{conf:.2f}"}, ids)
