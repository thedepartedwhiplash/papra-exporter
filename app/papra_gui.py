"""Desktop GUI for exporting documents from a self-hosted Papra instance.

A Flet front end for ``papra_export``. Connect with a server URL and API
token, pick an organization (or all of them), tags, filters or hand-picked
documents, choose an output folder and layout, and watch the export run
with a live speed and time-left estimate. All network and file work is
done by ``papra_export``; this module only builds the interface.

Requirements:
  Python 3.10 or later and ``pip install flet``. ``papra_export.py`` must
  sit next to this file.

Typical usage example:

  python papra_gui.py

The API token is never written to disk, so it has to be entered again every
time the app starts. The server URL, User-Agent, output folder, folder
layout, date and verification preferences and the theme are remembered in
``~/.papra-exporter.json``. Filters are not remembered.
"""

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from datetime import date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import flet as ft

import papra_export as pe

SETTINGS_PATH = Path.home() / ".papra-exporter.json"
# Papra's own colors (apps/papra-client/src/app.css in the Papra repo),
# converted from HSL: coral in light mode, lime in dark mode.
PAPRA_LIGHT = ft.ColorScheme(
    primary="#FE7D4D", on_primary="#0A0A0A", secondary_container="#FFE1D6",
    on_secondary_container="#0A0A0A", surface="#FFFFFF", on_surface="#0A0A0A",
    on_surface_variant="#737373", outline="#A3A3A3",
    outline_variant="#E5E5E5", surface_container_lowest="#FFFFFF",
    surface_container_low="#F5F5F5", surface_container="#FAFAFA",
    surface_container_high="#F5F5F5", surface_container_highest="#EBEBEB",
    error="#EF4444", on_error="#0A0A0A")
PAPRA_DARK = ft.ColorScheme(
    primary="#D9FF7A", on_primary="#171717", secondary_container="#3C471F",
    on_secondary_container="#FAFAFA", surface="#18181B", on_surface="#FAFAFA",
    on_surface_variant="#A3A3A3", outline="#525252",
    outline_variant="#2E2E32", surface_container_lowest="#101011",
    surface_container_low="#101011", surface_container="#141415",
    surface_container_high="#202022", surface_container_highest="#262626",
    error="#E87D7D", on_error="#171717")
# The step rail follows the theme, like Papra's sidebar.
RAIL = {
    True: {"bg": "#141415", "active": "#202022", "text": "#A3A3A3",
           "title": "#FAFAFA", "accent": "#D9FF7A", "on_accent": "#171717",
           "idle": "#525252", "glow": "#2C3318"},
    False: {"bg": "#FAFAFA", "active": "#EFEFEF", "text": "#737373",
            "title": "#0A0A0A", "accent": "#FE7D4D", "on_accent": "#0A0A0A",
            "idle": "#A3A3A3", "glow": "#FFE1D6"},
}
# Lines kept in the on-screen log; older ones are dropped.
MAX_LOG_LINES = 500
STEPS = ["Connect", "Select", "Export"]
LAYOUT_LABELS = {
    "flat": "All in one folder",
    "year": "By year",
    "year-month": "By year and month",
    "tag": "By first tag",
}
DATE_LABELS = {"document": "Document date", "created": "Upload date"}
# Organization dropdown value that stands for every organization.
ALL_ORGS = "__all__"
# Exports that run at least this many seconds end with a sound and the
# window coming to the front.
NOTIFY_AFTER = 10
# Rows shown in the document picker; longer lists are narrowed by search.
MAX_PICKER_ROWS = 200
# Fonts are fetched when the app starts; offline, Flet falls back to the
# system font. shortcut: bundle the files in an assets folder to avoid the
# download.
FONT_SANS = "IBM Plex Sans"
FONT_MONO = "IBM Plex Mono"
FONT_URLS = {
    FONT_SANS: "https://raw.githubusercontent.com/IBM/plex/master/packages/"
               "plex-sans/fonts/complete/ttf/IBMPlexSans-Regular.ttf",
    FONT_MONO: "https://raw.githubusercontent.com/google/fonts/main/ofl/"
               "ibmplexmono/IBMPlexMono-Regular.ttf",
}
# Colors of the log lines and the finish banner, per theme.
KIND_COLORS = {
    True: {"ok": ft.Colors.GREEN_300, "skipped": ft.Colors.AMBER_300,
           "repaired": ft.Colors.LIGHT_BLUE_300, "mismatch": ft.Colors.RED_300,
           "failed": ft.Colors.RED_300, "header": "#D9FF7A"},
    False: {"ok": ft.Colors.GREEN_800, "skipped": ft.Colors.AMBER_900,
            "repaired": ft.Colors.BLUE_800, "mismatch": ft.Colors.RED_800,
            "failed": ft.Colors.RED_800, "header": "#C13401"},
}
LOG_LINE = re.compile(
    r"^\[\d+/\d+\] (FAILED|ok|skipped|repaired|hash_mismatch)")
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def load_settings() -> Dict[str, Any]:
    """Reads the saved settings.

    Returns:
        The settings, or an empty dict if there are none or they are
        unreadable.
    """
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: Dict[str, Any]) -> None:
    """Writes the settings, never including the API token.

    Failures are ignored because remembering settings is only a convenience.

    Args:
        settings: The settings to save.
    """
    settings.pop("token", None)
    try:
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2),
                                 encoding="utf-8")
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


def split_list(text: str) -> List[str]:
    """Splits comma-separated input into trimmed, non-empty items."""
    return [item.strip() for item in text.split(",") if item.strip()]


def parse_day(text: str) -> Optional[date]:
    """Parses a ``YYYY-MM-DD`` entry.

    Args:
        text: The typed text; blank means no limit.

    Returns:
        The date, or None for blank text.

    Raises:
        ValueError: If the text is not a valid date.
    """
    text = text.strip()
    return date.fromisoformat(text) if text else None


