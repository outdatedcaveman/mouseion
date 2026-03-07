"""
Google Drive PDF storage.

PDFs are uploaded to a designated Drive folder and tracked by their Drive
file ID (stored in ``refs.pdf_drive_id``).  The web UI can then redirect to
the Drive view URL instead of serving files from disk, keeping the server's
persistent volume small.

Authentication — service account (headless, no OAuth browser flow):
  1. Go to https://console.cloud.google.com → APIs & Services → Credentials
  2. Create a Service Account and download its JSON key
  3. Enable the Google Drive API for the project
  4. Share the target Drive folder with the service account's email
     (Editor permission is sufficient)
  5. Supply credentials via ONE of:
       • GOOGLE_DRIVE_CREDENTIALS_JSON env var  (recommended for Fly.io —
         paste the full JSON string as a secret)
       • google_drive_credentials_path in ~/.config/zoterpile/config.toml
         (local / Docker volume approach)
  6. Supply the folder ID via ONE of:
       • GOOGLE_DRIVE_FOLDER_ID env var (Fly.io secret)
       • google_drive_folder_id in config.toml
     The folder ID is the last path segment of the folder's URL:
       https://drive.google.com/drive/folders/<FOLDER_ID>
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Optional

from ..models import Reference

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_MIME_PDF = "application/pdf"


def _build_service():
    """Return an authenticated Drive v3 service client.

    Prefers the GOOGLE_DRIVE_CREDENTIALS_JSON env var (suitable for secrets
    managers / Fly.io) and falls back to the file path in config.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Google Drive support requires extra dependencies.\n"
            "Install with:  pip install 'zoterpile[drive]'"
        )

    raw_json = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        info = json.loads(raw_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES
        )
    else:
        from ..config import get_config
        path = get_config().google_drive_credentials_path
        if not path:
            raise ValueError(
                "Google Drive credentials not configured. "
                "Set GOOGLE_DRIVE_CREDENTIALS_JSON (env var) or "
                "google_drive_credentials_path in config.toml."
            )
        creds = service_account.Credentials.from_service_account_file(
            str(Path(path).expanduser()), scopes=_SCOPES
        )

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _folder_id() -> Optional[str]:
    """Return the configured Drive folder ID, or None to use Drive root."""
    folder = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if folder:
        return folder
    from ..config import get_config
    return get_config().google_drive_folder_id or None


def upload_pdf(local_path: Path, ref: Reference) -> str:
    """Upload *local_path* to Google Drive and return its file ID.

    The file is placed in the configured folder (Drive root if unconfigured).
    Existing files with the same name in the same folder are NOT deduplicated
    by Drive — the DB ``pdf_drive_id`` is the canonical record.
    """
    from googleapiclient.http import MediaFileUpload

    service = _build_service()
    metadata: dict = {"name": local_path.name, "mimeType": _MIME_PDF}
    fid = _folder_id()
    if fid:
        metadata["parents"] = [fid]

    media = MediaFileUpload(str(local_path), mimetype=_MIME_PDF, resumable=True)
    result = (
        service.files()
        .create(body=metadata, media_body=media, fields="id")
        .execute()
    )
    return result["id"]


def get_view_url(drive_id: str) -> str:
    """Return a shareable Google Drive viewer URL for the file."""
    return f"https://drive.google.com/file/d/{drive_id}/view"


def stream_pdf(drive_id: str) -> bytes:
    """Download PDF bytes from Drive (for proxying through the web UI)."""
    from googleapiclient.http import MediaIoBaseDownload

    service = _build_service()
    request = service.files().get_media(fileId=drive_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def delete_file(drive_id: str) -> None:
    """Permanently delete a file from Drive (no trash)."""
    _build_service().files().delete(fileId=drive_id).execute()


def is_configured() -> bool:
    """Return True if at least credentials are available."""
    if os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON", "").strip():
        return True
    try:
        from ..config import get_config
        return bool(get_config().google_drive_credentials_path)
    except Exception:
        return False
