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
    if not shutil.which(cmd[0]):
        return "", f"{cmd[0]} not found"
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
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


def collect_cpu_and_procs(top_n: int = 6) -> dict:
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


def _cpu_temp() -> float | None:
    best = None
    for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
        kind = read(f"{zone}/type").strip()
        if kind in ("x86_pkg_temp", "acpitz", "cpu-thermal", "k10temp"):
            raw = read(f"{zone}/temp").strip()
            if raw.isdigit():
                best = max(best or 0, int(raw) / 1000)
    return round(best, 1) if best else None


def collect_mem() -> dict:
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


def collect_gpu() -> dict:
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
    path = os.path.realpath(path)
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


def collect_disk(paths=("/", "/home"), io_interval: float = 0.1,
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


DEEP_STORAGE_ROOTS = (
    ("ollama", "~/.ollama"),
    ("claude", "~/.claude"),
    ("opencode", "~/.local/share/opencode"),
    ("huggingface", "~/.cache/huggingface"),
    ("llama", "~/.cache/llama.cpp"),
    ("docker", "/var/lib/docker"),
)


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


def collect_services() -> dict:
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


def collect_listeners() -> tuple[list[dict], str | None]:
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
    "part": {"message_id", "session_id", "time_created", "data"},
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


def _agent_processes(scrub: bool = False) -> list[dict]:
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
        "available": False, "reason": None, "source": "local_files",
        "version": None, "window_days": days, "projects": [],
        "totals": dict(ZERO_TOKENS, turns=0),
        "parse": {"files_seen": 0, "files_failed": 0, "records_ignored": 0,
                  "records_recognized": 0},
        "cost_usd": None,
    }
    if not os.path.isdir(root):
        out["reason"] = "Claude Code projects directory not found"
        return out

    version = run(["claude", "--version"]).strip().splitlines()
    out["version"] = version[0] if version else None
    cutoff = time.time() - days * 86400
    projects = {}
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
    if not out["available"]:
        out["reason"] = "Claude Code JSONL schema unsupported"
    elif out["parse"]["files_failed"]:
        out["reason"] = "some Claude Code files could not be read"
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
    out = {"available": False, "reason": None, "source": "ollama_cli",
           "version": None, "models": [], "running": []}
    version = run(["ollama", "--version"]).strip()
    out["version"] = version or None
    listing, list_error = run_result(["ollama", "list"])
    if list_error:
        out["reason"] = list_error
        return out
    for row in _parse_ollama_table(listing):
        if len(row) < 5:
            continue
        name, model_id = row[0], row[1]
        size_text = " ".join(row[2:4])
        size_bytes = _ollama_size(size_text)
        if size_bytes is None:
            continue
        item = {"name": name, "id": model_id, "size_bytes": size_bytes,
                "modified": " ".join(row[4:])}
        if scrub:
            item["name"] = f"sha256:{_path_hash(name)}"
            item["id"] = None
        out["models"].append(item)
    running, running_error = run_result(["ollama", "ps"])
    if not running_error:
        for row in _parse_ollama_table(running):
            name = row[0]
            out["running"].append({
                "name": f"sha256:{_path_hash(name)}" if scrub else name,
                "id": None if scrub else (row[1] if len(row) > 1 else None),
                "raw": None if scrub else " ".join(row[2:]),
            })
    out["available"] = True
    return out


def collect_changes(scrub: bool = False) -> dict:
    out = {"available": False, "reason": None, "source": "git", "root": None,
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
        "reason": None,
        "db_path": None,
        "version": None,   # filled from session.version below - no subprocess
        "tested_version": TESTED_OPENCODE_VERSION,
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
            "SELECT p.session_id AS sid, p.time_created AS ts, "
            "       p.data AS pdata, m.data AS mdata "
            "FROM part p LEFT JOIN message m ON m.id = p.message_id "
            "WHERE p.time_created >= ?", (cutoff_ms,))

        for r in rows:
            pdata = _jload(r["pdata"])
            if pdata.get("type") != "step-finish":
                continue
            tok = _part_tokens(pdata)
            if not tok or not any(tok.values()):
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

        out["available"] = True
    except sqlite3.Error as exc:
        out["reason"] = f"query failed: {exc}"
    finally:
        con.close()

    return out


# --------------------------------------------------------------------------- #
# snapshot + rendering
# --------------------------------------------------------------------------- #

def collect_warnings(snap: dict) -> list[dict]:
    """Derive a small, deterministic health summary from collected data."""
    warnings = []

    def add(code: str, message: str) -> None:
        warnings.append({"code": code, "message": message})

    if c := snap.get("cpu"):
        if c["usage_pct"] >= 90 or c["load_per_core"] >= 1.0:
            add("cpu_high", f"CPU pressure is high ({c['usage_pct']:.1f}%, "
                f"{c['load_per_core']:.2f}x load per core)")
    if m := snap.get("memory"):
        if m["total_bytes"] and m["available_bytes"] / m["total_bytes"] <= 0.10:
            add("memory_low", f"only {human_bytes(m['available_bytes'])} RAM available")
    for disk in snap.get("disk", []):
        if disk["used_pct"] >= 85:
            add("disk_high", f"{disk['path']} is {disk['used_pct']:.1f}% full")
        if disk.get("inode_used_pct") is not None and disk["inode_used_pct"] >= 85:
            add("inode_high", f"{disk['path']} inodes are {disk['inode_used_pct']:.1f}% used")
        if disk.get("read_only"):
            add("disk_read_only", f"{disk['path']} is mounted read-only")
        if disk.get("deep", {}).get("partial"):
            add("disk_deep_partial", "deep storage scan completed with errors")

    if pressure := snap.get("pressure"):
        if not pressure.get("available", True):
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
        if not g.get("available", True):
            add("gpu_unavailable", f"GPU data unavailable: {g.get('reason') or 'unknown error'}")
        for gpu in g.get("gpus", []):
            if gpu.get("temp_c") is not None and gpu["temp_c"] >= 85:
                add("gpu_hot", f"GPU {gpu['index']} is {gpu['temp_c']:.0f} C")

    if services := snap.get("services"):
        if not services.get("available", True):
            add("services_unavailable",
                f"service data unavailable: {services.get('reason') or 'unknown error'}")
        failed = {unit.removesuffix(".service") for unit in services.get("failed", [])}
        for unit in sorted(failed):
            add("service_failed", f"{unit} service failed")
        for watched in services.get("watched", []):
            if watched["state"] == "failed" and watched["unit"] not in failed:
                add("watched_service_failed", f"{watched['unit']} service failed")
        if not services.get("listeners_available", True):
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
        elif oc.get("version") and oc["version"] != oc["tested_version"]:
            add("opencode_version", f"opencode {oc['version']} differs from tested "
                f"{oc['tested_version']}")
    for name in ("claude", "ollama"):
        if provider := snap.get(name):
            if not provider.get("available"):
                add(f"{name}_unavailable",
                    f"{name} data unavailable: {provider.get('reason') or 'unknown error'}")
            elif provider.get("reason"):
                add(f"{name}_partial", f"{name} data is partial: {provider['reason']}")
    return warnings


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


def snapshot(days: int = 7, sections: set[str] | None = None,
             scrub: bool = False, deep: bool = False) -> dict:
    want = sections or {"cpu", "mem", "gpu", "disk", "pressure", "services",
                        "agents", "opencode"}
    snap = {
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
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
    snap["warnings"] = collect_warnings(snap)
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
            return "\n".join(clip(line, width) for line in L)

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
