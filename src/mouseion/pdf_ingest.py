"""Turn a PDF file into a complete library entry -- the way Zotero/Mendeley do.

One pipeline for every PDF that enters Mouseion: a folder sweep (the owner's
Archives folder) and PDFs dropped into the app.

  1. extract   first pages' text, embedded metadata, the largest-font title,
               DOI / arXiv / ISBN, an arXiv id in the file name, and hints from
               the folder path (journal name, year, volume);
  2. relevance skip what is not a work of scholarship -- device manuals,
               basic school textbooks, receipts ... (recorded with the reason,
               never deleted, reviewable);
  3. resolve   DOI -> Crossref / doi.org; arXiv -> arXiv API; ISBN -> Open
               Library; otherwise the title (+ journal hint) -> Crossref and
               OpenAlex bibliographic search, accepted only on close title
               agreement and a compatible year;
  4. match     against the library by DOI, arXiv id, ISBN and normalised title
               (+year): a known entry without a PDF gets this file; one that
               already has a PDF is left alone;
  5. create    otherwise a new entry from the resolved record -- or, when
               nothing resolves, from what the PDF itself says -- linked to the
               file in place (nothing is copied), tagged with its origin.

The enrichment routines then complete whatever is still missing.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from .models import Author, Reference, RefType

logger = logging.getLogger(__name__)

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>\]\)]+)", re.I)
ARXIV_RE = re.compile(r"arXiv:\s?(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?", re.I)
ARXIV_FILE_RE = re.compile(r"^((?:0[7-9]|1\d|2\d)(?:0[1-9]|1[0-2])\.\d{4,5})(?:v\d+)?$")
ISBN_RE = re.compile(r"ISBN(?:-1[03])?[:\s]*((?:97[89][-\s]?)?\d{1,5}[-\s]?\d{1,7}[-\s]?\d{1,7}[-\s]?[\dX])\b", re.I)
FILE_DOI_RE = re.compile(r"(10\.\d{4,9})[_/]([^\s]+)$")      # "10.1007_s00220-019-03608-z"
PII_RE = re.compile(r"(?:^|[-_])(S?)(\d{4})(\d{3}[\dX])(\d{2})(\d{5})(\d)(?:[-_]|$)", re.I)   # Elsevier 1-s2.0-<PII>-main
YEAR_RE = re.compile(r"(?<!\d)(1[6-9]\d\d|20[0-4]\d)(?!\d)")

_HEADER = re.compile(r"^(arxiv|doi|http|www\.|vol\.?|pp\.|page|issn|isbn|copyright|©|journal of|proceedings|"
                     r"lecture notes|working paper|nber|preprint|draft|submitted|accepted|received|"
                     r"united states patent|sage|springer|elsevier|chapter \d|\d+$)", re.I)

# --- relevance: what is NOT scholarship (the owner's list: tech manuals, basic
# Portuguese textbooks; plus the usual administrative clutter) -----------------
_SKIP_DIRS = re.compile(r"(^|[\\/])(videos?|apps?|images?|fotos?|photos?|music|musica|software|drivers?|"
                        r"installers?|setup|receipts?|recibos?|notas? fiscais|boletos?|faturas?)([\\/]|$)", re.I)
_SKIP_WORDS = re.compile(
    r"\b(user'?s? (guide|manual)|owner'?s manual|instruction manual|quick ?start|installation guide|"
    r"setup guide|reference manual for (the )?(printer|router|camera|device)|datasheet|data sheet|"
    r"service manual|manual do (usu[aá]rio|propriet[aá]rio|produto)|guia (do usu[aá]rio|r[aá]pido)|"
    r"manual de instru[cç][oõ]es|garantia|warranty|"
    r"gram[aá]tica (b[aá]sica|escolar)|l[ií]ngua portuguesa|portugu[eê]s (b[aá]sico|para (iniciantes|concursos))|"
    r"ortografia|reda[cç][aã]o (escolar|para)|apostila|caderno de (exerc[ií]cios|atividades)|"
    r"ensino fundamental|ensino m[eé]dio|livro did[aá]tico|"
    r"boleto|nota fiscal|fatura|invoice|receipt|recibo|comprovante|extrato banc[aá]rio|"
    r"curriculum vitae|curr[ií]culo)\b", re.I)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


@dataclass
class PdfFacts:
    path: str
    pages: int = 0
    text: str = ""
    meta_title: str = ""
    meta_author: str = ""
    font_title: str = ""
    doi: str = ""
    arxiv: str = ""
    isbn: str = ""
    year_hint: Optional[int] = None
    journal_hint: str = ""
    error: str = ""
    ocr: bool = False

    @property
    def title(self) -> str:
        """The best title the file itself offers."""
        mt = self.meta_title
        good_meta = len(mt) > 12 and " " in mt and not re.search(r"\.(docx?|tex|dvi|indd|pdf)\b|^untitled|^microsoft",
                                                                 mt, re.I)
        return mt if good_meta else (self.font_title or Path(self.path).stem.replace("_", " "))


@dataclass
class IngestResult:
    action: str                  # attached | exists | created | created_unresolved | skipped | error
    ref_id: str = ""
    detail: str = ""
    via: str = ""                # how it was identified: doi | arxiv | isbn | title | pdf-only
    ref: Optional[Reference] = field(default=None, repr=False)


# ---------------------------------------------------------------- 1. extract
def extract(path: str | Path, data: bytes | None = None) -> PdfFacts:
    import pymupdf
    f = PdfFacts(path=str(path))
    try:
        doc = pymupdf.open(stream=data, filetype="pdf") if data is not None else pymupdf.open(str(path))
    except Exception as e:
        f.error = f"{type(e).__name__}: {str(e)[:80]}"
        return f
    try:
        f.pages = doc.page_count
        meta = doc.metadata or {}
        f.meta_title = " ".join((meta.get("title") or "").split())
        f.meta_author = " ".join((meta.get("author") or "").split())
        f.text = "\n".join(doc[i].get_text() for i in range(min(2, doc.page_count)))
        f.font_title = _font_title(doc)
    except Exception as e:
        f.error = f"{type(e).__name__}: {str(e)[:80]}"
    finally:
        doc.close()
    if len(f.text.strip()) < 100 and not f.error:
        # a scan without a text layer: OCR page 1 (Windows' built-in engine), and take
        # its first substantial lines as the title when nothing better exists
        try:
            from .ocr import ocr_pdf_pages
            ocr_text = ocr_pdf_pages(str(path), pages=1, data=data)
        except Exception:
            ocr_text = ""
        if ocr_text:
            f.text = ocr_text
            f.ocr = True
            if not f.font_title:
                lines = [ln.strip() for ln in ocr_text.splitlines() if len(ln.strip()) > 3]
                head = []
                for ln in lines[:4]:
                    if re.match(r"^(by|par|von|por)|abstract|summary|resumo", ln, re.I):
                        break
                    head.append(ln)
                    if len(" ".join(head)) > 60:
                        break
                f.font_title = " ".join(head)[:300]
    m = ARXIV_RE.search(f.text)
    if m:
        f.arxiv = m.group(1)
    else:
        m = ARXIV_FILE_RE.match(Path(path).stem)
        if m:
            f.arxiv = m.group(1)
    m = DOI_RE.search(f.text)
    if m:
        f.doi = m.group(1).rstrip(".,;:").lower()
    stem = Path(path).stem
    if not f.doi:
        m = FILE_DOI_RE.search(stem)
        if m:
            f.doi = (m.group(1) + "/" + m.group(2)).lower()
    if not f.doi and "1-s2.0" in stem:
        m = PII_RE.search(stem.replace("1-s2.0-", "-"))
        if m:        # Elsevier PII -> DOI: S0168007200000580 -> 10.1016/S0168-0072(00)00058-0
            s_, a, b, yy, item, chk = m.groups()
            f.doi = f"10.1016/{s_.upper()}{a}-{b}({yy}){item}-{chk}".lower()
    m = ISBN_RE.search(f.text)
    if m:
        f.isbn = re.sub(r"[^\dX]", "", m.group(1).upper())
    # folder hints: ".../Mathematics of Computation/pdf/1972_v026_n120/..."
    parts = Path(path).parts
    for p in reversed(parts[:-1]):
        y = YEAR_RE.search(p)
        if y and not f.year_hint:
            f.year_hint = int(y.group(1))
        if not f.journal_hint and re.search(r"(journal|proceedings|annals|transactions|mathematica|review|"
                                            r"fundamenta|computation|letters|bulletin|acta)", p, re.I):
            f.journal_hint = p
    return f


def _font_title(doc) -> str:
    try:
        if not doc.page_count:
            return ""
        spans = []
        for b in doc[0].get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for sp in line["spans"]:
                    t = " ".join(sp["text"].split())
                    if len(t) > 2 and not _HEADER.match(t):
                        spans.append((round(sp["size"], 1), sp["bbox"][1], t))
        if not spans:
            return ""
        for top in sorted({sz for sz, _, _ in spans}, reverse=True)[:3]:
            title = " ".join(t for sz, y, t in sorted(spans, key=lambda x: x[1]) if abs(sz - top) <= 0.5)
            if len([w for w in title.split() if len(w) > 2]) >= 2:
                return title[:300]
    except Exception:
        pass
    return ""


# -------------------------------------------------------------- 2. relevance
def relevance(f: PdfFacts) -> Tuple[bool, str]:
    """(keep, reason). Scholarship stays; manuals, basic textbooks, admin paper go."""
    if _SKIP_DIRS.search(str(Path(f.path).parent)):
        return False, "folder: " + Path(f.path).parent.name
    probe = " ".join([Path(f.path).stem, f.meta_title, f.font_title, f.text[:1500]])
    m = _SKIP_WORDS.search(probe)
    if m and not (f.doi or f.arxiv):      # a paper ABOUT manuals keeps its DOI
        return False, "looks like: " + m.group(0)
    if f.error and not f.text:
        return False, "unreadable: " + f.error
    return True, ""


# ---------------------------------------------------------------- 3. resolve
_UA = {"User-Agent": "mouseion/0.3 (https://github.com/outdatedcaveman/mouseion; library ingest)"}


def _crossref_doi(client: httpx.Client, doi: str, mailto: str) -> Optional[Reference]:
    from .providers.crossref import CrossRefProvider
    try:
        r = client.get(f"https://api.crossref.org/works/{doi}", params={"mailto": mailto} if mailto else None)
        if r.status_code == 200:
            return CrossRefProvider()._parse_work(r.json()["message"])
        r = client.get(f"https://doi.org/{doi}", headers={"Accept": "application/vnd.citationstyles.csl+json"},
                       follow_redirects=True)
        if r.status_code == 200:
            j = r.json()
            ref = Reference(title=(j.get("title") or "").strip() or None, doi=doi)
            ref.authors = [Author(family=a.get("family") or a.get("literal") or "", given=a.get("given") or "")
                           for a in j.get("author") or []]
            dp = ((j.get("issued") or {}).get("date-parts") or [[None]])[0]
            ref.year = dp[0] if dp and dp[0] else None
            ref.journal = j.get("container-title") or None
            return ref if ref.title else None
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


def _arxiv(client: httpx.Client, aid: str) -> Optional[Reference]:
    try:
        from .providers.arxiv import ArXivProvider
        prov = ArXivProvider()
        r = client.get("https://export.arxiv.org/api/query", params={"id_list": aid})
        if r.status_code != 200:
            return None
        ref = prov._parse_atom(r.text, single=True)
        if ref and ref.title and "error" not in (ref.title or "").lower():
            ref.arxiv_id = ref.arxiv_id or aid
            return ref
        # minimal parse
        t = re.search(r"<entry>.*?<title>(.*?)</title>", r.text, re.S)
        if not t:
            return None
        ref = Reference(title=" ".join(t.group(1).split()), arxiv_id=aid)
        ref.authors = [Author(family=n.split()[-1], given=" ".join(n.split()[:-1]))
                       for n in re.findall(r"<author>\s*<name>(.*?)</name>", r.text)]
        y = re.search(r"<published>(\d{4})", r.text)
        ref.year = int(y.group(1)) if y else None
        ref.ref_type = RefType.PREPRINT
        return ref
    except httpx.HTTPError:
        return None


def _isbn(client: httpx.Client, isbn: str) -> Optional[Reference]:
    try:
        r = client.get("https://openlibrary.org/api/books",
                       params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"})
        d = (r.json() or {}).get(f"ISBN:{isbn}") if r.status_code == 200 else None
        if not d:
            return None
        ref = Reference(title=d.get("title"), isbn=isbn, ref_type=RefType.BOOK)
        ref.authors = [Author(family=a["name"].split()[-1], given=" ".join(a["name"].split()[:-1]))
                       for a in d.get("authors") or [] if a.get("name")]
        y = YEAR_RE.search(d.get("publish_date") or "")
        ref.year = int(y.group(1)) if y else None
        ref.publisher = ((d.get("publishers") or [{}])[0]).get("name")
        return ref
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return None


def _title_search(client: httpx.Client, f: PdfFacts, mailto: str, oa_key: str) -> Optional[Reference]:
    from .providers.crossref import CrossRefProvider
    from .providers.openalex import OpenAlexProvider
    title = f.title
    if len(_norm(title).split()) < 3:
        return None
    q = title[:250]
    cands: List[Tuple[float, Reference]] = []
    try:
        cp = {"query.bibliographic": q, "rows": 5, **({"mailto": mailto} if mailto else {})}
        if f.journal_hint:      # e.g. the folder "Mathematics of Computation"
            cp["query.container-title"] = re.sub(r"[_\d]+", " ", f.journal_hint)[:120]
        r = client.get("https://api.crossref.org/works", params=cp)
        if r.status_code == 200:
            for it in r.json()["message"]["items"]:
                try:
                    cands.append((0.0, CrossRefProvider()._parse_work(it)))
                except Exception:
                    pass
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    try:
        p = {"search": title[:250], "per-page": 5}
        if oa_key:
            p["api_key"] = oa_key
        if mailto:
            p["mailto"] = mailto
        r = client.get("https://api.openalex.org/works", params=p)
        if r.status_code == 200:
            for it in r.json().get("results") or []:
                try:
                    cands.append((0.0, OpenAlexProvider._parse_work(it)))
                except Exception:
                    pass
    except (httpx.HTTPError, ValueError):
        pass
    best, best_s = None, 0.0
    nt = _norm(title)
    for _, c in cands:
        ct = _norm(c.title or "")
        if not ct:
            continue
        s = difflib.SequenceMatcher(None, nt, ct).ratio()
        # a long running head or subtitle on page 1: accept a clean containment too
        # a truncated file name / a running head: accept containment only between titles of
        # similar length ("Combinatorics, Second Edition" is not "A Course in Combinatorics")
        if s < 0.9 and len(ct) >= 25 and (ct in nt or nt in ct) and                 min(len(ct), len(nt)) >= 0.75 * max(len(ct), len(nt)):
            s = 0.9
        # front-matter years in books are copyright/reprint years: trust only a folder year there
        y = f.year_hint
        if y and c.year and abs(int(c.year) - y) > 2:
            continue
        if s > best_s:
            best, best_s = c, s
    return best if best_s >= 0.9 else None


def clean_filename(stem: str) -> str:
    """A file name as a title: download-site tags, leading document numbers,
    copy counters and underscores/hyphens-for-spaces removed."""
    s = re.sub(r"\((?:z-?lib(?:\.org)?|libgen[^)]*|b-ok[^)]*|pdfdrive[^)]*|\d+)\)", " ", stem, flags=re.I)
    s = re.sub(r"(z-?lib\.org|libgen(\.\w+)?|www\.\S+)", " ", s, flags=re.I)
    s = re.sub(r"^\d{6,}[-_ ]+", "", s)                  # "356062879-Livro-..." (Scribd ids)
    if s.count(" ") < 2 and (s.count("-") >= 3 or s.count("_") >= 3):
        s = re.sub(r"[-_]+", " ", s)
    s = re.sub(r"_+", " ", s)
    return " ".join(s.split()).strip(" -.,")


def _split_title_author(stem: str) -> Tuple[str, str]:
    """"Probability, A. N. Shiryaev" / "Title - Author" / "[Author] Title" / "Title (Author)" -> (title, author)."""
    stem = clean_filename(stem)
    m = re.match(r"^(.*\S)\s*\(((?:[A-Z][\w.'\-]*\s*){2,4})\)$", stem)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    stem = re.sub(r"\((?:\d\w*\s*ed\.?|[^)]*(?:series|studies|texts|graduate|lecture)[^)]*)\)", " ", stem, flags=re.I)
    m = re.match(r"^\[([^\]]+)\]\s*(.+)$", stem)
    if m:
        return m.group(2).strip(), m.group(1).replace("_", " ").strip()
    for sep in (r",\s*", r"\s+-\s+", r"\s+by\s+"):
        parts = re.split(sep, stem)
        if len(parts) >= 2:
            tail = parts[-1].strip()
            if re.fullmatch(r"(?:[A-Z]\.\s*)*[A-Z][\w'\-]+(?:\s+(?:[A-Z]\.\s*)*[A-Z][\w'\-]+){0,3}", tail):
                return " ".join(parts[:-1]).strip(), tail
    return stem.strip(), ""


def _book_search(client: httpx.Client, f: PdfFacts, mailto: str) -> Optional[Reference]:
    """Books: title + author from the file name, Crossref (math books often have DOIs) then Open Library."""
    title, author = _split_title_author(Path(f.path).stem)
    if len(_norm(title).split()) < 2:
        return None
    fam = author.split()[-1].lower() if author else ""
    from .providers.crossref import CrossRefProvider
    try:
        prm = {"query.bibliographic": title, "rows": 5, **({"mailto": mailto} if mailto else {})}
        if author:
            prm["query.author"] = author
        r = client.get("https://api.crossref.org/works", params=prm)
        if r.status_code == 200:
            for it in r.json()["message"]["items"]:
                c = CrossRefProvider()._parse_work(it)
                if difflib.SequenceMatcher(None, _norm(title), _norm(c.title or "")).ratio() < 0.85:
                    continue
                if fam and fam not in " ".join((a.family or "").lower() for a in c.authors or []):
                    continue
                return c
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    try:
        prm = {"title": title, "limit": 5}
        if author:
            prm["author"] = author
        r = client.get("https://openlibrary.org/search.json", params=prm)
        for d in (r.json().get("docs") or []) if r.status_code == 200 else []:
            if difflib.SequenceMatcher(None, _norm(title), _norm(d.get("title") or "")).ratio() < 0.85:
                continue
            names = d.get("author_name") or []
            if fam and not any(fam in n.lower() for n in names):
                continue
            ref = Reference(title=d.get("title"), ref_type=RefType.BOOK, year=d.get("first_publish_year"))
            ref.authors = [Author(family=n.split()[-1], given=" ".join(n.split()[:-1])) for n in names[:6]]
            isbns = [i for i in (d.get("isbn") or []) if len(i) == 13]
            ref.isbn = isbns[0] if isbns else None
            ref.publisher = (d.get("publisher") or [None])[0]
            if d.get("key"):
                ref.url = "https://openlibrary.org" + d["key"]
            return ref
    except (httpx.HTTPError, ValueError):
        pass
    return None


def _year_from_text(text: str) -> Optional[int]:
    ys = [int(y) for y in YEAR_RE.findall(text[:3000])]
    return max(set(ys), key=ys.count) if ys else None


def resolve(f: PdfFacts, cfg=None) -> Tuple[Optional[Reference], str]:
    """(record, via). None when nothing reliable was found."""
    if cfg is None:
        from .config import get_config
        cfg = get_config()
    mailto = cfg.crossref_email or cfg.openalex_email or ""
    if is_whole_volume(f):
        return None, ""
    with httpx.Client(timeout=30, headers=_UA, follow_redirects=True) as client:
        if f.doi:
            rec = _crossref_doi(client, f.doi, mailto)
            if rec and _title_ok(f, rec):
                rec.doi = rec.doi or f.doi
                return rec, "doi"
        if f.arxiv:
            # arXiv's own API allows one call per 3 s and 429s a shared address: every
            # arXiv paper also has a DataCite DOI, resolvable through doi.org
            rec = _crossref_doi(client, f"10.48550/arxiv.{f.arxiv}", mailto)
            if rec is None:
                rec = _arxiv(client, f.arxiv)
            if rec and not _title_ok(f, rec):     # the first "arXiv:" on page 1-2 can be a CITED paper
                rec = None
            if rec:
                rec.arxiv_id = f.arxiv
                if (rec.doi or "").lower().startswith("10.48550/"):
                    rec.doi = None       # keep the journal DOI slot free for the published version
                rec.ref_type = RefType.PREPRINT
                return rec, "arxiv"
        for doi in filename_dois(f.path):
            rec = _crossref_doi(client, doi, mailto)
            if rec and _title_ok(f, rec):
                rec.doi = rec.doi or doi
                return rec, "filename-doi"
        if f.isbn:
            rec = _isbn(client, f.isbn)
            if rec:
                return rec, "isbn"
        rec = _title_search(client, f, mailto, getattr(cfg, "openalex_api_key", ""))
        if rec:
            return rec, "title"
        if f.pages >= 80:
            rec = _book_search(client, f, mailto)
            if rec:
                return rec, "book"
        fname = _split_title_author(Path(f.path).stem)[0]
        if len(_norm(fname).split()) >= 4 and _norm(fname) != _norm(f.title):
            alt = PdfFacts(path=f.path, text=f.text, meta_title=fname, year_hint=f.year_hint,
                           journal_hint=f.journal_hint)
            rec = _title_search(client, alt, mailto, getattr(cfg, "openalex_api_key", ""))
            if rec:
                return rec, "filename-title"
    return None, ""


def filename_dois(path: str) -> List[str]:
    """DOIs that journal archives encode in file and folder names (checked against
    the page text before they are trusted):
      JSTOR stable id  .../1952_v003_n02/2032262.pdf            -> 10.2307/2032262
      Phys Rev Lett    PRL_v84_i23_p5331_1.pdf | v95/i25/e257003 -> 10.1103/PhysRevLett.84.5331
      IOP              1751-8121_43_7_075303.pdf                 -> 10.1088/1751-8113/43/7/075303
    """
    p = str(path).replace("\\", "/")
    stem = Path(p).stem
    out: List[str] = []
    m = re.fullmatch(r"(\d{5,9})(?: \(\d+\))?", stem)
    if m:
        out.append(f"10.2307/{m.group(1)}")
    m = re.search(r"PRL_v(\d+)_i\d+_p(\d+)", stem) or re.search(r"/v(\d+)/i\d+/e?(\d+)\.pdf$", p) \
        if re.search(r"PhysRevLett|/PRL|PRL_", p) else None
    if m:
        out.append(f"10.1103/PhysRevLett.{m.group(1)}.{m.group(2)}")
    m = re.fullmatch(r"(\d{4}-\d{3}[\dX])_(\d+)_(\d+)_(\w+)", stem)
    if m:
        issn, vol, iss, art = m.groups()
        issns = [issn] + (["1751-8113"] if issn == "1751-8121" else [])
        out += [f"10.1088/{i}/{vol}/{iss}/{art}" for i in issns]
    return out


def is_whole_volume(f: PdfFacts) -> bool:
    """An entire journal volume/issue (JMP1992V33.pdf, 'Volume 9, Number 4 (2001).pdf')."""
    stem = Path(f.path).stem
    return f.pages >= 150 and bool(re.search(r"(^|[^a-z])v(ol(ume)?)?[ ._-]?\d+|number \d+|\bV\d+\b", stem, re.I)) \
        and not f.doi and not f.arxiv


def _title_ok(f: PdfFacts, rec: Reference) -> bool:
    """A DOI found on page 1 can be a CITED work's; the record must resemble the PDF."""
    rt = _norm(rec.title or "")
    if not rt:
        return False
    page = _norm(f.text[:4000])
    words = [w for w in rt.split() if len(w) > 3][:8]
    return not words or sum(w in page for w in words) >= max(2, int(0.6 * len(words)))


