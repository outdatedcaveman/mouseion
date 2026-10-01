"""Identify entries whose stored title is junk, from EVERYTHING they carry (owner, 2026-10-01:
"if it has ANY other info besides the title ... that already helps us ... start by searching the
pdf for hints ... my guess is that 99% can be resolved").

Evidence, cheapest and most certain first:
  1. the PDF itself: printed title, author line, DOI/arXiv/AMS id, year (pdf_ingest.extract/resolve)
  2. the stored web address and extras.original_url: the page's citation_* / Dublin Core / og meta,
     or the PDF behind it
  3. extras.original_page_title (the title the page had when it was saved), a stored DOI
  4. web search (free allowances, mouseion.web_trail.SearchPool) built from the best evidence:
     the PDF's title, the exact file name ("Report24W5237"), a quoted sentence of the abstract,
     the original page title, author + year + the citation key's title word ("haug2020no").
A search result is ACCEPTED only on strong evidence:
  - the file name / stored URL's file part appears in the result's address
  - the abstract's wording appears in the result (8+ consecutive words, or 60% of its opening)
  - the result's title is printed on the PDF's first pages
  - surname AND year AND the citation key's title word all agree
Anything weaker is kept as a SUGGESTION for the review page, never written.

Accepted identities become records (DOI -> Crossref; else a Crossref/OpenAlex title search that
must agree), and fill the entry through pdf_ingest.fill_plan (junk title replaced, empty fields
filled), with the evidence in extras.identified. Backups in identify_bak_<date>; ledger
identify_scan (resumable).

Usage: python scripts/identify_entries.py <ids.json> <dry|write> [limit] [--no-search] [--show N]
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import httpx  # noqa: E402

from mouseion import pdf_ingest as PI  # noqa: E402
from mouseion import web_trail as W  # noqa: E402
from mouseion.config import get_config  # noqa: E402
from mouseion.db import RefDatabase  # noqa: E402
from mouseion.models import Author, Reference  # noqa: E402
sys.path.insert(0, str(REPO / "scripts"))
import junk_candidates as J  # noqa: E402

BOT_PAGE = re.compile(r"(checking your browser|recaptcha|just a moment|attention required|wayback machine|client challenge|"
                      r"verify(ing)? you are human|security check|ddos|are you a robot|one more step|"
                      r"access denied|page not found|\b404\b|cloudflare|sign in|log ?in\b|redirecting|"
                      r"error\b|forbidden|not available|robot|captcha|cookies?)", re.I)


def good_title(t: str) -> bool:
    """A found title must itself be a work: not junk by the same test, not a bot/archive page."""
    t = (t or "").strip()
    return len(words(t)) >= 2 and not J.reason(t, set(), Counter()) and not BOT_PAGE.search(t) \
        and not W.GENERIC_PAGE.match(t)

args = [a for a in sys.argv[1:] if not a.startswith("--")]
IDS = Path(args[0])
WRITE = len(args) > 1 and args[1] == "write"
LIMIT = int(args[2]) if len(args) > 2 else 10 ** 9
SEARCH = "--no-search" not in sys.argv
SHOW = int(sys.argv[sys.argv.index("--show") + 1]) if "--show" in sys.argv else 60
STAMP = date.today().strftime("%Y%m%d")
CFG = get_config()
MAILTO = CFG.crossref_email or CFG.openalex_email or ""
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"}
GENERIC_STEMS = re.compile(r"^(paper|main|download|document|file|article|pdf|fulltext|full[-_ ]?text|view|index|"
                           r"manuscript|preprint|draft|untitled|scan|doc|chapter|book|slides?|report|print)\d{0,3}$", re.I)


# ------------------------------------------------------------------ evidence
def meta_from_html(html: str) -> dict:
    def metas(name):
        out = re.findall(r'<meta[^>]+(?:name|property)=["\']' + re.escape(name) + r'["\'][^>]+content=["\']([^"\']+)', html, re.I)
        out += re.findall(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']' + re.escape(name) + r'["\']', html, re.I)
        return [x.strip() for x in out if x.strip()]
    import html as H
    first = lambda xs: H.unescape(xs[0]) if xs else ""  # noqa: E731
    title = first(metas("citation_title")) or first(metas("dc.title")) or first(metas("DC.title")) or first(metas("og:title"))
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = H.unescape(" ".join(m.group(1).split())) if m else ""
    return {"title": title[:400], "authors": [H.unescape(a) for a in metas("citation_author")][:20],
            "doi": first(metas("citation_doi")) or first(metas("dc.identifier")) if "10." in (first(metas("citation_doi")) or first(metas("dc.identifier"))) else "",
            "date": first(metas("citation_publication_date")) or first(metas("citation_date")) or first(metas("dc.date")),
            "journal": first(metas("citation_journal_title")), "pdf_url": first(metas("citation_pdf_url")),
            "description": (first(metas("citation_abstract")) or first(metas("description")) or first(metas("og:description")))[:1500]}


def fetch_meta(client: httpx.Client, url: str) -> dict:
    """What a web address says about its work: page meta, or the PDF behind it."""
    if not url.startswith("http") or W.SHADOW.search(url):
        return {}
    try:
        r = client.get(url, timeout=25, follow_redirects=True)
    except Exception:
        return {}
    if r.status_code != 200:
        return {}
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or r.content[:4] == b"%PDF":
        if len(r.content) > 40_000_000:
            return {}
        f = PI.extract(url, data=r.content)
        return {"pdf_facts": f, "title": f.title, "doi": f.doi, "final_url": str(r.url)}
    if "html" in ctype:
        m = meta_from_html(r.text[:400_000])
        m["final_url"] = str(r.url)
        return m
    return {}


def citekey_parts(key: str):
    """'haug2020no' -> ('haug', 2020, 'no'); '0000https' -> None."""
    m = re.fullmatch(r"([a-z][a-z'\-]{1,30})(1[5-9]\d\d|20\d\d)([a-z]{2,20})?[a-z]?", (key or "").lower())
    return (m.group(1), int(m.group(2)), m.group(3) or "") if m else None


def stem_of(entry) -> str:
    for x in (entry.get("pdf_local") or "", entry.get("url") or "", entry.get("title") or ""):
        s = Path(urlparse(x).path if x.startswith("http") else x).stem if x else ""
        s = unquote(s)
        core = re.sub(r"[^A-Za-z0-9]", "", s)
        if len(core) >= 6 and not GENERIC_STEMS.match(core) and re.search(r"[A-Za-z]", core) and \
                (re.search(r"\d", core) or re.search(r"[A-Z].*[A-Z]", s)):
            return s
    return ""



# ------------------------------------------------------------------ more evidence (2026-10-01 dry run)
def peel_google(u: str) -> str:
    """Google's 'unusual traffic' page wraps the address you wanted -- often re-encoded ten times
    (sorry/index?continue=search?q=sorry/index?continue=...). Returns the innermost real address,
    or the search text that was typed; '' when only Google's token is left."""
    if not u or not re.search(r"google\.[a-z.]+/(sorry|search)|^EhAo", u):
        return ""
    for _ in range(15):
        v = unquote(u)
        if v == u:
            break
        u = v
    vals = re.findall(r"(?:continue|q|url)=([^&]+)", u)
    for cand in reversed(vals):
        cand = cand.strip()
        if cand and not re.search(r"google\.[a-z.]+/(sorry|search)|^EhAo", cand, re.I):
            return cand.replace("+", " ")
    return ""


