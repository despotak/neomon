"""Background data collectors for NeoMon."""
from __future__ import annotations

import platform
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import NamedTuple

import psutil

# Shared refresh interval (seconds).  Used by both the Collector threads and
# the Textual panel timers so changing it in one place affects everything.
INTERVAL: float = 0.5

HIST = 360          # history length (~3 min at INTERVAL = 0.5 s)
_HW_INTERVAL = 2.0  # CPU hardware sensor rate; LHM/.NET subprocess startup is expensive


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fv(x: str) -> float | None:
    """Parse a CSV string field to float; return None on failure."""
    try:
        return float(x)
    except Exception:
        return None


# Lower-case name fragments that identify virtual / noise network adapters.
# Applied to both the per-interface display list and the bandwidth totals so
# loopback / docker / WSL traffic never inflates the "Total" row.
_SKIP_IFACE = frozenset({
    "loopback", "vethernet", "isatap", "teredo",
    "vmware", "virtualbox", "hyper-v", "wsl", "pseudo",
})


# ── Data types ────────────────────────────────────────────────────────────────

class ProcInfo(NamedTuple):
    pid: int
    name: str
    cpu: float
    mem_pct: float
    mem_mb: float
    status: str
    user: str


class DiskPart(NamedTuple):
    device: str
    fstype: str
    used_gb: float
    total_gb: float
    pct: float


class NetIface(NamedTuple):
    name: str
    send: float   # bytes/sec
    recv: float   # bytes/sec


@dataclass
class Snap:
    """
    Immutable-ish system metrics snapshot.

    Built fresh each tick by Collector._build_snap() and published via an
    atomic reference swap (self.snap = new_snap).  The UI always reads a
    *complete* snapshot – never a half-written one.
    """

    # ── CPU ───────────────────────────────────────────────────────────────────
    cpu_total: float = 0.0
    cpu_per: list[float] = field(default_factory=list)
    cpu_freq: float = 0.0
    cpu_freq_max: float = 0.0
    cpu_logical: int = 1
    cpu_physical: int = 1
    cpu_name: str = "CPU"
    cpu_hist: list[float] = field(default_factory=list)
    cpu_temp: float | None = None    # °C package temperature
    cpu_power: float | None = None   # W package power

    # ── Memory ────────────────────────────────────────────────────────────────
    ram_used: float = 0.0           # GB physical used
    ram_total: float = 0.0          # GB
    ram_pct: float = 0.0
    ram_available: float = 0.0      # GB
    ram_committed: float = 0.0      # GB Windows commit charge
    ram_hist: list[float] = field(default_factory=list)
    swap_used: float = 0.0
    swap_total: float = 0.0
    swap_pct: float = 0.0

    # ── GPU (nvidia-smi) ──────────────────────────────────────────────────────
    gpu_ok: bool = False
    gpu_name: str = ""
    gpu_util: float = 0.0
    gpu_vram_used: float = 0.0      # GB
    gpu_vram_total: float = 0.0     # GB
    gpu_temp: float | None = None
    gpu_power: float | None = None
    gpu_plim: float | None = None
    gpu_fan: float | None = None
    gpu_clk: float | None = None
    gpu_mclk: float | None = None
    gpu_hist: list[float] = field(default_factory=list)

    # ── Disk ──────────────────────────────────────────────────────────────────
    disk_parts: list[DiskPart] = field(default_factory=list)
    disk_r_bps: float = 0.0
    disk_w_bps: float = 0.0

    # ── Network ───────────────────────────────────────────────────────────────
    net_ifaces: list[NetIface] = field(default_factory=list)
    net_send: float = 0.0
    net_recv: float = 0.0
    net_send_hist: list[float] = field(default_factory=list)
    net_recv_hist: list[float] = field(default_factory=list)

    # ── Processes ─────────────────────────────────────────────────────────────
    procs: list[ProcInfo] = field(default_factory=list)
    proc_total: int = 0

    # ── Battery ───────────────────────────────────────────────────────────────
    batt_pct: float | None = None
    batt_plugged: bool | None = None
    batt_secs: int | None = None

    # ── System (static after init) ────────────────────────────────────────────
    hostname: str = ""
    os_name: str = ""
    boot_time: float = 0.0
    uptime: int = 0


