"""NeoMon – The ultimate terminal system monitor (Textual-based)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import psutil
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, Label, Static

from .collectors import Collector, Snap
from .graph import bar, block_spark, braille_spark, fmt_bytes, pcolor, tcolor, uptime_str

# ── Themes ────────────────────────────────────────────────────────────────────

THEMES: dict[str, dict[str, str]] = {
    "default": {
        "cpu": "cyan",        "mem": "magenta",   "gpu": "yellow",
        "disk": "#5b8af5",    "net": "green",     "proc": "white",
        "accent": "#00d7ff",  "dim": "#8b949e",
        "bg": "#0d1117",      "surface": "#161b22", "border": "#21262d",
    },
    "nord": {
        "cpu": "#88c0d0",     "mem": "#b48ead",   "gpu": "#ebcb8b",
        "disk": "#81a1c1",    "net": "#a3be8c",   "proc": "#d8dee9",
        "accent": "#88c0d0",  "dim": "#4c566a",
        "bg": "#2e3440",      "surface": "#3b4252", "border": "#4c566a",
    },
    "gruvbox": {
        "cpu": "#83a598",     "mem": "#d3869b",   "gpu": "#fabd2f",
        "disk": "#458588",    "net": "#b8bb26",   "proc": "#ebdbb2",
        "accent": "#83a598",  "dim": "#928374",
        "bg": "#282828",      "surface": "#3c3836", "border": "#504945",
    },
    "dracula": {
        "cpu": "#8be9fd",     "mem": "#ff79c6",   "gpu": "#f1fa8c",
        "disk": "#6272a4",    "net": "#50fa7b",   "proc": "#f8f8f2",
        "accent": "#bd93f9",  "dim": "#6272a4",
        "bg": "#282a36",      "surface": "#44475a", "border": "#6272a4",
    },
    "monokai": {
        "cpu": "#66d9e8",     "mem": "#ae81ff",   "gpu": "#e6db74",
        "disk": "#a6e22e",    "net": "#f92672",   "proc": "#f8f8f2",
        "accent": "#fd971f",  "dim": "#75715e",
        "bg": "#272822",      "surface": "#3e3d32", "border": "#49483e",
    },
}


# ── Help Modal ────────────────────────────────────────────────────────────────

class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
        Binding("h", "dismiss", "Close"),
    ]
    CSS = """
    HelpScreen { align: center middle; }
    #help-box {
        width: 64; height: auto; max-height: 36;
        background: $surface; border: double $border;
        padding: 1 2; color: $text;
    }
    #help-title {
        text-align: center; text-style: bold;
        color: $accent; padding-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label("⬡  NeoMon  –  Keyboard Reference", id="help-title")
            yield Label("""\
[bold]Navigation[/bold]
  Tab / Shift+Tab       Focus next / previous panel
  ↑ ↓  Page↑ Page↓     Scroll process list

[bold]Sort Processes[/bold]
  p  c                  Sort by CPU%
  m                     Sort by Memory%
  n                     Sort by Name
  i                     Sort by PID

[bold]Process Actions[/bold]
  /                     Toggle search bar
  Esc                   Close search / clear filter
  k                     Terminate selected process
  K                     Force-kill selected process

[bold]Display[/bold]
  b                     Toggle braille ↔ block graphs
  F1–F5                 Switch theme (Default/Nord/Gruvbox/Dracula/Monokai)

[bold]Data[/bold]
  Ctrl+S                Export full snapshot → ~/neomon_*.json

[bold]General[/bold]
  ?  h                  This help screen
  q  Ctrl+C             Quit""")


# ── Header ────────────────────────────────────────────────────────────────────

class HeaderBar(Static):
    DEFAULT_CSS = "HeaderBar { height: 1; }"

    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh)

    def render(self) -> Text:
        app: NeoMon = self.app  # type: ignore[assignment]
        s   = app.snap
        tc  = app.theme_colors
        now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

        t = Text()
        t.append(f"  ⬡ NeoMon  ", style=f"bold {tc['accent']}")
        t.append(s.hostname, style="bold white")
        t.append("  ·  ", style=f"dim {tc['dim']}")
        t.append(s.os_name[:55], style=f"dim {tc['dim']}")
        t.append("  ·  up ", style=f"dim {tc['dim']}")
        t.append(uptime_str(s.uptime), style="green")

        # Battery indicator
        if s.batt_pct is not None:
            icon  = "⚡" if s.batt_plugged else "🔋"
            bc    = pcolor(100.0 - s.batt_pct, 30, 15)   # warn when LOW
            t.append(f"  {icon} ", style=f"dim {tc['dim']}")
            t.append(f"{s.batt_pct:.0f}%", style=bc)

        t.append(f"  {now}  ", style="bold white")
        theme_names = list(THEMES)
        idx = theme_names.index(app.theme_name)
        t.append(f"[F1-F5: theme]  ", style=f"dim {tc['dim']}")
        t.append(f"{app.theme_name}", style=f"dim {tc['accent']}")
        return t


