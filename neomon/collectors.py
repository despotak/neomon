"""Background data collectors for NeoMon."""
from __future__ import annotations

import platform
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, NamedTuple, Optional

import psutil

HIST = 90  # ~3 min of history at 2 s intervals


class ProcInfo(NamedTuple):
    pid: int
    name: str
    cpu: float
    mem_pct: float
    mem_mb: float
    status: str
    user: str


@dataclass
class Snap:
    """All system metrics – written by background Collector, read by UI."""

    # ── CPU ───────────────────────────────────────────────────────────────────
    cpu_total: float = 0.0
    cpu_per: List[float] = field(default_factory=list)
    cpu_freq: float = 0.0
    cpu_freq_max: float = 0.0
    cpu_logical: int = 1
    cpu_physical: int = 1
    cpu_name: str = "CPU"
    cpu_hist: deque = field(default_factory=lambda: deque([0.0] * HIST, maxlen=HIST))

    # ── Memory ────────────────────────────────────────────────────────────────
    ram_used: float = 0.0    # GB
    ram_total: float = 0.0   # GB
    ram_pct: float = 0.0
    ram_available: float = 0.0  # GB
    ram_hist: deque = field(default_factory=lambda: deque([0.0] * HIST, maxlen=HIST))
    swap_used: float = 0.0
    swap_total: float = 0.0
    swap_pct: float = 0.0

    # ── GPU (nvidia-smi) ──────────────────────────────────────────────────────
    gpu_ok: bool = False
    gpu_name: str = ""
    gpu_util: float = 0.0
    gpu_vram_used: float = 0.0   # GB
    gpu_vram_total: float = 0.0  # GB
    gpu_temp: Optional[float] = None
    gpu_power: Optional[float] = None
    gpu_plim: Optional[float] = None
    gpu_fan: Optional[float] = None
    gpu_clk: Optional[float] = None
    gpu_mclk: Optional[float] = None
    gpu_hist: deque = field(default_factory=lambda: deque([0.0] * HIST, maxlen=HIST))

    # ── Disk ──────────────────────────────────────────────────────────────────
    disk_parts: List[dict] = field(default_factory=list)
    disk_r_bps: float = 0.0
    disk_w_bps: float = 0.0

    # ── Network ───────────────────────────────────────────────────────────────
    net_ifaces: List[dict] = field(default_factory=list)
    net_send: float = 0.0
    net_recv: float = 0.0
    net_send_hist: deque = field(default_factory=lambda: deque([0.0] * HIST, maxlen=HIST))
    net_recv_hist: deque = field(default_factory=lambda: deque([0.0] * HIST, maxlen=HIST))

    # ── Processes ─────────────────────────────────────────────────────────────
    procs: List[ProcInfo] = field(default_factory=list)
    proc_total: int = 0

    # ── Battery ───────────────────────────────────────────────────────────────
    batt_pct: Optional[float] = None
    batt_plugged: Optional[bool] = None
    batt_secs: Optional[int] = None

    # ── System (mostly static) ────────────────────────────────────────────────
    hostname: str = ""
    os_name: str = ""
    boot_time: float = 0.0
    uptime: int = 0