@dataclass
class _GpuData:
    """
    GPU metrics written atomically by _gpu_loop.
    The main loop reads self._gpu (a single attribute read = atomic under GIL)
    and copies all fields into the new Snap, so the UI never sees a torn read.
    """
    ok: bool = False
    name: str = ""
    util: float = 0.0
    vram_used: float = 0.0
    vram_total: float = 0.0
    temp: float | None = None
    power: float | None = None
    plim: float | None = None
    fan: float | None = None
    clk: float | None = None
    mclk: float | None = None


@dataclass
class _CpuHwData:
    """
    CPU hardware sensor values (temp + power) written atomically by nm-gpu.
    Packing both fields into one object means nm-collector reads a coherent
    pair via a single LOAD_ATTR, avoiding torn reads between temp and power.
    """
    temp: float | None = None
    power: float | None = None


# ── Collector ─────────────────────────────────────────────────────────────────

class Collector:
    """
    Manages background threads that build Snap objects.

    Thread model:
      nm-collector  – CPU / RAM / Disk / Net every INTERVAL seconds.
                      Calls _build_snap() which atomically publishes self.snap.
      nm-gpu        – nvidia-smi + CPU hardware sensors every INTERVAL seconds.
                      Writes self._gpu (atomic reference swap).
      nm-proc       – psutil.process_iter() every INTERVAL seconds.
                      Heavy call isolated so it never delays the other metrics.
    """

    def __init__(self, interval: float = INTERVAL) -> None:
        self.interval = interval

        # Public: atomic reference – replaced each tick, never mutated in-place.
        self.snap = Snap()

        self._running = False

        # ── History deques (owned by nm-collector only) ───────────────────────
        self._cpu_hist = deque([0.0] * HIST, maxlen=HIST)
        self._ram_hist = deque([0.0] * HIST, maxlen=HIST)
        self._gpu_hist = deque([0.0] * HIST, maxlen=HIST)
        self._ns_hist  = deque([0.0] * HIST, maxlen=HIST)  # net send
        self._nr_hist  = deque([0.0] * HIST, maxlen=HIST)  # net recv

        # ── GPU (written by nm-gpu, read by nm-collector) ─────────────────────
        # Atomic reference swap: nm-gpu builds a new _GpuData, then does
        # self._gpu = new_g  (one STORE_ATTR bytecode → GIL-safe).
        self._gpu: _GpuData = _GpuData()

        # ── CPU hardware sensors (written atomically by nm-gpu) ───────────────
        # Both values packed in one object so nm-collector reads a coherent
        # pair with a single LOAD_ATTR (GIL-safe reference swap).
        self._cpu_hw: _CpuHwData = _CpuHwData()

        # ── IO delta state (nm-collector only) ────────────────────────────────
        self._prev_disk = None
        self._prev_net: dict = {}
        self._prev_t = time.monotonic()

        # ── Disk partition cache (nm-collector only) ──────────────────────────
        self._part_cache: list[DiskPart] = []
        self._part_cache_t: float = 0.0

        # ── Process snapshot (nm-proc writes, nm-collector reads) ─────────────
        self._procs: list[ProcInfo] = []
        self._proc_total: int = 0
        self._proc_lock = threading.Lock()

        # ── Static system info (set once in _init_static) ─────────────────────
        self._static_cpu_name:     str   = "CPU"
        self._static_os_name:      str   = ""
        self._static_hostname:     str   = ""
        self._static_boot_time:    float = 0.0
        self._static_cpu_logical:  int   = 1
        self._static_cpu_physical: int   = 1
        self._static_cpu_freq_max: float = 0.0

        self._init_static()

    # ── one-time setup ────────────────────────────────────────────────────────

    def _init_static(self) -> None:
        """
        Read static system facts once at startup.  Results are stored as
        instance attributes (self._static_*) so _build_snap() can populate
        new Snap objects directly without reading from the previous one.

        The initial self.snap is also updated here so the UI sees real data
        during the brief 0.5 s delay before the first _build_snap() runs.
        """
        # CPU brand string + OS name – both need winreg, so one import/try block
        cpu_name = platform.processor() or "CPU"
        os_name  = f"{platform.system()} {platform.release()}"
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as k:
                cpu_name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as k:
                prod  = winreg.QueryValueEx(k, "ProductName")[0]
                build = winreg.QueryValueEx(k, "CurrentBuildNumber")[0]
                os_name = f"{prod} (build {build})"
        except Exception:
            pass   # fallback values already set above

        freq = psutil.cpu_freq()

        self._static_cpu_name     = cpu_name
        self._static_os_name      = os_name
        self._static_hostname     = socket.gethostname()
        self._static_boot_time    = psutil.boot_time()
        self._static_cpu_logical  = psutil.cpu_count(logical=True)  or 1
        self._static_cpu_physical = psutil.cpu_count(logical=False) or 1
        self._static_cpu_freq_max = (freq.max or freq.current) if freq else 0.0

        # Populate the initial Snap so the UI has real data before the first tick
        s = self.snap
        s.cpu_name     = self._static_cpu_name
        s.os_name      = self._static_os_name
        s.hostname     = self._static_hostname
        s.boot_time    = self._static_boot_time
        s.cpu_logical  = self._static_cpu_logical
        s.cpu_physical = self._static_cpu_physical
        s.cpu_freq_max = self._static_cpu_freq_max

        # Prime CPU counters (first call always returns 0.0)
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running   = True
        self._prev_disk = psutil.disk_io_counters()
        self._prev_net  = psutil.net_io_counters(pernic=True)
        from neomon import lhm
        lhm.start_background_init()
        threading.Thread(target=self._main_loop, daemon=True, name="nm-collector").start()
        threading.Thread(target=self._gpu_loop,  daemon=True, name="nm-gpu").start()
        threading.Thread(target=self._proc_loop, daemon=True, name="nm-proc").start()

    def stop(self) -> None:
        self._running = False

    # ── main collection loop ──────────────────────────────────────────────────

    def _main_loop(self) -> None:
        time.sleep(self.interval)   # allow CPU counters to prime
        while self._running:
            t0 = time.monotonic()
            elapsed = max(t0 - self._prev_t, 0.001)
            self._prev_t = t0
            self._build_snap(elapsed)
            sleep_for = max(0.0, self.interval - (time.monotonic() - t0))
            time.sleep(sleep_for)

    def _build_snap(self, elapsed: float) -> None:
        """
        Build a complete new Snap and atomically publish it via self.snap = new.

        All mutations stay local until the final assignment.  The UI always
        reads a coherent, fully-populated snapshot.
        """
        new = Snap()

        # Static fields come from self._static_* (set once; never need prev.*)
        new.cpu_name     = self._static_cpu_name
        new.cpu_logical  = self._static_cpu_logical
        new.cpu_physical = self._static_cpu_physical
        new.cpu_freq_max = self._static_cpu_freq_max
        new.hostname     = self._static_hostname
        new.os_name      = self._static_os_name
        new.boot_time    = self._static_boot_time

        # ── CPU ───────────────────────────────────────────────────────────────
        new.cpu_total = psutil.cpu_percent(interval=None)
        new.cpu_per   = psutil.cpu_percent(interval=None, percpu=True)
        self._cpu_hist.append(new.cpu_total)
        new.cpu_hist  = list(self._cpu_hist)
        freq = psutil.cpu_freq()
        if freq:
            new.cpu_freq = freq.current

        # CPU hardware sensors: one LOAD_ATTR reads the whole pair atomically.
        hw = self._cpu_hw
        new.cpu_temp  = hw.temp
        new.cpu_power = hw.power

        # ── Memory ────────────────────────────────────────────────────────────
        ram  = psutil.virtual_memory()
        swap = psutil.swap_memory()
        new.ram_used      = ram.used      / 1024 ** 3
        new.ram_total     = ram.total     / 1024 ** 3
        new.ram_available = ram.available / 1024 ** 3
        new.ram_pct       = ram.percent
        new.ram_committed = getattr(ram, "committed", ram.used) / 1024 ** 3
        self._ram_hist.append(ram.percent)
        new.ram_hist  = list(self._ram_hist)
        new.swap_used  = swap.used  / 1024 ** 3
        new.swap_total = swap.total / 1024 ** 3
        new.swap_pct   = swap.percent

        # ── Disk I/O ──────────────────────────────────────────────────────────
        curr_disk = psutil.disk_io_counters()
        if curr_disk and self._prev_disk:
            new.disk_r_bps = (curr_disk.read_bytes  - self._prev_disk.read_bytes)  / elapsed
            new.disk_w_bps = (curr_disk.write_bytes - self._prev_disk.write_bytes) / elapsed
        self._prev_disk = curr_disk

        # Disk partitions: re-scan at most once per 60 s
        now = time.monotonic()
        if now - self._part_cache_t > 60.0:
            self._part_cache   = self._scan_partitions()
            self._part_cache_t = now
        new.disk_parts = self._part_cache

        # ── Network ───────────────────────────────────────────────────────────
        # The _SKIP_IFACE filter is applied to *both* the display list and the
        # bandwidth totals, so loopback / docker / WSL never inflates "Total".
        curr_net = psutil.net_io_counters(pernic=True)
        ifaces: list[NetIface] = []
        total_s = total_r = 0.0
        for name, st in curr_net.items():
            p = self._prev_net.get(name)
            if not p:
                continue
            name_lo = name.lower()
            if any(v in name_lo for v in _SKIP_IFACE):
                continue
            snd = (st.bytes_sent - p.bytes_sent) / elapsed
            rcv = (st.bytes_recv - p.bytes_recv) / elapsed
            total_s += snd
            total_r += rcv
            if snd > 0 or rcv > 0:
                ifaces.append(NetIface(name[:24], snd, rcv))
        self._prev_net    = curr_net
        new.net_ifaces    = ifaces
        new.net_send      = total_s
        new.net_recv      = total_r
        self._ns_hist.append(total_s)
        self._nr_hist.append(total_r)
        new.net_send_hist = list(self._ns_hist)
        new.net_recv_hist = list(self._nr_hist)

        # ── GPU (atomic reference from nm-gpu) ────────────────────────────────
        g = self._gpu   # single LOAD_ATTR = atomic under GIL
        new.gpu_ok         = g.ok
        new.gpu_name       = g.name
        new.gpu_util       = g.util
        new.gpu_vram_used  = g.vram_used
        new.gpu_vram_total = g.vram_total
        new.gpu_temp       = g.temp
        new.gpu_power      = g.power
        new.gpu_plim       = g.plim
        new.gpu_fan        = g.fan
        new.gpu_clk        = g.clk
        new.gpu_mclk       = g.mclk
        self._gpu_hist.append(g.util)
        new.gpu_hist       = list(self._gpu_hist)

        # ── Processes (from nm-proc, behind a lock) ───────────────────────────
        with self._proc_lock:
            new.procs      = self._procs
            new.proc_total = self._proc_total

        # ── Battery ───────────────────────────────────────────────────────────
        try:
            batt = psutil.sensors_battery()
            if batt:
                new.batt_pct     = batt.percent
                new.batt_plugged = batt.power_plugged
                new.batt_secs    = batt.secsleft if batt.secsleft > 0 else None
        except Exception:
            pass

        # ── Uptime ────────────────────────────────────────────────────────────
        new.uptime = int(time.time() - self._static_boot_time)

        # ── Atomic publish ────────────────────────────────────────────────────
        # STORE_ATTR is a single CPython bytecode executed under the GIL.
        # Any concurrent reader of self.snap sees either the old or the new
        # complete Snap – never a partially-written one.
        self.snap = new

    def _scan_partitions(self) -> list[DiskPart]:
        parts: list[DiskPart] = []
        for p in psutil.disk_partitions():
            try:
                u   = psutil.disk_usage(p.mountpoint)
                lbl = p.device.replace("\\\\", "").rstrip("\\")
                parts.append(DiskPart(
                    device   = lbl,
                    fstype   = p.fstype,
                    used_gb  = u.used  / 1024 ** 3,
                    total_gb = u.total / 1024 ** 3,
                    pct      = u.percent,
                ))
            except (PermissionError, OSError):
                pass
        return parts

    # ── process loop ──────────────────────────────────────────────────────────

    def _proc_loop(self) -> None:
        """
        Collect the process list in its own thread.

        psutil.process_iter() can take 200-800 ms on busy systems; isolating
        it here prevents the CPU/RAM/Net metrics from stalling.
        """
        time.sleep(self.interval * 1.5)   # stagger after nm-collector primes CPU counters
        attrs = ["pid", "name", "cpu_percent", "memory_percent",
                 "memory_info", "status", "username"]
        while self._running:
            t0 = time.monotonic()
            procs: list[ProcInfo] = []
            for proc in psutil.process_iter(attrs):
                try:
                    info   = proc.info
                    if info.get("pid") == 0:
                        continue
                    mem_mb = (info["memory_info"].rss / 1024 ** 2) if info.get("memory_info") else 0.0
                    user   = (info.get("username") or "").split("\\")[-1][:14]
                    procs.append(ProcInfo(
                        pid     = info["pid"],
                        name    = (info.get("name") or "")[:32],
                        cpu     = info.get("cpu_percent") or 0.0,
                        mem_pct = info.get("memory_percent") or 0.0,
                        mem_mb  = mem_mb,
                        status  = info.get("status") or "",
                        user    = user,
                    ))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            with self._proc_lock:
                self._procs      = procs
                self._proc_total = len(procs)
            sleep_for = max(0.0, self.interval - (time.monotonic() - t0))
            time.sleep(sleep_for)

    # ── CPU hardware sensors (called from nm-gpu) ─────────────────────────────

    def _fetch_cpu_hw(self) -> None:
        """
        Fetch CPU package temp + power via LHM, then WMI, then ACPI PowerShell.

        Each metric (temp, power) is fetched independently: if LHM provides
        temp but not power, the fallback chain continues for power only.
        All results are accumulated into a local _CpuHwData, then published
        via a single atomic reference swap (one STORE_ATTR under the GIL).
        """
        from neomon import lhm

        hw         = _CpuHwData()
        temp_done  = False
        power_done = False

        # 1 – LibreHardwareMonitor via pre-built exe (preferred)
        t, p = lhm.get_cpu_temps_and_power()
        if t is not None:
            hw.temp   = t
            temp_done = True
        if p is not None:
            hw.power   = p
            power_done = True

        if not (temp_done and power_done):
            # 2 – LibreHardwareMonitor / OpenHardwareMonitor WMI namespace
            for ns in ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor"):
                if temp_done and power_done:
                    break
                try:
                    import wmi  # type: ignore
                    w       = wmi.WMI(namespace=ns)
                    sensors = w.Sensor()
                    if not temp_done:
                        temps = [float(x.Value) for x in sensors
                                 if x.SensorType == "Temperature" and "CPU Package" in x.Name]
                        if temps:
                            hw.temp   = max(temps)
                            temp_done = True
                    if not power_done:
                        powers = [float(x.Value) for x in sensors
                                  if x.SensorType == "Power" and "CPU Package" in x.Name]
                        if powers:
                            hw.power   = sum(powers)
                            power_done = True
                except Exception:
                    pass

            # 3 – ACPI thermal zones via PowerShell (temp only; no power via ACPI)
            if not temp_done:
                try:
                    r = subprocess.run(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "(Get-WmiObject MSAcpi_ThermalZoneTemperature "
                         "-Namespace root/wmi).CurrentTemperature"],
                        capture_output=True, text=True, timeout=3,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        raw = [float(v) for v in r.stdout.split() if v.strip()]
                        if raw:
                            hw.temp = (max(raw) / 10.0) - 273.15
                except Exception:
                    pass

        # Atomic reference swap – nm-collector reads self._cpu_hw once per
        # tick (one LOAD_ATTR = GIL-safe) and sees the complete _CpuHwData.
        self._cpu_hw = hw

    # ── GPU loop ──────────────────────────────────────────────────────────────

    def _gpu_loop(self) -> None:
        _last_hw = 0.0
        while self._running:
            t0 = time.monotonic()

            # CPU hardware sensors at their own slower cadence.
            # LHM spawns a full .NET process; WMI also has high overhead.
            # Running these every 0.5 s would burn CPU continuously.
            if t0 - _last_hw >= _HW_INTERVAL:
                self._fetch_cpu_hw()
                _last_hw = time.monotonic()

            g = _GpuData()
            try:
                r = subprocess.run(
                    [
                        "nvidia-smi",
                        "--query-gpu=name,temperature.gpu,utilization.gpu,"
                        "memory.used,memory.total,power.draw,power.limit,"
                        "fan.speed,clocks.gr,clocks.mem",
                        "--format=csv,noheader,nounits",
                    ],
                    capture_output=True, text=True, timeout=4,
                )
                if r.returncode == 0:
                    p = [x.strip() for x in r.stdout.strip().split(",")]
                    g.ok         = True
                    g.name       = p[0]
                    g.temp       = _fv(p[1])
                    g.util       = _fv(p[2]) or 0.0
                    mu           = _fv(p[3]) or 0.0
                    mt           = _fv(p[4]) or 1.0
                    g.vram_used  = mu / 1024
                    g.vram_total = mt / 1024
                    g.power      = _fv(p[5])
                    g.plim       = _fv(p[6])
                    g.fan        = _fv(p[7])
                    g.clk        = _fv(p[8])
                    g.mclk       = _fv(p[9])
            except Exception:
                pass

            # Atomic reference swap – nm-collector reads self._gpu once per
            # tick (one LOAD_ATTR = GIL-safe) and sees the complete _GpuData.
            self._gpu = g
            sleep_for = max(0.0, self.interval - (time.monotonic() - t0))
            time.sleep(sleep_for)
