"""
Configuration management.

Config is loaded from (in increasing priority order):
  1. Built-in defaults
  2. ~/.config/zoterpile/config.toml
  3. ZOTERPILE_* environment variables
  4. .env file in the current working directory

Call `get_config()` anywhere in the codebase.
Call `save_config(cfg)` to persist changes.
Run `zoterpile init-config` to create a template config file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

# TOML: use stdlib on 3.11+, otherwise fall back to tomlkit for both read+write
try:
    import tomllib                       # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib          # type: ignore[no-redef]
    except ImportError:
        tomllib = None                   # type: ignore[assignment]

try:
    import tomlkit                       # for writing; always preferred
    _HAS_TOMLKIT = True
except ImportError:
    _HAS_TOMLKIT = False


_CONFIG_PATH = Path.home() / ".config" / "zoterpile" / "config.toml"
_DEFAULT_DB   = Path.home() / ".local" / "share" / "zoterpile" / "refs.db"
_DEFAULT_PDFS = Path.home() / ".local" / "share" / "zoterpile" / "pdfs"


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class AutoTagRule:
    """A single auto-tagging rule."""
    tags: List[str] = field(default_factory=list)
    # Matching criteria (any can be set; all that are set must match)
    keywords: List[str] = field(default_factory=list)    # match in title/abstract/keywords
    journal_pattern: str = ""                             # regex on journal name
    ref_type: str = ""                                    # exact match on ref_type value
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    open_access_only: bool = False


@dataclass
class Config:
    # --- Database ---
    db_path: str = str(_DEFAULT_DB)

    # --- Provider API keys ---
    crossref_email: str = ""
    semantic_scholar_api_key: str = ""
    ncbi_api_key: str = ""
    openalex_email: str = ""

    # --- PDF ---
    pdf_storage_path: str = str(_DEFAULT_PDFS)
    auto_fetch_pdfs: bool = False

    # --- Notion ---
    notion_api_key: str = ""
    notion_database_id: str = ""

    # --- Zotero ---
    zotero_api_key: str = ""
    zotero_user_id: str = ""
    zotero_library_type: str = "user"   # "user" or "group"
    zotero_library_id: str = ""         # same as user_id for user libraries
    zotero_collection_id: str = ""      # optional collection to add items to

    # --- Obsidian ---
    obsidian_vault_path: str = ""
    obsidian_notes_folder: str = "References"
    obsidian_filename_template: str = "{cite_key}"  # or "{author} ({year}) {title}"

    # --- Google Drive ---
    google_drive_credentials_path: str = ""
    google_drive_folder_id: str = ""

    # --- Instapaper ---
    instapaper_username: str = ""
    instapaper_password: str = ""

    # --- Auto-tagging ---
    auto_tag_rules: List[AutoTagRule] = field(default_factory=list)
    # Built-in tags always applied based on ref properties
    tag_by_type: bool = True        # e.g. "journal", "preprint", "book"
    tag_open_access: bool = True    # add "open-access" tag when OA is True
    tag_by_year: bool = False       # add "2023", "2024" etc.

    def __post_init__(self):
        # Expand ~ in paths
        for attr in ("db_path", "pdf_storage_path",
                     "obsidian_vault_path", "google_drive_credentials_path"):
            val = getattr(self, attr)
            if val:
                setattr(self, attr, str(Path(val).expanduser()))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[Config] = None


def get_config(reload: bool = False) -> Config:
    """Return the global Config instance (loaded on first call)."""
    global _instance
    if _instance is None or reload:
        _instance = _load()
    return _instance


def save_config(cfg: Optional[Config] = None) -> None:
    """Persist the given (or global) Config to disk."""
    if cfg is None:
        cfg = get_config()
    _save(cfg)


# ---------------------------------------------------------------------------
# Load / save internals
# ---------------------------------------------------------------------------

def _load() -> Config:
    cfg = Config()

    # 1. Load from file
    if _CONFIG_PATH.exists():
        raw = _read_toml(_CONFIG_PATH)
        _apply_toml(cfg, raw)

    # 2. .env in current working dir
    _load_dotenv()

    # 3. Environment variables (ZOTERPILE_*)
    _apply_env(cfg)

    cfg.__post_init__()
    return cfg


def _apply_toml(cfg: Config, raw: dict) -> None:
    """Apply a loaded TOML dict to a Config instance."""
    simple_sections = {
        "database":     {"path": "db_path"},
        "providers":    {
            "crossref_email": "crossref_email",
            "semantic_scholar_api_key": "semantic_scholar_api_key",
            "ncbi_api_key": "ncbi_api_key",
            "openalex_email": "openalex_email",
        },
        "pdf":          {"storage_path": "pdf_storage_path", "auto_fetch": "auto_fetch_pdfs"},
        "notion":       {"api_key": "notion_api_key", "database_id": "notion_database_id"},
        "zotero":       {
            "api_key": "zotero_api_key",
            "user_id": "zotero_user_id",
            "library_type": "zotero_library_type",
            "library_id": "zotero_library_id",
            "collection_id": "zotero_collection_id",
        },
        "obsidian":     {
            "vault_path": "obsidian_vault_path",
            "notes_folder": "obsidian_notes_folder",
            "filename_template": "obsidian_filename_template",
        },
        "google_drive": {
            "credentials_path": "google_drive_credentials_path",
            "folder_id": "google_drive_folder_id",
        },
        "instapaper":   {"username": "instapaper_username", "password": "instapaper_password"},
        "auto_tag":     {
            "tag_by_type": "tag_by_type",
            "tag_open_access": "tag_open_access",
            "tag_by_year": "tag_by_year",
        },
    }
    for section, mapping in simple_sections.items():
        sec = raw.get(section, {})
        for toml_key, cfg_attr in mapping.items():
            if toml_key in sec:
                setattr(cfg, cfg_attr, sec[toml_key])

    # Auto-tag rules
    for rule_dict in raw.get("auto_tag", {}).get("rules", []):
        rule = AutoTagRule(
            tags=rule_dict.get("tags", []),
            keywords=rule_dict.get("keywords", []),
            journal_pattern=rule_dict.get("journal_pattern", ""),
            ref_type=rule_dict.get("ref_type", ""),
            year_from=rule_dict.get("year_from"),
            year_to=rule_dict.get("year_to"),
            open_access_only=rule_dict.get("open_access_only", False),
        )
        cfg.auto_tag_rules.append(rule)


_ENV_MAP: Dict[str, str] = {
    "ZOTERPILE_DB_PATH":                    "db_path",
    "ZOTERPILE_CROSSREF_EMAIL":             "crossref_email",
    "ZOTERPILE_S2_API_KEY":                 "semantic_scholar_api_key",
    "ZOTERPILE_NCBI_API_KEY":               "ncbi_api_key",
    "ZOTERPILE_OPENALEX_EMAIL":             "openalex_email",
    "ZOTERPILE_NOTION_API_KEY":             "notion_api_key",
    "ZOTERPILE_NOTION_DATABASE_ID":         "notion_database_id",
    "ZOTERPILE_ZOTERO_API_KEY":             "zotero_api_key",
    "ZOTERPILE_ZOTERO_USER_ID":             "zotero_user_id",
    "ZOTERPILE_ZOTERO_LIBRARY_ID":          "zotero_library_id",
    "ZOTERPILE_OBSIDIAN_VAULT_PATH":        "obsidian_vault_path",
    "ZOTERPILE_INSTAPAPER_USERNAME":        "instapaper_username",
    "ZOTERPILE_INSTAPAPER_PASSWORD":        "instapaper_password",
    "CROSSREF_EMAIL":                       "crossref_email",
    "SEMANTIC_SCHOLAR_API_KEY":             "semantic_scholar_api_key",
    "NCBI_API_KEY":                         "ncbi_api_key",
    "OPENALEX_EMAIL":                       "openalex_email",
}


def _apply_env(cfg: Config) -> None:
    for env_var, attr in _ENV_MAP.items():
        val = os.environ.get(env_var, "")
        if val:
            setattr(cfg, attr, val)


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass


def _read_toml(path: Path) -> dict:
    if _HAS_TOMLKIT:
        import tomlkit
        return dict(tomlkit.parse(path.read_text()))
    if tomllib is not None:
        with open(path, "rb") as f:
            return tomllib.load(f)
    # Fallback: empty (no TOML parser available)
    return {}


def _save(cfg: Config) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = _render_toml(cfg)
    _CONFIG_PATH.write_text(content, encoding="utf-8")


def _render_toml(cfg: Config) -> str:
    """Render Config to a human-readable TOML string with comments."""
    lines = [
        "# zoterpile configuration",
        "# Generated by: zoterpile init-config",
        "# All values can also be set via ZOTERPILE_* environment variables.",
        "",
        "[providers]",
        f'crossref_email             = "{cfg.crossref_email}"  # your email for CrossRef polite pool',
        f'semantic_scholar_api_key   = "{cfg.semantic_scholar_api_key}"',
        f'ncbi_api_key               = "{cfg.ncbi_api_key}"',
        f'openalex_email             = "{cfg.openalex_email}"',
        "",
        "[database]",
        f'path = "{cfg.db_path}"',
        "",
        "[pdf]",
        f'storage_path = "{cfg.pdf_storage_path}"',
        f"auto_fetch   = {str(cfg.auto_fetch_pdfs).lower()}",
        "",
        "[notion]",
        f'api_key     = "{cfg.notion_api_key}"',
        f'database_id = "{cfg.notion_database_id}"  # Notion database page ID',
        "",
        "[zotero]",
        f'api_key      = "{cfg.zotero_api_key}"',
        f'user_id      = "{cfg.zotero_user_id}"',
        f'library_type = "{cfg.zotero_library_type}"  # "user" or "group"',
        f'library_id   = "{cfg.zotero_library_id}"',
        f'collection_id = "{cfg.zotero_collection_id}"  # optional',
        "",
        "[obsidian]",
        f'vault_path        = "{cfg.obsidian_vault_path}"',
        f'notes_folder      = "{cfg.obsidian_notes_folder}"',
        f'filename_template = "{cfg.obsidian_filename_template}"  # {{cite_key}} or {{author}} ({{year}}) {{title}}',
        "",
        "[instapaper]",
        f'username = "{cfg.instapaper_username}"',
        f'password = "{cfg.instapaper_password}"',
        "",
        "[auto_tag]",
        f"tag_by_type    = {str(cfg.tag_by_type).lower()}",
        f"tag_open_access = {str(cfg.tag_open_access).lower()}",
        f"tag_by_year    = {str(cfg.tag_by_year).lower()}",
        "",
        "# Example auto-tag rules:",
        "# [[auto_tag.rules]]",
        '# keywords = ["machine learning", "deep learning", "neural network"]',
        '# tags = ["ML", "AI"]',
        "#",
        "# [[auto_tag.rules]]",
        '# journal_pattern = "Nature|Science|Cell|Lancet|NEJM"',
        '# tags = ["high-impact"]',
        "#",
        "# [[auto_tag.rules]]",
        '# ref_type = "preprint"',
        '# tags = ["to-review"]',
        "",
    ]
    # Append actual rules if any
    for rule in cfg.auto_tag_rules:
        lines.append("[[auto_tag.rules]]")
        if rule.keywords:
            kw = ", ".join(f'"{k}"' for k in rule.keywords)
            lines.append(f"keywords = [{kw}]")
        if rule.journal_pattern:
            lines.append(f'journal_pattern = "{rule.journal_pattern}"')
        if rule.ref_type:
            lines.append(f'ref_type = "{rule.ref_type}"')
        if rule.tags:
            tg = ", ".join(f'"{t}"' for t in rule.tags)
            lines.append(f"tags = [{tg}]")
        lines.append("")
    return "\n".join(lines)
