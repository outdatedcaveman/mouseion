"""Measure BASE (base-search.net) on the PDF tail: refs with a DOI but no PDF.

    python scripts/base_search_measure.py --probe      # is this machine allowed in?
    python scripts/base_search_measure.py [N=200]      # the measurement

--probe sends one query and says whether BASE answers this IP + User-Agent (or
the configured base_search_api_key); exit code 2 when refused.

The measurement samples N refs (fixed seed) that have a DOI and no PDF, asks
BASE for repository copies (DOI, then title + first author), downloads each
candidate into a scratch folder (never the library) and keeps only real PDFs,
then asks the existing repository sources (OpenAlex locations, Unpaywall
oa_locations, Europe PMC, PMC) for the same ref to measure overlap. Read-only
on the library; one ref at a time (BASE allows 1 request/s), so RAM stays flat.
Writes a JSONL of every ref and prints a markdown table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import httpx  # noqa: E402

from mouseion.config import get_config  # noqa: E402
from mouseion.db import _row_to_ref  # noqa: E402
from mouseion.providers import base_search as B  # noqa: E402

SEED = 20261018


async def _public_ip(client: httpx.AsyncClient) -> str:
    try:
        return (await client.get("https://api.ipify.org", timeout=10)).text.strip()
    except httpx.RequestError:
        return "?"


async def probe() -> int:
    cfg = get_config()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        ip = await _public_ip(client)
        print(f"public IP   : {ip}")
        print(f"User-Agent  : {B.USER_AGENT}")
        print(f"apikey set  : {'yes' if cfg.base_search_api_key else 'no'}")
        print(f"flag enabled: {cfg.base_search_enabled}")
        try:
            docs = await B.perform_search(client, B.doi_query("10.1371/journal.pone.0000308"), hits=3,
                                          max_wait=30)
        except B.BaseAccessDenied as e:
            print(f"BASE REFUSED: {e}")
            return 2
        if docs is None:
            print("BASE did not answer (network / server / budget) - try again later")
            return 1
        print(f"BASE OK: {len(docs)} record(s) for the test DOI")
        for d in docs[:3]:
            print("  ", B._first(d.get("dccollection")), "|", B._first(d.get("dctitle"))[:70])
        return 0


def _sample(n: int):
    cfg = get_config()
    con = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True, timeout=60)
    con.row_factory = sqlite3.Row
    ids = [r[0] for r in con.execute(
        """SELECT id FROM refs WHERE COALESCE(doi,'') != ''
             AND COALESCE(pdf_local,'') = '' AND COALESCE(pdf_drive_id,'') = ''
             AND COALESCE(pdf_path,'') = '' ORDER BY id""")]
    pick = random.Random(SEED).sample(ids, min(n, len(ids)))
    refs = []
    for rid in pick:
        row = con.execute("SELECT * FROM refs WHERE id = ?", (rid,)).fetchone()
        ref = _row_to_ref(row)
        ref._db_id = rid
        refs.append(ref)
    con.close()
    return len(ids), refs


async def _existing_copies(client, ref, cfg) -> list:
    from mouseion.pdf_manager import _repository_copies
    try:
        urls = await _repository_copies(client, ref, cfg)
    except Exception:
        urls = []
    if ref.oa_url:
        urls = [ref.oa_url] + urls
    return urls


async def _download(client, url: str, dest: Path) -> tuple:
    from mouseion.pdf_manager import _stream_download, TemporaryDownloadError
    from mouseion.publisher_gate import PublisherBlocked
    try:
        ok = await _stream_download(client, url, dest)
    except (TemporaryDownloadError, PublisherBlocked) as e:
        return False, type(e).__name__
    if not ok:
        return False, "not a pdf"
    pages = None
    try:
        import pypdf
        pages = len(pypdf.PdfReader(str(dest)).pages)
    except Exception:
        return False, "pdf unreadable"
    return True, pages


async def measure(n: int, out_dir: Path) -> int:
    cfg = get_config()
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(exist_ok=True)
    pool, refs = _sample(n)
    print(f"pool: {pool} refs with a DOI and no PDF; sample {len(refs)} (seed {SEED})", flush=True)
    jl = (out_dir / "base_measure.jsonl").open("w", encoding="utf-8")
    t0 = time.time()
    stats = dict(asked=0, unasked=0, hit=0, hit_doi=0, hit_title=0, pdf=0, pdf_only_base=0,
                 overlap_ref=0, same_url=0, existing_any=0)
    async with httpx.AsyncClient(follow_redirects=True, timeout=25,
                                 headers={"User-Agent": "mouseion/0.1 (https://github.com/outdatedcaveman/mouseion)"}) as client:
        for i, ref in enumerate(refs, 1):
            rec = {"id": ref._db_id, "doi": ref.doi, "title": (ref.title or "")[:120]}
            try:
                copies = await B.repository_copies(client, ref)
            except B.BaseAccessDenied as e:
                print(f"BASE REFUSED after {i - 1} refs: {e}", flush=True)
                jl.close()
                return 2
            if copies is None:
                stats["unasked"] += 1
                rec["base"] = "unasked"
                jl.write(json.dumps(rec) + "\n")
                continue
            stats["asked"] += 1
            existing = await _existing_copies(client, ref, cfg)
            stats["existing_any"] += bool(existing)
            rec["existing"] = existing
            rec["base"] = [dict(url=c.url, repo=c.collection, oa=c.oa, file_like=c.file_like,
                                matched_by=c.matched_by) for c in copies]
            if copies:
                stats["hit"] += 1
                stats["hit_" + copies[0].matched_by] += 1
                stats["overlap_ref"] += bool(existing)
                stats["same_url"] += any(c.url in existing for c in copies)
                for c in copies:
                    dest = pdf_dir / f"{ref._db_id}.pdf"
                    ok, info = await _download(client, c.url, dest)
                    rec.setdefault("downloads", []).append([c.url, ok, info])
                    if ok:
                        stats["pdf"] += 1
                        stats["pdf_only_base"] += c.url not in existing
                        rec["pdf"] = dict(url=c.url, repo=c.collection, provider=c.provider, pages=info)
                        break
            jl.write(json.dumps(rec, ensure_ascii=False) + "\n")
            jl.flush()
            if i % 20 == 0:
                print(f"{i}/{len(refs)} {stats} {time.time() - t0:.0f}s", flush=True)
    jl.close()
    asked = max(stats["asked"], 1)
    rows = [
        ("Sample (DOI, no PDF)", len(refs), ""),
        ("BASE asked / could not ask", f"{stats['asked']} / {stats['unasked']}", ""),
        ("Refs with >=1 BASE repository copy", stats["hit"], f"{100 * stats['hit'] / asked:.1f}%"),
        ("  matched by DOI / by title+author", f"{stats['hit_doi']} / {stats['hit_title']}", ""),
        ("Valid PDFs downloaded (pypdf opens it)", stats["pdf"], f"{100 * stats['pdf'] / asked:.1f}%"),
        ("  PDF URL not listed by existing sources", stats["pdf_only_base"], ""),
        ("Refs where existing sources list any copy", stats["existing_any"], f"{100 * stats['existing_any'] / asked:.1f}%"),
        ("BASE hits that existing sources also cover", stats["overlap_ref"], ""),
        ("BASE hits sharing an exact URL with them", stats["same_url"], ""),
    ]
    print("\n| measure | n | % of asked |\n|---|---|---|")
    for r in rows:
        print(f"| {r[0]} | {r[1]} | {r[2]} |")
    print(f"\nwall time {time.time() - t0:.0f}s; per-ref log: {out_dir / 'base_measure.jsonl'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("n", nargs="?", type=int, default=200)
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--out", default=str(Path.home() / ".local" / "share" / "mouseion" / "base_measure"))
    a = ap.parse_args()
    if a.probe:
        return asyncio.run(probe())
    return asyncio.run(measure(a.n, Path(a.out)))


if __name__ == "__main__":
    sys.exit(main())
