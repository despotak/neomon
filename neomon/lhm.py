"""
LibreHardwareMonitor integration for NeoMon.

Builds a tiny C# helper (lhm_reader.exe) into ~/.neomon/ on first run using
the .NET SDK, then calls it via subprocess to read CPU temperature and power.
Admin rights are required on most systems to read CPU MSR registers.
Without admin, the exe still runs but sensors return 0 (shown as N/A).
"""
from __future__ import annotations

import ctypes
import json
import logging
import subprocess
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# ── paths ─────────────────────────────────────────────────────────────────────

LHM_DIR   = Path.home() / ".neomon"
LHM_EXE   = LHM_DIR / "lhm_reader" / "bin" / "lhm_reader.exe"
_PROJ_DIR = LHM_DIR / "lhm_reader"

# ── state ─────────────────────────────────────────────────────────────────────

_lock        = threading.Lock()
_ready       = False   # True once exe is confirmed working
_unavailable = False   # True if we permanently gave up
_status_msg  = "initialising"


def status() -> str:
    """Human-readable status string shown in the CPU panel."""
    return _status_msg


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ── C# project source (embedded) ──────────────────────────────────────────────

_CSPROJ = """\
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0-windows</TargetFramework>
    <Nullable>enable</Nullable>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <SelfContained>false</SelfContained>
    <RuntimeIdentifier>win-x64</RuntimeIdentifier>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="LibreHardwareMonitorLib" Version="0.9.4" />
  </ItemGroup>
</Project>
"""

_PROGRAM_CS = """\
using System;
using LibreHardwareMonitor.Hardware;

var computer = new Computer { IsCpuEnabled = true };
computer.Open();

double? temp  = null;
double? power = null;

foreach (var hw in computer.Hardware)
{
    hw.Update();
    foreach (var sub in hw.SubHardware) sub.Update();

    foreach (var sensor in hw.Sensors)
    {
        if (sensor.Value is null) continue;
        var name  = sensor.Name.ToLower();
        var stype = sensor.SensorType.ToString().ToLower();
        var val   = (double)(float)sensor.Value;

        if (stype == "temperature")
        {
            if (name.Contains("package"))
                temp = val;
            else if (temp is null && (name.Contains("core") || name.Contains("cpu") || name.Contains("tdie")))
                temp = val;
        }
        if (stype == "power" && name.Contains("package"))
            power = val;
    }
}

computer.Close();

var tempStr  = temp  is null ? "null" : temp.Value.ToString("F1", System.Globalization.CultureInfo.InvariantCulture);
var powerStr = power is null ? "null" : power.Value.ToString("F1", System.Globalization.CultureInfo.InvariantCulture);
Console.WriteLine($"{{\\\"temp\\\":{tempStr},\\\"power\\\":{powerStr}}}");
"""


# ── build helper ──────────────────────────────────────────────────────────────

def _build_exe() -> bool:
    """Write C# project files and compile with dotnet.  Thread-safe."""
    global _status_msg

    if LHM_EXE.exists():
        return True

    try:
        r = subprocess.run(["dotnet", "--version"], capture_output=True, timeout=5)
        if r.returncode != 0:
            _status_msg = "dotnet not found"
            return False
    except Exception:
        _status_msg = "dotnet not found"
        return False

    _status_msg = "building LHM helper…"
    try:
        _PROJ_DIR.mkdir(parents=True, exist_ok=True)
        (_PROJ_DIR / "lhm_reader.csproj").write_text(_CSPROJ, encoding="utf-8")
        (_PROJ_DIR / "Program.cs").write_text(_PROGRAM_CS, encoding="utf-8")

        r = subprocess.run(
            ["dotnet", "build", "-c", "Release", "-r", "win-x64",
             "-o", str(LHM_EXE.parent)],
            cwd=str(_PROJ_DIR),
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            _status_msg = "build failed"
            log.warning("LHM build stderr: %s", r.stderr[-500:])
            return False

        if not LHM_EXE.exists():
            _status_msg = "exe not produced"
            return False

        log.info("LHM helper built at %s", LHM_EXE)
        return True

    except Exception as exc:
        _status_msg = f"build error: {exc}"
        log.warning("LHM build failed: %s", exc)
        return False


# ── initialisation ────────────────────────────────────────────────────────────

def _init() -> bool:
    """Build and smoke-test the helper exe.  Called once from background thread."""
    global _ready, _unavailable, _status_msg

    with _lock:
        if _ready or _unavailable:
            return _ready

        if not _build_exe():
            _unavailable = True
            return False

        try:
            r = subprocess.run(
                [str(LHM_EXE)],
                capture_output=True, text=True, timeout=10,
            )
            json.loads(r.stdout.strip())   # must be valid JSON
            _ready = True
            admin = is_admin()
            _status_msg = "ok" if admin else "ok (run as admin for temps)"
            log.info("LHM helper ready. Admin=%s", admin)
            return True
        except Exception as exc:
            _status_msg = f"LHM run error: {exc}"
            _unavailable = True
            log.warning("LHM smoke-test failed: %s", exc)
            return False


# ── public API ────────────────────────────────────────────────────────────────

def get_cpu_temps_and_power() -> tuple[float | None, float | None]:
    """
    Return (cpu_package_temp_celsius, cpu_package_power_watts).
    Either value is None when unavailable or when admin rights are absent.

    IMPORTANT: This function never calls _init() – it only checks the _ready
    flag (a GIL-safe bool read).  Initialisation is exclusively owned by
    start_background_init() → _init(), which holds _lock for up to 120 s on
    the first run.  Calling _init() here would deadlock the GPU thread.
    """
    if not _ready:
        return None, None

    try:
        r = subprocess.run(
            [str(LHM_EXE)],
            capture_output=True, text=True, timeout=5,
        )
        data  = json.loads(r.stdout.strip().splitlines()[-1])
        temp  = data.get("temp")
        power = data.get("power")

        # 0.0 is the sentinel for "sensor present but no access" – treat as None
        temp  = float(temp)  if temp  is not None and float(temp)  > 0.5 else None
        power = float(power) if power is not None and float(power) > 0.5 else None
        return temp, power

    except Exception as exc:
        log.debug("LHM read error: %s", exc)
        return None, None


def start_background_init() -> None:
    """Kick off build + exe smoke-test in a daemon thread so startup is fast."""
    t = threading.Thread(target=_init, daemon=True, name="nm-lhm-init")
    t.start()
