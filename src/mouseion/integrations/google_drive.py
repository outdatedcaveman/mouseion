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
       • google_drive_credentials_path in ~/.config/mouseion/config.toml
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
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..models import Reference

logger = logging.getLogger("mouseion.google_drive")

_SCOPES = ["https://www.googleapis.com/auth/drive"]
_MIME_PDF = "application/pdf"
_MIME_FOLDER = "application/vnd.google-apps.folder"
_MIME_SQLITE = "application/x-sqlite3"

# Cache folder IDs to avoid repeated API lookups
_folder_cache: Dict[str, str] = {}  # path-like key -> Drive folder ID


def _build_service():
    """Return an authenticated Drive v3 service client.

    Prefers the GOOGLE_DRIVE_CREDENTIALS_JSON env var (suitable for secrets
    managers / Fly.io) and falls back to the file path in config.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
    except ImportError:
        raise ImportError(
            "Google Drive support requires extra dependencies.\n"
            "Install with:  pip install 'google-api-python-client google-auth-httplib2 google-auth-oauthlib'"
        )

    raw_json = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON", "").strip()
    if raw_json:
        info = json.loads(raw_json)
        if info.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=_SCOPES
            )
        else:
            creds = Credentials.from_authorized_user_info(info, scopes=_SCOPES)
    else:
        from ..config import get_config
        path = get_config().google_drive_credentials_path
        if not path:
            raise ValueError(
                "Google Drive credentials not configured. "
                "Set GOOGLE_DRIVE_CREDENTIALS_JSON (env var) or "
                "google_drive_credentials_path in config.toml."
            )
        
        full_path = Path(path).expanduser()
        if not full_path.exists():
            raise FileNotFoundError(f"Credentials file not found at {full_path}")

        with open(str(full_path), "r") as f:
            creds_data = json.load(f)

        if creds_data.get("type") == "service_account":
            creds = service_account.Credentials.from_service_account_file(
                str(full_path), scopes=_SCOPES
            )
        else:
            token_path = Path("~/.config/mouseion/drive_token.json").expanduser()
            token_path.parent.mkdir(parents=True, exist_ok=True)
            creds = None
            if token_path.exists():
                try:
                    creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)
                except Exception:
                    pass
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    from google.auth.transport.requests import Request
                    try:
                        creds.refresh(Request())
                    except Exception:
                        creds = None
                
                if not creds or not creds.valid:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(full_path), _SCOPES
                    )
                    creds = flow.run_local_server(port=0)
                
                with open(str(token_path), "w") as token:
                    token.write(creds.to_json())

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


# ---------------------------------------------------------------------------
# Folder management
# ---------------------------------------------------------------------------

def find_or_create_subfolder(service, parent_id: str, name: str) -> str:
    """Find or create a subfolder under parent_id. Returns the folder ID.

    Results are cached in _folder_cache to avoid repeated API calls.
    """
    cache_key = f"{parent_id}/{name}"
    if cache_key in _folder_cache:
        return _folder_cache[cache_key]

    # Search for existing folder
    q = (
        f"'{parent_id}' in parents and name = '{name}' "
        f"and mimeType = '{_MIME_FOLDER}' and trashed = false"
    )
    results = service.files().list(
        q=q, fields="files(id, name)", pageSize=1
    ).execute()
    files = results.get("files", [])

    if files:
        fid = files[0]["id"]
    else:
        metadata = {
            "name": name,
            "mimeType": _MIME_FOLDER,
            "parents": [parent_id],
        }
        folder = service.files().create(
            body=metadata, fields="id"
        ).execute()
        fid = folder["id"]
        logger.info("Created Drive folder: %s/%s -> %s", parent_id[:8], name, fid[:8])

    _folder_cache[cache_key] = fid
    return fid


def ensure_folder_structure(service=None) -> Dict[str, str]:
    """Create the Mouseion folder structure on Drive. Returns folder IDs.

    Structure:
        <root>/Mouseion/
            Database/
            PDFs/
            Exports/

    If service is None, builds one via _build_service().
    Returns dict with keys: root, database, pdfs, exports
    """
    if service is None:
        service = _build_service()

    root_id = _folder_id()
    if not root_id:
        raise ValueError(
            "Google Drive folder_id not configured. "
            "Set google_drive_folder_id in config.toml or "
            "GOOGLE_DRIVE_FOLDER_ID env var."
        )

    mouseion_id = find_or_create_subfolder(service, root_id, "Mouseion")
    db_id = find_or_create_subfolder(service, mouseion_id, "Database")
    pdfs_id = find_or_create_subfolder(service, mouseion_id, "PDFs")
    exports_id = find_or_create_subfolder(service, mouseion_id, "Exports")

    return {
        "root": mouseion_id,
        "database": db_id,
        "pdfs": pdfs_id,
        "exports": exports_id,
    }


def get_year_folder(service, pdfs_folder_id: str, year: Optional[int]) -> str:
    """Return the folder ID for PDFs/{Year}/ (or PDFs/Unsorted/)."""
    name = str(year) if year and 1900 <= year <= 2100 else "Unsorted"
    return find_or_create_subfolder(service, pdfs_folder_id, name)


def _safe_filename(ref: Reference) -> str:
    """Generate a safe filename for a reference PDF on Drive."""
    parts = []
    if ref.authors:
        # First author's family name
        family = ref.authors[0].family or ref.authors[0].given or "Unknown"
        parts.append(re.sub(r'[^\w]', '_', family)[:30])
    if ref.title:
        # First few words of title
        words = re.sub(r'[^\w\s]', '', ref.title).split()[:5]
        parts.append("_".join(words)[:60])
    if not parts:
        parts.append(ref.id[:12] if ref.id else "unknown")
    name = "_".join(parts)
    # Sanitize for Drive (no slashes, limit length)
    name = re.sub(r'[/\\:*?"<>|]', '_', name)
    return name[:120] + ".pdf"


def upload_pdf_to_folder(
    local_path: Path,
    ref: Reference,
    service=None,
    pdfs_folder_id: Optional[str] = None,
) -> str:
    """Upload a PDF to the structured folder: PDFs/{Year}/{SafeName}.pdf.

    Returns the Drive file ID.
    """
    from googleapiclient.http import MediaFileUpload

    if service is None:
        service = _build_service()
    if pdfs_folder_id is None:
        folders = ensure_folder_structure(service)
        pdfs_folder_id = folders["pdfs"]

    year_folder = get_year_folder(service, pdfs_folder_id, ref.year)
    filename = _safe_filename(ref)

    metadata = {
        "name": filename,
        "mimeType": _MIME_PDF,
        "parents": [year_folder],
    }
    media = MediaFileUpload(str(local_path), mimetype=_MIME_PDF, resumable=True)
    result = service.files().create(
        body=metadata, media_body=media, fields="id"
    ).execute()

    drive_id = result["id"]
    logger.info("Uploaded PDF: %s -> Drive %s (year=%s)", filename, drive_id[:8], ref.year)
    return drive_id


def upload_db_backup(local_path: Path, service=None) -> str:
    """Upload a SQLite backup to Database/ folder, rotating the previous one.

    Returns the Drive file ID of the new backup.
    """
    from googleapiclient.http import MediaFileUpload

    if service is None:
        service = _build_service()

    folders = ensure_folder_structure(service)
    db_folder = folders["database"]

    # Find existing backup to rename as _prev
    q = (
        f"'{db_folder}' in parents and name = 'refs_backup.db' "
        f"and trashed = false"
    )
    existing = service.files().list(q=q, fields="files(id)").execute().get("files", [])

    for f in existing:
        # Rename to refs_backup_prev.db (delete any existing _prev first)
        q_prev = (
            f"'{db_folder}' in parents and name = 'refs_backup_prev.db' "
            f"and trashed = false"
        )
        prev = service.files().list(q=q_prev, fields="files(id)").execute().get("files", [])
        for p in prev:
            try:
                service.files().delete(fileId=p["id"]).execute()
            except Exception:
                pass
        try:
            service.files().update(
                fileId=f["id"], body={"name": "refs_backup_prev.db"}
            ).execute()
        except Exception:
            pass

    # Upload new backup
    metadata = {
        "name": "refs_backup.db",
        "mimeType": _MIME_SQLITE,
        "parents": [db_folder],
    }
    media = MediaFileUpload(str(local_path), mimetype=_MIME_SQLITE, resumable=True)
    result = service.files().create(
        body=metadata, media_body=media, fields="id"
    ).execute()
    logger.info("Uploaded DB backup: %s", result["id"][:8])
    return result["id"]


def list_drive_files(service, folder_id: str, mime_type: Optional[str] = None) -> List[dict]:
    """List files in a Drive folder. Returns list of {id, name, modifiedTime}."""
    q = f"'{folder_id}' in parents and trashed = false"
    if mime_type:
        q += f" and mimeType = '{mime_type}'"

    all_files = []
    page_token = None
    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken, files(id, name, modifiedTime, size)",
            pageSize=1000,
            pageToken=page_token,
        ).execute()
        all_files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return all_files


def clear_folder_cache():
    """Clear cached folder IDs (call after config changes)."""
    _folder_cache.clear()