def isbn_ok(x: str) -> str:
    d = re.sub(r"[^0-9Xx]", "", x or "").upper()
    if len(d) == 10 and re.fullmatch(r"\d{9}[\dX]", d):
        tot = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
        return d if tot % 11 == 0 else ""
    if len(d) == 13 and d.isdigit() and d[:3] in ("978", "979"):
        tot = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(d))
        return d if tot % 10 == 0 else ""
    return ""


def osf_title(client, u: str) -> dict:
    m = re.search(r"osf\.io/(?:preprints/[a-z]+/)?([a-z0-9]{5})(?:_v\d+)?", u or "", re.I)
    if not m:
        return {}
    for kind in ("preprints", "nodes"):
        try:
            r = client.get(f"https://api.osf.io/v2/{kind}/{m.group(1)}/", timeout=20)
            if r.status_code == 200:
                a = r.json().get("data", {}).get("attributes", {})
                doi = (a.get("doi") or "") or ((r.json().get("data", {}).get("links") or {}).get("preprint_doi") or "")
                return {"title": a.get("title", ""), "doi": doi.replace("https://doi.org/", ""),
                        "date": a.get("date_published") or a.get("date_created", "")}
        except Exception:
            pass
    return {}


VOL_HEADER = re.compile(r"([A-Z][A-Za-z.&' ]{3,70}?)\s*,?\s*Vol(?:ume)?\.?\s*(\d{1,4})\b(?:[^\n]{0,40}?\b(1[89]\d\d|20\d\d)\b)?")


