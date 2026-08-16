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
    if not shutil.which(cmd[0]):
        return ""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return out.stdout
    except Exception:
        return ""


def read(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# collectors
# --------------------------------------------------------------------------- #

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
    out = run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"])
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
        return {"vendor": vendor, "gpus": gpus, "processes": apps}

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

    return {"vendor": vendor, "gpus": gpus, "processes": []}


def collect_disk(paths=("/", "/home")) -> list[dict]:
    seen, out = set(), []
    for p in paths:
        if not os.path.isdir(p):
            continue
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        key = (st.f_blocks, st.f_bsize)
        if key in seen:
            continue
        seen.add(key)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        out.append({
            "path": p, "total_bytes": total, "used_bytes": total - free,
            "free_bytes": free,
            "used_pct": round(100 * (total - free) / total, 1) if total else 0.0,
        })
    return out


def collect_services() -> dict:
    """Running systemd units + explicit status for the ones we care about."""
    running = []
    out = run(["systemctl", "list-units", "--type=service", "--state=running",
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
        state = run(["systemctl", "is-active", unit]).strip()
        if not state or state == "inactive":
            # also check the user bus (opencode/ollama are often user units)
            state = run(["systemctl", "--user", "is-active", unit]).strip() or state
        if state and state != "inactive":
            watched.append({"unit": unit, "state": state})

    failed = []
    fout = run(["systemctl", "list-units", "--state=failed", "--no-legend",
                "--no-pager", "--plain"])
    for line in fout.splitlines():
        parts = line.split(None, 1)
        if parts:
            failed.append(parts[0])

    return {"running_count": len(running), "running": running,
            "watched": watched, "failed": failed}


def collect_listeners() -> list[dict]:
    """Listening TCP sockets with owning process - shows agent servers."""
    out = run(["ss", "-ltnpH"])
    res = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        addr = cols[3]
        proc = ""
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        if m:
            proc = f"{m.group(1)}({m.group(2)})"
        port = addr.rsplit(":", 1)[-1]
        res.append({"address": addr, "port": port, "process": proc})
    return res


# --------------------------------------------------------------------------- #
# opencode  (SQLite backend; schema verified against opencode 1.18.18)
# --------------------------------------------------------------------------- #

TESTED_OPENCODE_VERSION = "1.18.18"
REQUIRED_TABLES = {"session", "message", "part"}
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
    return {
        "input": int(t.get("input") or 0),
        "output": int(t.get("output") or 0),
        "reasoning": int(t.get("reasoning") or 0),
        "cache_read": int(cache.get("read") or 0),
        "cache_write": int(cache.get("write") or 0),
    }


def _add(dst: dict, src: dict) -> None:
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _tty_of(pid: int) -> str | None:
    try:
        target = os.readlink(f"/proc/{pid}/fd/0")
        return target.replace("/dev/", "") if "/dev/pts/" in target else None
    except OSError:
        return None


AGENT_EXES = ("opencode", "llama-server", "llama", "ollama")


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
    for n in names:
        if not n:
            continue
        base = n.removesuffix(".js").removesuffix(".mjs")
        if base in ("opencode", "opencode-desktop"):
            return "opencode"
        if base in ("ollama",):
            return "ollama"
        if base.startswith("llama"):
            return "llama"
    return None


def _agent_processes(scrub: bool = False) -> list[dict]:
    """Live opencode / llama / ollama processes, read straight from /proc."""
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


def collect_opencode(days: int = 7, scrub: bool = False) -> dict:
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
        "live_processes": _agent_processes(scrub),
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
    out["db_path"] = os.path.basename(path) if scrub else path

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
            cost = float(pdata.get("cost") or 0)

            _add(out["tokens_total"], tok)
            out["tokens_total"]["turns"] += 1
            out["cost_usd"] += cost

            _add(by_model[label], tok)
            by_model[label]["turns"] += 1

            day = datetime.fromtimestamp((r["ts"] or 0) / 1000).strftime("%Y-%m-%d")
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

def snapshot(days: int = 7, sections: set[str] | None = None,
             scrub: bool = False) -> dict:
    want = sections or {"cpu", "mem", "gpu", "disk", "services", "opencode"}
    snap = {
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if "cpu" in want or "procs" in want:
        snap["cpu"] = collect_cpu_and_procs()
        if scrub:
            for p in snap["cpu"]["top"]:
                p["cmdline"] = p["name"]
    if "mem" in want:
        snap["memory"] = collect_mem()
    if "gpu" in want:
        snap["gpu"] = collect_gpu()
    if "disk" in want:
        snap["disk"] = collect_disk()
    if "services" in want:
        snap["services"] = collect_services()
        snap["listening"] = collect_listeners()
    if "opencode" in want:
        snap["opencode"] = collect_opencode(days, scrub=scrub)
    return snap


def render(snap: dict) -> str:
    L: list[str] = []
    L.append(f"── {snap['hostname']}  ·  {snap['timestamp']}")

    if c := snap.get("cpu"):
        L.append("")
        L.append(f"CPU   {bar(c['usage_pct'])} {c['usage_pct']:5.1f}%  "
                 f"{c['cores']} cores   load {c['load'][0]}/{c['load'][1]}/{c['load'][2]}"
                 f" ({c['load_per_core']}x per core)"
                 + (f"   {c['temp_c']}°C" if c.get("temp_c") else ""))
        L.append(f"      up {human_delta(c['uptime_seconds'])}")

    if m := snap.get("memory"):
        L.append(f"RAM   {bar(m['used_pct'])} {m['used_pct']:5.1f}%  "
                 f"{human_bytes(m['used_bytes'])} / {human_bytes(m['total_bytes'])}"
                 f"   ({human_bytes(m['available_bytes'])} available)")
        if m["swap_total_bytes"]:
            L.append(f"SWAP  {bar(m['swap_used_pct'])} {m['swap_used_pct']:5.1f}%  "
                     f"{human_bytes(m['swap_used_bytes'])} / {human_bytes(m['swap_total_bytes'])}")

    g = snap.get("gpu")
    if g and g.get("gpus"):
        for gpu in g["gpus"]:
            util = gpu.get("util_pct")
            mem_pct = (100 * gpu["mem_used_bytes"] / gpu["mem_total_bytes"]
                       if gpu.get("mem_total_bytes") else 0)
            extra = ""
            if gpu.get("temp_c"):
                extra += f"   {gpu['temp_c']:.0f}°C"
            if gpu.get("power_w"):
                extra += f"   {gpu['power_w']:.0f}W"
                if gpu.get("power_limit_w"):
                    extra += f"/{gpu['power_limit_w']:.0f}W"
            L.append(f"GPU   {bar(util or 0)} {(util or 0):5.1f}%  {gpu['name']}{extra}")
            L.append(f"VRAM  {bar(mem_pct)} {mem_pct:5.1f}%  "
                     f"{human_bytes(gpu['mem_used_bytes'])} / "
                     f"{human_bytes(gpu['mem_total_bytes'])}")
        for p in g.get("processes", []):
            L.append(f"        · {p['name']} (pid {p['pid']}) "
                     f"{human_bytes(p['mem_bytes'])} VRAM")
    elif g is not None:
        L.append("GPU   no NVIDIA/AMD GPU detected (Intel iGPU? try intel_gpu_top)")

    for d in snap.get("disk", []):
        L.append(f"DISK  {bar(d['used_pct'])} {d['used_pct']:5.1f}%  {d['path']}  "
                 f"{human_bytes(d['used_bytes'])} / {human_bytes(d['total_bytes'])}"
                 f"   ({human_bytes(d['free_bytes'])} free)")

    if c := snap.get("cpu"):
        if c.get("top"):
            L.append("")
            L.append("TOP PROCESSES")
            for p in c["top"]:
                L.append(f"  {p['cpu_pct']:5.1f}%cpu  {human_bytes(p['rss_bytes']):>7} rss  "
                         f"{p['pid']:>7}  {p['cmdline'][:64]}")

    if s := snap.get("services"):
        L.append("")
        L.append(f"SERVICES  {s['running_count']} running"
                 + (f"   ⚠ {len(s['failed'])} FAILED" if s["failed"] else ""))
        for w in s["watched"]:
            L.append(f"  ● {w['unit']:<22} {w['state']}")
        for f in s["failed"]:
            L.append(f"  ✗ {f}  (failed)")

    if listeners := snap.get("listening"):
        interesting = [l for l in listeners if l["process"]]
        if interesting:
            L.append("")
            L.append("LISTENING")
            for l in interesting[:12]:
                L.append(f"  {l['address']:<28} {l['process']}")

    if oc := snap.get("opencode"):
        L.append("")
        header = f"OPENCODE  (last {oc['window_days']}d)"
        if oc.get("version"):
            header += f"   v{oc['version']}"
        L.append(header)

        if oc.get("version") and oc["version"] != oc["tested_version"]:
            L.append(f"  ! schema tested against v{oc['tested_version']}; "
                     f"verify token numbers")

        live = oc.get("live_processes") or []
        if live:
            for p in live:
                age = human_delta(p["age_seconds"]) if p["age_seconds"] else "?"
                tty = p["tty"] or "-"
                what = p["cmdline"] or p["kind"]
                L.append(f"  > {p['kind']:<9} pid {p['pid']:<7} {tty:<9} "
                         f"up {age:<7} {human_bytes(p['rss_bytes']):>7} rss")
        else:
            L.append("  no opencode/llama/ollama process running")

        if not oc.get("available"):
            L.append(f"  tokens unavailable: {oc.get('reason')}")
            return "\n".join(L)

        t = oc["tokens_total"]
        billable = t["input"] + t["output"]
        L.append(f"  tokens: {human_count(billable)} billable "
                 f"(in {human_count(t['input'])} / out {human_count(t['output'])}"
                 f" / reason {human_count(t['reasoning'])})")
        L.append(f"          cache read {human_count(t['cache_read'])}, "
                 f"write {human_count(t['cache_write'])}   "
                 f"{t['turns']} turns"
                 + (f"   ${oc['cost_usd']:.2f}" if oc["cost_usd"] else "   $0 (local)"))

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
            L.append(f"  sessions ({oc['session_count']} in window, "
                     f"showing {len(oc['sessions'])}):")
            for s in oc["sessions"][:8]:
                ago = human_delta(s["age_seconds"]) if s.get("age_seconds") else "?"
                tk = s["summed"]["input"] + s["summed"]["output"]
                model = (s["model"] or "?").split("/")[-1]
                tag = "+" if s["is_subagent"] else " "
                name = s["title"] or s["id"]
                L.append(f"    {ago:>6} ago {human_count(tk):>8} tok  "
                         f"{tag}{(s['agent'] or '?') + '/' + model:<22} {name[:36]}")

        if oc["todos"]:
            L.append("  open todos:")
            for td in oc["todos"][:6]:
                mark = "*" if td["status"] == "in_progress" else "-"
                L.append(f"    {mark} {td['content'] or '(hidden)'}")

        if oc.get("stored_matches_summed") is False:
            L.append("  note: session.tokens_* holds the last turn, not the "
                     "lifetime sum; totals above are summed from part rows")

    return "\n".join(L)


# --------------------------------------------------------------------------- #

SECTION_ALIASES = {
    "status": None, "all": None,
    "cpu": {"cpu"}, "procs": {"cpu"}, "mem": {"mem"}, "ram": {"mem"},
    "gpu": {"gpu"}, "disk": {"disk"},
    "services": {"services"}, "svc": {"services"},
    "opencode": {"opencode"}, "oc": {"opencode"}, "tokens": {"opencode"},
}


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="agentbox", description="Status probe for a Linux AI agent box.")
    ap.add_argument("section", nargs="?", default="status",
                    choices=sorted(SECTION_ALIASES))
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--days", type=int, default=7,
                    help="opencode token window (default 7)")
    ap.add_argument("--watch", type=int, metavar="SECS",
                    help="refresh every N seconds")
    ap.add_argument("--no-titles", action="store_true",
                    help="scrub session titles, paths and cmdlines "
                         "(use when publishing or logging)")
    args = ap.parse_args()

    sections = SECTION_ALIASES[args.section]

    def once():
        snap = snapshot(days=args.days, sections=sections,
                        scrub=args.no_titles)
        if args.json:
            print(json.dumps(snap, indent=2, default=str))
        else:
            print(render(snap))

    if args.watch:
        try:
            while True:
                print("\033[2J\033[H", end="")
                once()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0
    else:
        once()
    return 0


if __name__ == "__main__":
    sys.exit(main())