def from_pdf_only(f: PdfFacts) -> Reference:
    """What the file itself says, for works no index knows (the title-fixer retries later)."""
    ref = Reference(title=f.title[:500] or Path(f.path).stem)
    if is_whole_volume(f):
        parent = Path(f.path).parent.name
        ref.title = f"{Path(f.path).stem} ({parent})" if parent.lower() not in ("pdf",) else Path(f.path).stem
        ref.ref_type = RefType.OTHER if hasattr(RefType, "OTHER") else ref.ref_type
    if f.meta_author and not re.fullmatch(r"(admin\w*|user|owner|author|unknown|[a-z]+\d+)", f.meta_author, re.I):
        for name in re.split(r"\s*(?:;|&| and |,(?=\s*[A-Z][a-z]+\s+[A-Z]))\s*", f.meta_author)[:10]:
            parts = name.split()
            if parts:
                ref.authors.append(Author(family=parts[-1], given=" ".join(parts[:-1])))
    ref.year = f.year_hint or _year_from_text(f.text)
    ref.doi, ref.arxiv_id, ref.isbn = f.doi or None, f.arxiv or None, f.isbn or None
    if f.pages >= 120:
        ref.ref_type = RefType.BOOK
    return ref


# ------------------------------------------------------------------ 4. match
class LibraryIndex:
    """In-memory lookups over the library (built once per sweep)."""

    def __init__(self, conn) -> None:
        self.by_doi: Dict[str, Tuple[str, bool]] = {}
        self.by_arxiv: Dict[str, Tuple[str, bool]] = {}
        self.by_isbn: Dict[str, Tuple[str, bool]] = {}
        self.by_title: Dict[str, Tuple[str, bool]] = {}
        for rid, doi, arx, isbn, title, pl, pd in conn.execute(
                "SELECT id, lower(doi), lower(arxiv_id), isbn, title, pdf_local, pdf_drive_id FROM refs"):
            has = bool(pl or pd)
            if doi:
                self.by_doi[doi] = (rid, has)
            if arx:
                self.by_arxiv[re.sub(r"v\d+$", "", arx)] = (rid, has)
            if isbn:
                self.by_isbn[re.sub(r"[^\dX]", "", isbn.upper())] = (rid, has)
            nt = _norm(title or "")
            if len(nt) >= 25:
                self.by_title[nt] = (rid, has)

    def find(self, ref: Reference) -> Optional[Tuple[str, bool]]:
        for key, table in (((ref.doi or "").lower(), self.by_doi),
                           (re.sub(r"v\d+$", "", (ref.arxiv_id or "").lower()), self.by_arxiv),
                           (re.sub(r"[^\dX]", "", (ref.isbn or "").upper()), self.by_isbn),
                           (_norm(ref.title or ""), self.by_title)):
            if key and key in table:
                return table[key]
        return None

    def add(self, rid: str, ref: Reference, has_pdf: bool = True) -> None:
        if ref.doi:
            self.by_doi[ref.doi.lower()] = (rid, has_pdf)
        if ref.arxiv_id:
            self.by_arxiv[ref.arxiv_id.lower()] = (rid, has_pdf)
        nt = _norm(ref.title or "")
        if len(nt) >= 25:
            self.by_title[nt] = (rid, has_pdf)


