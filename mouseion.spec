# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Mouseion — single-file .exe desktop app."""

import sys
from pathlib import Path

block_cipher = None

SRC = Path("src")

a = Analysis(
    [str(SRC / "mouseion" / "__main__.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Core app modules PyInstaller may miss due to lazy imports
        "mouseion.__main__",
        "mouseion.web",
        "mouseion.cli",
        "mouseion.config",
        "mouseion.db",
        "mouseion.models",
        "mouseion.cache",
        "mouseion.input",
        "mouseion.lookup",
        "mouseion.batch_lookup",
        "mouseion.web_search",
        "mouseion.merge",
        "mouseion.semantic",
        "mouseion.tagger",
        "mouseion.quota",
        "mouseion.pdf_fetch",
        # Providers (loaded dynamically)
        "mouseion.providers",
        "mouseion.providers.arxiv",
        "mouseion.providers.crossref",
        "mouseion.providers.dblp",
        "mouseion.providers.openalex",
        "mouseion.providers.openlibrary",
        "mouseion.providers.pubmed",
        "mouseion.providers.semantic_scholar",
        "mouseion.providers.doi_org",
        "mouseion.providers.unpaywall",
        "mouseion.providers.arxiv_api",
        "mouseion.providers.google_books",
        # Enrichment daemon
        "mouseion.enrich_daemon",
        # Dedup maintenance
        "mouseion.maintenance_dedup",
        # PDF manager
        "mouseion.pdf_manager",
        # Drive sync daemon
        "mouseion.sync_daemon",
        # Parsers
        "mouseion.parsers",
        "mouseion.parsers.bibtex",
        "mouseion.parsers.ris",
        "mouseion.parsers.html",
        "mouseion.parsers.pdf",
        "mouseion.parsers.bookmarks",
        # Exporters
        "mouseion.exporters",
        "mouseion.exporters.bibtex",
        "mouseion.exporters.ris",
        "mouseion.exporters.markdown",
        "mouseion.exporters.zotero_rdf",
        # Integrations
        "mouseion.integrations",
        "mouseion.integrations.zotero",
        "mouseion.integrations.notion",
        "mouseion.integrations.obsidian",
        "mouseion.integrations.google_drive",
        # Google API client (for Drive sync)
        "google.oauth2",
        "google.oauth2.service_account",
        "google.oauth2.credentials",
        "google_auth_oauthlib",
        "google_auth_oauthlib.flow",
        "google.auth",
        "google.auth.transport",
        "google.auth.transport.requests",
        "googleapiclient",
        "googleapiclient.discovery",
        "googleapiclient.http",
        "mouseion.integrations.instapaper",
        # Dependencies that use lazy/conditional imports
        "sqlite3",
        "lxml",
        "lxml.etree",
        "lxml.html",
        "bibtexparser",
        "rispy",
        "rapidfuzz",
        "rapidfuzz.fuzz",
        "rapidfuzz.process",
        "diskcache",
        "pypdf",
        "pymupdf",
        "tomlkit",
        "dotenv",
        "httpx",
        "h2",
        "hpack",
        "hyperframe",
        "anyio",
        "anyio._backends._asyncio",
        "flask",
        "flask.json",
        "jinja2",
        "markupsafe",
        "werkzeug",
        "click",
        "rich",
        "certifi",
        "charset_normalizer",
        "idna",
        "sniffio",
        "httpcore",
        "bs4",
        "soupsieve",
        # Desktop window (pywebview)
        "webview",
        "webview.platforms.edgechromium",
        "clr_loader",
        "pythonnet",
        "bottle",
        "proxy_tools",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Unix-only
        "gunicorn",
        # TUI not needed in .exe (desktop mode)
        "textual",
        # Testing
        "test", "pytest", "respx", "pytest_asyncio", "pytest_httpx",
        # Heavy ML/science stack (dragged in transitively, not used by mouseion)
        "torch", "torchvision", "torchaudio",
        "transformers", "tokenizers", "huggingface_hub",
        "tensorflow", "keras",
        "numpy", "scipy", "sklearn", "scikit-learn",
        "pandas", "pyarrow",
        "matplotlib", "PIL", "Pillow",
        # Other heavy transitive deps not needed
        "IPython", "jedi", "parso", "pygments",
        "sphinx", "docutils", "babel",
        "sqlalchemy",
        "notebook", "jupyter", "jupyterlab",
        "pydantic",
        "fsspec",
        "win32com", "pywin32",
        "pip",
    ],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="mouseion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False: this is a windowed desktop app (pywebview). A console
    # would pop a visible cmd.exe terminal on every launch. All diagnostics go
    # to the rotating log file (mouseion.log), so no console is needed.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='mouseion.ico',
)