def volume_title(facts, stem: str) -> str:
    m = VOL_HEADER.search((facts.text or "")[:4000]) if facts is not None else None
    if m:
        j = " ".join(m.group(1).split()).rstrip(".,")
        return f"{j}, Vol. {m.group(2)}" + (f" ({m.group(3)})" if m.group(3) else "")
    return ""


# ------------------------------------------------------------------ acceptance
def words(s: str):
    return [w for w in W.norm(s).split() if len(w) > 2]


def abstract_hit(abstract: str, text: str) -> bool:
    a, t = W.norm(abstract), W.norm(text)
    if not a or not t:
        return False
    aw = a.split()
    for i in range(0, max(1, len(aw) - 8)):
        if " ".join(aw[i:i + 8]) in t:
            return True
    opening = [w for w in aw[:20] if len(w) > 3]
    return bool(opening) and sum(w in set(t.split()) for w in opening) / len(opening) >= 0.6


def pdf_has_title(f, title: str) -> bool:
    if f is None or not title:
        return False
    tw = words(title)
    page = set(W.norm((f.text or "")[:6000]).split())
    return len(tw) >= 3 and sum(w in page for w in tw) / len(tw) >= 0.8


def best_title_from_hit(h) -> str:
    cands = [c for c in W._title_candidates(h.title) if not W.GENERIC_PAGE.match(c) and len(words(c)) >= 2]
    return max(cands, key=len) if cands else h.title


# ------------------------------------------------------------------ records
def record_for(client, title: str, surnames, year, doi: str = ""):
    if doi:
        rec = PI._crossref_doi(client, doi.replace("https://doi.org/", "").strip(), MAILTO)
        if rec:
            return rec, "doi"
    if title and len(words(title)) >= 3:
        f = PI.PdfFacts(path="x", meta_title=title, text=title, year_hint=year)
        try:
            rec = PI._title_search(client, f, MAILTO, getattr(CFG, "openalex_api_key", ""))
        except Exception:
            rec = None
        if rec and rec.title:
            rs = {W.norm(a.family).split()[-1] for a in rec.authors if W.norm(a.family)}
            if (not surnames or rs & set(surnames)) and W.title_score(title, rec.title) >= 0.85:
                return rec, "title-search"
    return None, ""


