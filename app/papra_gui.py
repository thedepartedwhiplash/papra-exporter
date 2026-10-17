"""Desktop GUI for exporting documents from a self-hosted Papra instance.

A Flet front end for ``papra_export``. Connect with a server URL and API
token, pick an organization and optional tags, choose an output folder and
watch the export run. All network and file work is done by ``papra_export``;
this module only builds the interface.

Requirements:
  Python 3.10 or later and ``pip install flet``. ``papra_export.py`` must
  sit next to this file.

Typical usage example:

  python papra_gui.py

The API token is never written to disk, so it has to be entered again every
time the app starts. The server URL, User-Agent, output folder and theme are
remembered in ``~/.papra-exporter.json``.
"""

import json
import os
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import flet as ft
import papra_export as pe

SETTINGS_PATH = Path.home() / ".papra-exporter.json"
# The step rail stays dark in both themes, as in the mockup.
RAIL_BG = "#17202B"
RAIL_ACTIVE_BG = "#243342"
RAIL_TEXT = "#C9D3DE"
ACCENT = "#2DD4BF"
BUTTON_BG = ft.Colors.TEAL_700
# Lines kept in the on-screen log; older ones are dropped.
MAX_LOG_LINES = 500
STEPS = ["Connect", "Select", "Export"]


def load_settings() -> Dict[str, Any]:
    """Reads the saved settings.

    Returns:
        The settings, or an empty dict if there are none or they are
        unreadable.
    """
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}


def save_settings(settings: Dict[str, Any]) -> None:
    """Writes the settings, never including the API token.

    Failures are ignored because remembering settings is only a convenience.

    Args:
        settings: The settings to save.
    """
    settings.pop("token", None)
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    except OSError:
        pass


def describe(e: Exception) -> str:
    """Returns a short human-readable description of an exception."""
    if isinstance(e, urllib.error.HTTPError):
        return pe.http_error_detail(e)
    return str(e)


def open_path(path: Path) -> None:
    """Opens a file or folder with the operating system's default app.

    Args:
        path: The file or folder to open.
    """
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def muted(text: str, size: int = 12) -> ft.Text:
    """Builds a small, low-emphasis text."""
    return ft.Text(text, size=size, color=ft.Colors.ON_SURFACE_VARIANT)


def heading(title: ft.Control, subtitle: ft.Control) -> ft.Column:
    """Builds the title block shown at the top of every screen."""
    return ft.Column([title, subtitle], spacing=4)


def card(*controls: ft.Control) -> ft.Container:
    """Wraps controls in a rounded, outlined card."""
    return ft.Container(
        content=ft.Column(
            list(controls),
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        ),
        bgcolor=ft.Colors.SURFACE_CONTAINER,
        padding=20,
        border_radius=12,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
    )


def primary(text: str, on_click: Any, disabled: bool = False) -> ft.Button:
    """Builds the filled accent button."""
    return ft.Button(
        text,
        on_click=on_click,
        disabled=disabled,
        height=44,
        style=ft.ButtonStyle(
            bgcolor={
                ft.ControlState.DEFAULT: BUTTON_BG,
                ft.ControlState.DISABLED: ft.Colors.SURFACE_CONTAINER_HIGHEST,
            },
            color={
                ft.ControlState.DEFAULT: ft.Colors.WHITE,
                ft.ControlState.DISABLED: ft.Colors.ON_SURFACE_VARIANT,
            },
            shape=ft.RoundedRectangleBorder(radius=8),
        ),
    )


def secondary(
    text: str,
    on_click: Any,
    disabled: bool = False,
    visible: bool = True,
    icon: Optional[ft.IconData] = None,
) -> ft.OutlinedButton:
    """Builds the outlined button."""
    return ft.OutlinedButton(
        text,
        on_click=on_click,
        disabled=disabled,
        visible=visible,
        icon=icon,
        height=44,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)),
    )