# ── CPU Panel ─────────────────────────────────────────────────────────────────

class CPUPanel(Static):
    DEFAULT_CSS = "CPUPanel { height: 100%; }"

    def on_mount(self) -> None:
        self.set_interval(2.0, self._tick)
        self._tick()

    def _tick(self) -> None:
        self.update(self._build())

    def _build(self) -> Text:
        app: NeoMon = self.app  # type: ignore[assignment]
        s   = app.snap
        tc  = app.theme_colors
        spark = braille_spark if app.use_braille else block_spark

        t = Text(overflow="fold")
        t.append(s.cpu_name[:40], style="bold white")
        t.append(f"  {s.cpu_physical}C/{s.cpu_logical}T", style=f"dim {tc['dim']}")
        if s.cpu_freq:
            t.append(f"  {s.cpu_freq / 1000:.2f} GHz", style=f"dim {tc['dim']}")
        t.append("\n\n")

        t.append(f"Total  {s.cpu_total:5.1f}%  ")
        t.append_text(bar(s.cpu_total, 22))
        t.append("  ")
        t.append_text(spark(list(s.cpu_hist), 18, tc["cpu"]))
        t.append("\n\n")

        cores = s.cpu_per
        half  = (len(cores) + 1) // 2
        for i in range(half):
            p0 = cores[i]
            t.append(f"C{i:<2} {p0:4.0f}%  ")
            t.append_text(bar(p0, 10))
            j = i + half
            if j < len(cores):
                p1 = cores[j]
                t.append(f"  C{j:<2} {p1:4.0f}%  ")
                t.append_text(bar(p1, 10))
            t.append("\n")
        return t


# ── Memory Panel ──────────────────────────────────────────────────────────────

class MemPanel(Static):
    DEFAULT_CSS = "MemPanel { height: 100%; }"

    def on_mount(self) -> None:
        self.set_interval(2.0, self._tick)
        self._tick()

    def _tick(self) -> None:
        self.update(self._build())

    def _build(self) -> Text:
        app: NeoMon = self.app  # type: ignore[assignment]
        s   = app.snap
        tc  = app.theme_colors
        spark = braille_spark if app.use_braille else block_spark

        t = Text(overflow="fold")
        t.append(f"RAM   {s.ram_used:5.1f} / {s.ram_total:.1f} GB   ")
        t.append(f"{s.ram_pct:.1f}%", style=pcolor(s.ram_pct))
        t.append("\n")
        t.append_text(bar(s.ram_pct, 28))
        t.append("  ")
        t.append_text(spark(list(s.ram_hist), 14, tc["mem"]))
        t.append("\n\n")

        t.append(f"Swap  {s.swap_used:5.1f} / {s.swap_total:.1f} GB   ")
        t.append(f"{s.swap_pct:.1f}%", style=pcolor(s.swap_pct))
        t.append("\n")
        t.append_text(bar(s.swap_pct, 28))
        t.append("\n\n")

        t.append(f"Available  {s.ram_available:.1f} GB\n", style=f"dim {tc['dim']}")
        t.append(f"Committed  {s.ram_used:.1f} GB\n", style=f"dim {tc['dim']}")
        return t


# ── GPU Panel ─────────────────────────────────────────────────────────────────

