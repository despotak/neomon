"""Graph-rendering helpers: braille sparklines, block bars, formatters."""
from __future__ import annotations

from typing import Sequence

from rich.text import Text

# ── Braille sparkline ─────────────────────────────────────────────────────────
#
# Braille cell layout (Unicode block U+2800):
#   dot1  dot4      bits: dot1=0, dot2=1, dot3=2, dot7=6  (left col, top→bottom)
#   dot2  dot5            dot4=3, dot5=4, dot6=5, dot8=7  (right col, top→bottom)
#   dot3  dot6
#   dot7  dot8
#
# Filling bottom-to-top (as a bar chart):
#   Left  column 0-4 dots: 0x00, 0x40, 0x44, 0x46, 0x47
#   Right column 0-4 dots: 0x00, 0x80, 0xA0, 0xB0, 0xB8

_L = (0x00, 0x40, 0x44, 0x46, 0x47)   # left  column fill levels 0-4
_R = (0x00, 0x80, 0xA0, 0xB0, 0xB8)   # right column fill levels 0-4
_B = " ▁▂▃▄▅▆▇█"                      # block char fill levels 0-8


def braille_spark(
    values: Sequence[float],
    width: int = 32,
    color: str = "bright_cyan",
    fixed_max: float = 0.0,
) -> Text:
    """
    Return a braille sparkline with 2× horizontal resolution vs. block chars.

    Args:
        values:    Data points (most-recent last).
        width:     Number of braille characters (each covers 2 data points).
        color:     Rich color string.
        fixed_max: If > 0, pin the scale to this maximum (e.g. 100.0 for
                   percentage metrics).  If 0, auto-scale to max(values).
    """
    n    = width * 2
    data = list(values)
    if len(data) < n:
        data = [0.0] * (n - len(data)) + data
    data = data[-n:]
    if fixed_max > 0:
        mx = fixed_max
    else:
        _m = max(data)
        mx = _m if _m > 0 else 1.0
    chars: list[str] = []
    for i in range(0, n, 2):
        l = min(round(data[i]     / mx * 4), 4)
        r = min(round(data[i + 1] / mx * 4), 4)
        chars.append(chr(0x2800 | _L[l] | _R[r]))
    return Text("".join(chars), style=color)


def block_spark(
    values: Sequence[float],
    width: int = 32,
    color: str = "bright_cyan",
    fixed_max: float = 0.0,
) -> Text:
    """
    Block-character sparkline (8 levels). Fallback for terminals without braille.

    Args:
        fixed_max: If > 0, pin the scale to this maximum (e.g. 100.0 for
                   percentage metrics).  If 0, auto-scale to max(values).
    """
    data = list(values)[-width:]
    if not data:
        return Text("")
    if fixed_max > 0:
        mx = fixed_max
    else:
        _m = max(data)
        mx = _m if _m > 0 else 1.0
    return Text("".join(_B[min(int(v / mx * 8), 8)] for v in data), style=color)


# ── Progress bar ──────────────────────────────────────────────────────────────

def bar(
    pct: float,
    width: int = 20,
    warn: float = 60.0,
    crit: float = 85.0,
) -> Text:
    """Colored filled block progress bar."""
    pct    = max(0.0, min(100.0, pct))
    filled = int(width * pct / 100)
    color  = "red bold" if pct >= crit else "yellow" if pct >= warn else "green"
    t = Text()
    t.append("█" * filled,           style=color)
    t.append("░" * (width - filled), style="bright_black")
    return t


# ── Color helpers ─────────────────────────────────────────────────────────────

def pcolor(pct: float, warn: float = 60.0, crit: float = 85.0) -> str:
    """Percent → Rich color string."""
    return "red bold" if pct >= crit else "yellow" if pct >= warn else "green"


def tcolor(temp: float) -> str:
    """Temperature (°C) → Rich color string."""
    return "red bold" if temp >= 90 else "yellow" if temp >= 75 else "green"


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_bytes(n: float) -> str:
    """Format a byte-rate value with auto-scaling unit."""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if abs(n) < 1024:
            return f"{n:6.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB/s"


def uptime_str(secs: int) -> str:
    """Format seconds into human-readable uptime string."""
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _   = divmod(rem, 60)
    return f"{d}d {h:02}h {m:02}m" if d else f"{h:02}h {m:02}m"