class App:
    """The exporter window: shared state plus one builder per screen."""

    def __init__(self, page: ft.Page) -> None:
        """Configures the window and shows the first screen.

        Args:
            page: The Flet page to build on.
        """
        self.page = page
        self.settings = load_settings()
        self.step = 1
        self.base = ""
        self.token = ""
        self.orgs: List[Dict[str, Any]] = []
        self.org_id = ""
        self.docs: Optional[List[Dict[str, Any]]] = None
        self.tags: Set[str] = set()
        self.match_all = False
        self.include_trash = False
        self.out_dir = self.settings.get("out") or str(Path.home() / "papra-export")
        self.cancel = threading.Event()
        self.picker = ft.FilePicker()

        page.title = "Papra Exporter"
        page.padding = 0
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
        page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.TEAL)
        page.theme_mode = (
            ft.ThemeMode.LIGHT
            if self.settings.get("theme") == "light"
            else ft.ThemeMode.DARK
        )
        page.window.width = 1000
        page.window.height = 700
        page.window.min_width = 860
        page.window.min_height = 660
        page.services.append(self.picker)

        self.rail = ft.Container(
            width=210,
            bgcolor=RAIL_BG,
            padding=ft.Padding.symmetric(vertical=24, horizontal=16),
        )
        self.body = ft.Container(
            expand=True, padding=ft.Padding.symmetric(vertical=28, horizontal=32)
        )

        # Connect-screen controls live for the whole session so that going
        # back from a later screen finds them as the user left them.
        self.url = ft.TextField(
            label="Server URL",
            hint_text="https://papra.example.com",
            value=self.settings.get("url", ""),
            on_change=self._reset_connection,
        )
        self.token_field = ft.TextField(
            label="API token",
            password=True,
            can_reveal_password=True,
            on_change=self._reset_connection,
        )
        self.ua = ft.TextField(
            label="User-Agent (advanced)",
            value=self.settings.get("user_agent") or pe.DEFAULT_USER_AGENT,
            helper="Change this if a proxy or firewall answers with HTTP 403.",
            helper_max_lines=3,
            on_change=self._reset_connection,
        )
        self.status = ft.Text("", size=13, selectable=True)
        self.test_btn = secondary("Test connection", self._on_test)
        self.continue_btn = primary("Continue", self._show_select, disabled=True)

        page.add(ft.Row([self.rail, self.body], expand=True, spacing=0))
        self._show_connect()

    # ------------------------------------------------------------------
    # Shared layout
    # ------------------------------------------------------------------

    def _rail_content(self) -> ft.Control:
        """Builds the left rail: title, step indicator and theme toggle."""
        items: List[ft.Control] = [
            ft.Container(
                ft.Text(
                    "Papra Exporter",
                    size=16,
                    weight=ft.FontWeight.W_600,
                    color=ft.Colors.WHITE,
                ),
                padding=ft.Padding.only(left=8, bottom=18),
            )
        ]
        for n, name in enumerate(STEPS, 1):
            active, done = n == self.step, n < self.step
            ring = ACCENT if (active or done) else "#6B7B8C"
            dot = ft.Container(
                ft.Text(str(n), size=12, color="#06312C" if active else RAIL_TEXT),
                width=22,
                height=22,
                border_radius=11,
                alignment=ft.Alignment.CENTER,
                border=ft.Border.all(1.5, ring),
                bgcolor=ACCENT if active else None,
            )
            items.append(
                ft.Container(
                    ft.Row(
                        [
                            dot,
                            ft.Text(
                                name, color=(ft.Colors.WHITE if active else RAIL_TEXT)
                            ),
                        ],
                        spacing=10,
                    ),
                    padding=ft.Padding.symmetric(vertical=10, horizontal=8),
                    border_radius=8,
                    bgcolor=RAIL_ACTIVE_BG if active else None,
                )
            )
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        items.append(ft.Container(expand=True))
        items.append(
            ft.IconButton(
                icon=ft.Icons.LIGHT_MODE if dark else ft.Icons.DARK_MODE,
                icon_color=RAIL_TEXT,
                tooltip="Switch light / dark theme",
                on_click=self._toggle_theme,
            )
        )
        return ft.Column(items, spacing=6, expand=True)

    def _show(
        self, step: int, content: List[ft.Control], actions: ft.Row, scroll: bool = True
    ) -> None:
        """Displays a screen.

        Args:
            step: Which step (1-3) is active in the rail.
            content: The screen's controls.
            actions: The button row pinned to the bottom.
            scroll: Whether the content may scroll. The export screen turns
                this off so that its log can fill the free space.
        """
        self.step = step
        self.rail.content = self._rail_content()
        column = ft.Column(
            content,
            spacing=16,
            expand=True,
            scroll=ft.ScrollMode.AUTO if scroll else None,
        )
        self.body.content = ft.Column([column, actions], spacing=16, expand=True)
        self.page.update()

    def _toggle_theme(self, _e: Any) -> None:
        """Switches between the dark and light theme and remembers it."""
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        self.page.theme_mode = ft.ThemeMode.LIGHT if dark else ft.ThemeMode.DARK
        self.settings["theme"] = "light" if dark else "dark"
        save_settings(self.settings)
        self.rail.content = self._rail_content()
        self.page.update()

    @staticmethod
    def _actions(left: List[ft.Control], right: List[ft.Control]) -> ft.Row:
        """Builds the bottom button row with groups at both ends."""
        return ft.Row(
            [ft.Row(left, spacing=12), ft.Row(right, spacing=12)],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

    # ------------------------------------------------------------------
    # Screen 1: Connect
    # ------------------------------------------------------------------

    def _show_connect(self, _e: Any = None) -> None:
        """Shows the connection screen."""
        self._show(
            1,
            [
                heading(
                    ft.Text("Connect to Papra", size=22, weight=ft.FontWeight.W_600),
                    muted("Use a read-only API token for your Papra instance."),
                ),
                card(
                    self.url,
                    self.token_field,
                    self.ua,
                    ft.Row([self.test_btn], spacing=14),
                    self.status,
                ),
            ],
            self._actions(
                [
                    muted(
                        "Token needs organizations:read and documents:read. "
                        "It is not saved."
                    )
                ],
                [self.continue_btn],
            ),
        )

    def _set_status(self, text: str, kind: str) -> None:
        """Shows a message under the connection form.

        Args:
            text: The message.
            kind: ``"ok"``, ``"error"`` or ``"info"``; chooses the color.
        """
        self.status.value = text
        self.status.color = {"ok": ft.Colors.GREEN_400, "error": ft.Colors.ERROR}.get(
            kind, ft.Colors.ON_SURFACE_VARIANT
        )
        self.page.update()

    def _reset_connection(self, _e: Any) -> None:
        """Invalidates a previous successful test after the form changed."""
        self.orgs = []
        self.continue_btn.disabled = True
        self.status.value = ""
        self.page.update()

    def _on_test(self, _e: Any) -> None:
        """Starts the connection test in the background."""
        self.page.run_thread(self._test_connection)

    def _test_connection(self) -> None:
        """Checks the URL and token, then enables the Continue button."""
        url = (self.url.value or "").strip()
        token = (self.token_field.value or "").strip()
        if not url or not token:
            self._set_status("Enter the server URL and the API token.", "error")
            return
        base = pe.normalize_base(url)
        self.test_btn.disabled = True
        self.continue_btn.disabled = True
        self._set_status("Testing connection…", "info")
        try:
            pe.install_user_agent(
                (self.ua.value or "").strip() or pe.DEFAULT_USER_AGENT
            )
            try:
                orgs = pe.fetch_organizations(base, token)
            except (urllib.error.URLError, KeyError, ValueError) as e:
                self._set_status(pe.connection_error_message(e, base), "error")
                return
            try:
                total = sum(
                    pe.get_json(
                        base,
                        token,
                        f"/organizations/{o['id']}/documents",
                        {"pageIndex": 0, "pageSize": 1},
                    ).get("documentsCount", 0)
                    for o in orgs
                )
            except (urllib.error.URLError, KeyError, ValueError) as e:
                self._set_status(
                    f"Could not read documents (needs documents:read): {describe(e)}",
                    "error",
                )
                return
            self.base, self.token, self.orgs = base, token, orgs
            self.settings.update(url=url, user_agent=self.ua.value.strip())
            save_settings(self.settings)
            self.continue_btn.disabled = False
            self._set_status(
                f"Connected · {len(orgs)} organization(s) · {total} documents", "ok"
            )
        finally:
            self.test_btn.disabled = False
            self.page.update()

    # ------------------------------------------------------------------
    # Screen 2: Select
    # ------------------------------------------------------------------

    def _org_name(self) -> str:
        """Returns the display name of the selected organization."""
        for o in self.orgs:
            if o["id"] == self.org_id:
                return o.get("name") or o["id"]
        return self.org_id

    def _show_select(self, _e: Any = None) -> None:
        """Shows the screen for choosing organization, tags and folder."""
        if not self.org_id or self.org_id not in {o["id"] for o in self.orgs}:
            self.org_id = self.orgs[0]["id"]
        self.org_dd = ft.Dropdown(
            label="Organization",
            value=self.org_id,
            options=[
                ft.DropdownOption(key=o["id"], text=o.get("name") or o["id"])
                for o in self.orgs
            ],
            on_select=self._on_org,
        )
        self.tags_row = ft.Row(wrap=True, spacing=8, run_spacing=8)
        self.match_btn = ft.SegmentedButton(
            segments=[
                ft.Segment(value="any", label=ft.Text("Any tag")),
                ft.Segment(value="all", label=ft.Text("All tags")),
            ],
            selected=["all" if self.match_all else "any"],
            allow_multiple_selection=False,
            on_change=self._on_match,
        )
        self.trash_cb = ft.Checkbox(
            label="Include trash", value=self.include_trash, on_change=self._on_trash
        )
        self.out_field = ft.TextField(
            label="Save to", value=self.out_dir, expand=True, on_change=self._on_out
        )
        self.summary = ft.Text("", weight=ft.FontWeight.W_600)
        self.start_btn = primary("Start export", self._start, disabled=True)
        self._show(
            2,
            [
                heading(
                    ft.Text(
                        "Choose what to export", size=22, weight=ft.FontWeight.W_600
                    ),
                    muted("Pick tags, or leave all unselected to export everything."),
                ),
                card(
                    self.org_dd,
                    ft.Column([muted("Tags"), self.tags_row], spacing=6),
                    ft.Row([self.match_btn, self.trash_cb], spacing=24),
                    muted(
                        "Untagged documents are only exported when no tag is selected."
                    ),
                ),
                card(
                    ft.Row(
                        [
                            self.out_field,
                            secondary(
                                "Browse", self._browse, icon=ft.Icons.FOLDER_OPEN
                            ),
                        ],
                        spacing=12,
                    )
                ),
            ],
            self._actions(
                [secondary("Back", self._show_connect)], [self.summary, self.start_btn]
            ),
        )
        self._load_tags()

    def _on_org(self, e: Any) -> None:
        """Reloads the tags after the organization changed."""
        self.org_id = e.control.value
        self._load_tags()

    def _on_match(self, e: Any) -> None:
        """Applies the Any/All switch."""
        self.match_all = "all" in (e.control.selected or [])
        self._refresh_summary()

    def _on_trash(self, e: Any) -> None:
        """Applies the include-trash checkbox."""
        self.include_trash = bool(e.control.value)
        self._refresh_summary()

    def _on_out(self, e: Any) -> None:
        """Remembers the typed output folder."""
        self.out_dir = (e.control.value or "").strip()
        self._refresh_summary()

    async def _browse(self, _e: Any) -> None:
        """Lets the user pick the output folder in a native dialog."""
        path = await self.picker.get_directory_path(
            dialog_title="Choose the export folder"
        )
        if path:
            self.out_dir = path
            self.out_field.value = path
            self._refresh_summary()

    def _load_tags(self) -> None:
        """Clears the tag selection and loads the organization's tags."""
        self.docs = None
        self.tags.clear()
        self.tags_row.controls = [muted("Loading tags…", 14)]
        self._refresh_summary()
        self.page.run_thread(self._fetch_tags, self.org_id)

    def _fetch_tags(self, org_id: str) -> None:
        """Lists the organization's documents and builds the tag chips.

        Args:
            org_id: The organization to load; ignored if the user has
                already switched to another one.
        """
        try:
            docs, _ = pe.list_documents(self.base, self.token, org_id)
        except (urllib.error.URLError, KeyError, ValueError) as e:
            if org_id == self.org_id:
                self.tags_row.controls = [
                    ft.Text(
                        f"Could not load documents: {describe(e)}",
                        color=ft.Colors.ERROR,
                    )
                ]
                self.page.update()
            return
        if org_id != self.org_id:
            return
        self.docs = docs
        counts, _ = pe.tag_counts(docs)
        self.tags_row.controls = [
            self._make_chip(name, n)
            for name, n in sorted(counts.items(), key=lambda kv: kv[0].lower())
        ] or [muted("This organization has no tags.", 14)]
        self._refresh_summary()

    def _make_chip(self, name: str, count: int) -> ft.Chip:
        """Builds a selectable chip for one tag.

        Args:
            name: The tag name.
            count: Number of documents carrying it.

        Returns:
            The chip; clicking it toggles the tag in the selection.
        """
        chip = ft.Chip(label=f"{name} ({count})", selected=name in self.tags)

        def toggle(_e: Any) -> None:
            """Toggles this tag in the selection."""
            self.tags.symmetric_difference_update({name})
            chip.selected = name in self.tags
            self._refresh_summary()

        chip.on_select = toggle
        return chip

    def _matching(self) -> int:
        """Returns how many active documents match the current selection."""
        if self.docs is None:
            return 0
        if not self.tags:
            return len(self.docs)
        return len(pe.filter_by_tags(self.docs, self.tags, self.match_all))

    def _refresh_summary(self) -> None:
        """Updates the match count and the Start button."""
        n = self._matching()
        if self.docs is None:
            self.summary.value = ""
        else:
            extra = " (plus matching trashed ones)" if self.include_trash else ""
            self.summary.value = f"{n} documents match{extra}"
        self.start_btn.disabled = (
            self.docs is None or not self.out_dir or (n == 0 and not self.include_trash)
        )
        self.page.update()

    # ------------------------------------------------------------------
    # Screen 3: Export
    # ------------------------------------------------------------------

    def _start(self, _e: Any) -> None:
        """Remembers the output folder and starts the export."""
        self.settings["out"] = self.out_dir
        save_settings(self.settings)
        self.cancel.clear()
        self._show_export()
        org = [o for o in self.orgs if o["id"] == self.org_id]
        self.page.run_thread(
            self._export_worker,
            org,
            sorted(self.tags),
            self.match_all,
            self.include_trash,
            self.out_dir,
        )

    def _show_export(self) -> None:
        """Shows the progress screen."""
        self.title = ft.Text("Exporting…", size=22, weight=ft.FontWeight.W_600)
        tag_text = (
            (" ALL " if self.match_all else " ANY ").join(sorted(self.tags))
            if self.tags
            else "all documents"
        )
        subtitle = muted(f"{self._org_name()} · {tag_text} · saving to {self.out_dir}")
        self.count = ft.Text("Preparing…", weight=ft.FontWeight.W_600)
        self.bar = ft.ProgressBar(value=None, border_radius=5)
        self.counters = {
            key: ft.Text("0", size=22, weight=ft.FontWeight.W_600)
            for key in ("ok", "skipped", "failed", "hash_mismatch")
        }
        labels = {
            "ok": "Downloaded",
            "skipped": "Skipped",
            "failed": "Failed",
            "hash_mismatch": "Hash mismatch",
        }
        tiles = ft.Row(
            [
                ft.Container(
                    ft.Column([self.counters[key], muted(labels[key])], spacing=0),
                    expand=True,
                    padding=ft.Padding.symmetric(vertical=12, horizontal=14),
                    border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
                    border_radius=10,
                )
                for key in self.counters
            ],
            spacing=12,
        )
        self.log_view = ft.ListView(
            controls=[], auto_scroll=True, spacing=0, expand=True
        )
        log_box = ft.Container(
            self.log_view,
            expand=True,
            padding=12,
            border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW,
        )
        self.cancel_btn = secondary("Cancel", self._on_cancel)
        self.open_btn = secondary("Open folder", self._open_folder, disabled=True)
        self.manifest_btn = primary("View manifest", self._open_manifest, disabled=True)
        self.again_btn = secondary("New export", self._show_select, visible=False)
        self._show(
            3,
            [
                heading(self.title, subtitle),
                card(self.count, self.bar, tiles),
                log_box,
            ],
            self._actions(
                [self.cancel_btn, self.again_btn], [self.open_btn, self.manifest_btn]
            ),
            scroll=False,
        )

    def _add_log(self, msg: str) -> None:
        """Appends the lines of a message to the on-screen log.

        Args:
            msg: One or more lines; blank lines are skipped.
        """
        for line in msg.splitlines():
            if line.strip():
                self.log_view.controls.append(
                    ft.Text(
                        line,
                        size=12,
                        font_family="Consolas",
                        selectable=True,
                        color=ft.Colors.ON_SURFACE_VARIANT,
                    )
                )
        del self.log_view.controls[:-MAX_LOG_LINES]
        self.page.update()

    def _on_progress(self, done: int, total: int, result: pe.ExportResult) -> None:
        """Updates the progress bar and counters.

        Args:
            done: Documents finished so far in the current organization.
            total: Documents selected in the current organization.
            result: The running totals.
        """
        self.bar.value = done / total if total else 1
        self.count.value = f"{done} of {total}"
        for key, text in self.counters.items():
            text.value = str(getattr(result, key))
        self.page.update()

    def _export_worker(
        self,
        orgs: List[Dict[str, Any]],
        tags: List[str],
        match_all: bool,
        include_trash: bool,
        out: str,
    ) -> None:
        """Runs the export in a background thread.

        Args:
            orgs: The organizations to export.
            tags: Selected tags; empty for all documents.
            match_all: Require all tags instead of any.
            include_trash: Also export trashed documents.
            out: Output folder.
        """
        try:
            result = pe.run_export(
                self.base,
                self.token,
                Path(out),
                orgs,
                tags=tags,
                match_all=match_all,
                include_trash=include_trash,
                log_fn=self._add_log,
                on_progress=self._on_progress,
                cancel=self.cancel,
            )
        except Exception as e:  # shown to the user; a dead thread hides it
            self._add_log(f"ERROR: {describe(e)}")
            self._finish("Export failed", ok=False)
            return
        if result.cancelled:
            self._finish("Export cancelled", ok=True)
        elif result.has_problems:
            self._finish("Finished with problems - rerun to retry failures", ok=True)
        else:
            self._finish("Export complete", ok=True)

    def _finish(self, title: str, ok: bool) -> None:
        """Switches the export screen to its finished state.

        Args:
            title: The new heading.
            ok: Whether there is something to open (False after a crash).
        """
        self.title.value = title
        if self.bar.value is None:
            self.bar.value = 0
        self.cancel_btn.visible = False
        self.again_btn.visible = True
        self.open_btn.disabled = not ok
        self.manifest_btn.disabled = not ok
        self.page.update()

    def _on_cancel(self, _e: Any) -> None:
        """Asks the running export to stop starting new downloads."""
        self.cancel.set()
        self.cancel_btn.disabled = True
        self.title.value = "Cancelling…"
        self.page.update()

    def _open_folder(self, _e: Any) -> None:
        """Opens the output folder."""
        open_path(Path(self.out_dir))

    def _open_manifest(self, _e: Any) -> None:
        """Opens the manifest in the default app."""
        open_path(Path(self.out_dir) / "manifest.json")


def main(page: ft.Page) -> None:
    """Flet entry point.

    Args:
        page: The page Flet provides.
    """
    App(page)


if __name__ == "__main__":
    ft.run(main)