class Collector:
    """Manages background threads that populate a Snap object."""

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.snap = Snap()
        self._running = False
        self._prev_disk = None
        self._prev_net: dict = {}
        self._prev_t = time.monotonic()
        self._init_static()

    # ── one-time setup ────────────────────────────────────────────────────────

    def _init_static(self) -> None:
        s = self.snap

        # CPU name ────────────────────────────────────────────────────────────
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as k:
                s.cpu_name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            s.cpu_name = platform.processor() or "CPU"

        # OS name ─────────────────────────────────────────────────────────────
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as k:
                prod = winreg.QueryValueEx(k, "ProductName")[0]
                build = winreg.QueryValueEx(k, "CurrentBuildNumber")[0]
                s.os_name = f"{prod} (build {build})"
        except Exception:
            s.os_name = f"{platform.system()} {platform.release()}"

        s.hostname = socket.gethostname()
        s.boot_time = psutil.boot_time()
        s.cpu_logical = psutil.cpu_count(logical=True) or 1
        s.cpu_physical = psutil.cpu_count(logical=False) or 1
        freq = psutil.cpu_freq()
        if freq:
            s.cpu_freq_max = freq.max or freq.current

        # Prime CPU counters (first call always returns 0.0)
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._prev_disk = psutil.disk_io_counters()
        self._prev_net = psutil.net_io_counters(pernic=True)
        threading.Thread(target=self._main_loop, daemon=True, name="nm-collector").start()
        threading.Thread(target=self._gpu_loop,  daemon=True, name="nm-gpu").start()

    def stop(self) -> None:
        self._running = False

    # ── main collection loop ──────────────────────────────────────────────────

    def _main_loop(self) -> None:
        time.sleep(0.5)          # allow CPU counters to prime
        while self._running:
            t0 = time.monotonic()
            elapsed = max(t0 - self._prev_t, 0.001)
            self._prev_t = t0
            self._collect(elapsed)
            sleep_for = max(0.0, self.interval - (time.monotonic() - t0))
            time.sleep(sleep_for)

    def _collect(self, elapsed: float) -> None:
        s = self.snap

        # CPU ─────────────────────────────────────────────────────────────────
        s.cpu_total = psutil.cpu_percent(interval=None)
        s.cpu_per   = psutil.cpu_percent(interval=None, percpu=True)
        s.cpu_hist.append(s.cpu_total)
        freq = psutil.cpu_freq()
        if freq:
            s.cpu_freq = freq.current

        # Memory ──────────────────────────────────────────────────────────────
        ram  = psutil.virtual_memory()
        swap = psutil.swap_memory()
        s.ram_used      = ram.used      / 1024 ** 3
        s.ram_total     = ram.total     / 1024 ** 3
        s.ram_available = ram.available / 1024 ** 3
        s.ram_pct = ram.percent
        s.ram_hist.append(ram.percent)
        s.swap_used  = swap.used  / 1024 ** 3
        s.swap_total = swap.total / 1024 ** 3
        s.swap_pct   = swap.percent

        # Disk I/O ────────────────────────────────────────────────────────────
        curr_disk = psutil.disk_io_counters()
        if curr_disk and self._prev_disk:
            s.disk_r_bps = (curr_disk.read_bytes  - self._prev_disk.read_bytes)  / elapsed
            s.disk_w_bps = (curr_disk.write_bytes - self._prev_disk.write_bytes) / elapsed
        self._prev_disk = curr_disk

        parts = []
        for p in psutil.disk_partitions():
            try:
                u   = psutil.disk_usage(p.mountpoint)
                lbl = p.device.replace("\\\\", "").rstrip("\\")
                parts.append({
                    "device":   lbl,
                    "fstype":   p.fstype,
                    "used_gb":  u.used  / 1024 ** 3,
                    "total_gb": u.total / 1024 ** 3,
                    "pct":      u.percent,
                })
            except (PermissionError, OSError):
                pass
        s.disk_parts = parts

        # Network ─────────────────────────────────────────────────────────────
        curr_net  = psutil.net_io_counters(pernic=True)
        prev_net  = self._prev_net
        self._prev_net = curr_net
        ifaces, total_s, total_r = [], 0.0, 0.0
        for name, st in curr_net.items():
            p = prev_net.get(name)
            if not p:
                continue
            snd = (st.bytes_sent - p.bytes_sent) / elapsed
            rcv = (st.bytes_recv - p.bytes_recv) / elapsed
            total_s += snd
            total_r += rcv
            if (snd > 0 or rcv > 0) and "loopback" not in name.lower():
                ifaces.append({"name": name[:24], "send": snd, "recv": rcv})
        s.net_ifaces = ifaces
        s.net_send   = total_s
        s.net_recv   = total_r
        s.net_send_hist.append(total_s)
        s.net_recv_hist.append(total_r)

        # Processes ───────────────────────────────────────────────────────────
        attrs = ["pid", "name", "cpu_percent", "memory_percent",
                 "memory_info", "status", "username"]
        procs = []
        for proc in psutil.process_iter(attrs):
            try:
                info = proc.info
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
        s.procs      = procs
        s.proc_total = len(procs)

        # Battery ─────────────────────────────────────────────────────────────
        try:
            batt = psutil.sensors_battery()
            if batt:
                s.batt_pct    = batt.percent
                s.batt_plugged = batt.power_plugged
                s.batt_secs   = batt.secsleft if batt.secsleft and batt.secsleft > 0 else None
        except Exception:
            pass

        # Uptime ──────────────────────────────────────────────────────────────
        s.uptime = int(
            (datetime.now() - datetime.fromtimestamp(s.boot_time)).total_seconds()
        )

    # ── GPU loop (separate, slower) ───────────────────────────────────────────

    def _gpu_loop(self) -> None:
        while self._running:
            s = self.snap
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

                    def fv(x: str) -> Optional[float]:
                        try:
                            return float(x)
                        except Exception:
                            return None

                    s.gpu_ok         = True
                    s.gpu_name       = p[0]
                    s.gpu_temp       = fv(p[1])
                    s.gpu_util       = fv(p[2]) or 0.0
                    mu               = fv(p[3]) or 0.0
                    mt               = fv(p[4]) or 1.0
                    s.gpu_vram_used  = mu / 1024
                    s.gpu_vram_total = mt / 1024
                    s.gpu_power      = fv(p[5])
                    s.gpu_plim       = fv(p[6])
                    s.gpu_fan        = fv(p[7])
                    s.gpu_clk        = fv(p[8])
                    s.gpu_mclk       = fv(p[9])
                    s.gpu_hist.append(s.gpu_util)
                else:
                    s.gpu_ok = False
            except Exception:
                s.gpu_ok = False
            time.sleep(self.interval)