class GPUPanel(Static):
    DEFAULT_CSS = "GPUPanel { height: 100%; }"

    def on_mount(self) -> None:
        self.set_interval(2.0, self._tick)
        self._tick()

    def _tick(self) -> None:
        self.update(self._build())

    def _build(self) -> Text:
        app: NeoMon = self.app  # type: ignore[assignment]
        s   = app.snap
        tc  = app.theme_colors
        spark = braille_spark if app.use_braille else block_spark

        t = Text(overflow="fold")
        if not s.gpu_ok:
            t.append("nvidia-smi unavailable\n", style=f"dim {tc['dim']}")
            t.append("AMD / Intel / no discrete GPU?", style=f"dim {tc['dim']}")
            return t

        t.append(s.gpu_name[:36], style="bold white")
        t.append("\n\n")

        t.append(f"GPU   {s.gpu_util:5.1f}%  ")
        t.append_text(bar(s.gpu_util, 22))
        t.append("  ")
        t.append_text(spark(list(s.gpu_hist), 14, tc["gpu"]))
        t.append("\n")

        vram_pct = (s.gpu_vram_used / s.gpu_vram_total * 100) if s.gpu_vram_total else 0
        t.append(f"VRAM  {s.gpu_vram_used:5.2f} / {s.gpu_vram_total:.1f} GB  ")
        t.append(f"{vram_pct:.1f}%", style=pcolor(vram_pct))
        t.append("\n")
        t.append_text(bar(vram_pct, 28))
        t.append("\n\n")

        sep   = Text("  ·  ", style=f"dim {tc['dim']}")
        parts: list[Text] = []
        if s.gpu_temp is not None:
            parts.append(Text(f"{s.gpu_temp:.0f}°C", style=tcolor(s.gpu_temp)))
        if s.gpu_fan is not None:
            parts.append(Text(f"Fan {s.gpu_fan:.0f}%", style=f"dim {tc['dim']}"))
        if s.gpu_clk is not None:
            parts.append(Text(f"Core {s.gpu_clk:.0f} MHz", style=f"dim {tc['dim']}"))
        if s.gpu_mclk is not None:
            parts.append(Text(f"Mem {s.gpu_mclk:.0f} MHz", style=f"dim {tc['dim']}"))
        if s.gpu_power is not None:
            pw = Text("Power ")
            if s.gpu_plim:
                pp = s.gpu_power / s.gpu_plim * 100
                pw.append(f"{s.gpu_power:.0f}", style=pcolor(pp))
                pw.append(f"/{s.gpu_plim:.0f} W")
            else:
                pw.append(f"{s.gpu_power:.0f} W")
            parts.append(pw)

        for i, part in enumerate(parts):
            if i:
                t.append_text(sep)
            t.append_text(part)
        if parts:
            t.append("\n")
        return t


# ── Disk Panel ────────────────────────────────────────────────────────────────

class DiskPanel(Static):
    DEFAULT_CSS = "DiskPanel { height: 100%; }"

    def on_mount(self) -> None:
        self.set_interval(2.0, self._tick)
        self._tick()

    def _tick(self) -> None:
        self.update(self._build())

    def _build(self) -> Text:
        app: NeoMon = self.app  # type: ignore[assignment]
        s  = app.snap
        tc = app.theme_colors

        t = Text(overflow="fold")
        for part in s.disk_parts[:4]:
            lbl = part["device"][:14]
            pct = part["pct"]
            t.append(f"{lbl:<14} {part['used_gb']:5.1f}/{part['total_gb']:.0f} GB ")
            t.append(f"{pct:.0f}%", style=pcolor(pct, 80, 92))
            t.append("\n")
            t.append_text(bar(pct, 26, 80, 92))
            t.append("\n")

        t.append("\n")
        t.append("Read   ", style=f"dim {tc['dim']}")
        t.append(fmt_bytes(s.disk_r_bps), style="green")
        t.append("\nWrite  ", style=f"dim {tc['dim']}")
        t.append(fmt_bytes(s.disk_w_bps), style="yellow")
        return t


# ── Network Panel ─────────────────────────────────────────────────────────────

class NetPanel(Static):
    DEFAULT_CSS = "NetPanel { height: 100%; }"

    def on_mount(self) -> None:
        self.set_interval(2.0, self._tick)
        self._tick()

    def _tick(self) -> None:
        self.update(self._build())

    def _build(self) -> Text:
        app: NeoMon = self.app  # type: ignore[assignment]
        s   = app.snap
        tc  = app.theme_colors
        spark = braille_spark if app.use_braille else block_spark

        t = Text(overflow="fold")
        for iface in s.net_ifaces[:3]:
            t.append(f"{iface['name']}\n", style="bold white")
            t.append("  ↑ ", style=f"dim {tc['dim']}")
            t.append(fmt_bytes(iface["send"]), style="green")
            t.append("  ↓ ", style=f"dim {tc['dim']}")
            t.append(fmt_bytes(iface["recv"]), style="cyan")
            t.append("\n")
        if not s.net_ifaces:
            t.append("no active traffic\n", style=f"dim {tc['dim']}")

        t.append("\n")
        t.append("Total ↑ ", style=f"dim {tc['dim']}")
        t.append(fmt_bytes(s.net_send), style="green")
        t.append("  ↓ ", style=f"dim {tc['dim']}")
        t.append(fmt_bytes(s.net_recv), style="cyan")
        t.append("\n")
        t.append_text(spark(list(s.net_send_hist), 20, "green"))
        t.append(" ↑\n", style="dim green")
        t.append_text(spark(list(s.net_recv_hist), 20, "cyan"))
        t.append(" ↓", style="dim cyan")
        return t


