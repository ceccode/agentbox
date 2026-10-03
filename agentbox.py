#!/usr/bin/env python3
"""
agentbox - a one-file, zero-dependency status probe for a Linux box used as an
AI agent environment (opencode / llama.cpp / ollama).

Design rule: --json is the real interface. The human text output is just a
pretty-printer over the same dict, so an LLM agent can consume the JSON and a
human can read the text.

Usage:
    ./agentbox.py                 # full snapshot, human readable
    ./agentbox.py status          # same
    ./agentbox.py cpu | mem | gpu | disk | services | procs | opencode
    ./agentbox.py --json          # machine readable, for agents / logging
    ./agentbox.py --watch 5       # refresh every 5s
    ./agentbox.py opencode --days 7

License: MIT
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Units that matter on an AI box. Edit for your machine.
WATCHED_UNITS = [
    "ssh", "sshd", "tailscaled", "wg-quick@wg0", "docker",
    "ollama", "llama-server", "opencode", "nvidia-persistenced",
]

SAMPLE_INTERVAL = 0.4  # seconds, for CPU delta sampling
JSON_SCHEMA_VERSION = 1

AVAILABILITY_AVAILABLE = "available"
AVAILABILITY_UNSUPPORTED = "unsupported"
AVAILABILITY_UNAVAILABLE = "unavailable"
AVAILABILITY_PARTIAL = "partial"
AVAILABILITY_UNVERIFIED = "unverified"

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def human_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def human_count(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def human_delta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"


def bar(pct: float, width: int = 20) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "[" + "#" * filled + "." * (width - filled) + "]"


def run(cmd: list[str], timeout: int = 5) -> str:
    """Run a command, return stdout or '' on any failure. Never raises."""
    return run_result(cmd, timeout)[0]


def run_result(cmd: list[str], timeout: int = 5) -> tuple[str, str | None]:
    """Run a command and preserve why its output may be unavailable."""
    exe = shutil.which(cmd[0])
    if not exe:
        for prefix in ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin"):
            candidate = os.path.join(prefix, cmd[0])
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                exe = candidate
                break
    if not exe:
        return "", f"{cmd[0]} not found"
    try:
        out = subprocess.run(
            [exe, *cmd[1:]], capture_output=True, text=True, errors="replace",
            timeout=timeout, check=False
        )
        if out.returncode:
            reason = out.stderr.strip() or f"{cmd[0]} exited {out.returncode}"
            return out.stdout, reason
        return out.stdout, None
    except subprocess.TimeoutExpired:
        return "", f"{cmd[0]} timed out after {timeout}s"
    except OSError as exc:
        return "", f"{cmd[0]} failed: {exc}"


def read(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _platform_id() -> str:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def platform_info() -> dict:
    system = _platform_id()
    return {
        "system": system,
        "sys_platform": sys.platform,
        "machine": os.uname().machine,
        "release": os.uname().release,
        "capabilities": {
            "procfs": system == "linux" and os.path.isdir("/proc"),
            "pressure": system == "linux" and os.path.isdir("/proc/pressure"),
            "systemd": system == "linux" and shutil.which("systemctl") is not None,
            "macos_vm_stat": system == "macos" and shutil.which("vm_stat") is not None,
            "macos_lsof": system == "macos" and shutil.which("lsof") is not None,
        },
    }


def unavailable(reason: str, availability: str = AVAILABILITY_UNAVAILABLE,
                source: str | None = None) -> dict:
    out = {"available": False, "availability": availability, "reason": reason}
    if source:
        out["source"] = source
    return out


def is_unsupported(section: dict | None) -> bool:
    return bool(section and section.get("availability") == AVAILABILITY_UNSUPPORTED)


def clean_text(value: object) -> str:
    text = str(value)
    text = ANSI_CSI.sub("", text)
    return CONTROL_CHARS.sub("", text)


# --------------------------------------------------------------------------- #
# collectors
# --------------------------------------------------------------------------- #

def _parse_pressure(text: str) -> dict | None:
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        values = {}
        for token in parts[1:]:
            key, sep, value = token.partition("=")
            if not sep:
                continue
            try:
                values["total_us" if key == "total" else key] = (
                    int(value) if key == "total" else float(value))
            except ValueError:
                return None
        if not {"avg10", "avg60", "avg300", "total_us"} <= values.keys():
            return None
        result[parts[0]] = values
    return result if "some" in result else None


def collect_pressure(resources=("cpu", "memory", "io")) -> dict:
    if _platform_id() != "linux":
        out = unavailable("pressure stall information is Linux-only",
                          AVAILABILITY_UNSUPPORTED, "procfs")
        out.update({"cpu": None, "memory": None, "io": None})
        return out
    out = {"available": True, "reason": None}
    errors = []
    for resource in resources:
        path = f"/proc/pressure/{resource}"
        try:
            with open(path, errors="replace") as fh:
                parsed = _parse_pressure(fh.read())
        except OSError as exc:
            parsed = None
            errors.append(f"{resource}: {exc.strerror or exc}")
        if parsed is None:
            if not any(error.startswith(f"{resource}:") for error in errors):
                errors.append(f"{resource}: invalid pressure data")
        out[resource] = parsed
    out["available"] = not errors
    out["reason"] = "; ".join(errors) or None
    return out


def _cpu_totals() -> tuple[int, int]:
    """Return (idle_jiffies, total_jiffies) from /proc/stat."""
    line = read("/proc/stat").split("\n", 1)[0]
    parts = [int(x) for x in line.split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    return idle, sum(parts)


def _proc_times() -> dict[int, tuple[int, str]]:
    """pid -> (utime+stime jiffies, comm)."""
    out: dict[int, tuple[int, str]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        stat = read(f"/proc/{entry}/stat")
        if not stat:
            continue
        # comm can contain spaces and parens; split on the LAST ')'
        try:
            close = stat.rindex(")")
            comm = stat[stat.index("(") + 1:close]
            fields = stat[close + 2:].split()
            out[int(entry)] = (int(fields[11]) + int(fields[12]), comm)
        except (ValueError, IndexError):
            continue
    return out


def collect_linux_cpu_and_procs(top_n: int = 6) -> dict:
    """Sample CPU + per-process usage over SAMPLE_INTERVAL for real numbers."""
    ncpu = os.cpu_count() or 1
    hz = os.sysconf("SC_CLK_TCK")

    idle0, total0 = _cpu_totals()
    procs0 = _proc_times()
    time.sleep(SAMPLE_INTERVAL)
    idle1, total1 = _cpu_totals()
    procs1 = _proc_times()

    d_total = max(1, total1 - total0)
    cpu_pct = 100.0 * (1 - (idle1 - idle0) / d_total)

    # per-process share of one whole machine (0-100)
    deltas = []
    for pid, (jiff1, comm) in procs1.items():
        jiff0 = procs0.get(pid, (jiff1, comm))[0]
        used = (jiff1 - jiff0) / hz  # cpu-seconds
        pct = 100.0 * used / (SAMPLE_INTERVAL * ncpu)
        if pct > 0.5:
            rss = 0
            m = re.search(r"VmRSS:\s+(\d+) kB", read(f"/proc/{pid}/status"))
            if m:
                rss = int(m.group(1)) * 1024
            cmdline = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
            deltas.append({
                "pid": pid, "name": comm, "cpu_pct": round(pct, 1),
                "rss_bytes": rss, "cmdline": cmdline[:120] or comm,
            })
    deltas.sort(key=lambda p: p["cpu_pct"], reverse=True)

    load1, load5, load15 = os.getloadavg()
    uptime = float((read("/proc/uptime").split() or ["0"])[0])

    return {
        "cores": ncpu,
        "usage_pct": round(cpu_pct, 1),
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "load_per_core": round(load1 / ncpu, 2),
        "uptime_seconds": int(uptime),
        "temp_c": _cpu_temp(),
        "top": deltas[:top_n],
    }


def _sysctl_int(name: str) -> int | None:
    out, error = run_result(["sysctl", "-n", name])
    if error:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _macos_cpu_usage(interval: float = SAMPLE_INTERVAL) -> tuple[float | None, str | None, float]:
    start = time.monotonic()
    out, error = run_result(["top", "-l", "2", "-s", str(interval), "-n", "0"], timeout=5)
    elapsed = max(0.001, time.monotonic() - start)
    if error and not out:
        ps_out, ps_error = run_result(["ps", "-A", "-o", "%cpu="])
        if ps_error:
            return None, error, elapsed
        total = 0.0
        for line in ps_out.splitlines():
            try:
                total += float(line.strip())
            except ValueError:
                continue
        cores = os.cpu_count() or 1
        return round(max(0.0, min(100.0, total / cores)), 1), (
            f"{error}; fell back to ps %cpu"), elapsed
    matches = re.findall(r"CPU usage:\s+.*?([0-9.]+)% idle", out)
    if not matches:
        return None, "top CPU output schema unsupported", elapsed
    idle = float(matches[-1])
    return round(max(0.0, min(100.0, 100.0 - idle)), 1), None, elapsed


def _macos_top_processes(top_n: int = 6) -> list[dict]:
    out, error = run_result(["ps", "-axo", "pid=,pcpu=,rss=,comm="])
    if error:
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            cpu_pct = float(parts[1])
            rss = int(parts[2]) * 1024
        except ValueError:
            continue
        if cpu_pct <= 0.5:
            continue
        procs.append({
            "pid": pid,
            "name": os.path.basename(parts[3]),
            "cpu_pct": round(cpu_pct, 1),
            "rss_bytes": rss,
            "cmdline": parts[3][:120],
        })
    procs.sort(key=lambda p: p["cpu_pct"], reverse=True)
    return procs[:top_n]


def collect_macos_cpu_and_procs(top_n: int = 6) -> dict:
    ncpu = _sysctl_int("hw.ncpu") or os.cpu_count() or 1
    usage_pct, reason, elapsed = _macos_cpu_usage()
    load1, load5, load15 = os.getloadavg()
    boot = _sysctl_int("kern.boottime")
    uptime = None
    if boot is None:
        out, _ = run_result(["sysctl", "-n", "kern.boottime"])
        m = re.search(r"sec\s*=\s*(\d+)", out)
        boot = int(m.group(1)) if m else None
    if boot:
        uptime = max(0, int(time.time() - boot))
    result = {
        "available": usage_pct is not None,
        "availability": (AVAILABILITY_AVAILABLE if usage_pct is not None and not reason
                         else AVAILABILITY_UNVERIFIED if usage_pct is not None
                         else AVAILABILITY_UNAVAILABLE),
        "reason": reason,
        "source": "top+ps+sysctl",
        "sample_seconds": round(elapsed, 3),
        "cores": ncpu,
        "usage_pct": usage_pct if usage_pct is not None else 0.0,
        "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        "load_per_core": round(load1 / ncpu, 2),
        "uptime_seconds": uptime or 0,
        "temp_c": None,
        "top": _macos_top_processes(top_n),
    }
    return result


def collect_cpu_and_procs(top_n: int = 6) -> dict:
    if _platform_id() == "macos":
        return collect_macos_cpu_and_procs(top_n)
    if _platform_id() != "linux" or not os.path.isdir("/proc"):
        out = unavailable("CPU collector unsupported on this platform",
                          AVAILABILITY_UNSUPPORTED)
        out.update({"cores": os.cpu_count() or 1, "usage_pct": 0.0,
                    "load": [0, 0, 0], "load_per_core": 0,
                    "uptime_seconds": 0, "temp_c": None, "top": []})
        return out
    try:
        result = collect_linux_cpu_and_procs(top_n)
        result.setdefault("available", True)
        result.setdefault("availability", AVAILABILITY_AVAILABLE)
        result.setdefault("reason", None)
        result.setdefault("source", "procfs")
        result.setdefault("sample_seconds", SAMPLE_INTERVAL)
        return result
    except (OSError, ValueError, IndexError) as exc:
        out = unavailable(f"CPU data unavailable: {exc}", source="procfs")
        out.update({"cores": os.cpu_count() or 1, "usage_pct": 0.0,
                    "load": [0, 0, 0], "load_per_core": 0,
                    "uptime_seconds": 0, "temp_c": None, "top": []})
        return out


def _cpu_temp() -> float | None:
    best = None
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        kind = read(f"{zone}/type").strip()
        if kind in ("x86_pkg_temp", "acpitz", "cpu-thermal", "k10temp"):
            raw = read(f"{zone}/temp").strip()
            if raw.isdigit():
                best = max(best or 0, int(raw) / 1000)
    return round(best, 1) if best else None


def collect_linux_mem() -> dict:
    info = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        num = v.strip().split(" ")[0]
        if num.isdigit():
            info[k] = int(num) * 1024

    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    used = total - avail
    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": avail,
        "used_pct": round(100 * used / total, 1) if total else 0.0,
        "cached_bytes": info.get("Cached", 0),
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_used_pct": round(100 * swap_used / swap_total, 1) if swap_total else 0.0,
    }


def _parse_vm_stat(text: str) -> tuple[int | None, dict[str, int]]:
    first = text.splitlines()[0] if text.splitlines() else ""
    m = re.search(r"page size of (\d+) bytes", first)
    page_size = int(m.group(1)) if m else None
    pages = {}
    for line in text.splitlines()[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        raw = value.strip().rstrip(".")
        try:
            pages[key.strip()] = int(raw)
        except ValueError:
            continue
    return page_size, pages


def collect_macos_mem() -> dict:
    total = _sysctl_int("hw.memsize") or 0
    text, error = run_result(["vm_stat"])
    page_size, pages = _parse_vm_stat(text)
    if not total and page_size:
        try:
            total = os.sysconf("SC_PHYS_PAGES") * page_size
        except (ValueError, OSError, AttributeError):
            total = 0
    if error or not page_size or not pages:
        out = unavailable(error or "vm_stat output schema unsupported", source="vm_stat")
        out.update({
            "total_bytes": total, "used_bytes": 0, "available_bytes": 0,
            "used_pct": 0.0, "cached_bytes": 0,
            "swap_total_bytes": 0, "swap_used_bytes": 0, "swap_used_pct": 0.0,
            "basis": "macos_vm_stat",
        })
        return out
    free_pages = pages.get("Pages free", 0)
    inactive_pages = pages.get("Pages inactive", 0)
    speculative_pages = pages.get("Pages speculative", 0)
    wired_pages = pages.get("Pages wired down", pages.get("Pages wired", 0))
    compressed_pages = pages.get("Pages occupied by compressor", 0)
    available = (free_pages + inactive_pages + speculative_pages) * page_size
    used = max(0, total - available) if total else 0
    cached = inactive_pages * page_size
    swap_total = swap_used = 0
    swap, _ = run_result(["sysctl", "-n", "vm.swapusage"])
    numbers = re.findall(r"([0-9.]+)M", swap)
    if len(numbers) >= 2:
        try:
            swap_total = int(float(numbers[0]) * 1024 ** 2)
            swap_used = int(float(numbers[1]) * 1024 ** 2)
        except ValueError:
            pass
    return {
        "available": True,
        "availability": AVAILABILITY_AVAILABLE,
        "reason": None,
        "source": "vm_stat+sysctl",
        "basis": "free+inactive+speculative pages; not Linux MemAvailable",
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_pct": round(100 * used / total, 1) if total else 0.0,
        "cached_bytes": cached,
        "wired_bytes": wired_pages * page_size,
        "compressed_bytes": compressed_pages * page_size,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_used,
        "swap_used_pct": round(100 * swap_used / swap_total, 1) if swap_total else 0.0,
    }


def collect_mem() -> dict:
    if _platform_id() == "macos":
        return collect_macos_mem()
    if _platform_id() != "linux" or not os.path.exists("/proc/meminfo"):
        out = unavailable("memory collector unsupported on this platform",
                          AVAILABILITY_UNSUPPORTED)
        out.update({"total_bytes": 0, "used_bytes": 0, "available_bytes": 0,
                    "used_pct": 0.0, "cached_bytes": 0, "swap_total_bytes": 0,
                    "swap_used_bytes": 0, "swap_used_pct": 0.0})
        return out
    result = collect_linux_mem()
    if result["total_bytes"] <= 0:
        result.update(unavailable("/proc/meminfo missing or unreadable", source="procfs"))
    else:
        result.setdefault("available", True)
        result.setdefault("availability", AVAILABILITY_AVAILABLE)
        result.setdefault("reason", None)
        result.setdefault("source", "procfs")
    return result


def collect_gpu() -> dict:
    if _platform_id() == "macos":
        out = unavailable("Apple GPU telemetry is not implemented yet",
                          AVAILABILITY_UNSUPPORTED, "macos")
        out.update({"vendor": None, "gpus": [], "processes": []})
        return out
    gpus: list[dict] = []
    vendor = None

    # --- NVIDIA ---
    q = ("name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
         "power.draw,power.limit")
    out, nvidia_error = run_result(
        ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"])
    if out.strip():
        vendor = "nvidia"
        for idx, line in enumerate(out.strip().splitlines()):
            f = [x.strip() for x in line.split(",")]

            def num(i):
                try:
                    return float(f[i])
                except (ValueError, IndexError):
                    return None

            gpus.append({
                "index": idx, "name": f[0], "util_pct": num(1),
                "mem_used_bytes": (num(2) or 0) * 1024 ** 2,
                "mem_total_bytes": (num(3) or 0) * 1024 ** 2,
                "temp_c": num(4), "power_w": num(5), "power_limit_w": num(6),
            })

        procs = run(["nvidia-smi",
                     "--query-compute-apps=pid,process_name,used_memory",
                     "--format=csv,noheader,nounits"])
        apps = []
        for line in procs.strip().splitlines():
            f = [x.strip() for x in line.split(",")]
            if len(f) >= 3:
                apps.append({
                    "pid": int(f[0]) if f[0].isdigit() else None,
                    "name": os.path.basename(f[1]),
                    "mem_bytes": (float(f[2]) if f[2].replace(".", "").isdigit() else 0) * 1024 ** 2,
                })
        return {"available": True, "reason": None, "vendor": vendor,
                "gpus": gpus, "processes": apps}

    # --- AMD (amdgpu sysfs) ---
    for card in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
        vendor = "amd"
        dev = os.path.dirname(card)
        busy = read(card).strip()
        used = read(f"{dev}/mem_info_vram_used").strip()
        total = read(f"{dev}/mem_info_vram_total").strip()
        temp = None
        for hw in glob.glob(f"{dev}/hwmon/hwmon*/temp1_input"):
            t = read(hw).strip()
            if t.isdigit():
                temp = int(t) / 1000
        gpus.append({
            "index": len(gpus), "name": "amdgpu",
            "util_pct": float(busy) if busy.isdigit() else None,
            "mem_used_bytes": int(used) if used.isdigit() else 0,
            "mem_total_bytes": int(total) if total.isdigit() else 0,
            "temp_c": temp, "power_w": None, "power_limit_w": None,
        })

    available = bool(gpus) or nvidia_error == "nvidia-smi not found"
    return {"available": available, "reason": None if available else nvidia_error,
            "vendor": vendor, "gpus": gpus, "processes": []}


def _unescape_mountinfo(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def _parse_mountinfo(text: str) -> list[dict]:
    mounts = []
    for line in text.splitlines():
        left, sep, right = line.partition(" - ")
        before, after = left.split(), right.split()
        if not sep or len(before) < 6 or len(after) < 3:
            continue
        mounts.append({
            "device": before[2],
            "root": _unescape_mountinfo(before[3]),
            "mount_point": _unescape_mountinfo(before[4]),
            "mount_options": before[5].split(","),
            "filesystem": after[0],
            "mount_source": _unescape_mountinfo(after[1]),
            "super_options": after[2].split(","),
        })
    return mounts


def _mount_for_path(path: str, mounts: list[dict]) -> dict | None:
    path = os.path.realpath(path) if os.path.exists(path) else os.path.abspath(path)
    matches = [m for m in mounts
               if path == m["mount_point"] or
               path.startswith(m["mount_point"].rstrip("/") + "/")]
    return max(matches, key=lambda m: len(m["mount_point"])) if matches else None


def _diskstats() -> dict[str, tuple[int, int, int]]:
    stats = {}
    for line in read("/proc/diskstats").splitlines():
        fields = line.split()
        if len(fields) < 14:
            continue
        try:
            stats[f"{fields[0]}:{fields[1]}"] = (
                int(fields[5]), int(fields[9]), int(fields[12]))
        except ValueError:
            continue
    return stats


def collect_linux_disk(paths=("/", "/home"), io_interval: float = 0.1,
                       deep: bool = False) -> list[dict]:
    mounts = _parse_mountinfo(read("/proc/self/mountinfo"))
    io0 = _diskstats() if io_interval else {}
    if io_interval:
        time.sleep(io_interval)
    io1 = _diskstats() if io_interval else {}
    seen, out = set(), []
    for p in paths:
        if not os.path.isdir(p):
            continue
        try:
            st = os.statvfs(p)
            device = os.stat(p).st_dev
        except OSError:
            continue
        mount = _mount_for_path(p, mounts)
        key = (device, mount["mount_point"] if mount else None)
        if key in seen:
            continue
        seen.add(key)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        inode_total = st.f_files
        inode_used = max(0, inode_total - st.f_ffree)
        item = {
            "path": p, "total_bytes": total, "used_bytes": total - free,
            "free_bytes": free,
            "used_pct": round(100 * (total - free) / total, 1) if total else 0.0,
            "inode_total": inode_total,
            "inode_used": inode_used,
            "inode_free": st.f_ffree,
            "inode_available": st.f_favail,
            "inode_used_pct": (round(100 * inode_used / inode_total, 1)
                               if inode_total else None),
            "read_only": bool(st.f_flag & getattr(os, "ST_RDONLY", 1)),
            "operational": True,
            "filesystem": mount["filesystem"] if mount else None,
            "mount_source": mount["mount_source"] if mount else None,
            "mount_point": mount["mount_point"] if mount else None,
            "mount_options": mount["mount_options"] if mount else [],
            "device": mount["device"] if mount else None,
            "read_bytes_per_sec": None,
            "write_bytes_per_sec": None,
            "io_busy_pct": None,
        }
        if mount and mount["device"] in io0 and mount["device"] in io1:
            before, after = io0[mount["device"]], io1[mount["device"]]
            item["read_bytes_per_sec"] = max(0, after[0] - before[0]) * 512 / io_interval
            item["write_bytes_per_sec"] = max(0, after[1] - before[1]) * 512 / io_interval
            item["io_busy_pct"] = round(
                min(100.0, max(0, after[2] - before[2]) / (io_interval * 10)), 1)
        out.append(item)
    if deep:
        if out:
            out[0]["deep"] = collect_deep_storage()
    return out


def _parse_macos_mounts(text: str) -> list[dict]:
    mounts = []
    pattern = re.compile(r"^(.*?) on (.*?) \((.*?)\)$")
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        source, mount_point, opts = match.groups()
        options = [opt.strip() for opt in opts.split(",")]
        filesystem = options[0] if options else None
        mounts.append({
            "device": None,
            "root": "/",
            "mount_point": mount_point,
            "mount_options": options,
            "filesystem": filesystem,
            "mount_source": source,
            "super_options": options,
        })
    return mounts


def collect_macos_disk(paths: tuple[str, ...] | None = None,
                       deep: bool = False) -> list[dict]:
    if paths is None:
        paths = (os.path.expanduser("~"),)
    mounts = _parse_macos_mounts(run(["mount"]))
    seen, out = set(), []
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isdir(p):
            continue
        try:
            st = os.statvfs(p)
            device = os.stat(p).st_dev
        except OSError:
            continue
        mount = _mount_for_path(p, mounts)
        if _platform_id() == "macos" and p.startswith("/Users/"):
            data_mount = next((m for m in mounts
                               if m.get("mount_point") == "/System/Volumes/Data"), None)
            if data_mount:
                mount = data_mount
        key = (device, mount["mount_point"] if mount else p)
        if key in seen:
            continue
        seen.add(key)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        inode_total = st.f_files
        inode_used = max(0, inode_total - st.f_ffree)
        options = mount["mount_options"] if mount else []
        sealed_root = p == "/" and "sealed" in options and "read-only" in options
        item = {
            "available": True,
            "availability": AVAILABILITY_AVAILABLE,
            "reason": None,
            "source": "statvfs+mount",
            "path": p, "total_bytes": total, "used_bytes": total - free,
            "free_bytes": free,
            "used_pct": round(100 * (total - free) / total, 1) if total else 0.0,
            "inode_total": inode_total,
            "inode_used": inode_used,
            "inode_free": st.f_ffree,
            "inode_available": st.f_favail,
            "inode_used_pct": (round(100 * inode_used / inode_total, 1)
                               if inode_total else None),
            "read_only": bool(st.f_flag & getattr(os, "ST_RDONLY", 1)) or "read-only" in options,
            "operational": not sealed_root,
            "filesystem": mount["filesystem"] if mount else None,
            "mount_source": mount["mount_source"] if mount else None,
            "mount_point": mount["mount_point"] if mount else p,
            "mount_options": options,
            "device": mount["device"] if mount else None,
            "read_bytes_per_sec": None,
            "write_bytes_per_sec": None,
            "io_busy_pct": None,
        }
        out.append(item)
    if deep and out:
        out[0]["deep"] = collect_deep_storage()
    return out


def collect_disk(paths=None, io_interval: float = 0.1,
                 deep: bool = False) -> list[dict]:
    if _platform_id() == "macos":
        return collect_macos_disk(paths, deep=deep)
    if paths is None:
        paths = ("/", "/home")
    if _platform_id() != "linux" or not os.path.isdir("/proc"):
        return []
    return collect_linux_disk(paths=paths, io_interval=io_interval, deep=deep)


DEEP_STORAGE_ROOTS = (
    ("ollama", "~/.ollama"),
    ("claude", "~/.claude"),
    ("opencode", "~/.local/share/opencode"),
    ("huggingface", "~/.cache/huggingface"),
    ("llama", "~/.cache/llama.cpp"),
    ("docker", "/var/lib/docker"),
)
CONFIG_PATH = "~/.config/agentbox/config.json"


def load_config() -> tuple[dict, str | None]:
    path = os.path.expanduser(CONFIG_PATH)
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}, "cannot read config"
    if not isinstance(value, dict):
        return {}, "config must contain a JSON object"
    sanitized = dict(value)
    errors = []
    for key in ("disk_warning_pct", "inode_warning_pct"):
        if key in value:
            try:
                if isinstance(value[key], bool):
                    raise ValueError
                number = float(value[key])
                if not math.isfinite(number) or not 0 <= number <= 100:
                    errors.append(f"{key} must be between 0 and 100")
                    sanitized.pop(key, None)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{key} must be numeric")
                sanitized.pop(key, None)
    for key in ("capacity_min_ram_bytes", "capacity_min_disk_bytes"):
        if key in value:
            try:
                if isinstance(value[key], bool):
                    raise ValueError
                number = float(value[key])
                if not math.isfinite(number) or number <= 0 or not number.is_integer():
                    errors.append(f"{key} must be positive")
                    sanitized.pop(key, None)
            except (TypeError, ValueError, OverflowError):
                errors.append(f"{key} must be a positive integer")
                sanitized.pop(key, None)
    usage = value.get("usage", {})
    if usage is not None and not isinstance(usage, dict):
        errors.append("usage must be an object")
        sanitized.pop("usage", None)
    elif isinstance(usage, dict):
        sanitized_usage = dict(usage)
        for key, item in usage.items():
            if not key.endswith("_daily_tokens"):
                errors.append(f"unknown usage key: {key}")
                sanitized_usage.pop(key, None)
            else:
                try:
                    if isinstance(item, bool):
                        raise ValueError
                    number = float(item)
                    if not math.isfinite(number) or number <= 0 or not number.is_integer():
                        errors.append(f"{key} must be a positive integer")
                        sanitized_usage.pop(key, None)
                except (TypeError, ValueError, OverflowError):
                    errors.append(f"{key} must be a positive integer")
                    sanitized_usage.pop(key, None)
        sanitized["usage"] = sanitized_usage
    allowed_providers = {"opencode", "claude", "ollama"}
    if "expected_providers" in value:
        providers = value.get("expected_providers")
        if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
            errors.append("expected_providers must be a list of provider names")
            sanitized.pop("expected_providers", None)
        else:
            unknown = sorted(set(providers) - allowed_providers)
            if unknown:
                errors.append(f"unknown expected provider: {', '.join(unknown)}")
                sanitized.pop("expected_providers", None)
            else:
                sanitized["expected_providers"] = sorted(set(providers))
    if "expected_services" in value:
        services = value.get("expected_services")
        if not isinstance(services, list) or not all(isinstance(s, str) and s for s in services):
            errors.append("expected_services must be a list of service names")
            sanitized.pop("expected_services", None)
        else:
            sanitized["expected_services"] = sorted(set(services))
    return sanitized, "; ".join(errors) or None


def _daily_budget(config: dict, provider: str) -> int | None:
    usage = config.get("usage")
    if not isinstance(usage, dict):
        return None
    value = usage.get(f"{provider}_daily_tokens")
    try:
        value = int(value)
        return value if value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _config_number(config: dict, key: str, default: float,
                   minimum: float = 0) -> float:
    try:
        value = float(config.get(key, default))
        return value if math.isfinite(value) and value >= minimum else default
    except (TypeError, ValueError, OverflowError):
        return default


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode()).hexdigest()[:16]


def _parse_du_size(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def collect_deep_storage(timeout: int = 15) -> dict:
    roots = []
    largest = []
    errors = []
    deadline = time.monotonic() + timeout
    for label, configured in DEEP_STORAGE_ROOTS:
        if time.monotonic() >= deadline:
            errors.append("deep storage scan timed out")
            break
        root = os.path.abspath(os.path.expanduser(configured))
        if not os.path.isdir(root):
            continue
        roots.append({"name": label, "path": root})
        remaining = max(1, int(deadline - time.monotonic()))
        output, error = run_result(
            ["du", "-B1", "--max-depth=2", "--one-file-system", root],
            timeout=remaining)
        if error:
            errors.append(f"{label}: {error}")
            continue
        for line in output.splitlines():
            size, sep, path = line.partition("\t")
            if not sep:
                continue
            bytes_used = _parse_du_size(size)
            if bytes_used is None:
                continue
            largest.append({"name": label, "path": path, "bytes": bytes_used})
    largest.sort(key=lambda item: (-item["bytes"], item["path"]))
    return {
        "available": not errors,
        "reason": "; ".join(errors) or None,
        "roots": roots,
        "largest": largest[:30],
        "partial": bool(errors),
    }


def collect_linux_services() -> dict:
    """Running systemd units + explicit status for the ones we care about."""
    running = []
    out, list_error = run_result(
        ["systemctl", "list-units", "--type=service", "--state=running",
         "--no-legend", "--no-pager", "--plain"])
    for line in out.splitlines():
        parts = line.split(None, 4)
        if parts and parts[0].endswith(".service"):
            running.append({
                "unit": parts[0].removesuffix(".service"),
                "description": parts[4] if len(parts) > 4 else "",
            })

    watched = []
    for unit in WATCHED_UNITS:
        system_state = run(["systemctl", "is-active", unit]).strip() or "unknown"
        user_state = "unknown"
        if system_state != "active":
            user_state = run(["systemctl", "--user", "is-active", unit]).strip() or "unknown"
        if system_state == "active":
            state, scope = system_state, "system"
        elif user_state == "active":
            state, scope = user_state, "user"
        else:
            states = (system_state, user_state)
            state = next((s for s in ("failed", "activating", "deactivating", "inactive")
                          if s in states), "unknown")
            scope = ("system" if state == system_state and state != "unknown"
                     else "user" if state == user_state and state != "unknown" else None)
        watched.append({"unit": unit, "state": state, "scope": scope})

    failed = []
    fout, failed_error = run_result(
        ["systemctl", "list-units", "--type=service", "--state=failed", "--no-legend",
         "--no-pager", "--plain"])
    for line in fout.splitlines():
        parts = line.split(None, 1)
        if parts:
            failed.append(parts[0])

    errors = [e for e in (list_error, failed_error) if e]
    return {"available": not errors, "reason": "; ".join(errors) or None,
            "running_count": len(running), "running": running,
            "watched": watched, "failed": failed}


def collect_services() -> dict:
    if _platform_id() != "linux":
        return {
            "available": False,
            "availability": AVAILABILITY_UNSUPPORTED,
            "reason": "systemd service checks are Linux-only",
            "running_count": 0,
            "running": [],
            "watched": [{"unit": unit, "state": "not_applicable", "scope": None}
                        for unit in WATCHED_UNITS],
            "failed": [],
        }
    result = collect_linux_services()
    result.setdefault("availability", (AVAILABILITY_AVAILABLE if result.get("available")
                                       else AVAILABILITY_UNAVAILABLE))
    return result


LAN_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "fc00::/7", "fe80::/10",
))


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    if endpoint.startswith("[") and "]:" in endpoint:
        host, port = endpoint[1:].split("]: ", 1) if "]: " in endpoint else endpoint[1:].split("]:", 1)
        return host, port
    host, sep, port = endpoint.rpartition(":")
    return (host, port) if sep else (endpoint, "")


def _tailscale_addresses() -> set[str]:
    output = run(["tailscale", "ip"])
    return {line.strip() for line in output.splitlines() if line.strip()}


def _listener_scope(host: str, tailscale_addresses: set[str] | None = None) -> str:
    bare = host.split("%", 1)[0]
    if bare in ("*", "0.0.0.0", "::"):
        return "wildcard"
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return "unknown"
    if address.is_loopback:
        return "loopback"
    if bare in (tailscale_addresses or set()):
        return "tailscale"
    if any(address in network for network in LAN_NETWORKS):
        return "lan"
    return "external"


def collect_linux_listeners() -> tuple[list[dict], str | None]:
    """Listening TCP sockets with owning process - shows agent servers."""
    out, error = run_result(["ss", "-ltnpH"])
    tailscale_addresses = _tailscale_addresses()
    res = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        addr = cols[3]
        proc, process_name, pid = "", None, None
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            process_name, pid = m.group(1), int(m.group(2))
            proc = f"{process_name}({pid})"
        host, port = _split_endpoint(addr)
        res.append({
            "address": addr, "host": host, "port": port,
            "scope": _listener_scope(host, tailscale_addresses), "process": proc,
            "process_name": process_name, "pid": pid,
            "agent_kind": _agent_kind_from_names([process_name]),
        })
    return res, error


def collect_macos_listeners() -> tuple[list[dict], str | None]:
    out, error = run_result(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], timeout=5)
    if error:
        return [], error
    res = []
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 9:
            continue
        command = cols[0]
        try:
            pid = int(cols[1])
        except ValueError:
            pid = None
        endpoint = cols[-2] if cols[-1] == "(LISTEN)" else cols[-1]
        host, port = _split_endpoint(endpoint)
        res.append({
            "address": endpoint, "host": host, "port": port,
            "scope": _listener_scope(host, set()), "process": (
                f"{command}({pid})" if pid is not None else command),
            "process_name": command, "pid": pid,
            "agent_kind": _agent_kind_from_names([command]),
        })
    return res, None


def collect_listeners() -> tuple[list[dict], str | None]:
    if _platform_id() == "macos":
        return collect_macos_listeners()
    if _platform_id() != "linux":
        return [], "listener checks unsupported on this platform"
    return collect_linux_listeners()


# --------------------------------------------------------------------------- #
# opencode  (SQLite backend; schema verified against opencode 1.18.18)
# --------------------------------------------------------------------------- #

TESTED_OPENCODE_VERSION = "1.18.18"
REQUIRED_TABLES = {"session", "message", "part"}
REQUIRED_COLUMNS = {
    "session": {"id", "parent_id", "title", "directory", "agent", "model",
                "cost", "tokens_input", "tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write", "time_created",
                "time_updated", "time_archived", "version"},
    "message": {"id", "data"},
    "part": {"id", "message_id", "session_id", "time_created", "data"},
}
ZERO_TOKENS = {"input": 0, "output": 0, "reasoning": 0,
               "cache_read": 0, "cache_write": 0}


def _opencode_db_path() -> str | None:
    """Resolve the DB path without hardcoding it.

    AGENTBOX_OPENCODE_DB is AUTHORITATIVE: if set, we use it or fail. Falling
    back would silently monitor a different database than the one named.

    Otherwise try the default location before shelling out to
    `opencode db path` - that subprocess pays a Bun cold start (~1s) and this
    runs on every snapshot.
    """
    env = os.environ.get("AGENTBOX_OPENCODE_DB")
    if env:
        return env if os.path.exists(env) else None

    default = os.path.expanduser("~/.local/share/opencode/opencode.db")
    if os.path.exists(default):
        return default

    for line in run(["opencode", "db", "path"], timeout=15).strip().splitlines():
        line = line.strip()
        if line.endswith(".db") and os.path.exists(line):
            return line
    return None


def _jload(raw) -> dict:
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _model_label(*sources) -> str | None:
    """model is {'id':..,'providerID':..} on session, or flat keys on message."""
    for src in sources:
        if not isinstance(src, dict):
            continue
        # session.model is a bare {"id": .., "providerID": ..} object
        if src.get("id") and ("providerID" in src or "provider" in src):
            return f"{src.get('providerID') or src.get('provider')}/{src['id']}"
        m = src.get("model")
        if isinstance(m, dict):
            mid = m.get("id") or m.get("modelID")
            if mid:
                return f"{m.get('providerID') or m.get('provider') or '?'}/{mid}"
        mid = src.get("modelID") or src.get("model_id") or (m if isinstance(m, str) else None)
        if mid:
            prov = (src.get("providerID") or src.get("provider_id")
                    or src.get("provider") or "?")
            return f"{prov}/{mid}"
    return None


def _part_tokens(data: dict) -> dict | None:
    t = data.get("tokens")
    if not isinstance(t, dict):
        return None
    cache = t.get("cache") if isinstance(t.get("cache"), dict) else {}
    try:
        return {
            "input": int(t.get("input") or 0),
            "output": int(t.get("output") or 0),
            "reasoning": int(t.get("reasoning") or 0),
            "cache_read": int(cache.get("read") or 0),
            "cache_write": int(cache.get("write") or 0),
        }
    except (TypeError, ValueError):
        return None


def _add(dst: dict, src: dict) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _tty_of(pid: int) -> str | None:
    try:
        target = os.readlink(f"/proc/{pid}/fd/0")
        return target.replace("/dev/", "") if "/dev/pts/" in target else None
    except OSError:
        return None


def _agent_kind_from_names(names) -> str | None:
    for name in names:
        if not name:
            continue
        base = os.path.basename(name).removesuffix(".js").removesuffix(".mjs")
        if base in ("opencode", "opencode-desktop", "opencode-deskto"):
            return "opencode"
        if base == "claude":
            return "claude"
        if base == "ollama":
            return "ollama"
        if base.startswith("llama"):
            return "llama"
    return None


def _is_agent_proc(pid: int, cmd: str) -> str | None:
    """Match on the EXECUTABLE, not the whole cmdline.

    Matching the raw cmdline gives false positives on anything that merely
    mentions opencode in its arguments (grep, editors, this script).
    """
    comm = read(f"/proc/{pid}/comm").strip()
    argv = cmd.split(" ")
    names = [comm, os.path.basename(argv[0] if argv else "")]
    # node/bun launchers: the real program is argv[1]
    if names[1] in ("node", "bun", "deno") and len(argv) > 1:
        names.append(os.path.basename(argv[1]))
        script = argv[1].replace("\\", "/")
        if (script.endswith("/cli.js") and
                "/@anthropic-ai/claude-code/" in script):
            return "claude"
    return _agent_kind_from_names(names)


def _linux_agent_processes(scrub: bool = False) -> list[dict]:
    """Live AI agent and model runtime processes, read straight from /proc."""
    procs = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        cmd = read(f"/proc/{pid}/cmdline").replace("\0", " ").strip()
        if not cmd:
            continue
        kind = _is_agent_proc(pid, cmd)
        if not kind:
            continue
        rss = 0
        m = re.search(r"VmRSS:\s+(\d+) kB", read(f"/proc/{pid}/status"))
        if m:
            rss = int(m.group(1)) * 1024
        try:
            started = os.path.getmtime(f"/proc/{pid}")
        except OSError:
            started = None
        procs.append({
            "pid": pid,
            "kind": kind,
            "tty": _tty_of(pid),
            "rss_bytes": rss,
            "age_seconds": int(time.time() - started) if started else None,
            "cmdline": None if scrub else cmd[:160],
        })
    procs.sort(key=lambda p: p["age_seconds"] or 0)
    return procs


def _macos_agent_processes(scrub: bool = False) -> list[dict]:
    out, error = run_result(["ps", "-axo", "pid=,comm=,rss=,etimes=,args="])
    if error:
        return []
    procs = []
    for line in out.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            rss = int(parts[2]) * 1024
            age = int(parts[3])
        except ValueError:
            continue
        comm = parts[1]
        args = parts[4]
        names = [comm, os.path.basename(args.split(" ", 1)[0])]
        argv = args.split()
        if names[-1] in ("node", "bun", "deno") and len(argv) > 1:
            names.append(os.path.basename(argv[1]))
            script = argv[1].replace("\\", "/")
            if script.endswith("/cli.js") and "/@anthropic-ai/claude-code/" in script:
                kind = "claude"
            else:
                kind = _agent_kind_from_names(names)
        else:
            kind = _agent_kind_from_names(names)
        if not kind:
            continue
        procs.append({
            "pid": pid,
            "kind": kind,
            "tty": None,
            "rss_bytes": rss,
            "age_seconds": age,
            "cmdline": None if scrub else args[:160],
        })
    procs.sort(key=lambda p: p["age_seconds"] or 0)
    return procs


def _agent_processes(scrub: bool = False) -> list[dict]:
    if _platform_id() == "macos":
        return _macos_agent_processes(scrub)
    if _platform_id() != "linux" or not os.path.isdir("/proc"):
        return []
    return _linux_agent_processes(scrub)


def collect_agents(processes: list[dict]) -> dict:
    public = [{k: v for k, v in proc.items() if k != "cmdline"} for proc in processes]
    counts = {kind: sum(p["kind"] == kind for p in public)
              for kind in ("opencode", "claude", "ollama", "llama")}
    return {"processes": public, "counts": counts}


def _numeric(value) -> int:
    try:
        value = int(value)
        return value if value >= 0 else 0
    except (TypeError, ValueError):
        return 0


def _timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _claude_usage(record: dict) -> dict | None:
    message = record.get("message") if isinstance(record.get("message"), dict) else record
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    return {
        "input": _numeric(usage.get("input_tokens")),
        "output": _numeric(usage.get("output_tokens")),
        "cache_read": _numeric(usage.get("cache_read_input_tokens")),
        "cache_write": _numeric(usage.get("cache_creation_input_tokens")),
    }


def collect_claude(days: int = 7, scrub: bool = False) -> dict:
    root = os.path.expanduser("~/.claude/projects")
    out = {
        "available": False, "availability": AVAILABILITY_UNAVAILABLE,
        "reason": None, "source": "local_files",
        "version": None, "window_days": days, "projects": [],
        "totals": dict(ZERO_TOKENS, turns=0),
        "tokens_by_day": {},
        "parse": {"files_seen": 0, "files_failed": 0, "records_ignored": 0,
                  "records_recognized": 0, "records_duplicate": 0},
        "partial": False,
        "cost_usd": None,
    }
    if not os.path.isdir(root):
        out["reason"] = "Claude Code projects directory not found"
        return out

    version = run(["claude", "--version"]).strip().splitlines()
    out["version"] = version[0] if version else None
    cutoff = time.time() - days * 86400
    projects = {}
    seen_records = set()
    for project_dir, _, filenames in os.walk(root):
        for filename in filenames:
            if not filename.endswith(".jsonl"):
                continue
            path = os.path.join(project_dir, filename)
            out["parse"]["files_seen"] += 1
            project = projects.setdefault(project_dir, {
                "project_hash": f"sha256:{_path_hash(project_dir)}",
                "sessions": 0, "last_activity": None,
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
            })
            valid_session = False
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        if len(line) > 2_000_000:
                            out["parse"]["records_ignored"] += 1
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            out["parse"]["records_ignored"] += 1
                            continue
                        if not isinstance(record, dict):
                            out["parse"]["records_ignored"] += 1
                            continue
                        timestamp = _timestamp(record.get("timestamp"))
                        usage = _claude_usage(record)
                        if usage is None:
                            continue
                        out["parse"]["records_recognized"] += 1
                        record_id = (record.get("uuid") or record.get("id") or
                                     record.get("requestId") or record.get("request_id"))
                        signature = (("id", record_id) if record_id else
                                     ("file", path, json.dumps({
                                         "timestamp": record.get("timestamp"),
                                         "usage": usage,
                                     }, sort_keys=True)))
                        if signature in seen_records:
                            out["parse"]["records_duplicate"] += 1
                            continue
                        seen_records.add(signature)
                        if timestamp is None or timestamp < cutoff:
                            continue
                        valid_session = True
                        for key, value in usage.items():
                            target = f"{key}_tokens" if key != "input" and key != "output" else f"{key}_tokens"
                            project[target] = project.get(target, 0) + value
                            total_key = key if key in ZERO_TOKENS else key
                            if total_key in out["totals"]:
                                out["totals"][total_key] += value
                        project["last_activity"] = max(
                            project["last_activity"] or 0, timestamp)
                        out["totals"]["turns"] += 1
                        day = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
                        out["tokens_by_day"][day] = (
                            out["tokens_by_day"].get(day, 0) + usage["input"] + usage["output"])
                    if valid_session:
                        project["sessions"] += 1
            except OSError:
                out["parse"]["files_failed"] += 1

    for project in projects.values():
        if project["sessions"]:
            if project["last_activity"]:
                project["last_activity"] = datetime.fromtimestamp(
                    project["last_activity"], timezone.utc).isoformat(timespec="seconds")
            out["projects"].append(project)
    out["projects"].sort(key=lambda item: item["project_hash"])
    recognized = out["parse"]["records_recognized"]
    out["available"] = bool(recognized or not out["parse"]["files_seen"])
    out["availability"] = (AVAILABILITY_AVAILABLE if out["available"]
                           else AVAILABILITY_UNAVAILABLE)
    if not out["available"]:
        out["reason"] = "Claude Code JSONL schema unsupported"
    elif out["parse"]["files_failed"]:
        out["partial"] = True
        out["availability"] = AVAILABILITY_PARTIAL
        out["reason"] = "some Claude Code files could not be read"
    elif out["parse"]["records_ignored"] or out["parse"]["records_duplicate"]:
        out["partial"] = True
        out["availability"] = AVAILABILITY_PARTIAL
        parts = []
        if out["parse"]["records_ignored"]:
            parts.append("some Claude Code records could not be parsed")
        if out["parse"]["records_duplicate"]:
            parts.append("duplicate Claude Code records were ignored")
        out["reason"] = "; ".join(parts)
    return out


def _ollama_size(value: str) -> int | None:
    match = re.fullmatch(r"([0-9.]+)\s*([KMGT]B|[KMGT]iB|B)", value.strip(), re.I)
    if not match:
        return None
    try:
        amount = float(match.group(1))
    except ValueError:
        return None
    units = {"B": 0, "KB": 1, "KIB": 1, "MB": 2, "MIB": 2,
             "GB": 3, "GIB": 3, "TB": 4, "TIB": 4}
    return int(amount * 1024 ** units[match.group(2).upper()])


def _parse_ollama_table(output: str) -> list[list[str]]:
    rows = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4:
            rows.append(parts)
    return rows


def collect_ollama(scrub: bool = False) -> dict:
    out = {"available": False, "availability": AVAILABILITY_UNAVAILABLE,
           "reason": None, "source": "ollama_cli",
           "version": None, "models": [], "running": [], "partial": False}
    version = run(["ollama", "--version"]).strip()
    out["version"] = version or None
    listing, list_error = run_result(["ollama", "list"])
    if list_error:
        out["reason"] = list_error
        return out
    data_lines = [line for line in listing.splitlines()[1:] if line.strip()]
    parsed_rows = _parse_ollama_table(listing)
    valid_model_rows = 0
    for row in parsed_rows:
        if len(row) < 5:
            continue
        name, model_id = row[0], row[1]
        size_text = " ".join(row[2:4])
        size_bytes = _ollama_size(size_text)
        if size_bytes is None:
            continue
        valid_model_rows += 1
        item = {"name": name, "id": model_id, "size_bytes": size_bytes,
                "modified": " ".join(row[4:])}
        if scrub:
            item["name"] = f"sha256:{_path_hash(name)}"
            item["id"] = None
        out["models"].append(item)
    if data_lines and not valid_model_rows:
        out["reason"] = "Ollama list schema unsupported"
        return out
    if valid_model_rows != len(data_lines):
        out["partial"] = True
        out["availability"] = AVAILABILITY_PARTIAL
        out["reason"] = "some Ollama list rows could not be parsed"
    running, running_error = run_result(["ollama", "ps"])
    if running_error:
        out["partial"] = True
        out["availability"] = AVAILABILITY_PARTIAL
        out["reason"] = f"ollama ps unavailable: {running_error}"
    else:
        running_lines = [line for line in running.splitlines()[1:] if line.strip()]
        running_rows = _parse_ollama_table(running)
        valid_running_rows = []
        for row in running_rows:
            if len(row) < 6 or _ollama_size(" ".join(row[2:4])) is None:
                continue
            valid_running_rows.append(row)
            name = row[0]
            out["running"].append({
                "name": f"sha256:{_path_hash(name)}" if scrub else name,
                "id": None if scrub else (row[1] if len(row) > 1 else None),
                "raw": None if scrub else " ".join(row[2:]),
            })
        if len(valid_running_rows) != len(running_lines):
            out["partial"] = True
            out["availability"] = AVAILABILITY_PARTIAL
            out["reason"] = out["reason"] or "some Ollama process rows could not be parsed"
    out["available"] = True
    if not out["partial"]:
        out["availability"] = AVAILABILITY_AVAILABLE
    return out


def collect_changes(scrub: bool = False) -> dict:
    out = {"available": False, "availability": AVAILABILITY_UNAVAILABLE,
           "reason": None, "source": "git", "root": None,
           "head": None, "dirty": False,
           "counts": {"modified": 0, "added": 0, "deleted": 0,
                      "renamed": 0, "untracked": 0},
           "diff": {"files": 0, "insertions": 0, "deletions": 0},
           "fingerprint": None}
    root, error = run_result(["git", "rev-parse", "--show-toplevel"])
    if error or not root.strip():
        out["reason"] = error or "not a git repository"
        return out
    root = root.strip()
    out["root"] = None if scrub else root
    head, head_error = run_result(["git", "log", "-1", "--format=%H%x00%s%x00%ct"])
    status, status_error = run_result(["git", "status", "--porcelain=v1", "--untracked-files=all"])
    numstat, numstat_error = run_result(["git", "diff", "HEAD", "--numstat"])
    if head_error or status_error or numstat_error:
        out["reason"] = "; ".join(e for e in (head_error, status_error, numstat_error) if e)
        return out
    head_parts = head.rstrip("\n").split("\0")
    if len(head_parts) >= 3:
        out["head"] = {"sha": head_parts[0], "subject": None if scrub else head_parts[1],
                       "timestamp": datetime.fromtimestamp(int(head_parts[2]), timezone.utc).isoformat()}
    for line in status.splitlines():
        if len(line) < 3:
            continue
        code = line[:2]
        if code == "??":
            out["counts"]["untracked"] += 1
        elif "R" in code:
            out["counts"]["renamed"] += 1
        else:
            if "A" in code:
                out["counts"]["added"] += 1
            if "D" in code:
                out["counts"]["deleted"] += 1
            if "M" in code or "T" in code:
                out["counts"]["modified"] += 1
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            out["diff"]["insertions"] += int(parts[0]) if parts[0].isdigit() else 0
            out["diff"]["deletions"] += int(parts[1]) if parts[1].isdigit() else 0
            out["diff"]["files"] += 1
        except ValueError:
            continue
    out["dirty"] = bool(status.strip())
    fingerprint = json.dumps({"head": head, "status": status, "numstat": numstat}, sort_keys=True)
    out["fingerprint"] = f"sha256:{hashlib.sha256(fingerprint.encode()).hexdigest()[:16]}"
    out["available"] = True
    out["availability"] = AVAILABILITY_AVAILABLE
    return out


def collect_opencode(days: int = 7, scrub: bool = False,
                     live_processes: list[dict] | None = None) -> dict:
    """Token + session accounting straight from opencode's SQLite DB.

    Tokens are summed from `part` rows of type 'step-finish' (the per-turn
    delta). The pre-aggregated session.tokens_* columns are reported
    separately as `stored` because they appear to hold the LAST turn only,
    not a lifetime sum - see `stored_matches_summed`.
    """
    out: dict = {
        "available": False,
        "availability": AVAILABILITY_UNAVAILABLE,
        "reason": None,
        "db_path": None,
        "version": None,   # filled from session.version below - no subprocess
        "tested_version": TESTED_OPENCODE_VERSION,
        "data_confidence": "unverified",
        "window_days": days,
        "live_processes": (_agent_processes(scrub) if live_processes is None
                           else live_processes),
        "sessions": [],
        "session_count": 0,
        "tokens_total": dict(ZERO_TOKENS, turns=0),
        "tokens_by_model": {},
        "tokens_by_day": {},
        "cost_usd": 0.0,
        "todos": [],
        "stored_matches_summed": None,
        "partial": False,
        "parse": {
            "part_rows": 0,
            "step_finish_rows": 0,
            "records_counted": 0,
            "records_ignored": 0,
            "records_invalid": 0,
            "records_duplicate": 0,
        },
    }

    path = _opencode_db_path()
    if not path:
        out["reason"] = "opencode database not found (set AGENTBOX_OPENCODE_DB)"
        return out
    out["db_path"] = None if scrub else path

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        out["reason"] = f"cannot open database read-only: {exc}"
        return out

    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            out["reason"] = (f"schema mismatch: missing {sorted(missing)} "
                             f"(tested against opencode {TESTED_OPENCODE_VERSION})")
            return out

        for table, required in REQUIRED_COLUMNS.items():
            columns = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            missing_columns = required - columns
            if missing_columns:
                out["reason"] = (f"schema mismatch: {table} missing "
                                 f"{sorted(missing_columns)} (tested against "
                                 f"opencode {TESTED_OPENCODE_VERSION})")
                return out

        cutoff_ms = int((time.time() - days * 86400) * 1000)

        # opencode stamps the writing version on every session row
        row = con.execute("SELECT version FROM session "
                          "ORDER BY time_updated DESC LIMIT 1").fetchone()
        if row and row["version"]:
            out["version"] = row["version"]
            if out["version"] == TESTED_OPENCODE_VERSION:
                out["data_confidence"] = "verified"

        # ---- sessions -------------------------------------------------- #
        sessions: dict[str, dict] = {}
        for r in con.execute(
                "SELECT id, parent_id, title, directory, agent, model, cost, "
                "tokens_input, tokens_output, tokens_reasoning, "
                "tokens_cache_read, tokens_cache_write, "
                "time_created, time_updated, time_archived FROM session"):
            sessions[r["id"]] = {
                "id": r["id"],
                "parent_id": r["parent_id"],
                "title": None if scrub else (r["title"] or "")[:70],
                "directory": None if scrub else r["directory"],
                "agent": r["agent"],
                "model": _model_label(_jload(r["model"])),
                "created": (r["time_created"] or 0) / 1000,
                "updated": (r["time_updated"] or 0) / 1000,
                "archived": bool(r["time_archived"]),
                "is_subagent": bool(r["parent_id"]),
                "stored": {
                    "input": r["tokens_input"], "output": r["tokens_output"],
                    "reasoning": r["tokens_reasoning"],
                    "cache_read": r["tokens_cache_read"],
                    "cache_write": r["tokens_cache_write"],
                },
                "stored_cost": r["cost"],
                "summed": dict(ZERO_TOKENS),
                "turns": 0,
                "cost_usd": 0.0,
                "models": set(),
            }

        # ---- token deltas from part.step-finish ------------------------- #
        by_model: dict[str, dict] = defaultdict(lambda: dict(ZERO_TOKENS, turns=0))
        by_day: dict[str, int] = defaultdict(int)

        rows = con.execute(
            "SELECT p.id AS pid, p.message_id AS mid, p.session_id AS sid, "
            "       p.time_created AS ts, p.data AS pdata, m.data AS mdata "
            "FROM part p LEFT JOIN message m ON m.id = p.message_id "
            "WHERE p.time_created >= ?", (cutoff_ms,))

        seen_step_finish = set()
        for r in rows:
            out["parse"]["part_rows"] += 1
            try:
                pdata_raw = r["pdata"]
                pdata = json.loads(pdata_raw) if isinstance(pdata_raw, str) else pdata_raw
            except (json.JSONDecodeError, TypeError, ValueError):
                out["parse"]["records_invalid"] += 1
                continue
            if not isinstance(pdata, dict):
                out["parse"]["records_invalid"] += 1
                continue
            if pdata.get("type") != "step-finish":
                continue
            out["parse"]["step_finish_rows"] += 1
            duplicate_signature = (r["mid"], r["ts"],
                                   json.dumps(pdata, sort_keys=True, default=str))
            if duplicate_signature in seen_step_finish:
                out["parse"]["records_duplicate"] += 1
                continue
            seen_step_finish.add(duplicate_signature)
            tok = _part_tokens(pdata)
            if not tok or not any(tok.values()):
                out["parse"]["records_ignored"] += 1
                continue

            sess = sessions.get(r["sid"])
            label = (_model_label(_jload(r["mdata"]),
                                  {"model": sess["model"]} if sess else None)
                     or (sess or {}).get("model") or "unknown")
            try:
                cost = float(pdata.get("cost") or 0)
            except (TypeError, ValueError):
                cost = 0.0

            _add(out["tokens_total"], tok)
            out["tokens_total"]["turns"] += 1
            out["cost_usd"] += cost

            _add(by_model[label], tok)
            by_model[label]["turns"] += 1

            day = datetime.fromtimestamp(
                (r["ts"] or 0) / 1000, timezone.utc).strftime("%Y-%m-%d")
            by_day[day] += tok["input"] + tok["output"]

            if sess:
                _add(sess["summed"], tok)
                sess["turns"] += 1
                sess["cost_usd"] += cost
                sess["models"].add(label)
            out["parse"]["records_counted"] += 1

        out["tokens_by_model"] = {k: dict(v) for k, v in by_model.items()}
        out["tokens_by_day"] = dict(sorted(by_day.items()))

        # ---- pick the sessions worth showing ---------------------------- #
        active = [s for s in sessions.values()
                  if s["turns"] > 0 or s["updated"] * 1000 >= cutoff_ms]
        active.sort(key=lambda s: s["updated"], reverse=True)
        for s in active:
            s["models"] = sorted(s["models"])
            s["age_seconds"] = int(time.time() - s["updated"]) if s["updated"] else None
        out["session_count"] = len(active)
        out["sessions"] = active[:15]

        # Does session.tokens_* look like a lifetime sum, or just the last turn?
        checkable = [s for s in active if s["turns"] >= 3]
        if checkable:
            out["stored_matches_summed"] = all(
                s["stored"]["input"] == s["summed"]["input"] for s in checkable)

        # ---- open todos: what the agent is doing right now -------------- #
        if "todo" in tables:
            shown = {s["id"] for s in out["sessions"][:5]}
            for r in con.execute(
                    "SELECT session_id, content, status, priority FROM todo "
                    "WHERE status != 'completed' ORDER BY session_id, position"):
                if r["session_id"] in shown:
                    out["todos"].append({
                        "session_id": r["session_id"],
                        "status": r["status"],
                        "priority": r["priority"],
                        "content": None if scrub else r["content"][:80],
                    })

        if (out["parse"]["records_invalid"] or out["parse"]["records_ignored"] or
                out["parse"]["records_duplicate"]):
            out["partial"] = True
            out["availability"] = AVAILABILITY_PARTIAL
            parts = []
            if out["parse"]["records_invalid"]:
                parts.append("some opencode part rows were invalid")
            if out["parse"]["records_ignored"]:
                parts.append("some opencode step-finish rows lacked usable tokens")
            if out["parse"]["records_duplicate"]:
                parts.append("duplicate opencode step-finish rows were ignored")
            out["reason"] = "; ".join(parts)
        else:
            out["availability"] = (AVAILABILITY_AVAILABLE
                                   if out["data_confidence"] == "verified"
                                   else AVAILABILITY_UNVERIFIED)
        out["available"] = True
    except sqlite3.Error as exc:
        out["reason"] = f"query failed: {exc}"
    finally:
        con.close()

    return out


# --------------------------------------------------------------------------- #
# snapshot + rendering
# --------------------------------------------------------------------------- #

def collect_warnings(snap: dict, config: dict | None = None) -> list[dict]:
    """Derive a small, deterministic health summary from collected data."""
    warnings = []

    def add(code: str, message: str) -> None:
        warnings.append({"code": code, "message": message})

    if c := snap.get("cpu"):
        if not c.get("available", True) and not is_unsupported(c):
            add("cpu_unavailable", f"CPU data unavailable: {c.get('reason') or 'unknown error'}")
        elif c["usage_pct"] >= 90 or c["load_per_core"] >= 1.0:
            add("cpu_high", f"CPU pressure is high ({c['usage_pct']:.1f}%, "
                f"{c['load_per_core']:.2f}x load per core)")
    if m := snap.get("memory"):
        if not m.get("available", True) and not is_unsupported(m):
            add("memory_unavailable",
                f"memory data unavailable: {m.get('reason') or 'unknown error'}")
        elif m["total_bytes"] and m["available_bytes"] / m["total_bytes"] <= 0.10:
            add("memory_low", f"only {human_bytes(m['available_bytes'])} RAM available")
    config = config or {}
    disk_warning_pct = _config_number(config, "disk_warning_pct", 85, 0)
    inode_warning_pct = _config_number(config, "inode_warning_pct", 85, 0)
    for disk in snap.get("disk", []):
        if disk["used_pct"] >= disk_warning_pct:
            add("disk_high", f"{disk['path']} is {disk['used_pct']:.1f}% full")
        if (disk.get("inode_used_pct") is not None and
                disk["inode_used_pct"] >= inode_warning_pct):
            add("inode_high", f"{disk['path']} inodes are {disk['inode_used_pct']:.1f}% used")
        if disk.get("read_only") and disk.get("operational", True):
            add("disk_read_only", f"{disk['path']} is mounted read-only")
        if disk.get("deep", {}).get("partial"):
            add("disk_deep_partial", "deep storage scan completed with errors")

    if pressure := snap.get("pressure"):
        if not pressure.get("available", True) and not is_unsupported(pressure):
            add("pressure_unavailable",
                f"pressure data unavailable: {pressure.get('reason') or 'unknown error'}")
        cpu = (pressure.get("cpu") or {}).get("some", {}).get("avg10", 0)
        memory = (pressure.get("memory") or {}).get("full", {}).get("avg10", 0)
        io = (pressure.get("io") or {}).get("full", {}).get("avg10", 0)
        if cpu >= 50:
            add("cpu_pressure_high", f"CPU pressure is {cpu:.1f}% over 10 seconds")
        if memory >= 5:
            add("memory_pressure_high", f"memory full pressure is {memory:.1f}%")
        if io >= 10:
            add("io_pressure_high", f"I/O full pressure is {io:.1f}%")

    if g := snap.get("gpu"):
        if not g.get("available", True) and not is_unsupported(g):
            add("gpu_unavailable", f"GPU data unavailable: {g.get('reason') or 'unknown error'}")
        for gpu in g.get("gpus", []):
            if gpu.get("temp_c") is not None and gpu["temp_c"] >= 85:
                add("gpu_hot", f"GPU {gpu['index']} is {gpu['temp_c']:.0f} C")

    if services := snap.get("services"):
        if not services.get("available", True) and not is_unsupported(services):
            add("services_unavailable",
                f"service data unavailable: {services.get('reason') or 'unknown error'}")
        failed = {unit.removesuffix(".service") for unit in services.get("failed", [])}
        for unit in sorted(failed):
            add("service_failed", f"{unit} service failed")
        for watched in services.get("watched", []):
            if watched["state"] == "failed" and watched["unit"] not in failed:
                add("watched_service_failed", f"{watched['unit']} service failed")
        if is_unsupported(services):
            pass
        elif not services.get("listeners_available", True):
            add("listeners_unavailable",
                f"listener data unavailable: {services.get('listeners_reason') or 'unknown error'}")
        elif not services.get("listener_owners_available", True):
            add("listener_owners_unavailable",
                "listener ownership unavailable; run with sufficient permissions")

    for listener in snap.get("listening", []):
        if (listener.get("agent_kind") and
                listener.get("scope") in ("wildcard", "lan", "external")):
            scope = {"wildcard": "all interfaces", "lan": "the LAN",
                     "external": "an external address"}[listener["scope"]]
            add("agent_server_exposed", f"{listener['agent_kind']} listens on "
                f"{scope} at {listener['address']}")

    if oc := snap.get("opencode"):
        if not oc.get("available"):
            add("opencode_unavailable", f"opencode data unavailable: {oc.get('reason')}")
        elif oc.get("partial"):
            add("opencode_partial",
                f"opencode data is partial: {oc.get('reason') or 'incomplete records'}")
        elif oc.get("data_confidence") != "verified":
            if oc.get("version"):
                message = (f"opencode {oc['version']} differs from tested "
                           f"{oc['tested_version']}")
            else:
                message = "opencode version unavailable; token data is unverified"
            add("opencode_version", message)
    for name in ("claude", "ollama"):
        if provider := snap.get(name):
            if not provider.get("available"):
                expected = name in set(config.get("expected_providers", []))
                if expected or not is_unsupported(provider):
                    add(f"{name}_unavailable",
                        f"{name} data unavailable: {provider.get('reason') or 'unknown error'}")
            elif provider.get("reason"):
                add(f"{name}_partial", f"{name} data is partial: {provider['reason']}")
    for provider in config.get("expected_providers", []):
        if provider not in snap:
            add("expected_provider_missing", f"{provider} was expected but not checked")
    expected_services = set(config.get("expected_services", []))
    if expected_services and (services := snap.get("services")):
        watched = {item["unit"]: item["state"] for item in services.get("watched", [])}
        for service in sorted(expected_services):
            if watched.get(service) != "active":
                add("expected_service_missing", f"{service} was expected but is not active")
    return warnings


def collect_usage(snap: dict, config: dict | None = None) -> dict:
    config = config or {}
    providers = {}
    warnings = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for name in ("opencode", "claude"):
        provider = snap.get(name)
        if not provider:
            continue
        available = provider.get("available", False)
        if not available:
            providers[name] = {"available": False, "today_tokens": None,
                               "window_tokens": None, "daily_average": None,
                               "days": 0, "budget_daily_tokens": _daily_budget(config, name)}
            continue
        totals = provider.get("tokens_total", provider.get("totals", {}))
        by_day = provider.get("tokens_by_day", {})
        current = by_day.get(today, 0)
        window_days = provider.get("window_days", 7)
        total = totals.get("input", 0) + totals.get("output", 0)
        providers[name] = {
            "available": provider.get("available", False),
            "today_tokens": current,
            "window_tokens": total,
            "daily_average": round(sum(by_day.values()) / max(1, window_days)),
            "days": len(by_day),
            "budget_daily_tokens": _daily_budget(config, name),
        }
        budget = providers[name]["budget_daily_tokens"]
        if budget and current > budget:
            warnings.append({"code": "token_budget", "message":
                             f"{name} used {human_count(current)} tokens today "
                             f"(budget {human_count(budget)})"})
    return {"providers": providers, "warnings": warnings}


def collect_capacity(snap: dict, config: dict | None = None) -> dict:
    config = config or {}
    checks = []
    min_ram = int(_config_number(config, "capacity_min_ram_bytes", 2 * 1024 ** 3, 1))
    min_disk = int(_config_number(config, "capacity_min_disk_bytes", 10 * 1024 ** 3, 1))
    cpu = snap.get("cpu")
    if cpu and cpu.get("available", True):
        if cpu.get("usage_pct", 0) >= 95 or cpu.get("load_per_core", 0) >= 2.0:
            checks.append({"name": "cpu", "status": "warning",
                           "message": f"{cpu.get('usage_pct', 0):.1f}% used, "
                                      f"{cpu.get('load_per_core', 0):.2f}x load/core"})
        else:
            checks.append({"name": "cpu", "status": "ready",
                           "message": f"{cpu.get('usage_pct', 0):.1f}% used"})
    elif cpu and is_unsupported(cpu):
        checks.append({"name": "cpu", "status": "not_applicable",
                       "message": "not supported on this platform"})
    else:
        checks.append({"name": "cpu", "status": "unknown", "message": "data unavailable"})
    memory = snap.get("memory")
    if memory and memory.get("total_bytes", 0) > 0:
        checks.append({"name": "ram", "status": "ready" if memory["available_bytes"] >= min_ram
                       else "blocked", "message": human_bytes(memory["available_bytes"]) + " available"})
    else:
        checks.append({"name": "ram", "status": "unknown", "message": "data unavailable"})
    disk = snap.get("disk", [])
    if disk and all(item.get("total_bytes", 0) > 0 for item in disk):
        free = min(item["free_bytes"] for item in disk)
        read_only = [item["path"] for item in disk
                     if item.get("read_only") and item.get("operational", True)]
        if read_only:
            checks.append({"name": "disk", "status": "blocked",
                           "message": f"{read_only[0]} is read-only"})
        else:
            checks.append({"name": "disk", "status": "ready" if free >= min_disk
                           else "blocked", "message": human_bytes(free) + " free"})
    else:
        checks.append({"name": "disk", "status": "unknown", "message": "data unavailable"})
    pressure = snap.get("pressure")
    if pressure and is_unsupported(pressure):
        checks.append({"name": "io", "status": "not_applicable",
                       "message": "PSI is Linux-only"})
    else:
        io_value = ((pressure or {}).get("io")
                    if pressure and pressure.get("available", True) else None)
        io10 = (io_value or {}).get("full", {}).get("avg10")
        checks.append({"name": "io", "status": ("unknown" if io10 is None else
                                                   "warning" if io10 >= 10 else "ready"),
                       "message": ("data unavailable" if io10 is None else
                                   f"{io10:.1f}% I/O full pressure")})
    status = "blocked" if any(c["status"] == "blocked" for c in checks) else (
        "warning" if any(c["status"] in ("warning", "unknown") for c in checks) else "ready")
    return {"status": status, "checks": checks}


EXPLANATIONS = {
    "disk_high": ("A filesystem is close to full.", "Inspect `agentbox --deep disk`."),
    "inode_high": ("The filesystem may run out of file entries before bytes.",
                   "Remove caches or directories with many small files."),
    "disk_deep_partial": ("Some storage roots could not be scanned.",
                          "Run the deep scan with permissions for those roots."),
    "cpu_pressure_high": ("Runnable work is waiting for CPU time.",
                          "Inspect top processes and reduce concurrent work."),
    "memory_pressure_high": ("Processes are stalled waiting for memory.",
                              "Stop unused agents or reduce model size."),
    "io_pressure_high": ("Processes are waiting for storage I/O.",
                         "Inspect disk activity and large model/cache operations."),
    "agent_server_exposed": ("An agent server is reachable beyond loopback.",
                             "Bind it to loopback or restrict access with a firewall."),
    "token_budget": ("A configured daily token budget was exceeded.",
                      "Review active sessions and the configured budget."),
}


def collect_explain(warnings: list[dict]) -> list[dict]:
    result = []
    for warning in warnings:
        meaning, action = EXPLANATIONS.get(
            warning["code"], ("A collector reported a condition requiring attention.",
                               "Inspect the related section for details."))
        result.append({"code": warning["code"], "message": warning["message"],
                       "meaning": meaning, "suggestion": action})
    return result


def redact_snapshot(snap: dict) -> None:
    """Remove host, network and process identifiers in place."""
    snap["hostname"] = "redacted"
    for proc in snap.get("cpu", {}).get("top", []):
        proc["pid"] = None
        proc["name"] = "(redacted)"
        proc["cmdline"] = "(redacted)"
    for listener in snap.get("listening", []):
        listener["address"] = f"*:{listener['port']}"
        listener["process"] = "(redacted)" if listener["process"] else ""
        listener["host"] = "*"
        listener["process_name"] = "(redacted)" if listener.get("process_name") else None
        listener["pid"] = None
    if gpu := snap.get("gpu"):
        for proc in gpu.get("processes", []):
            proc["pid"] = None
            proc["name"] = "(redacted)"
    for disk in snap.get("disk", []):
        if disk.get("path"):
            disk["path"] = "(redacted)"
        if disk.get("mount_point"):
            disk["mount_point"] = "(redacted)"
        if disk.get("mount_source"):
            disk["mount_source"] = "(redacted)"
        if deep := disk.get("deep"):
            if deep.get("reason"):
                deep["reason"] = "one or more storage roots could not be read"
            for root in deep.get("roots", []):
                root["path"] = None
            for item in deep.get("largest", []):
                item["path_hash"] = f"sha256:{_path_hash(item['path'])}"
                item["path"] = None
    if oc := snap.get("opencode"):
        oc["db_path"] = None
        for session in oc.get("sessions", []):
            if session.get("model"):
                session["model"] = f"sha256:{_path_hash(session['model'])}"
            session["models"] = [f"sha256:{_path_hash(model)}"
                                 for model in session.get("models", [])]
        if oc.get("tokens_by_model"):
            oc["tokens_by_model"] = {
                f"sha256:{_path_hash(label)}": tokens
                for label, tokens in oc["tokens_by_model"].items()
            }
        for proc in oc.get("live_processes", []):
            proc["pid"] = None
            proc["tty"] = None
            proc["cmdline"] = None
        for session in oc.get("sessions", []):
            session["id"] = None
            session["parent_id"] = None
            session["title"] = None
            session["directory"] = None
        for todo in oc.get("todos", []):
            todo["session_id"] = None
            todo["content"] = None
    if agents := snap.get("agents"):
        for proc in agents.get("processes", []):
            proc["pid"] = None
            proc["tty"] = None
    if claude := snap.get("claude"):
        for project in claude.get("projects", []):
            project.pop("path", None)
    if ollama := snap.get("ollama"):
        for model in ollama.get("models", []):
            if not model["name"].startswith("sha256:"):
                model["name"] = f"sha256:{_path_hash(model['name'])}"
            model["id"] = None
        for model in ollama.get("running", []):
            model["id"] = None
            if not model["name"].startswith("sha256:"):
                model["name"] = f"sha256:{_path_hash(model['name'])}"
    if changes := snap.get("changes"):
        changes["root"] = None
        if changes.get("head"):
            changes["head"]["sha"] = None
            changes["head"]["subject"] = None


def redact_warning_text(snap: dict) -> None:
    home = re.escape(os.path.expanduser("~"))
    path_re = re.compile(rf"({home}|/Users/[^,\s:;]+|/home/[^,\s:;]+)[^,\s:;]*")
    for warning in snap.get("warnings", []):
        warning["message"] = path_re.sub("(redacted-path)", clean_text(warning["message"]))


def snapshot(days: int = 7, sections: set[str] | None = None,
             scrub: bool = False, deep: bool = False) -> dict:
    want = sections or {"cpu", "mem", "gpu", "disk", "pressure", "services",
                        "agents", "opencode"}
    if "capacity" in want:
        want |= {"cpu", "mem", "disk", "pressure"}
    if "usage" in want:
        want |= {"opencode", "claude"}
    if "explain" in want:
        want |= {"cpu", "mem", "gpu", "disk", "pressure", "services",
                 "agents", "opencode", "claude", "ollama"}
    config, config_error = load_config()
    snap = {
        "schema_version": JSON_SCHEMA_VERSION,
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "platform": platform_info(),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if "cpu" in want or "procs" in want:
        snap["cpu"] = collect_cpu_and_procs()
    if "mem" in want:
        snap["memory"] = collect_mem()
    if "pressure" in want:
        snap["pressure"] = collect_pressure()
    if "gpu" in want:
        snap["gpu"] = collect_gpu()
    if "disk" in want:
        snap["disk"] = collect_disk(deep=deep)
    if "services" in want:
        snap["services"] = collect_services()
        snap["listening"], listener_error = collect_listeners()
        snap["services"]["listeners_available"] = listener_error is None
        snap["services"]["listeners_reason"] = listener_error
        snap["services"]["listener_owners_available"] = (
            not snap["listening"] or all(item["process"] for item in snap["listening"]))
    processes = _agent_processes(scrub) if want & {"agents", "opencode"} else []
    if "agents" in want:
        snap["agents"] = collect_agents(processes)
    if "opencode" in want:
        snap["opencode"] = collect_opencode(
            days, scrub=scrub, live_processes=processes)
    if "claude" in want:
        snap["claude"] = collect_claude(days, scrub=scrub)
    if "ollama" in want:
        snap["ollama"] = collect_ollama(scrub=scrub)
    if "changes" in want:
        snap["changes"] = collect_changes(scrub=scrub)
    if scrub:
        redact_snapshot(snap)
    snap["warnings"] = collect_warnings(snap, config)
    if config_error:
        snap["warnings"].append({"code": "config_invalid", "message": config_error})
    if "usage" in want:
        usage = collect_usage(snap, config)
        snap["usage"] = usage
        snap["warnings"].extend(usage["warnings"])
    if "capacity" in want:
        snap["capacity"] = collect_capacity(snap, config)
        if snap["capacity"]["status"] == "blocked":
            snap["warnings"].append({
                "code": "capacity_blocked",
                "message": "capacity check is blocked by a hard resource limit",
            })
        elif snap["capacity"]["status"] == "warning":
            snap["warnings"].append({
                "code": "capacity_unknown",
                "message": "capacity check has unavailable or contended data",
            })
    if "explain" in want:
        snap["explain"] = collect_explain(snap["warnings"])
    if scrub:
        redact_warning_text(snap)
    snap["status"] = "WARNING" if snap["warnings"] else "OK"
    return snap


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return "." * width
    return text[:max(0, width - 3)] + "..."


def render(snap: dict, width: int = 120, plain: bool = False) -> str:
    L: list[str] = []
    compact = width < 100
    sep = " | " if plain else "  ·  "
    L.append(f"{'--' if plain else '──'} {snap['hostname']}{sep}{snap['timestamp']}")
    warnings = snap.get("warnings", [])
    status = snap.get("status", "WARNING" if warnings else "OK")
    summary = f"STATUS  {status}"
    if warnings:
        summary += sep + sep.join(w["message"] for w in warnings[:2])
        if len(warnings) > 2:
            summary += f"{sep}+{len(warnings) - 2} more"
    L.append(summary)

    if c := snap.get("cpu"):
        L.append("")
        meter = "" if compact else f"{bar(c['usage_pct'])} "
        degree = " C" if plain else "°C"
        L.append(f"CPU   {meter}{c['usage_pct']:5.1f}%  {c['cores']} cores   "
                 f"load {c['load'][0]}/{c['load'][1]}/{c['load'][2]}"
                 f" ({c['load_per_core']}x per core)"
                 + (f"   {c['temp_c']}{degree}" if c.get("temp_c") else ""))
        L.append(f"      up {human_delta(c['uptime_seconds'])}")

    if m := snap.get("memory"):
        meter = "" if compact else f"{bar(m['used_pct'])} "
        L.append(f"RAM   {meter}{m['used_pct']:5.1f}%  "
                 f"{human_bytes(m['used_bytes'])} / {human_bytes(m['total_bytes'])}"
                 f"   ({human_bytes(m['available_bytes'])} available)")
        if m["swap_total_bytes"]:
            meter = "" if compact else f"{bar(m['swap_used_pct'])} "
            L.append(f"SWAP  {meter}{m['swap_used_pct']:5.1f}%  "
                     f"{human_bytes(m['swap_used_bytes'])} / {human_bytes(m['swap_total_bytes'])}")

    if pressure := snap.get("pressure"):
        values = []
        for resource, level in (("cpu", "some"), ("memory", "full"), ("io", "full")):
            avg10 = (pressure.get(resource) or {}).get(level, {}).get("avg10")
            values.append(f"{resource} {level} {avg10:.1f}%" if avg10 is not None
                          else f"{resource} unavailable")
        L.append("PSI   " + "   ".join(values) + "  (avg10)")

    g = snap.get("gpu")
    if g and not g.get("available", True):
        L.append(f"GPU   data unavailable: {g.get('reason') or 'unknown error'}")
    elif g and g.get("gpus"):
        for gpu in g["gpus"]:
            util = gpu.get("util_pct")
            mem_pct = (100 * gpu["mem_used_bytes"] / gpu["mem_total_bytes"]
                       if gpu.get("mem_total_bytes") else 0)
            extra = ""
            if gpu.get("temp_c"):
                extra += f"   {gpu['temp_c']:.0f}{' C' if plain else '°C'}"
            if gpu.get("power_w"):
                extra += f"   {gpu['power_w']:.0f}W"
                if gpu.get("power_limit_w"):
                    extra += f"/{gpu['power_limit_w']:.0f}W"
            util_text = "unknown" if util is None else f"{util:5.1f}%"
            meter = "" if compact or util is None else f"{bar(util)} "
            L.append(f"GPU   {meter}{util_text}  {gpu['name']}{extra}")
            meter = "" if compact else f"{bar(mem_pct)} "
            L.append(f"VRAM  {meter}{mem_pct:5.1f}%  "
                      f"{human_bytes(gpu['mem_used_bytes'])} / "
                     f"{human_bytes(gpu['mem_total_bytes'])}")
        for p in g.get("processes", []):
            bullet = "-" if plain else "·"
            pid = str(p["pid"]) if p.get("pid") is not None else "-"
            L.append(f"        {bullet} {p['name']} (pid {pid}) "
                     f"{human_bytes(p['mem_bytes'])} VRAM")
    elif g is not None:
        L.append("GPU   no NVIDIA/AMD GPU detected (Intel iGPU? try intel_gpu_top)")

    for d in snap.get("disk", []):
        meter = "" if compact else f"{bar(d['used_pct'])} "
        L.append(f"DISK  {meter}{d['used_pct']:5.1f}%  {d['path']}  "
                 f"{human_bytes(d['used_bytes'])} / {human_bytes(d['total_bytes'])}"
                 f"   ({human_bytes(d['free_bytes'])} free)")
        details = []
        if d.get("filesystem"):
            details.append(f"{d['filesystem']} on {d.get('mount_source') or '?'}")
        if d.get("inode_used_pct") is not None:
            details.append(f"inodes {d['inode_used_pct']:.1f}%")
        if d.get("read_only"):
            details.append("read-only")
        if d.get("read_bytes_per_sec") is not None:
            details.append(f"I/O {human_bytes(d['read_bytes_per_sec'])}/s read "
                           f"{human_bytes(d['write_bytes_per_sec'])}/s write")
        if details:
            L.append("      " + "   ".join(details))
        if deep := d.get("deep"):
            L.append(f"      AI STORAGE  {len(deep['roots'])} roots, "
                     f"{len(deep['largest'])} entries"
                     + ("  partial" if deep.get("partial") else ""))
            for item in deep.get("largest", [])[:8]:
                L.append(f"        {human_bytes(item['bytes']):>8}  {item['path']}")

    if c := snap.get("cpu"):
        if c.get("top"):
            L.append("")
            L.append("TOP PROCESSES")
            for p in c["top"]:
                pid = str(p["pid"]) if p.get("pid") is not None else "-"
                L.append(f"  {p['cpu_pct']:5.1f}%cpu  {human_bytes(p['rss_bytes']):>7} rss  "
                         f"{pid:>7}  {p['cmdline'][:64]}")

    if agents := snap.get("agents"):
        L.append("")
        counts = [f"{kind} {count}" for kind, count in agents["counts"].items() if count]
        L.append("AGENTS  " + ("   ".join(counts) if counts else "none running"))
        for proc in agents["processes"]:
            age = human_delta(proc["age_seconds"]) if proc.get("age_seconds") else "?"
            pid = str(proc["pid"]) if proc.get("pid") is not None else "-"
            tty = proc.get("tty") or "-"
            L.append(f"  > {proc['kind']:<9} pid {pid:<7} {tty:<9} "
                     f"up {age:<7} {human_bytes(proc['rss_bytes']):>7} rss")

    if s := snap.get("services"):
        L.append("")
        active = sum(w["state"] == "active" for w in s["watched"])
        failed_mark = "!" if plain else "⚠"
        L.append(f"SERVICES  {s['running_count']} system running   "
                 f"watched {active}/{len(s['watched'])} active"
                 + (f"   {failed_mark} {len(s['failed'])} FAILED" if s["failed"] else ""))
        if not s.get("available", True):
            L.append(f"  data unavailable: {s.get('reason') or 'unknown error'}")
        for w in s["watched"]:
            if plain:
                mark = "+" if w["state"] == "active" else "!" if w["state"] == "failed" else "-"
            else:
                mark = "●" if w["state"] == "active" else "○"
            scope = f" ({w['scope']})" if w.get("scope") else ""
            L.append(f"  {mark} {w['unit']:<22} {w['state']}{scope}")
        for f in s["failed"]:
            L.append(f"  {'x' if plain else '✗'} {f}  (failed)")

    if listeners := snap.get("listening"):
        L.append("")
        L.append("LISTENING")
        for listener in listeners[:12]:
            owner = listener["process"] or "(owner unavailable)"
            scope = f" [{listener.get('scope')}]" if listener.get("scope") else ""
            L.append(f"  {listener['address']:<28} {owner}{scope}")

    if oc := snap.get("opencode"):
        L.append("")
        header = f"OPENCODE  (last {oc['window_days']}d)"
        if oc.get("version"):
            header += f"   v{oc['version']}"
        L.append(header)

        if oc.get("version") and oc["version"] != oc["tested_version"]:
            L.append(f"  ! schema tested against v{oc['tested_version']}; "
                     f"verify token numbers")

        if "agents" not in snap:
            live = oc.get("live_processes") or []
            if live:
                for p in live:
                    age = human_delta(p["age_seconds"]) if p["age_seconds"] else "?"
                    tty = p["tty"] or "-"
                    pid = str(p["pid"]) if p.get("pid") is not None else "-"
                    L.append(f"  > {p['kind']:<9} pid {pid:<7} {tty:<9} "
                             f"up {age:<7} {human_bytes(p['rss_bytes']):>7} rss")
            else:
                L.append("  no opencode/claude/llama/ollama process running")

        if not oc.get("available"):
            L.append(f"  tokens unavailable: {oc.get('reason')}")
        else:
            t = oc["tokens_total"]
            input_output = t["input"] + t["output"]
            L.append(f"  tokens: {human_count(input_output)} input+output "
                     f"(in {human_count(t['input'])} / out {human_count(t['output'])}"
                     f" / reason {human_count(t['reasoning'])})")
            L.append(f"          cache read {human_count(t['cache_read'])}, "
                     f"write {human_count(t['cache_write'])}   "
                     f"{t['turns']} turns"
                     + (f"   ${oc['cost_usd']:.2f}" if oc["cost_usd"] else "   $0 reported"))

            if oc["tokens_by_model"]:
                for label, tk in sorted(oc["tokens_by_model"].items(),
                                        key=lambda kv: -(kv[1]["input"] + kv[1]["output"])):
                    L.append(f"    {label:<34} {human_count(tk['input'] + tk['output']):>9}"
                             f"   {tk['turns']} turns")

            if oc["tokens_by_day"]:
                L.append("  by day:")
                peak = max(oc["tokens_by_day"].values()) or 1
                for day, n in oc["tokens_by_day"].items():
                    L.append(f"    {day}  {bar(100 * n / peak, 24)} {human_count(n):>9}")

            if oc["sessions"]:
                shown_sessions = oc["sessions"][:8]
                L.append(f"  sessions ({oc['session_count']} in window, "
                         f"showing {len(shown_sessions)}):")
                for s in shown_sessions:
                    ago = human_delta(s["age_seconds"]) if s.get("age_seconds") else "?"
                    tk = s["summed"]["input"] + s["summed"]["output"]
                    model = (s["model"] or "?").split("/")[-1]
                    tag = "+" if s["is_subagent"] else " "
                    name = s["title"] or s["id"] or "(redacted)"
                    L.append(f"    {ago:>6} ago {human_count(tk):>8} tok  "
                             f"{tag}{(s['agent'] or '?') + '/' + model:<22} {clip(name, 36)}")

            if oc["todos"]:
                L.append("  open todos:")
                for td in oc["todos"][:6]:
                    mark = "*" if td["status"] == "in_progress" else "-"
                    L.append(f"    {mark} {td['content'] or '(hidden)'}")

            if oc.get("stored_matches_summed") is False:
                L.append("  note: session.tokens_* holds the last turn, not the "
                         "lifetime sum; totals above are summed from part rows")

    if claude := snap.get("claude"):
        L.append("")
        L.append(f"CLAUDE CODE  (last {claude['window_days']}d)"
                 + (f"   v{claude['version']}" if claude.get("version") else ""))
        if not claude.get("available"):
            L.append(f"  unavailable: {claude.get('reason')}")
        else:
            total = claude["totals"]
            L.append(f"  tokens: {human_count(total['input'] + total['output'])} input+output "
                     f"(in {human_count(total['input'])} / out {human_count(total['output'])})"
                     f"   {total['turns']} turns   $0 not estimated")
            L.append(f"  projects: {len(claude['projects'])}   "
                     f"files: {claude['parse']['files_seen']}   "
                     f"ignored: {claude['parse']['records_ignored']}")

    if ollama := snap.get("ollama"):
        L.append("")
        L.append("OLLAMA MODELS")
        if not ollama.get("available"):
            L.append(f"  unavailable: {ollama.get('reason')}")
        else:
            for model in ollama.get("models", []):
                L.append(f"  {model['name']:<24} {human_bytes(model['size_bytes'] or 0):>8}  "
                         f"{model['modified']}")
            if ollama.get("running"):
                L.append("  running: " + ", ".join(m["name"] for m in ollama["running"]))

    if changes := snap.get("changes"):
        L.append("")
        L.append("REPOSITORY")
        if not changes.get("available"):
            L.append(f"  unavailable: {changes.get('reason')}")
        else:
            c = changes["counts"]
            L.append(f"  {'dirty' if changes['dirty'] else 'clean'}  "
                     f"{c['modified']} modified, {c['untracked']} untracked, "
                     f"{c['added']} added, {c['deleted']} deleted")
            if changes.get("head"):
                L.append(f"  HEAD {changes['head']['sha'][:12]}  {changes['head']['subject'] or '(redacted)'}")

    if usage := snap.get("usage"):
        L.append("")
        L.append("USAGE TRENDS")
        for name, values in usage["providers"].items():
            budget = (f" / {human_count(values['budget_daily_tokens'])} budget"
                      if values.get("budget_daily_tokens") else "")
            today = (human_count(values["today_tokens"])
                     if values["today_tokens"] is not None else "unknown")
            average = (human_count(values["daily_average"])
                       if values["daily_average"] is not None else "unknown")
            L.append(f"  {name:<9} today {today:>8}  avg {average:>8}{budget}")

    if capacity := snap.get("capacity"):
        L.append("")
        L.append(f"CAPACITY  {capacity['status'].upper()}")
        for check in capacity["checks"]:
            L.append(f"  {check['name']:<8} {check['status']:<7} {check['message']}")

    if explain := snap.get("explain"):
        L.append("")
        L.append("EXPLAIN")
        for item in explain:
            L.append(f"  {item['code']}: {item['meaning']} {item['suggestion']}")

    if plain:
        L = [clean_text(line) for line in L]
    return "\n".join(clip(line, width) for line in L)


# --------------------------------------------------------------------------- #

SECTION_ALIASES = {
    "status": None, "all": None,
    "cpu": {"cpu"}, "procs": {"cpu"}, "mem": {"mem"}, "ram": {"mem"},
    "gpu": {"gpu"}, "disk": {"disk"},
    "pressure": {"pressure"}, "psi": {"pressure"},
    "agents": {"agents"},
    "claude": {"claude"}, "ollama": {"ollama"},
    "changes": {"changes"},
    "usage": {"usage", "opencode", "claude"}, "trends": {"usage", "opencode", "claude"},
    "capacity": {"capacity", "cpu", "mem", "disk", "pressure"},
    "explain": {"explain"},
    "services": {"services"}, "svc": {"services"},
    "opencode": {"opencode"}, "oc": {"opencode"}, "tokens": {"opencode"},
}


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer greater than zero") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Split out from main() so tests can diff it against
    the docs — SKILL.md promises agents a specific set of flags and sections,
    and a rename here would otherwise leave that promise silently false."""
    ap = argparse.ArgumentParser(
        prog="agentbox", description="Status probe for a Linux AI agent box.")
    ap.add_argument("section", nargs="?", default="status",
                    choices=sorted(SECTION_ALIASES))
    formats = ap.add_mutually_exclusive_group()
    formats.add_argument("--json", action="store_true", help="emit one formatted JSON object")
    formats.add_argument("--jsonl", action="store_true",
                         help="emit one compact JSON object per line")
    ap.add_argument("--days", type=positive_int, default=7,
                    help="opencode token window (default 7)")
    ap.add_argument("--watch", type=positive_int, metavar="SECS",
                    help="refresh every N seconds")
    ap.add_argument("--redact", "--no-titles", dest="redact", action="store_true",
                    help="redact host, network, process and session identifiers")
    ap.add_argument("--plain", action="store_true",
                    help="plain output without Unicode decorations or terminal controls")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 when the snapshot contains warnings")
    ap.add_argument("--deep", action="store_true",
                    help="deep AI storage scan (use with disk)")
    return ap


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.json and args.watch is not None:
        parser.error("--json cannot be used with --watch; use --jsonl --watch")
    if args.check and args.watch is not None:
        parser.error("--check cannot be used with --watch")

    sections = SECTION_ALIASES[args.section]

    def once() -> tuple[bool, bool]:
        snap = snapshot(days=args.days, sections=sections,
                        scrub=args.redact, deep=args.deep)
        if args.json:
            output = json.dumps(snap, indent=2, default=str)
        elif args.jsonl:
            output = json.dumps(snap, separators=(",", ":"), default=str)
        else:
            width = (shutil.get_terminal_size((120, 24)).columns
                     if sys.stdout.isatty() else 10_000)
            output = render(snap, width=width,
                            plain=args.plain or not sys.stdout.isatty())
        try:
            print(output, flush=True)
        except BrokenPipeError:
            try:
                sys.stdout.close()
            except BrokenPipeError:
                pass
            return False, bool(snap["warnings"])
        return True, bool(snap["warnings"])

    if args.watch is not None:
        try:
            first = True
            while True:
                if not args.jsonl and sys.stdout.isatty() and not args.plain:
                    print("\033[2J\033[H", end="")
                elif not args.jsonl and not first:
                    print()
                printed, _ = once()
                if not printed:
                    return 0
                first = False
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    else:
        printed, warned = once()
        if not printed:
            return 1 if args.check and warned else 0
        return 1 if args.check and warned else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