# --------------------------------------------------------- 5. the whole thing
def ingest(path: str, db, index: LibraryIndex, tag: str, cfg=None, write: bool = True,
           facts: PdfFacts | None = None) -> IngestResult:
    f = facts or extract(path)
    keep, why = relevance(f)
    if not keep:
        return IngestResult("skipped", detail=why)
    rec, via = resolve(f, cfg)
    ref = rec or from_pdf_only(f)
    hit = index.find(ref) or (index.find(from_pdf_only(f)) if rec else None)
    if hit:
        rid, has_pdf = hit
        if has_pdf:
            return IngestResult("exists", rid, "library already has a PDF for it", via, ref)
        if write:
            db.update_integration_ids(rid, pdf_local=path, pdf_path=Path(path).name)
            index.add(rid, ref, True)
        return IngestResult("attached", rid, "PDF linked to the existing entry", via, ref)
    ref.sources = {**(ref.sources or {}), "archive_pdf": 0.9 if rec else 0.4}
    rid = ""
    if write:
        rid = db.upsert(ref, tags=[tag] + ([] if rec else ["pdf:unresolved"]))
        db.update_integration_ids(rid, pdf_local=path, pdf_path=Path(path).name)
        index.add(rid, ref, True)
    return IngestResult("created" if rec else "created_unresolved", rid, "", via or "pdf-only", ref)
