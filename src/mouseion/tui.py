"""
mouseion TUI — Terminal User Interface

A keyboard-driven reference manager built with Textual.

Launch
------
    mouseion gui           # after pip install
    python -m mouseion.tui # dev / no-install

Layout
------
  ┌─────────────────────────────────────────────────────────────┐
  │  mouseion                              12:34        Help   │
  ├─────────────────────────────────────────────────────────────┤
  │  🔍  Search…         [All] [Article] [Preprint] [Book] [OA] │
  ├───────────────────────────────┬─────────────────────────────┤
  │  ● Title           Auth  Year │  Full Title                 │
  │  ──────────────────────────── │  Author A; Author B (2024)  │
  │  ● Attention Is All You Need  │  Nature · DOI: 10.xxx       │
  │  ● Deep Learning              │                             │
  │  ● CRISPR Advances            │  Tags: `ml`  `review`       │
  │                               │                             │
  │                               │  Abstract: …                │
  ├───────────────────────────────┴─────────────────────────────┤
  │  [a]Add  [/]Search  [t]Tag  [e]Export  [d]Delete  [q]Quit   │
  └─────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import List, Optional, Tuple

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Static,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ref_id(ref) -> str:
    """Stable deterministic ID matching db._ref_id()."""
    if ref.doi:
        key = "doi:" + ref.doi.lower().strip()
    elif ref.arxiv_id:
        key = "arxiv:" + ref.arxiv_id.lower().strip()
    elif ref.pmid:
        key = "pmid:" + ref.pmid.strip()
    elif ref.isbn:
        key = "isbn:" + re.sub(r"[-\s]", "", ref.isbn)
    else:
        title = (ref.title or "").lower().strip()
        year  = str(ref.year or "")
        auth  = ref.authors[0].family.lower() if ref.authors else ""
        key   = f"title:{title}:{year}:{auth}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def _fmt_authors(authors) -> str:
    if not authors:
        return "—"
    first = authors[0].family or authors[0].given or "?"
    return first if len(authors) == 1 else f"{first} et al."


def _fmt_title(title: Optional[str], width: int = 44) -> str:
    if not title:
        return "(untitled)"
    return title[:width] + "…" if len(title) > width else title


_TYPE_SHORT = {
    "journal-article":     "Article",
    "preprint":            "Preprint",
    "book":                "Book",
    "book-chapter":        "Chapter",
    "conference-paper":    "Conf.",
    "thesis":              "Thesis",
    "dataset":             "Dataset",
    "report":              "Report",
    "website":             "Web",
    "other":               "Other",
    "unknown":             "?",
}


def _completeness_dot(score: float) -> Text:
    color = "green" if score >= 0.8 else "yellow" if score >= 0.4 else "red"
    return Text("●", style=f"bold {color}")


def _ref_to_detail_md(ref, tags: List[str]) -> str:
    """Render a Reference as Markdown for the detail panel."""
    parts: List[str] = []

    # Title
    title = ref.title or "(untitled)"
    parts.append(f"## {title}\n")

    # Authors + year
    if ref.authors:
        names = "; ".join(
            f"{a.family}, {a.given}" if a.given else a.family
            for a in ref.authors[:8]
        )
        if len(ref.authors) > 8:
            names += " et al."
        year_str = f" ({ref.year})" if ref.year else ""
        parts.append(f"**{names}**{year_str}\n")

    # Venue
    venue = ref.journal or ref.container_title or ref.publisher or ""
    if venue:
        parts.append(f"*{venue}*\n")

    parts.append("")

    # Identifiers
    ids = []
    if ref.doi:
        ids.append(f"DOI: `{ref.doi}`")
    if ref.arxiv_id:
        ids.append(f"arXiv: `{ref.arxiv_id}`")
    if ref.pmid:
        ids.append(f"PMID: `{ref.pmid}`")
    if ref.isbn:
        ids.append(f"ISBN: `{ref.isbn}`")
    if ids:
        parts.append("  \n".join(ids) + "\n")

    # Open access
    if ref.open_access:
        parts.append("🔓 **Open Access**\n")

    # Tags
    if tags:
        tag_str = "  ".join(f"`{t}`" for t in tags)
        parts.append(f"**Tags:** {tag_str}\n")

    # Abstract
    if ref.abstract:
        snip = ref.abstract[:500]
        if len(ref.abstract) > 500:
            snip += "…"
        parts.append(f"---\n\n**Abstract:** {snip}\n")

    # Completeness
    score = ref.completeness
    filled = round(score * 10)
    bar = "▓" * filled + "░" * (10 - filled)
    parts.append(f"\n---\n**Completeness:** {bar} {int(score * 100)}%")

    if ref.citation_count is not None:
        parts.append(f"  \n**Citations:** {ref.citation_count:,}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Add Modal
# ---------------------------------------------------------------------------

class AddModal(ModalScreen):
    """Overlay: add one or more references by any identifier."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="add-dialog"):
            yield Label("Add Reference", classes="modal-title")
            yield Label(
                "Enter a DOI, arXiv ID, URL, PMID, title, or paste BibTeX / RIS.\n"
                "Separate multiple entries with newlines or semicolons.",
                classes="modal-hint",
            )
            yield Input(
                placeholder="10.1038/nature12373  ·  arXiv:1706.03762  ·  …",
                id="add-input",
            )
            yield Static("", id="add-status")
            with Horizontal(classes="modal-buttons"):
                yield Button("Add & Enrich", variant="primary", id="btn-add")
                yield Button("Cancel", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-add")
    @on(Input.Submitted, "#add-input")
    def _handle_add(self) -> None:
        inp = self.query_one("#add-input", Input)
        text = inp.value.strip()
        if not text:
            return
        self.query_one("#add-status", Static).update("[yellow]Looking up…[/yellow]")
        self.query_one("#btn-add", Button).disabled = True
        inp.disabled = True
        # Delegate to the app's async worker (keeps modal alive while enriching)
        self.app._start_add(text, self)  # type: ignore[attr-defined]

    @on(Button.Pressed, "#btn-cancel")
    def _handle_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Tag Modal
# ---------------------------------------------------------------------------

class TagModal(ModalScreen):
    """Overlay: view, add, and remove tags on a single reference."""

    BINDINGS = [Binding("escape", "dismiss_done", "Done")]

    def __init__(self, ref_id: str, title: str, current_tags: List[str]) -> None:
        super().__init__()
        self._ref_id = ref_id
        self._title  = title
        self._tags   = list(current_tags)

    def compose(self) -> ComposeResult:
        with Vertical(id="tag-dialog"):
            yield Label("Manage Tags", classes="modal-title")
            yield Label(
                f"Reference: {self._title[:60]}",
                classes="modal-hint",
            )
            yield Static(self._render_tags(), id="tag-chips")
            yield Label(
                "Add a tag and press ↵  •  type /rm tagname to remove",
                classes="modal-hint",
            )
            yield Input(placeholder="Add tag…", id="tag-input")
            with Horizontal(classes="modal-buttons"):
                yield Button("Done", variant="primary", id="btn-done")

    def _render_tags(self) -> str:
        if not self._tags:
            return "[dim]No tags yet — type one below[/dim]"
        return "  ".join(f"[on navy_blue] {t} [/on navy_blue]" for t in self._tags)

    @on(Input.Submitted, "#tag-input")
    def _handle_tag_input(self) -> None:
        inp = self.query_one("#tag-input", Input)
        text = inp.value.strip()
        if not text:
            return
        inp.value = ""

        if text.startswith("/rm "):
            tag_name = text[4:].strip()
            if tag_name in self._tags:
                self._tags.remove(tag_name)
                try:
                    from .db import RefDatabase
                    with RefDatabase() as db:
                        db.remove_tag(self._ref_id, tag_name)
                except Exception:
                    pass
        else:
            tag = text.lower().strip()
            if tag and tag not in self._tags:
                self._tags.append(tag)
                try:
                    from .db import RefDatabase
                    with RefDatabase() as db:
                        db.add_tags(self._ref_id, [tag])
                except Exception:
                    pass

        self.query_one("#tag-chips", Static).update(self._render_tags())

    @on(Button.Pressed, "#btn-done")
    def _handle_done(self) -> None:
        self.dismiss(self._tags)

    def action_dismiss_done(self) -> None:
        self.dismiss(self._tags)


# ---------------------------------------------------------------------------
# Export Modal
# ---------------------------------------------------------------------------

class ExportModal(ModalScreen):
    """Overlay: choose format and destination for export."""

    BINDINGS = [Binding("escape", "dismiss_cancel", "Cancel")]

    _fmt: str = "bibtex"

    def compose(self) -> ComposeResult:
        with Vertical(id="export-dialog"):
            yield Label("Export References", classes="modal-title")
            yield Label("Format:", classes="modal-hint")
            with Horizontal(id="fmt-row"):
                yield Button("BibTeX", id="fmt-bibtex", variant="primary")
                yield Button("RIS",    id="fmt-ris",    variant="default")
                yield Button("Markdown", id="fmt-md",  variant="default")
            yield Label("Output file:", classes="modal-hint")
            yield Input(value="refs.bib", id="export-path")
            yield Static("", id="export-status")
            with Horizontal(classes="modal-buttons"):
                yield Button("Export", variant="primary", id="btn-export")
                yield Button("Cancel", variant="default", id="btn-cancel")

    # ---- format toggle buttons ----
    @on(Button.Pressed, "#fmt-bibtex")
    def _set_bibtex(self) -> None:
        self._fmt = "bibtex"
        self._sync_ext(".bib")
        self._highlight("fmt-bibtex")

    @on(Button.Pressed, "#fmt-ris")
    def _set_ris(self) -> None:
        self._fmt = "ris"
        self._sync_ext(".ris")
        self._highlight("fmt-ris")

    @on(Button.Pressed, "#fmt-md")
    def _set_md(self) -> None:
        self._fmt = "markdown"
        self._sync_ext(".md")
        self._highlight("fmt-md")

    def _sync_ext(self, ext: str) -> None:
        inp = self.query_one("#export-path", Input)
        p = Path(inp.value)
        inp.value = str(p.with_suffix(ext))

    def _highlight(self, active_id: str) -> None:
        for fid in ("fmt-bibtex", "fmt-ris", "fmt-md"):
            self.query_one(f"#{fid}", Button).variant = (
                "primary" if fid == active_id else "default"
            )

    @on(Button.Pressed, "#btn-export")
    def _handle_export(self) -> None:
        path = self.query_one("#export-path", Input).value.strip()
        if path:
            self.dismiss((self._fmt, path))

    @on(Button.Pressed, "#btn-cancel")
    def _handle_cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Confirm Modal
# ---------------------------------------------------------------------------

class ConfirmModal(ModalScreen):
    """Generic yes/no confirmation overlay."""

    BINDINGS = [Binding("escape", "dismiss_no", "Cancel")]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self._message, id="confirm-msg")
            with Horizontal(classes="modal-buttons"):
                yield Button("Confirm", variant="error",   id="btn-yes")
                yield Button("Cancel",  variant="default", id="btn-no")

    @on(Button.Pressed, "#btn-yes")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-no")
    def _cancel(self) -> None:
        self.dismiss(False)

    def action_dismiss_no(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# App CSS
# ---------------------------------------------------------------------------

_CSS = """
/* ── Global ── */
Screen {
    background: $surface;
}

/* ── Search bar ── */
#search-bar {
    height: 3;
    background: $surface-darken-1;
    padding: 0 1;
}
#search-input {
    width: 1fr;
    margin-right: 1;
}
.filter-btn {
    min-width: 10;
    height: 3;
    border: none;
    background: $surface-darken-1;
    color: $text-muted;
}
.filter-btn:hover {
    background: $primary-darken-1;
    color: $text;
}
.filter-active {
    background: $primary;
    color: $text;
    text-style: bold;
}

/* ── Split layout ── */
#split {
    height: 1fr;
}
#table-pane {
    width: 60%;
    border-right: solid $primary-darken-2;
}
#detail-pane {
    width: 40%;
    overflow-y: auto;
    padding: 1 2;
}
#detail-placeholder {
    color: $text-muted;
    content-align: center middle;
    height: 100%;
    width: 100%;
}

/* ── Status bar ── */
#status-bar {
    height: 1;
    background: $primary-darken-2;
    padding: 0 2;
    color: $text-muted;
}

/* ── Modal shared ── */
AddModal, TagModal, ExportModal, ConfirmModal {
    align: center middle;
}
#add-dialog, #tag-dialog, #export-dialog, #confirm-dialog {
    padding: 2 3;
    background: $surface;
    border: double $primary;
    width: 68;
    height: auto;
}
.modal-title {
    text-style: bold;
    color: $primary;
    margin-bottom: 1;
}
.modal-hint {
    color: $text-muted;
    margin-bottom: 1;
}
.modal-buttons {
    margin-top: 1;
    height: 3;
    align: right middle;
}
.modal-buttons Button {
    margin-left: 1;
}
#add-status {
    height: 2;
}
#export-status {
    height: 2;
}
#tag-chips {
    margin-bottom: 1;
}
#fmt-row {
    height: 3;
    margin-bottom: 1;
}
#fmt-row Button {
    margin-right: 1;
}
#confirm-msg {
    margin-bottom: 1;
}

/* ── DataTable ── */
DataTable {
    height: 1fr;
}
"""


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class MouseionApp(App):
    """Mouseion — keyboard-driven reference manager."""

    CSS        = _CSS
    TITLE      = "mouseion"
    SUB_TITLE  = "Reference Manager"

    BINDINGS = [
        Binding("a",      "add_ref",     "Add",    show=True),
        Binding("slash",  "focus_search","Search", show=True),
        Binding("t",      "tag_ref",     "Tag",    show=True),
        Binding("e",      "export_refs", "Export", show=True),
        Binding("d",      "delete_ref",  "Delete", show=True),
        Binding("r",      "refresh",     "Refresh",show=True),
        Binding("o",      "open_url",    "Open",   show=False),
        Binding("q",      "quit",        "Quit",   show=True),
        Binding("ctrl+c", "quit",        "Quit",   show=False),
    ]

    # ---- reactive state ----
    _query:       reactive[str]           = reactive("")
    _type_filter: reactive[Optional[str]] = reactive(None)
    _oa_filter:   reactive[bool]          = reactive(False)

    # Current rows: (ref_id, ref, tags)
    _rows: List[Tuple[str, object, List[str]]] = []
    _selected_id: Optional[str] = None

    # ---- compose ----

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            with Horizontal(id="search-bar"):
                yield Input(
                    placeholder="🔍  Search by title, author, DOI, keyword…",
                    id="search-input",
                )
                yield Button("All",      id="f-all",      classes="filter-btn filter-active")
                yield Button("Article",  id="f-article",  classes="filter-btn")
                yield Button("Preprint", id="f-preprint", classes="filter-btn")
                yield Button("Book",     id="f-book",     classes="filter-btn")
                yield Button("OA",       id="f-oa",       classes="filter-btn")
            with Horizontal(id="split"):
                with Vertical(id="table-pane"):
                    yield DataTable(id="ref-table", cursor_type="row", zebra_stripes=True)
                with ScrollableContainer(id="detail-pane"):
                    yield Markdown("", id="detail-md")
                    yield Static(
                        "\n\n  Select a reference to see details\n\n"
                        "  Press [bold cyan]a[/] to add your first reference.",
                        id="detail-placeholder",
                    )
            yield Static("", id="status-bar")
        yield Footer()

    # ---- lifecycle ----

    def on_mount(self) -> None:
        table = self.query_one("#ref-table", DataTable)
        table.add_column("",       width=2,  key="status")
        table.add_column("Title",  width=46, key="title")
        table.add_column("Author", width=18, key="authors")
        table.add_column("Year",   width=6,  key="year")
        table.add_column("Type",   width=9,  key="type")
        table.add_column("Tags",             key="tags")
        self._load_refs()

    # ---- events ----

    @on(DataTable.RowHighlighted, "#ref-table")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value:
            self._show_detail(str(event.row_key.value))

    @on(Input.Changed, "#search-input")
    def _search_changed(self, event: Input.Changed) -> None:
        self._query = event.value
        self._load_refs()

    # ---- filter buttons ----

    @on(Button.Pressed, "#f-all")
    def _filter_all(self) -> None:
        self._type_filter = None
        self._oa_filter   = False
        self._set_filter_active("f-all")
        self._load_refs()

    @on(Button.Pressed, "#f-article")
    def _filter_article(self) -> None:
        self._type_filter = "journal-article"
        self._oa_filter   = False
        self._set_filter_active("f-article")
        self._load_refs()

    @on(Button.Pressed, "#f-preprint")
    def _filter_preprint(self) -> None:
        self._type_filter = "preprint"
        self._oa_filter   = False
        self._set_filter_active("f-preprint")
        self._load_refs()

    @on(Button.Pressed, "#f-book")
    def _filter_book(self) -> None:
        self._type_filter = "book"
        self._oa_filter   = False
        self._set_filter_active("f-book")
        self._load_refs()

    @on(Button.Pressed, "#f-oa")
    def _filter_oa(self) -> None:
        self._type_filter = None
        self._oa_filter   = True
        self._set_filter_active("f-oa")
        self._load_refs()

    def _set_filter_active(self, active_id: str) -> None:
        for fid in ("f-all", "f-article", "f-preprint", "f-book", "f-oa"):
            btn = self.query_one(f"#{fid}", Button)
            if fid == active_id:
                btn.add_class("filter-active")
            else:
                btn.remove_class("filter-active")

    # ---- actions ----

    def action_add_ref(self) -> None:
        self.push_screen(AddModal(), self._on_add_done)

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_tag_ref(self) -> None:
        if not self._selected_id:
            self.notify("Select a reference first", severity="warning")
            return
        row = self._find_row(self._selected_id)
        if not row:
            return
        ref_id, ref, tags = row
        title = (ref.title or "(untitled)")[:60]
        self.push_screen(TagModal(ref_id, title, tags), self._on_tag_done)

    def action_export_refs(self) -> None:
        self.push_screen(ExportModal(), self._on_export_done)

    def action_delete_ref(self) -> None:
        if not self._selected_id:
            self.notify("Select a reference first", severity="warning")
            return
        row = self._find_row(self._selected_id)
        if not row:
            return
        _, ref, _ = row
        title = (ref.title or "this reference")[:60]
        self.push_screen(
            ConfirmModal(f"Delete \"{title}\"?\nThis cannot be undone."),
            self._on_delete_done,
        )

    def action_refresh(self) -> None:
        self._load_refs()
        self.notify("Refreshed", severity="information")

    def action_open_url(self) -> None:
        if not self._selected_id:
            return
        row = self._find_row(self._selected_id)
        if not row:
            return
        _, ref, _ = row
        url = ref.oa_url or ref.url or (
            f"https://doi.org/{ref.doi}" if ref.doi else None
        )
        if url:
            import sys, subprocess
            if sys.platform == "win32":
                import os; os.startfile(url)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["xdg-open", url])
            self.notify(f"Opening: {url[:60]}")
        else:
            self.notify("No URL available", severity="warning")

    # ---- modal callbacks ----

    def _on_add_done(self, result) -> None:
        if result:
            self._load_refs()

    def _on_tag_done(self, updated_tags) -> None:
        if updated_tags is not None and self._selected_id:
            # Update in-memory row
            for i, (rid, ref, _) in enumerate(self._rows):
                if rid == self._selected_id:
                    self._rows[i] = (rid, ref, updated_tags)
                    break
            self._show_detail(self._selected_id)
            self._update_status()

    def _on_export_done(self, result) -> None:
        if not result:
            return
        fmt, path_str = result
        self._do_export(fmt, path_str)

    def _on_delete_done(self, confirmed: bool) -> None:
        if not confirmed or not self._selected_id:
            return
        try:
            from .db import RefDatabase
            with RefDatabase() as db:
                db.delete(self._selected_id)
        except Exception as e:
            self.notify(f"Delete failed: {e}", severity="error")
            return
        self.notify("Reference deleted", severity="information")
        self._selected_id = None
        self._clear_detail()
        self._load_refs()

    # ---- workers ----

    @work(thread=True)
    def _load_refs(self) -> None:
        """Reload references from DB in a thread."""
        try:
            from .db import RefDatabase
            with RefDatabase() as db:
                q = self._query.strip()
                raw = db.search(
                        q or "",
                        ref_type=self._type_filter,
                        oa_only=self._oa_filter,
                        limit=10_000_000,
                    )
                ref_ids = [_ref_id(ref) for ref, _ in raw]
                tags_map = db.get_tags_batch(ref_ids)
                result = [(rid, ref, tags_map[rid])
                          for (ref, _score), rid in zip(raw, ref_ids)]
        except Exception as e:
            self.call_from_thread(
                self.notify, f"Database error: {e}", severity="error"
            )
            return
        self.call_from_thread(self._populate_table, result)

    @work
    async def _start_add(self, text: str, modal: AddModal) -> None:
        """Parse, enrich, and save references (async worker, stays on event loop)."""
        try:
            from .input import parse_input
            from .lookup import enrich_batch
            from .tagger import auto_tag, tag_from_keywords
            from .config import get_config
            from .db import RefDatabase

            refs = parse_input(text)
            if not refs:
                modal.query_one("#add-status", Static).update(
                    "[red]Could not parse — try a DOI, arXiv ID, or URL[/red]"
                )
                modal.query_one("#btn-add", Button).disabled = False
                modal.query_one("#add-input", Input).disabled = False
                return

            modal.query_one("#add-status", Static).update(
                f"[yellow]Enriching {len(refs)} reference(s)…[/yellow]"
            )
            enriched = await enrich_batch(refs)

            cfg = get_config()
            with RefDatabase() as db:
                for ref in enriched:
                    tags = list(set(auto_tag(ref, cfg) + tag_from_keywords(ref)))
                    db.upsert(ref, tags=tags)

            modal.dismiss(True)
            n = len(enriched)
            self.notify(
                f"Added {n} reference{'s' if n != 1 else ''}",
                severity="information",
            )

        except Exception as e:
            modal.query_one("#add-status", Static).update(
                f"[red]Error: {e}[/red]"
            )
            modal.query_one("#btn-add", Button).disabled = False
            modal.query_one("#add-input", Input).disabled = False

    @work(thread=True)
    def _do_export(self, fmt: str, path_str: str) -> None:
        """Run the export in a thread."""
        try:
            from .db import RefDatabase
            from .exporters.bibtex   import export_bibtex_file
            from .exporters.ris      import export_ris_file
            from .exporters.markdown import export_markdown_file

            with RefDatabase() as db:
                refs = db.list_all(limit=10_000_000)
            path = Path(path_str).expanduser()

            if fmt == "bibtex":
                export_bibtex_file(refs, path)
            elif fmt == "ris":
                export_ris_file(refs, path)
            else:
                export_markdown_file(refs, path)

            self.call_from_thread(
                self.notify,
                f"Exported {len(refs)} refs → {path}",
                severity="information",
            )
        except Exception as e:
            self.call_from_thread(
                self.notify, f"Export failed: {e}", severity="error"
            )

    # ---- private helpers ----

    def _populate_table(
        self, rows: List[Tuple[str, object, List[str]]]
    ) -> None:
        """Update the DataTable with a new list of rows (main-thread safe)."""
        self._rows = rows
        table = self.query_one("#ref-table", DataTable)
        prev_id = self._selected_id
        table.clear()

        for ref_id, ref, tags in rows:
            dot     = _completeness_dot(ref.completeness)
            title   = _fmt_title(ref.title)
            authors = _fmt_authors(ref.authors)
            year    = str(ref.year) if ref.year else "—"
            rtype   = _TYPE_SHORT.get(
                ref.ref_type.value if ref.ref_type else "unknown", "?"
            )
            tag_str = ", ".join(tags[:4]) + ("…" if len(tags) > 4 else "")
            table.add_row(dot, title, authors, year, rtype, tag_str, key=ref_id)

        # Restore cursor to previously selected row
        if prev_id and rows:
            for i, (rid, _, _) in enumerate(rows):
                if rid == prev_id:
                    table.move_cursor(row=i)
                    break

        self._update_status()

        # Show empty-state hint when table is empty
        placeholder = self.query_one("#detail-placeholder", Static)
        if not rows:
            placeholder.display = True
        elif self._selected_id is None:
            placeholder.display = True

    def _show_detail(self, ref_id: str) -> None:
        """Populate the detail panel for the given ref_id."""
        self._selected_id = ref_id
        row = self._find_row(ref_id)
        if not row:
            return
        _, ref, tags = row
        md_text = _ref_to_detail_md(ref, tags)
        self.query_one("#detail-md", Markdown).update(md_text)
        self.query_one("#detail-placeholder", Static).display = False

    def _clear_detail(self) -> None:
        self.query_one("#detail-md", Markdown).update("")
        self.query_one("#detail-placeholder", Static).display = True

    def _find_row(self, ref_id: str):
        for row in self._rows:
            if row[0] == ref_id:
                return row
        return None

    def _update_status(self) -> None:
        n = len(self._rows)
        if n == 0:
            msg = "No references — press [bold cyan]a[/] to add one"
        else:
            avg = sum(r.completeness for _, r, _ in self._rows) / n
            msg = (
                f"{n} reference{'s' if n != 1 else ''}  ·  "
                f"avg completeness {int(avg * 100)}%"
            )
        self.query_one("#status-bar", Static).update(msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """Launch the TUI. Called by `mouseion gui` and the `mouseion-gui` script."""
    MouseionApp().run()


if __name__ == "__main__":
    run()