def human_size(n: float) -> str:
    """Formats a byte count, for example ``412.0 MB``."""
    for unit in ("B", "KB", "MB", "GB"):
        shown = f"{n:.0f}" if unit == "B" else f"{n:.1f}"
        if float(shown) < 1024:  # compare after rounding: no "1024.0 KB"
            return f"{shown} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_duration(seconds: float) -> str:
    """Formats a duration, for example ``1 min 20 s``."""
    secs = int(round(seconds))
    if secs < 60:
        return f"{secs} s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins} min {secs} s"
    hours, mins = divmod(mins, 60)
    return f"{hours} h {mins} min"


def free_bytes(path: str) -> Optional[int]:
    """Returns the free space of the drive that will hold ``path``.

    Args:
        path: A folder that may not exist yet; its nearest existing parent
            is measured.

    Returns:
        Free bytes, or None if they cannot be determined.
    """
    folder = Path(path).expanduser()
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None


def doc_label(doc: Dict[str, Any]) -> str:
    """Returns the name shown for a document."""
    return doc.get("name") or doc.get("originalName") or doc["id"]


def beep() -> None:
    """Plays a short alert sound, if the platform has an easy way to."""
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep()
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
        else:
            # shortcut: the terminal bell is silent without a terminal,
            # switch to paplay or similar if Linux users want a sound.
            print("\a", end="", flush=True)
    except (OSError, ImportError, RuntimeError):
        pass


def log_kind(line: str) -> str:
    """Classifies a log line for coloring.

    Args:
        line: One line of ``papra_export`` output.

    Returns:
        ``ok``, ``skipped``, ``repaired``, ``mismatch``, ``failed``,
        ``header`` or ``other``.
    """
    m = LOG_LINE.match(line)
    if m:
        word = m.group(1)
        return {"hash_mismatch": "mismatch", "FAILED": "failed"}.get(
            word, word)
    if line.startswith("ERROR"):
        return "failed"
    if line.startswith("== "):
        return "header"
    return "other"


def kind_color(kind: str, dark: bool) -> str:
    """Returns the color for a line or banner kind in the given theme."""
    return KIND_COLORS[dark].get(kind, ft.Colors.ON_SURFACE_VARIANT)


def tag_color(value: Any) -> Optional[str]:
    """Returns a tag color if it is a ``#RRGGBB`` string, else None."""
    return value if isinstance(value, str) and HEX_COLOR.match(value) \
        else None


