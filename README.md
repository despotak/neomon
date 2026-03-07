# ⬡ NeoMon

**The ultimate terminal system monitor** – built with [Textual](https://textual.textualize.io/) and inspired by btop, bottom, gotop, and glances.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

| Category | Details |
|---|---|
| **Panels** | CPU (per-core bars + sparkline), Memory (RAM + Swap), GPU (nvidia-smi), Disk (per-partition + I/O), Network (per-interface + sparklines) |
| **Graphs** | High-resolution **braille** sparklines (2× data density) or classic block chars — toggle with `b` |
| **Processes** | Live sortable table (CPU/MEM/Name/PID), real-time search/filter, terminate or force-kill |
| **Themes** | 5 built-in color themes: Default · Nord · Gruvbox · Dracula · Monokai |
| **Export** | One-key JSON snapshot of all metrics → `~/neomon_YYYYMMDD_HHMMSS.json` |
| **Battery** | Status + charge% shown in header when a battery is present |
| **Mouse** | Full mouse support via Textual (click, scroll) |

## Requirements

- Python 3.10+
- Windows (uses `winreg` for CPU/OS name detection; cross-platform psutil data still works on Linux/macOS)
- NVIDIA GPU monitoring requires `nvidia-smi` in PATH (optional)

## Installation

```bash
pip install textual psutil
python neomon.py
```

Or install as a package:

```bash
pip install .
neomon
```

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `p` / `c` | Sort processes by CPU% |
| `m` | Sort by Memory% |
| `n` | Sort by Name |
| `i` | Sort by PID |
| `/` | Toggle search bar |
| `Esc` | Close search / clear filter |
| `k` | Terminate selected process |
| `K` | Force-kill selected process |
| `b` | Toggle braille ↔ block graphs |
| `F1`–`F5` | Switch theme |
| `Ctrl+S` | Export JSON snapshot |
| `?` / `h` | Help screen |
| `q` | Quit |

## Architecture

```
neomon/
├── neomon.py            # Top-level launcher
└── neomon/
    ├── collectors.py    # Background data collection (CPU, Mem, GPU, Disk, Net, Procs)
    ├── graph.py         # Braille/block sparklines, bars, formatters
    └── app.py           # Textual App + all panel widgets + themes
```

**Data flow:** `Collector` runs two background daemon threads — one for all psutil metrics (2 s interval), one for `nvidia-smi` GPU data. The `Snap` dataclass is the shared state. Each Textual `Static` widget polls `app.snap` via its own `set_interval` timer and calls `self.update()` to re-render.

**Theme system:** Five named themes (`THEMES` dict in `app.py`) provide color palettes. `F1`–`F5` calls `_apply_theme()` which updates CSS variables and border title colors across all panels.

## License

MIT
