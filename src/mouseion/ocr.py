"""OCR for scanned PDFs (no text layer), with the engine built into Windows.

Windows.Media.Ocr ships with Windows 10/11: no download, no model files, small
memory -- which matters on an 8 GB machine that has crashed from memory
exhaustion (PaddleOCR is opt-in for that reason). Only the first page(s) are
read: that is where title, authors and identifiers are. Languages: whatever
OCR packs Windows has (Settings > Time & language > Language > add a language
with "Optical character recognition" to get e.g. Portuguese).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def available() -> bool:
    try:
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
        return True
    except Exception:
        return False


async def _ocr_png(png: bytes, lang: Optional[str]) -> str:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    engine = OcrEngine.try_create_from_language(Language(lang)) if lang else OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    result = await engine.recognize_async(bitmap)
    return "\n".join(line.text for line in result.lines)


def ocr_pdf_pages(path: str, pages: int = 1, dpi: int = 200, data: bytes | None = None,
                  lang: Optional[str] = None) -> str:
    """Text of the first `pages` pages of a scanned PDF ('' if OCR is unavailable)."""
    if not available():
        return ""
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf") if data is not None else pymupdf.open(path)
    except Exception:
        return ""
    out = []
    try:
        for i in range(min(pages, doc.page_count)):
            pix = doc[i].get_pixmap(dpi=dpi)
            png = pix.tobytes("png")
            try:
                out.append(asyncio.run(_ocr_png(png, lang)))
            except Exception as e:
                logger.debug("OCR failed on %s p%d: %s", path, i, e)
    finally:
        doc.close()
    return "\n".join(out)