# ── Search Bar ────────────────────────────────────────────────────────────────

class SearchBar(Horizontal):
    DEFAULT_CSS = """
    SearchBar {
        height: 3; display: none; padding: 0 1;
    }
    SearchBar.visible { display: block; }
    SearchBar Label {
        height: 3; content-align: left middle;
        width: auto; padding-right: 1;
    }
    SearchBar Input { width: 40; }
    """

    def compose(self) -> ComposeResult:
        yield Label("🔍 Filter: ")
        yield Input(placeholder="name or PID…", id="search-input")

    @on(Input.Changed, "#search-input")
    def _changed(self, event: Input.Changed) -> None:
        self.app.filter_str = event.value  # type: ignore[attr-defined]


# ── Process Panel ─────────────────────────────────────────────────────────────

class ProcessPanel(Widget):
    DEFAULT_CSS = """
    ProcessPanel { height: 1fr; }
    ProcessPanel DataTable { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield DataTable(id="proc-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        tbl = self.query_one(DataTable)
        tbl.add_columns("PID", "Name", "CPU %", "MEM %", "Memory", "Status", "User")
        self.set_interval(2.0, self._tick)

    def _tick(self) -> None:
        app: NeoMon       = self.app  # type: ignore[assignment]
        s                 = app.snap
        sort_key: str     = app.sort_key
        filter_str: str   = app.filter_str.lower()
        tbl               = self.query_one(DataTable)

        procs = list(s.procs)
        if filter_str:
            procs = [p for p in procs if filter_str in p.name.lower()
                     or filter_str in str(p.pid)
                     or filter_str in p.user.lower()]

        if sort_key == "cpu":
            procs.sort(key=lambda p: p.cpu, reverse=True)
        elif sort_key == "mem":
            procs.sort(key=lambda p: p.mem_pct, reverse=True)
        elif sort_key == "name":
            procs.sort(key=lambda p: p.name.lower())
        elif sort_key == "pid":
            procs.sort(key=lambda p: p.pid)

        cursor = tbl.cursor_row
        tbl.clear()
        for p in procs:
            cs = pcolor(p.cpu)
            ms = pcolor(p.mem_pct * 8)
            tbl.add_row(
                str(p.pid),
                p.name,
                Text(f"{p.cpu:5.1f}", style=cs),
                Text(f"{p.mem_pct:5.1f}", style=ms),
                f"{p.mem_mb:>7.0f} MB",
                p.status[:10],
                p.user,
            )
        if tbl.row_count > 0:
            tbl.move_cursor(row=min(cursor, tbl.row_count - 1))

        sort_lbl = {"cpu": "CPU↓", "mem": "MEM↓", "name": "Name↑", "pid": "PID↑"}.get(sort_key, "?")
        shown    = len(procs)
        total    = s.proc_total
        flt_str  = f"  filter: '{filter_str}'" if filter_str else ""
        self.border_title = f"Processes  {shown}/{total}  sort:{sort_lbl}{flt_str}"


# ── Status Footer ─────────────────────────────────────────────────────────────

class StatusFooter(Static):
    DEFAULT_CSS = "StatusFooter { height: 1; }"

    def on_mount(self) -> None:
        self.update(self._build())

    def _build(self) -> Text:
        app: NeoMon = self.app  # type: ignore[assignment]
        tc = app.theme_colors
        dim = f"dim {tc['dim']}"
        key = f"bold {tc['accent']}"

        t = Text()
        pairs = [
            ("[q]", "Quit"),
            ("[p/m/n/i]", "Sort"),
            ("[/]", "Search"),
            ("[k/K]", "Kill"),
            ("[b]", "Braille"),
            ("[F1-F5]", "Theme"),
            ("[Ctrl+S]", "Export"),
            ("[?]", "Help"),
        ]
        for k, v in pairs:
            t.append(f"  {k}", style=key)
            t.append(f" {v}", style=dim)
        return t


# ── Main App ──────────────────────────────────────────────────────────────────

class NeoMon(App):
    """NeoMon – the ultimate Python terminal system monitor."""

    TITLE = "NeoMon"

    CSS = """
    Screen  { layout: vertical; }
    HeaderBar { background: $surface; color: $text; }
    #top-row  { height: 15; }
    #mid-row  { height: 13; }
    CPUPanel, MemPanel, GPUPanel { border: solid $border; border-title-style: bold; }
    DiskPanel, NetPanel          { border: solid $border; border-title-style: bold; }
    ProcessPanel { border: solid $border; border-title-style: bold; }
    SearchBar { background: $surface; }
    StatusFooter { background: $surface; }
    DataTable { background: $background; }
    DataTable > .datatable--header { text-style: bold; color: $accent; }
    DataTable > .datatable--cursor { background: $surface; }
    """

    BINDINGS = [
        Binding("q",             "quit",              "Quit",       show=False),
        Binding("p",             "sort_cpu",          "Sort CPU",   show=False),
        Binding("c",             "sort_cpu",          "Sort CPU",   show=False),
        Binding("m",             "sort_mem",          "Sort MEM",   show=False),
        Binding("n",             "sort_name",         "Sort Name",  show=False),
        Binding("i",             "sort_pid",          "Sort PID",   show=False),
        Binding("slash",         "toggle_search",     "Search",     show=False),
        Binding("escape",        "clear_search",      "Clear",      show=False),
        Binding("k",             "kill_proc",         "Kill",       show=False),
        Binding("K",             "force_kill_proc",   "Force Kill", show=False),
        Binding("b",             "toggle_braille",    "Braille",    show=False),
        Binding("f1",            "theme('default')",  "Default",    show=False),
        Binding("f2",            "theme('nord')",     "Nord",       show=False),
        Binding("f3",            "theme('gruvbox')",  "Gruvbox",    show=False),
        Binding("f4",            "theme('dracula')",  "Dracula",    show=False),
        Binding("f5",            "theme('monokai')",  "Monokai",    show=False),
        Binding("ctrl+s",        "export",            "Export",     show=False),
        Binding("question_mark", "help",              "Help",       show=False),
        Binding("h",             "help",              "Help",       show=False),
    ]

    sort_key:    reactive[str]  = reactive("cpu")
    filter_str:  reactive[str]  = reactive("")
    use_braille: reactive[bool] = reactive(True)
    theme_name:  reactive[str]  = reactive("default")

    def __init__(self, collector: Collector) -> None:
        super().__init__()
        self.collector    = collector
        self.snap: Snap   = collector.snap
        self.theme_colors = THEMES["default"]

    # ── layout ───────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Horizontal(id="top-row"):
            yield CPUPanel(id="cpu")
            yield MemPanel(id="mem")
            yield GPUPanel(id="gpu")
        with Horizontal(id="mid-row"):
            yield DiskPanel(id="disk")
            yield NetPanel(id="net")
        yield SearchBar(id="search-bar")
        yield ProcessPanel(id="procs")
        yield StatusFooter(id="footer")

    def on_mount(self) -> None:
        self._apply_theme("default")
        self._update_border_titles()
        self.set_interval(2.0, self._sync)

    def _sync(self) -> None:
        """Keep snap reference current."""
        self.snap = self.collector.snap

    # ── theming ──────────────────────────────────────────────────────────────

    def _apply_theme(self, name: str) -> None:
        tc = THEMES.get(name, THEMES["default"])
        self.theme_colors = tc
        self.theme_name   = name
        self.app.theme    = "textual-dark"   # always dark base

        # Override CSS variables on the Screen
        self.screen.styles.background         = tc["bg"]
        self.screen.styles.color              = "#c9d1d9"

        # Update border titles after theme change
        self._update_border_titles()
        self.refresh(layout=True)

    def _update_border_titles(self) -> None:
        tc = self.theme_colors
        mapping = {
            "#cpu":   ("CPU",     tc["cpu"]),
            "#mem":   ("Memory",  tc["mem"]),
            "#gpu":   ("GPU",     tc["gpu"]),
            "#disk":  ("Disk",    tc["disk"]),
            "#net":   ("Network", tc["net"]),
            "#procs": ("Processes", tc["proc"]),
        }
        for selector, (title, color) in mapping.items():
            try:
                w = self.query_one(selector)
                w.border_title = title
                w.styles.border_title_color = color
            except Exception:
                pass

    def watch_theme_name(self, name: str) -> None:
        self._update_border_titles()
        # Refresh footer to reflect theme name
        try:
            self.query_one(StatusFooter).update(
                self.query_one(StatusFooter)._build()
            )
        except Exception:
            pass

    # ── actions ──────────────────────────────────────────────────────────────

    def action_sort_cpu(self)  -> None: self.sort_key = "cpu"
    def action_sort_mem(self)  -> None: self.sort_key = "mem"
    def action_sort_name(self) -> None: self.sort_key = "name"
    def action_sort_pid(self)  -> None: self.sort_key = "pid"

    def action_toggle_search(self) -> None:
        bar = self.query_one("#search-bar", SearchBar)
        if bar.has_class("visible"):
            bar.remove_class("visible")
            self.filter_str = ""
            bar.query_one(Input).value = ""
        else:
            bar.add_class("visible")
            bar.query_one(Input).focus()

    def action_clear_search(self) -> None:
        bar = self.query_one("#search-bar", SearchBar)
        if bar.has_class("visible"):
            bar.remove_class("visible")
            self.filter_str = ""
            bar.query_one(Input).value = ""
        else:
            # Escape with no search open → return focus to process table
            try:
                self.query_one("#proc-table", DataTable).focus()
            except Exception:
                pass

    def action_toggle_braille(self) -> None:
        self.use_braille = not self.use_braille
        mode = "braille" if self.use_braille else "block"
        self.notify(f"Graph mode: {mode}", timeout=2)

    def action_theme(self, name: str) -> None:
        self._apply_theme(name)
        self.notify(f"Theme: {name}", timeout=2)

    def action_kill_proc(self)       -> None: self._kill(force=False)
    def action_force_kill_proc(self) -> None: self._kill(force=True)

    def _kill(self, force: bool) -> None:
        try:
            tbl = self.query_one("#proc-table", DataTable)
            if tbl.row_count == 0:
                return
            row  = tbl.get_row_at(tbl.cursor_row)
            pid  = int(str(row[0]))
            name = str(row[1])
            proc = psutil.Process(pid)
            if force:
                proc.kill()
            else:
                proc.terminate()
            verb = "Force-killed" if force else "Terminated"
            self.notify(f"{verb} PID {pid} ({name})", timeout=3)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            self.notify(f"Cannot kill: {e}", severity="error", timeout=4)
        except Exception as e:
            self.notify(f"Error: {e}", severity="error", timeout=4)

    def action_export(self) -> None:
        s    = self.snap
        now  = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = Path.home() / f"neomon_snapshot_{now}.json"
        data = {
            "timestamp": now,
            "cpu": {
                "name": s.cpu_name, "total_pct": s.cpu_total,
                "per_core_pct": list(s.cpu_per), "freq_mhz": s.cpu_freq,
                "physical_cores": s.cpu_physical, "logical_cores": s.cpu_logical,
            },
            "memory": {
                "ram_used_gb": round(s.ram_used, 3),
                "ram_total_gb": round(s.ram_total, 3),
                "ram_pct": s.ram_pct,
                "swap_used_gb": round(s.swap_used, 3),
                "swap_total_gb": round(s.swap_total, 3),
                "swap_pct": s.swap_pct,
            },
            "gpu": {
                "available": s.gpu_ok,
                "name": s.gpu_name,
                "util_pct": s.gpu_util,
                "vram_used_gb": round(s.gpu_vram_used, 3),
                "vram_total_gb": round(s.gpu_vram_total, 3),
                "temp_c": s.gpu_temp,
                "power_w": s.gpu_power,
            },
            "disk": {
                "partitions": s.disk_parts,
                "read_bps": round(s.disk_r_bps, 1),
                "write_bps": round(s.disk_w_bps, 1),
            },
            "network": {
                "interfaces": s.net_ifaces,
                "total_send_bps": round(s.net_send, 1),
                "total_recv_bps": round(s.net_recv, 1),
            },
            "top_processes": [
                {"pid": p.pid, "name": p.name,
                 "cpu_pct": p.cpu, "mem_pct": p.mem_pct, "mem_mb": round(p.mem_mb, 1)}
                for p in sorted(s.procs, key=lambda x: x.cpu, reverse=True)[:50]
            ],
            "system": {
                "hostname": s.hostname, "os": s.os_name, "uptime_s": s.uptime,
            },
        }
        path.write_text(json.dumps(data, indent=2))
        self.notify(f"Snapshot → {path.name}", timeout=4)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
