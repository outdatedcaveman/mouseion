"""/api/pdfs/ingest exists and runs a dropped PDF through the ingest pipeline."""
import io

import pymupdf

from mouseion import web


def _pdf_bytes(title: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), title, fontsize=20)
    page.insert_text((72, 140), "An abstract about nothing in particular, " * 3, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out


def test_drop_pdf_creates_an_entry(tmp_path, monkeypatch):
    from mouseion import pdf_manager, pdf_ingest
    monkeypatch.setattr(pdf_manager, "get_pdf_dir", lambda: tmp_path)
    monkeypatch.setattr(pdf_ingest, "resolve", lambda f, cfg=None: (None, ""))     # offline
    web._ingest_index["idx"] = None
    c = web.app.test_client()
    data = {"file": (io.BytesIO(_pdf_bytes("A Wholly Imaginary Treatise On Tests")), "treatise.pdf")}
    key = web._get_or_create_api_key()
    r = c.post("/api/pdfs/ingest", data=data, content_type="multipart/form-data", headers={"X-API-Key": key})
    j = r.get_json()
    assert r.status_code == 200 and j["ok"], j
    assert j["action"] in ("created_unresolved", "created")
    assert (tmp_path / "treatise.pdf").exists()