def main() -> None:
    wanted = json.loads(IDS.read_text(encoding="utf-8"))
    wanted = [w["id"] if isinstance(w, dict) else w for w in wanted]
    conn = sqlite3.connect(str(Path(CFG.db_path).expanduser()), timeout=120, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS identify_scan (ref_id TEXT PRIMARY KEY, result TEXT, how TEXT, "
                 "suggestion TEXT, scanned_at TEXT DEFAULT (datetime('now')))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS identify_bak_{STAMP} (ref_id TEXT PRIMARY KEY, row_json TEXT)")
    done = {r[0] for r in conn.execute("SELECT ref_id FROM identify_scan WHERE result='identified'")}
    ids = [i for i in wanted if i not in done][:LIMIT]
    db = RefDatabase()
    client = httpx.Client(timeout=30, headers=UA, follow_redirects=True)
    pool = W.SearchPool.from_config(CFG, Path(CFG.db_path).expanduser().parent / "web_search_budget.json") if SEARCH else None
    known = PI.NameVocab(sqlite3.connect(str(Path(CFG.db_path).expanduser()), timeout=120)).common
    print(f"[identify] {len(ids):,} entries | {'WRITE' if WRITE else 'DRY-RUN'} | search {'on: ' + str(pool.status()) if pool else 'off'}",
          flush=True)
    stats: Counter = Counter()
    shown = 0
    t0 = time.time()
    for n, rid in enumerate(ids, 1):
        row = conn.execute("SELECT * FROM refs WHERE id=?", (rid,)).fetchone()
        if row is None:
            continue
        e = dict(row)
        seed = db.get(rid)
        try:
            ex = json.loads(e.get("extras") or "{}")
        except Exception:
            ex = {}
        surnames = [W.norm(a.family).split()[-1] for a in seed.authors if W.norm(a.family)]
        year = seed.year
        ck = citekey_parts(e.get("cite_key") or "")
        if ck and not surnames:
            surnames = [ck[0]]
        if ck and not year:
            year = ck[1]
        found = {}                 # title, authors, doi, url, how
        suggestion = ""
        facts = None
        # 1. the PDF
        pdf = e.get("pdf_local") or ""
        if pdf and Path(pdf).exists():
            try:
                facts = PI.extract(pdf)
                # journal-volume file names only ('JMP1985V26', 'SMJCAT.V28', 'V01'); 'Course_notes-v2' is a book
                whole = (facts.pages >= 150 and re.search(
                    r"^[A-Z]{2,8}\.?(1[89]\d\d|20\d\d)?V\d|^V\d{1,3}$|\bvol(ume)?[ ._-]?\d", Path(pdf).stem, re.I))
                rec, via = (None, "") if whole else PI.resolve(facts)
                if whole:
                    vt = volume_title(facts, Path(pdf).stem)
                    if vt:
                        found = {"title": vt, "how": "pdf:volume-header"}
                    else:
                        suggestion = f"whole volume ({facts.pages} pages): describe it as a volume, not a single work"
                elif rec is not None:
                    found = {"rec": rec, "how": "pdf:" + via}
                elif good_title(facts.title) and len(words(facts.title)) >= 3:
                    found = {"title": facts.title, "how": "pdf:title"}
                    if not seed.authors and facts.pages < PI.LONG_PAGES and not facts.ocr:
                        pa = PI.authors_from_page(facts)
                        if pa:
                            found["authors"] = pa
            except Exception:
                pass
        # 1b. the PDF's own file name, when it reads as a title ('Séparabilité et multiplicité ...')
        if "rec" not in found and "title" not in found and pdf:
            fn = re.sub(r"\s*\(\d+\)$", "", PI.clean_filename(Path(pdf).stem))
            if good_title(fn) and len(words(fn)) >= 3 and W.norm(fn) != W.norm(e.get("title") or ""):
                found = {"title": fn, "how": "pdf-file-name"}
        # 2. stored addresses (Google's bot-check wrapper peeled first)
        if "rec" not in found:
            addrs = []
            for u in [e.get("url") or "", ex.get("original_url") or "", e.get("title") or ""]:
                inner = peel_google(u)
                if inner.startswith("http"):
                    addrs.append(inner)
                elif inner and good_title(inner) and "title" not in found:
                    found = {"title": inner[:300], "how": "google-search-you-typed"}
                elif u.startswith("http") and not re.search(r"google\.[a-z.]+/(sorry|search)", u):
                    addrs.append(u)
            for u in dict.fromkeys(addrs):
                idf = W.identifiers(W.Hit("", u, ""))
                if idf.get("doi") or idf.get("arxiv") or idf.get("jstor"):
                    doi = idf.get("doi") or (f"10.48550/arxiv.{idf['arxiv']}" if idf.get("arxiv") else f"10.2307/{idf['jstor']}")
                    rec, via = record_for(client, "", surnames, year, doi)
                    if rec is not None:
                        found = {"rec": rec, "how": "doi-in-address", "url": u}
                        break
                o = osf_title(client, u)
                if o.get("title"):
                    rec, via = record_for(client, o["title"], surnames, year, o.get("doi", ""))
                    found = {"rec": rec, "how": "osf+" + via, "url": u} if rec is not None else \
                        {"title": o["title"], "how": "osf", "url": u}
                    if rec is not None:
                        break
                    continue
                m = fetch_meta(client, u)
                if not m:
                    continue
                f2 = m.get("pdf_facts")
                if f2 is not None:
                    facts = facts or f2
                    rec, via = PI.resolve(f2)
                    if rec is not None:
                        found = {"rec": rec, "how": "url-pdf:" + via, "url": u}
                        break
                    if good_title(f2.title) and len(words(f2.title)) >= 3 and "title" not in found:
                        found = {"title": f2.title, "how": "url-pdf:title", "url": u}
                elif good_title(m.get("title", "")):
                    rec, via = record_for(client, m["title"], surnames, year, m.get("doi", ""))
                    if rec is not None:
                        found = {"rec": rec, "how": "url-meta:" + via, "url": u}
                        break
                    if "title" not in found:
                        found = {"title": m["title"], "how": "url-meta", "url": u,
                                 "authors": [Author(family=a.split()[-1], given=" ".join(a.split()[:-1]))
                                             for a in m.get("authors", []) if a.split()]}
        # 3. saved page title / stored DOI
        if "rec" not in found and e.get("doi"):
            rec, via = record_for(client, "", surnames, year, e["doi"])
            if rec is not None:
                found = {"rec": rec, "how": "stored-doi"}
        isbn = isbn_ok(e.get("title") or "")
        if "rec" not in found and isbn:
            rec = PI._isbn(client, isbn)
            if rec is not None and rec.title:
                rec.isbn = rec.isbn or isbn
                found = {"rec": rec, "how": "isbn-as-title"}
        if "rec" not in found and "title" not in found and good_title(ex.get("original_page_title", "")):
            found = {"title": ex["original_page_title"], "how": "saved-page-title"}
        # a title alone -> try for a full record
        if "title" in found and "rec" not in found:
            rec, via = record_for(client, found["title"], surnames, year)
            if rec is not None:
                found["rec"], found["how"] = rec, found["how"] + "+" + via
        # 4. web search on the best evidence
        if "rec" not in found and "title" not in found and pool is not None:
            stem = stem_of({"pdf_local": pdf, "url": e.get("url"), "title": e.get("title")})
            abstract = (e.get("abstract") or "").strip()
            qs = []
            if facts is not None and len(words(facts.title)) >= 3:
                qs.append(("pdf-title", f'"{facts.title[:150]}"'))
            if stem:
                qs.append(("file-name", f'"{stem}"'))
            if len(abstract.split()) >= 10:
                sent = " ".join(abstract.split()[:16])
                qs.append(("abstract", f'{sent} {" ".join(surnames[:1])}'))
            if surnames and year:
                extra = (ck[2] if ck else "") + " " + " ".join(k for k in json.loads(e.get("keywords") or "[]")[:6]
                                                              if isinstance(k, str) and len(k) > 4 and not re.search(r"[A-Z]{3}|\.bib|collection|starred|unnamed|website|notion|all$", k, re.I))[:80]
                qs.append(("author-year", f"{seed.authors[0].given + ' ' if seed.authors and seed.authors[0].given else ''}{surnames[0]} {year} {extra}".strip()))
            # free engines first; Google (Gemini, a little prepaid credit) once with the best query if they fail
            plan = [(how, q, None) for how, q in qs]
            if qs and pool.has(W.SearchPool.LAST_RESORT):
                plan.append((qs[0][0], qs[0][1].replace('"', ''), W.SearchPool.LAST_RESORT))
            for how, q, only in plan:
                try:
                    hits = pool.search(q, only=only)
                except W.Exhausted:
                    pool = None
                    break
                for h in hits[:10]:
                    if W.SHADOW.search(h.url) or W.NOISE.search(h.url):
                        continue
                    m = fetch_meta(client, h.url) if how in ("abstract", "author-year", "pdf-title") else {}
                    page_title = (m or {}).get("title", "")
                    text = f"{h.title} {h.snippet} {page_title} {' '.join((m or {}).get('authors', []))} {(m or {}).get('description', '')}"
                    ok = ""
                    if stem and re.sub(r"[^a-z0-9]", "", stem.lower()) in re.sub(r"[^a-z0-9]", "", unquote(h.url).lower()):
                        ok = "file name in address"
                    elif abstract and abstract_hit(abstract, text):
                        ok = "abstract wording"
                    elif facts is not None and pdf_has_title(facts, best_title_from_hit(h)):
                        ok = "title printed in the PDF"
                    elif ck and surnames and year and ck[2] and \
                            re.search(rf"\b{re.escape(surnames[0])}\b", W.norm(text)) and \
                            any(str(y) in text for y in range(year - 2, year + 3)) and \
                            ck[2] in W.norm(page_title or best_title_from_hit(h)).split()[:3]:
                        ok = "author + year + citation-key word"
                    if ok:
                        m = m or fetch_meta(client, h.url)
                        title = (m.get("title") if m and good_title(m.get("title", "")) else "") or best_title_from_hit(h)
                        if not good_title(title):
                            continue
                        rec, via = record_for(client, title, surnames, year, (m or {}).get("doi", ""))
                        found = {"title": title, "url": h.url, "how": f"search:{how} ({ok})"}
                        if rec is not None:
                            found["rec"] = rec
                            found["how"] += "+" + via
                        elif m and m.get("authors"):
                            found["authors"] = [Author(family=a.split()[-1], given=" ".join(a.split()[:-1]))
                                                for a in m["authors"] if a.split()]
                        break
                    if not suggestion and surnames and re.search(rf"\b{re.escape(surnames[0])}\b", W.norm(text)):
                        suggestion = f"{best_title_from_hit(h)[:150]} | {h.url}"
                if "title" in found:
                    break
        # ---- write
        up = {}
        if "rec" in found:
            s2 = db.get(rid)
            s2.title = None                       # the stored title is junk: the record's replaces it
            up = PI.fill_plan(s2, found["rec"])
        elif "title" in found:
            up = {"title": found["title"][:500]}
            if found.get("authors") and not seed.authors:
                from mouseion.db import _authors_json
                up["authors_json"] = _authors_json(found["authors"])
        if found.get("url") and not e.get("url"):
            up["url"] = found["url"]
        result = "identified" if up else ("suggestion" if suggestion else "unresolved")
        stats[result] += 1
        if WRITE:
            if up:
                conn.execute(f"INSERT OR IGNORE INTO identify_bak_{STAMP} VALUES (?,?)", (rid, json.dumps(e, default=str)))
                PI.apply_fill(db, rid, up)
                ex2 = json.loads(conn.execute("SELECT extras FROM refs WHERE id=?", (rid,)).fetchone()[0] or "{}")
                ex2["identified"] = {"how": found.get("how", ""), "url": found.get("url", ""), "at": date.today().isoformat()}
                conn.execute("UPDATE refs SET extras=? WHERE id=?", (json.dumps(ex2, ensure_ascii=False), rid))
            conn.execute("INSERT OR REPLACE INTO identify_scan (ref_id, result, how, suggestion) VALUES (?,?,?,?)",
                         (rid, result, found.get("how", ""), suggestion))
        if shown < SHOW and (up or suggestion):
            shown += 1
            print(f"  {result:10s} {found.get('how', '')[:42]:42s} {(e.get('title') or '')[:34]:34s} -> "
                  f"{(up.get('title') or suggestion)[:70]}", flush=True)
        if n % 50 == 0:
            print(f"  ... {n:,}/{len(ids):,} | {dict(stats)} | {n / (time.time() - t0):.2f}/s"
                  + (f" | searches left {pool.status()}" if pool else ""), flush=True)
    print(f"done: {dict(stats)}" + (f" | searches left {pool.status()}" if pool else ""), flush=True)


if __name__ == "__main__":
    main()
