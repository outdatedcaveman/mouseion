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


# ------------------------------------------------------------------ 3. is it the entry?
def coverage(title: str, text: str) -> float:
    words = [w for w in norm(title).split() if len(w) > 3] or [w for w in norm(title).split() if len(w) > 1]
    if not words:
        return 0.0
    hay = norm(text)
    hay_words = set(hay.split())
    return sum(1 for w in words if w in hay_words) / len(words)


def matches(entry: Dict, hit: Hit) -> float:
    """0..1 confidence that the page is about this entry (0 = reject)."""
    if SHADOW.search(hit.url) or NOISE.search(hit.url):
        return 0.0
    text = f"{hit.title} {hit.snippet} {unquote(hit.url)}"
    surnames = [norm(s).split()[-1] for s in entry.get("surnames", []) if norm(s)]
    author_ok = not surnames or any(re.search(rf"\b{re.escape(s)}\b", norm(text)) for s in surnames)
    if not author_ok:
        return 0.0
    if entry.get("series") and entry.get("volume"):
        ser_ok = coverage(entry["series"], text) >= 0.75
        vol_ok = re.search(rf"(?<!\d){entry['volume']}(?!\d)", text) is not None
        if ser_ok and vol_ok:
            return 0.8 + (0.2 * coverage(entry["title"], text) if entry.get("title") else 0.0)
        if not entry.get("title"):
            return 0.0
    t = entry.get("title") or ""
    if len(norm(t).split()) < 2:
        return 0.0
    cov_title = coverage(t, f"{hit.title} {unquote(hit.url)}")
    cov_all = coverage(t, text)
    if cov_title >= 0.7 or (cov_all >= 0.85 and len(norm(t).split()) >= 3):
        return max(cov_title, cov_all * 0.95)
    return 0.0


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
    if best is None:
        return Trail("none")
    conf, rk, h, q = best
    return Trail("url", h.url, {"title": h.title[:300], "snippet": h.snippet[:500], "query": q,
                                "confidence": f"{conf:.2f}"}, ids)