def plural(n: int, word: str) -> str:
    """Returns ``n`` with ``word`` made plural when needed."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def finish_summary(result: Optional[pe.ExportResult], verify: bool
                   ) -> Tuple[Any, str, str, str]:
    """Describes how an export ended for the banner.

    Args:
        result: The outcome; None after a crash.
        verify: Whether existing files were verified in this run.

    Returns:
        A tuple ``(icon, kind, headline, detail)``.
    """
    if result is None:
        return (ft.Icons.ERROR_OUTLINE, "failed",
                "The export stopped unexpectedly", "See the log for details.")
    handled = (result.ok + result.skipped + result.repaired
               + result.hash_mismatch + result.failed)
    if result.cancelled:
        return (ft.Icons.PAUSE_CIRCLE_OUTLINE, "skipped", "Export cancelled",
                f"{handled} of {plural(result.expected, 'document')} were "
                "handled before stopping.")
    if result.has_problems:
        bad = result.failed + result.hash_mismatch
        parts = [f"{n} {what}" for n, what in
                 ((result.failed, "failed"),
                  (result.hash_mismatch, "with a hash mismatch")) if n]
        return (ft.Icons.WARNING_AMBER, "failed",
                f"{bad} of {plural(result.expected, 'file')} need attention",
                ", ".join(parts) + ". Use Retry failed to try them again.")
    headline = f"All {plural(result.expected, 'file')} exported"
    if verify:
        detail = "Every file was checked against its stored hash."
    else:
        detail = (f"{result.ok + result.repaired} downloaded and "
                  f"hash-checked, {result.skipped} already present.")
    return ft.Icons.CHECK_CIRCLE, "ok", headline, detail


def spinner_row(text: str) -> ft.Row:
    """Builds a small spinner followed by a loading message."""
    return ft.Row([ft.ProgressRing(width=16, height=16, stroke_width=2),
                   muted(text, 14)], spacing=10)


def muted(text: str, size: int = 12) -> ft.Text:
    """Builds a small, low-emphasis text."""
    return ft.Text(text, size=size, color=ft.Colors.ON_SURFACE_VARIANT)


def heading(title: ft.Control, subtitle: ft.Control) -> ft.Column:
    """Builds the title block shown at the top of every screen."""
    return ft.Column([title, subtitle], spacing=4)


def card(*controls: ft.Control) -> ft.Container:
    """Wraps controls in a rounded, outlined card."""
    return ft.Container(
        content=ft.Column(list(controls), spacing=14,
                          horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
        bgcolor=ft.Colors.SURFACE_CONTAINER, padding=20, border_radius=12,
        border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        shadow=ft.BoxShadow(
            blur_radius=12, offset=ft.Offset(0, 2),
            color=ft.Colors.with_opacity(0.10, ft.Colors.BLACK)))


def primary(text: str, on_click: Any, disabled: bool = False,
            icon: Optional[ft.IconData] = None) -> ft.Button:
    """Builds the filled accent button."""
    return ft.Button(
        text, on_click=on_click, disabled=disabled, icon=icon, height=44,
        style=ft.ButtonStyle(
            bgcolor={
                ft.ControlState.DEFAULT: ft.Colors.PRIMARY,
                ft.ControlState.DISABLED: ft.Colors.SURFACE_CONTAINER_HIGHEST},
            color={ft.ControlState.DEFAULT: ft.Colors.ON_PRIMARY,
                   ft.ControlState.DISABLED: ft.Colors.ON_SURFACE_VARIANT},
            shape=ft.RoundedRectangleBorder(radius=8)))


def secondary(text: str, on_click: Any, disabled: bool = False,
              visible: bool = True, icon: Optional[ft.IconData] = None
              ) -> ft.OutlinedButton:
    """Builds the outlined button."""
    return ft.OutlinedButton(
        text, on_click=on_click, disabled=disabled, visible=visible,
        icon=icon, height=44,
        style=ft.ButtonStyle(shape=ft.RoundedRectangleBorder(radius=8)))


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
        self.since_text = ""
        self.until_text = ""
        self.types_text = ""
        self.names_text = ""
        self.retry_failed = False
        self.make_zip = False
        self.picked: Set[str] = set()
        self.date_field = self._remembered(
            "date_field", DATE_LABELS, "document")
        self.layout = self._remembered("layout", LAYOUT_LABELS, "flat")
        self.preserve_dates = bool(self.settings.get("preserve_dates", True))
        self.verify = bool(self.settings.get("verify", False))
        self.options = pe.ExportOptions()
        self.zip_result: Optional[Path] = None
        self.started = 0.0
        self.out_dir = (self.settings.get("out")
                        or str(Path.home() / "papra-export"))
        self.cancel = threading.Event()
        self.picker = ft.FilePicker()

        page.title = "Papra Exporter"
        page.padding = 0
        page.fonts = dict(FONT_URLS)
        page.theme = ft.Theme(color_scheme=PAPRA_LIGHT, font_family=FONT_SANS)
        page.dark_theme = ft.Theme(color_scheme=PAPRA_DARK,
                                   font_family=FONT_SANS)
        page.theme_mode = (ft.ThemeMode.LIGHT
                           if self.settings.get("theme") == "light"
                           else ft.ThemeMode.DARK)
        page.window.width = 1000
        page.window.height = 700
        page.window.min_width = 860
        page.window.min_height = 660
        page.services.append(self.picker)

        self.rail = ft.Container(
            width=210,
            bgcolor=RAIL[page.theme_mode == ft.ThemeMode.DARK]["bg"],
            padding=ft.Padding.symmetric(vertical=24, horizontal=16))
        # Screens fade into each other; each gets a new key so that the
        # switcher sees a different child.
        self.switcher = ft.AnimatedSwitcher(
            content=ft.Container(), expand=True, duration=250,
            transition=ft.AnimatedSwitcherTransition.FADE)
        self.screens = 0
        self.tag_colors: Dict[str, str] = {}
        self.banner_kind = "ok"
        self.body = ft.Container(
            self.switcher, expand=True,
            padding=ft.Padding.symmetric(vertical=28, horizontal=32))

        # Connect-screen controls live for the whole session so that going
        # back from a later screen finds them as the user left them.
        self.url = ft.TextField(
            label="Server URL", hint_text="https://papra.example.com",
            value=self.settings.get("url", ""),
            on_change=self._reset_connection)
        self.token_field = ft.TextField(
            label="API token", password=True, can_reveal_password=True,
            on_change=self._reset_connection)
        self.ua = ft.TextField(
            label="User-Agent (advanced)",
            value=self.settings.get("user_agent") or pe.DEFAULT_USER_AGENT,
            helper="Change this if a proxy or firewall answers with "
                   "HTTP 403.",
            helper_max_lines=3,
            on_change=self._reset_connection)
        self.status = ft.Text("", size=13, selectable=True, expand=True)
        self.status_icon = ft.Icon(ft.Icons.CHECK_CIRCLE, size=18,
                                   visible=False)
        self.status_ring = ft.ProgressRing(width=16, height=16,
                                           stroke_width=2, visible=False)
        self.test_btn = secondary("Test connection", self._on_test,
                                  icon=ft.Icons.NETWORK_CHECK)
        self.continue_btn = primary("Continue", self._show_select,
                                    disabled=True,
                                    icon=ft.Icons.ARROW_FORWARD)

        page.add(ft.Row([self.rail, self.body], expand=True, spacing=0))
        self._show_connect()

    def _remembered(self, key: str, allowed: Dict[str, str],
                    default: str) -> str:
        """Returns a remembered choice, or ``default`` if it is not valid."""
        value = self.settings.get(key)
        return value if value in allowed else default

    # ------------------------------------------------------------------
    # Shared layout
    # ------------------------------------------------------------------

    def _rail_content(self) -> ft.Control:
        """Builds the left rail: title, step indicator and theme toggle."""
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        c = RAIL[dark]
        items: List[ft.Control] = [ft.Container(
            ft.Row([ft.Icon(ft.Icons.CLOUD_DOWNLOAD, color=c["accent"],
                            size=20),
                    ft.Text("Papra Exporter", size=16,
                            weight=ft.FontWeight.W_600,
                            color=c["title"])], spacing=10),
            padding=ft.Padding.symmetric(vertical=14, horizontal=12),
            border_radius=10, margin=ft.Margin.only(bottom=14),
            gradient=ft.LinearGradient(
                begin=ft.Alignment.TOP_LEFT, end=ft.Alignment.BOTTOM_RIGHT,
                colors=[c["glow"], c["bg"]]))]
        for n, name in enumerate(STEPS, 1):
            active, done = n == self.step, n < self.step
            ring = c["accent"] if (active or done) else c["idle"]
            mark: ft.Control = (
                ft.Icon(ft.Icons.CHECK, size=14, color=c["accent"]) if done
                else ft.Text(str(n), size=12,
                             color=c["on_accent"] if active else c["text"]))
            dot = ft.Container(
                mark,
                width=22, height=22, border_radius=11,
                alignment=ft.Alignment.CENTER, border=ft.Border.all(1.5, ring),
                bgcolor=c["accent"] if active else None)
            items.append(ft.Container(
                ft.Row([dot, ft.Text(name, color=(c["title"] if active
                                                  else c["text"]))],
                       spacing=10),
                padding=ft.Padding.symmetric(vertical=10, horizontal=8),
                border_radius=8,
                bgcolor=c["active"] if active else None))
        items.append(ft.Container(expand=True))
        items.append(ft.IconButton(
            icon=ft.Icons.LIGHT_MODE if dark else ft.Icons.DARK_MODE,
            icon_color=c["text"], tooltip="Switch light / dark theme",
            on_click=self._toggle_theme))
        return ft.Column(items, spacing=6, expand=True)

    def _show(self, step: int, content: List[ft.Control], actions: ft.Row,
              scroll: bool = True) -> None:
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
            content, spacing=16, expand=True,
            scroll=ft.ScrollMode.AUTO if scroll else None)
        self.screens += 1
        self.switcher.content = ft.Column(
            [column, actions], spacing=16, expand=True,
            key=f"screen-{self.screens}")
        self.page.update()

    def _toggle_theme(self, _e: Any) -> None:
        """Switches between the dark and light theme and remembers it."""
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        self.page.theme_mode = (ft.ThemeMode.LIGHT if dark
                                else ft.ThemeMode.DARK)
        self.settings["theme"] = "light" if dark else "dark"
        save_settings(self.settings)
        self.rail.bgcolor = RAIL[not dark]["bg"]
        self.rail.content = self._rail_content()
        if self.step == 3:
            self._repaint_log()
        self.page.update()

    def _repaint_log(self) -> None:
        """Recolors the log lines and banner for the current theme."""
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        for line in self.log_view.controls:
            if isinstance(line, ft.Text):
                line.color = kind_color(line.data, dark)
        self._paint_banner()

    def _paint_banner(self) -> None:
        """Colors the finish banner for its kind and the current theme."""
        color = kind_color(self.banner_kind,
                           self.page.theme_mode == ft.ThemeMode.DARK)
        self.banner.bgcolor = ft.Colors.with_opacity(0.12, color)
        self.banner.border = ft.Border.all(1, color)
        self.banner_icon.color = color
        self.banner_head.color = color

    @staticmethod
    def _actions(left: List[ft.Control], right: List[ft.Control]) -> ft.Row:
        """Builds the bottom button row with groups at both ends."""
        return ft.Row(
            [ft.Row(left, spacing=12), ft.Row(right, spacing=12)],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    # ------------------------------------------------------------------
    # Screen 1: Connect
    # ------------------------------------------------------------------

    def _show_connect(self, _e: Any = None) -> None:
        """Shows the connection screen."""
        self._show(1, [
            heading(ft.Text("Connect to Papra", size=22,
                            weight=ft.FontWeight.W_600),
                    muted("Use a read-only API token for your Papra "
                          "instance.")),
            card(self.url, self.token_field, self.ua,
                 ft.Row([self.test_btn], spacing=14),
                 ft.Row([self.status_ring, self.status_icon, self.status],
                        spacing=8)),
        ], self._actions(
            [muted("Token needs organizations:read and documents:read. "
                   "It is not saved.")],
            [self.continue_btn]))

    def _set_status(self, text: str, kind: str) -> None:
        """Shows a message under the connection form.

        Args:
            text: The message.
            kind: ``"ok"``, ``"error"``, ``"busy"`` (shows a spinner) or
                ``"info"``; chooses the color and icon.
        """
        self.status.value = text
        self.status.color = {"ok": ft.Colors.GREEN_400,
                             "error": ft.Colors.ERROR}.get(
                                 kind, ft.Colors.ON_SURFACE_VARIANT)
        self.status_ring.visible = bool(text) and kind == "busy"
        self.status_icon.visible = bool(text) and kind in ("ok", "error")
        self.status_icon.icon = (ft.Icons.CHECK_CIRCLE if kind == "ok"
                                 else ft.Icons.ERROR_OUTLINE)
        self.status_icon.color = self.status.color
        self.page.update()

    def _reset_connection(self, _e: Any) -> None:
        """Invalidates a previous successful test after the form changed."""
        self.orgs = []
        self.continue_btn.disabled = True
        self._set_status("", "info")

    def _on_test(self, _e: Any) -> None:
        """Starts the connection test in the background."""
        self.page.run_thread(self._test_connection)

    def _test_connection(self) -> None:
        """Checks the URL and token, then enables the Continue button."""
        url = (self.url.value or "").strip()
        token = (self.token_field.value or "").strip()
        if not url or not token:
            self._set_status("Enter the server URL and the API token.",
                             "error")
            return
        base = pe.normalize_base(url)
        self.test_btn.disabled = True
        self.continue_btn.disabled = True
        self._set_status("Testing connection…", "busy")
        try:
            pe.install_user_agent(
                (self.ua.value or "").strip() or pe.DEFAULT_USER_AGENT)
            try:
                orgs = pe.fetch_organizations(base, token)
            except (urllib.error.URLError, KeyError, ValueError) as e:
                self._set_status(pe.connection_error_message(e, base),
                                 "error")
                return
            try:
                total = sum(pe.get_json(
                    base, token, f"/organizations/{o['id']}/documents",
                    {"pageIndex": 0, "pageSize": 1}
                ).get("documentsCount", 0) for o in orgs)
            except (urllib.error.URLError, KeyError, ValueError) as e:
                self._set_status(
                    "Could not read documents (needs documents:read): "
                    f"{describe(e)}", "error")
                return
            self.base, self.token, self.orgs = base, token, orgs
            self.settings.update(url=url, user_agent=self.ua.value.strip())
            save_settings(self.settings)
            self.continue_btn.disabled = False
            self._set_status(
                f"Connected · {len(orgs)} organization(s) · "
                f"{total} documents", "ok")
        finally:
            self.test_btn.disabled = False
            self.page.update()

    # ------------------------------------------------------------------
    # Screen 2: Select
    # ------------------------------------------------------------------

    def _orgs_for(self, org_id: str) -> List[Dict[str, Any]]:
        """Returns the organizations a dropdown value stands for.

        Args:
            org_id: An organization ID, or ``ALL_ORGS``.
        """
        if org_id == ALL_ORGS:
            return list(self.orgs)
        return [o for o in self.orgs if o["id"] == org_id]

    def _org_name(self) -> str:
        """Returns the display name of the selected organization(s)."""
        if self.org_id == ALL_ORGS:
            return "All organizations"
        for o in self.orgs:
            if o["id"] == self.org_id:
                return o.get("name") or o["id"]
        return self.org_id

    def _setter(self, attr: str) -> Callable[[Any], None]:
        """Builds a change handler that stores a control's value.

        Args:
            attr: Name of the ``App`` attribute to set.

        Returns:
            A handler that stores the value and refreshes the summary.
        """
        def handler(e: Any) -> None:
            """Stores the new value and refreshes the summary."""
            value = e.control.value
            setattr(self, attr, "" if value is None else value)
            self._refresh_summary()

        return handler

    def _dropdown(self, label: str, labels: Dict[str, str], value: str,
                  attr: str, width: Optional[int] = None) -> ft.Dropdown:
        """Builds a dropdown over a key-to-label mapping."""
        return ft.Dropdown(
            label=label, value=value, width=width,
            expand=width is None,
            options=[ft.DropdownOption(key=k, text=t)
                     for k, t in labels.items()],
            on_select=self._setter(attr))

    def _show_select(self, _e: Any = None) -> None:
        """Shows the screen for choosing what to export and how to save."""
        known = {o["id"] for o in self.orgs}
        if len(self.orgs) > 1:
            known.add(ALL_ORGS)
        if self.org_id not in known:
            self.org_id = self.orgs[0]["id"]
        choices = [ft.DropdownOption(key=o["id"],
                                     text=o.get("name") or o["id"])
                   for o in self.orgs]
        if len(self.orgs) > 1:
            choices.insert(0, ft.DropdownOption(
                key=ALL_ORGS, text="All organizations"))
        self.org_dd = ft.Dropdown(
            label="Organization", value=self.org_id, options=choices,
            on_select=self._on_org)
        self.tags_row = ft.Row(wrap=True, spacing=8, run_spacing=8)
        self.match_btn = ft.SegmentedButton(
            segments=[ft.Segment(value="any", label=ft.Text("Any tag")),
                      ft.Segment(value="all", label=ft.Text("All tags"))],
            selected=["all" if self.match_all else "any"],
            allow_multiple_selection=False, on_change=self._on_match)
        self.trash_cb = ft.Checkbox(
            label="Include trash", value=self.include_trash,
            on_change=self._setter("include_trash"))
        self.since_field = ft.TextField(
            label="Since", hint_text="YYYY-MM-DD", expand=True,
            value=self.since_text, on_change=self._setter("since_text"))
        self.until_field = ft.TextField(
            label="Until", hint_text="YYYY-MM-DD", expand=True,
            value=self.until_text, on_change=self._setter("until_text"))
        self.date_dd = self._dropdown("Date used", DATE_LABELS,
                                      self.date_field, "date_field")
        self.types_field = ft.TextField(
            label="File types", hint_text="pdf, image/*", expand=True,
            value=self.types_text, on_change=self._setter("types_text"))
        self.names_field = ft.TextField(
            label="Name contains", hint_text="invoice-*, receipt",
            expand=True, value=self.names_text,
            on_change=self._setter("names_text"))
        self.out_field = ft.TextField(
            label="Save to", value=self.out_dir, expand=True,
            on_change=self._on_out)
        self.layout_dd = self._dropdown("Folder layout", LAYOUT_LABELS,
                                        self.layout, "layout", width=280)
        self.zip_note = muted("")
        self.space_note = muted("")
        self.pick_btn = secondary("Pick individual documents…",
                                  self._open_picker, disabled=True,
                                  icon=ft.Icons.CHECKLIST)
        self.pick_note = muted("")
        self.pick_clear = ft.TextButton(
            "Clear picks", on_click=self._clear_picks, visible=False)
        self.summary = ft.Text("", weight=ft.FontWeight.W_600)
        self.start_btn = primary("Start export", self._start, disabled=True,
                                 icon=ft.Icons.DOWNLOAD)
        options = ft.Column([
            ft.Checkbox(label="Set file dates to the document dates",
                        value=self.preserve_dates,
                        on_change=self._setter("preserve_dates")),
            ft.Checkbox(label="Verify files that already exist (slower)",
                        value=self.verify,
                        on_change=self._setter("verify")),
            ft.Checkbox(label="Also create a zip archive",
                        value=self.make_zip,
                        on_change=self._setter("make_zip")),
            self.zip_note,
            ft.Checkbox(
                label="Only retry documents that failed earlier in this "
                      "folder",
                value=self.retry_failed,
                on_change=self._setter("retry_failed")),
        ], spacing=0)
        self._show(2, [
            heading(ft.Text("Choose what to export", size=22,
                            weight=ft.FontWeight.W_600),
                    muted("Pick tags and filters, or leave them empty to "
                          "export everything.")),
            card(self.org_dd,
                 ft.Column([muted("Tags"), self.tags_row], spacing=6),
                 ft.Row([self.match_btn, self.trash_cb], spacing=24),
                 muted("Untagged documents are only exported when no tag "
                       "is selected.")),
            card(muted("Filters (a document must match all of them)"),
                 ft.Row([self.since_field, self.until_field, self.date_dd],
                        spacing=12),
                 ft.Row([self.types_field, self.names_field], spacing=12),
                 muted("Separate several file types or names with commas. "
                       "Names may use * and ?."),
                 ft.Row([self.pick_btn, self.pick_note, self.pick_clear],
                        spacing=12)),
            card(ft.Row([self.out_field,
                         secondary("Browse", self._browse,
                                   icon=ft.Icons.FOLDER_OPEN)],
                        spacing=12),
                 self.space_note, self.layout_dd, options),
        ], self._actions(
            [secondary("Back", self._show_connect, icon=ft.Icons.ARROW_BACK)],
            [self.summary, self.start_btn]))
        self._load_tags()

    def _on_org(self, e: Any) -> None:
        """Reloads the tags after the organization changed."""
        self.org_id = e.control.value
        self._load_tags()

    def _on_match(self, e: Any) -> None:
        """Applies the Any/All switch."""
        self.match_all = "all" in (e.control.selected or [])
        self._refresh_summary()

    def _on_out(self, e: Any) -> None:
        """Remembers the typed output folder."""
        self.out_dir = (e.control.value or "").strip()
        self._refresh_summary()

    async def _browse(self, _e: Any) -> None:
        """Lets the user pick the output folder in a native dialog."""
        path = await self.picker.get_directory_path(
            dialog_title="Choose the export folder")
        if path:
            self.out_dir = path
            self.out_field.value = path
            self._refresh_summary()

    def _load_tags(self) -> None:
        """Clears the selection and loads the organization's documents."""
        self.docs = None
        self.tags.clear()
        self.picked.clear()
        self.tags_row.controls = [spinner_row("Loading tags…")]
        self._refresh_summary()
        self.page.run_thread(self._fetch_tags, self.org_id)

    def _fetch_tags(self, org_id: str) -> None:
        """Lists the documents of the organization(s) and builds the chips.

        Args:
            org_id: The organization (or ``ALL_ORGS``) to load; ignored if
                the user has already switched to another one.
        """
        docs: List[Dict[str, Any]] = []
        try:
            for org in self._orgs_for(org_id):
                part, _ = pe.list_documents(self.base, self.token, org["id"])
                docs.extend(part)
        except (urllib.error.URLError, KeyError, ValueError) as e:
            if org_id == self.org_id:
                self.tags_row.controls = [ft.Text(
                    f"Could not load documents: {describe(e)}",
                    color=ft.Colors.ERROR)]
                self.page.update()
            return
        if org_id != self.org_id:
            return
        self.docs = docs
        counts, _ = pe.tag_counts(docs)
        self.tag_colors = {
            t["name"]: c for d in docs for t in d.get("tags") or []
            if t.get("name") and (c := tag_color(t.get("color")))}
        self.tags_row.controls = [
            self._make_chip(name, n) for name, n in
            sorted(counts.items(), key=lambda kv: kv[0].lower())
        ] or [muted("There are no tags to choose from.", 14)]
        self._refresh_summary()

    def _make_chip(self, name: str, count: int) -> ft.Chip:
        """Builds a selectable chip for one tag.

        Args:
            name: The tag name.
            count: Number of documents carrying it.

        Returns:
            The chip; clicking it toggles the tag in the selection.
        """
        color = self.tag_colors.get(name)
        chip = ft.Chip(
            label=f"{name} ({count})", selected=name in self.tags,
            leading=ft.Container(
                width=10, height=10, border_radius=5, bgcolor=color,
                border=ft.Border.all(1, ft.Colors.OUTLINE)) if color
            else None)

        def toggle(_e: Any) -> None:
            """Toggles this tag in the selection."""
            self.tags.symmetric_difference_update({name})
            chip.selected = name in self.tags
            self._refresh_summary()

        chip.on_select = toggle
        return chip

    def _options(self) -> Tuple[Optional[pe.ExportOptions], str]:
        """Builds the export options from the current form.

        Returns:
            A tuple ``(options, error)``. ``options`` is None and ``error``
            says why if the form is invalid; otherwise ``error`` is empty.
        """
        try:
            since = parse_day(self.since_text)
            until = parse_day(self.until_text)
        except ValueError:
            return None, "Dates must look like 2026-01-31."
        if since and until and since > until:
            return None, "Since must not be later than Until."
        zip_path = None
        if self.make_zip and self.out_dir:
            zip_path = pe.default_zip_path(Path(self.out_dir))
        return pe.ExportOptions(
            tags=sorted(self.tags), match_all=self.match_all, since=since,
            until=until, date_field=self.date_field,
            mime_types=split_list(self.types_text),
            names=split_list(self.names_text), ids=sorted(self.picked),
            retry_failed=self.retry_failed,
            include_trash=self.include_trash, layout=self.layout,
            preserve_dates=self.preserve_dates, verify=self.verify,
            zip_path=zip_path), ""

    def _selected(self, options: pe.ExportOptions,
                  ignore_ids: bool = False) -> List[Dict[str, Any]]:
        """Returns the active documents the options would select.

        Args:
            options: The options built from the current form.
            ignore_ids: Leave out the hand-picked restriction; the picker
                uses this to list what can be picked.
        """
        if self.docs is None:
            return []
        docs = self.docs
        if options.retry_failed:
            failed = pe.failed_document_ids(
                Path(self.out_dir) / "manifest.json")
            docs = [d for d in docs if d["id"] in failed]
        if ignore_ids:
            options = dataclasses.replace(options, ids=[])
        return pe.select_documents(docs, options)

    def _refresh_summary(self) -> None:
        """Updates validation, the count, size, space check and Start."""
        for control, text in ((self.since_field, self.since_text),
                              (self.until_field, self.until_text)):
            try:
                parse_day(text)
                control.error = None
            except ValueError:
                control.error = "Use YYYY-MM-DD"
        options, error = self._options()
        self.zip_note.visible = self.make_zip
        self.zip_note.value = (
            f"Archive: {pe.default_zip_path(Path(self.out_dir))}"
            if self.out_dir else "")
        docs = self._selected(options) if options else []
        n = len(docs)
        size = sum(d.get("originalSize") or 0 for d in docs)
        self.pick_note.value = (f"{len(self.picked)} picked"
                                if self.picked else "")
        self.pick_clear.visible = bool(self.picked)
        self.pick_btn.disabled = self.docs is None or options is None
        self._refresh_space(size)
        self.summary.color = ft.Colors.ERROR if error else None
        if error:
            self.summary.value = error
        elif self.docs is None:
            self.summary.value = ""
        else:
            what = ("previously failed documents" if self.retry_failed
                    else "documents")
            extra = " (plus matching trashed ones)" \
                if self.include_trash else ""
            self.summary.value = (
                f"{n} {what} match{extra} · {human_size(size)}")
        self.start_btn.disabled = (
            self.docs is None or options is None or not self.out_dir
            or (n == 0 and not self.include_trash))
        self.page.update()

    def _refresh_space(self, size: int) -> None:
        """Shows how much disk space the export needs and has.

        This only warns; files already on disk need no space, so the need
        is an upper bound.

        Args:
            size: Combined size in bytes of the selected documents.
        """
        free = (free_bytes(self.out_dir)
                if self.out_dir and self.docs is not None else None)
        need = size * (2 if self.make_zip else 1)
        if free is None:
            self.space_note.value = ""
        elif need > free:
            self.space_note.value = (
                f"Needs up to {human_size(need)} but only "
                f"{human_size(free)} is free on this drive.")
            self.space_note.color = ft.Colors.ERROR
        else:
            self.space_note.value = (
                f"Needs up to {human_size(need)} · "
                f"{human_size(free)} free on this drive.")
            self.space_note.color = ft.Colors.ON_SURFACE_VARIANT

    def _clear_picks(self, _e: Any) -> None:
        """Forgets the hand-picked documents."""
        self.picked.clear()
        self._refresh_summary()

    def _open_picker(self, _e: Any) -> None:
        """Opens a dialog for ticking individual documents.

        The list holds the documents that pass the other filters, so what
        is ticked is always a subset of what the filters allow.
        """
        options, _ = self._options()
        if options is None:
            return
        candidates = self._selected(options, ignore_ids=True)
        search = ft.TextField(label="Search by name", dense=True,
                              prefix_icon=ft.Icons.SEARCH)
        status = muted("")
        rows = ft.ListView(expand=True, spacing=0)

        def matching() -> List[Dict[str, Any]]:
            """Returns the candidates whose name contains the search."""
            query = (search.value or "").strip().lower()
            return [d for d in candidates if query in doc_label(d).lower()]

        def show_status() -> None:
            """Updates the line with the counts."""
            found = len(matching())
            more = (f" · showing the first {MAX_PICKER_ROWS}, search to "
                    "narrow down" if found > MAX_PICKER_ROWS else "")
            status.value = f"{found} documents{more} · {len(self.picked)} " \
                "picked"

        def make_row(doc: Dict[str, Any]) -> ft.Row:
            """Builds a checkbox row for one document."""
            def toggle(e: Any) -> None:
                """Adds or removes the document from the picks."""
                if e.control.value:
                    self.picked.add(doc["id"])
                else:
                    self.picked.discard(doc["id"])
                show_status()
                self.page.update()

            return ft.Row([
                ft.Checkbox(label=doc_label(doc), expand=True,
                            value=doc["id"] in self.picked,
                            on_change=toggle),
                muted(human_size(doc.get("originalSize") or 0))])

        def render(_e: Any = None) -> None:
            """Rebuilds the list for the current search."""
            rows.controls = [make_row(d)
                             for d in matching()[:MAX_PICKER_ROWS]]
            show_status()
            self.page.update()

        def select_matching(_e: Any) -> None:
            """Ticks every document matching the search, shown or not."""
            self.picked.update(d["id"] for d in matching())
            render()

        def clear_matching(_e: Any) -> None:
            """Unticks every document matching the search."""
            self.picked.difference_update(d["id"] for d in matching())
            render()

        def done(_e: Any) -> None:
            """Closes the dialog and applies the picks."""
            self.page.pop_dialog()
            self._refresh_summary()

        search.on_change = render
        render()
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text("Pick documents"),
            content=ft.Container(ft.Column([
                search,
                ft.Row([secondary("Select all matching", select_matching),
                        secondary("Clear matching", clear_matching)],
                       spacing=12),
                status, rows], spacing=8), width=640, height=440),
            actions=[ft.TextButton("Done", on_click=done)]))

    # ------------------------------------------------------------------
    # Screen 3: Export
    # ------------------------------------------------------------------

    def _start(self, _e: Any) -> None:
        """Remembers the preferences and starts the export."""
        options, _ = self._options()
        if options is None:
            return
        self.settings.update(
            out=self.out_dir, layout=self.layout, date_field=self.date_field,
            preserve_dates=self.preserve_dates, verify=self.verify)
        save_settings(self.settings)
        self._run(options)

    def _run(self, options: pe.ExportOptions) -> None:
        """Shows the progress screen and runs an export in the background.

        Args:
            options: What to export and how to save it.
        """
        self.options = options
        self.cancel.clear()
        self.started = time.monotonic()
        self._show_export(options)
        self.page.run_thread(self._export_worker,
                             self._orgs_for(self.org_id), options,
                             self.out_dir)

    def _rerun_failed(self, _e: Any) -> None:
        """Runs the same export again for the documents that went wrong.

        The zip archive is skipped here because it would contain only the
        retried documents.
        """
        self._run(dataclasses.replace(
            self.options, retry_failed=True, zip_path=None))

    def _show_export(self, options: pe.ExportOptions) -> None:
        """Shows the progress screen.

        Args:
            options: The options of the export being run.
        """
        self.title = ft.Text("Exporting…", size=22,
                             weight=ft.FontWeight.W_600)
        subtitle = muted(
            f"{self._org_name()} · {options.describe_filters()} · "
            f"{LAYOUT_LABELS[options.layout].lower()} · saving to "
            f"{self.out_dir}")
        self.count = ft.Text("Preparing…", weight=ft.FontWeight.W_600)
        self.speed = muted("", 13)
        self.bar = ft.ProgressBar(value=None, border_radius=5)
        self.banner_icon = ft.Icon(ft.Icons.CHECK_CIRCLE, size=28)
        self.banner_head = ft.Text("", size=16, weight=ft.FontWeight.W_600)
        self.banner_detail = muted("", 13)
        self.banner = ft.Container(
            ft.Row([self.banner_icon,
                    ft.Column([self.banner_head, self.banner_detail],
                              spacing=2, expand=True)], spacing=14),
            padding=14, border_radius=10, visible=False)
        self.banner_kind = "ok"
        self._paint_banner()
        labels = {"ok": "Downloaded", "skipped": "Skipped",
                  "repaired": "Repaired", "failed": "Failed",
                  "hash_mismatch": "Hash mismatch"}
        self.counters = {key: ft.Text("0", size=22,
                                      weight=ft.FontWeight.W_600)
                         for key in labels}
        tiles = ft.Row([ft.Container(
            ft.Column([self.counters[key], muted(labels[key])], spacing=0),
            expand=True, padding=ft.Padding.symmetric(vertical=12,
                                                      horizontal=14),
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=10) for key in self.counters], spacing=12)
        self.log_view = ft.ListView(controls=[], auto_scroll=True, spacing=0,
                                    expand=True)
        log_box = ft.Container(
            self.log_view, expand=True, padding=12, border_radius=8,
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW)
        self.cancel_btn = secondary("Cancel", self._on_cancel,
                                    icon=ft.Icons.CLOSE)
        self.retry_btn = secondary("Retry failed", self._rerun_failed,
                                   visible=False, icon=ft.Icons.REPLAY)
        self.again_btn = secondary("New export", self._show_select,
                                   visible=False, icon=ft.Icons.ADD)
        self.open_btn = secondary("Open folder", self._open_folder,
                                  disabled=True, icon=ft.Icons.FOLDER_OPEN)
        self.zip_btn = secondary("Open zip", self._open_zip, visible=False,
                                 icon=ft.Icons.FOLDER_ZIP)
        self.manifest_btn = primary("View manifest", self._open_manifest,
                                    disabled=True,
                                    icon=ft.Icons.DESCRIPTION)
        self._show(3, [
            heading(self.title, subtitle),
            card(ft.Row([self.count, self.speed],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                 self.bar, tiles, self.banner),
            log_box,
        ], self._actions(
            [self.cancel_btn, self.retry_btn, self.again_btn],
            [self.open_btn, self.zip_btn, self.manifest_btn]),
            scroll=False)

    def _add_log(self, msg: str) -> None:
        """Appends the lines of a message to the on-screen log.

        Args:
            msg: One or more lines; blank lines are skipped.
        """
        for line in msg.splitlines():
            if line.strip():
                kind = log_kind(line)
                self.log_view.controls.append(ft.Text(
                    line, size=12, font_family=FONT_MONO, selectable=True,
                    data=kind, color=kind_color(
                        kind, self.page.theme_mode == ft.ThemeMode.DARK)))
        del self.log_view.controls[:-MAX_LOG_LINES]
        self.page.update()

    def _on_progress(self, done: int, total: int,
                     result: pe.ExportResult) -> None:
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
        self.speed.value = self._speed_text(result)
        self.page.update()

    def _speed_text(self, result: pe.ExportResult) -> str:
        """Describes progress in bytes, with speed and time left.

        The speed counts only bytes really downloaded, so files that are
        skipped finish instantly without inflating it. shortcut: the
        estimate assumes the rest needs downloading too, so it errs on the
        long side when many files turn out to be present already.

        Args:
            result: The running totals.
        """
        if not result.bytes_total:
            return ""
        text = (f"{human_size(result.bytes_done)} of "
                f"{human_size(result.bytes_total)}")
        elapsed = time.monotonic() - self.started
        remaining = result.bytes_total - result.bytes_done
        if not result.bytes_downloaded or elapsed < 1 or remaining <= 0:
            return text
        rate = result.bytes_downloaded / elapsed
        return (f"{text} · {human_size(rate)}/s · about "
                f"{fmt_duration(remaining / rate)} left")

    def _export_worker(self, orgs: List[Dict[str, Any]],
                       options: pe.ExportOptions, out: str) -> None:
        """Runs the export in a background thread.

        Args:
            orgs: The organizations to export.
            options: What to export and how to save it.
            out: Output folder.
        """
        try:
            result = pe.run_export(
                self.base, self.token, Path(out), orgs, options,
                log_fn=self._add_log, on_progress=self._on_progress,
                cancel=self.cancel)
        except Exception as e:  # shown to the user; a dead thread hides it
            self._add_log(f"ERROR: {describe(e)}")
            self._finish("Export failed", None)
            return
        if result.cancelled:
            self._finish("Export cancelled", result)
        elif result.has_problems:
            self._finish("Finished with problems", result)
        else:
            self._finish("Export complete", result)

    def _finish(self, title: str, result: Optional[pe.ExportResult]) -> None:
        """Switches the export screen to its finished state.

        Args:
            title: The new heading.
            result: The outcome; None after a crash, when there is nothing
                to open.
        """
        self.title.value = title
        if self.bar.value is None:
            self.bar.value = 0
        elapsed = time.monotonic() - self.started
        if result and result.bytes_downloaded:
            self.speed.value = (f"{human_size(result.bytes_downloaded)} "
                                f"downloaded in {fmt_duration(elapsed)}")
        elif result:
            self.speed.value = ""
        if elapsed >= NOTIFY_AFTER and not (result and result.cancelled):
            beep()
            self.page.run_task(self.page.window.to_front)
        icon, self.banner_kind, head, detail = finish_summary(
            result, self.options.verify)
        self.banner_icon.icon = icon
        self.banner_head.value = head
        self.banner_detail.value = detail
        self.banner.visible = True
        self._paint_banner()
        self.zip_result = result.zip_path if result else None
        self.cancel_btn.visible = False
        self.again_btn.visible = True
        self.retry_btn.visible = bool(result and result.has_problems)
        self.zip_btn.visible = self.zip_result is not None
        self.open_btn.disabled = result is None
        self.manifest_btn.disabled = result is None
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

    def _open_zip(self, _e: Any) -> None:
        """Opens the zip archive that was created."""
        if self.zip_result is not None:
            open_path(self.zip_result)


def main(page: ft.Page) -> None:
    """Flet entry point.

    Args:
        page: The page Flet provides.
    """
    App(page)


if __name__ == "__main__":
    ft.run(main)
