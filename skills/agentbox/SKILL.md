---
name: agentbox
description: Inspect this Linux machine's health, AI agent processes, and opencode token usage. Use whenever asked how the box is doing, whether it's overloaded, how much RAM/disk/GPU is left, what's eating CPU, whether opencode or Claude Code is running, which services are up, how many tokens opencode burned, or which sessions ran recently. Also use before starting heavy work to check there is headroom.
---

# agentbox

`agentbox` is a single-file probe for this machine. It reads `/proc`, sysfs,
`nvidia-smi`, `systemctl`, and opencode's SQLite DB, and prints one snapshot.

## Rule: always use `--json`

The text output is a pretty-printer over the same dict. `--json` is the real
interface — parse it. Never read `/proc`, run `nvidia-smi`, `free`, `top`, or
`systemctl` by hand to answer these questions; agentbox already did it, more
carefully, in one pass.

The top-level JSON `schema_version` is currently `1`. Read `status` and
`warnings` before trusting any collector; `available: false`, `partial`,
`unknown`, or `data_confidence: "unverified"` must not be treated as healthy
zero values.

```bash
agentbox --json                 # everything
agentbox --json opencode        # just token accounting
agentbox --json --days 30       # widen the opencode window (default 7)
agentbox --jsonl --redact       # safe, appendable log record
agentbox --json --deep disk     # known AI storage directories
```

## Sections

Pass one positional section to narrow the snapshot:

| Section | Contents |
|---|---|
| `status` (default), `all` | everything |
| `cpu`, `procs` | usage, load, per-core load, uptime, temp, top processes |
| `mem`, `ram` | RAM + swap |
| `gpu` | NVIDIA (via `nvidia-smi`) or AMD (via sysfs) |
| `disk` | capacity, filesystem, inode and I/O data for `/` and `/home` |
| `pressure`, `psi` | Linux CPU, memory and I/O pressure |
| `agents` | live opencode, Claude Code, Ollama and llama.cpp processes |
| `claude` | local Claude Code token usage by hashed project |
| `ollama` | installed and running Ollama models |
| `changes` | Git working tree metadata without diff content |
| `usage`, `trends` | token trends and optional provider daily budgets |
| `capacity` | deterministic RAM, disk and pressure readiness checks |
| `explain` | static explanations and suggested actions for warnings |
| `services`, `svc` | systemd running/failed + watchlist + listening TCP ports |
| `opencode`, `oc`, `tokens` | tokens, cost, sessions, per-model/per-day, todos, live agent processes |

## Flags

- `--json` — machine-readable output. Use this.
- `--jsonl` — one compact JSON object per line, for logging or watch streams.
- `--days N` — opencode token window in days (default `7`).
- `--redact`, `--no-titles` — remove host, network, process and session
  identifiers. Use either for anything logged, pasted publicly, or shared;
  `--no-titles` is the compatibility alias.
- `--plain` — plain text without Unicode decorations or terminal controls.
- `--check` — exit `1` when the snapshot contains warnings, otherwise `0`.
- `--deep` — opt-in bounded scan for known AI storage roots; use with `disk`.
- `--watch N` — foreground redraw loop every N seconds. **Never run this**; it
  does not terminate. It is for a human at a terminal; JSON streaming requires
  `--jsonl --watch N` because `--json --watch` is rejected.

Env: `AGENTBOX_OPENCODE_DB` overrides the opencode DB path and is authoritative
— if it points at a missing file, the opencode section reports unavailable
rather than falling back.

## Reading the output

Report in plain language, then flag anything concerning:

- **Summary** — read top-level `status` and `warnings` first. Relay warnings;
  never interpret a missing collector as a healthy zero.
- **CPU** — `load_per_core` sustained above ~1.0 means saturated. A single spike
  is not a problem; check `uptime_seconds` and the load triple (1/5/15 min) to
  tell a burst from a trend.
- **RAM** — judge by `available_bytes`, not cache or free memory. Swap in use
  with low available RAM is real pressure.
- **Pressure** — read PSI `avg10` for current contention. `full` means all
  runnable work was stalled; the top-level warnings apply conservative limits.
- **Disk** — over 85% space or inode use is worth raising unprompted. A
  read-only operational mount is also a warning.
- **Network** — agent listeners on wildcard, LAN or external addresses are
  warnings. Loopback and Tailscale listeners are classified but not warned.
- **Agents** — `counts` and `processes` include opencode, Claude Code, Ollama and
  llama.cpp. This is process telemetry, separate from opencode token accounting.
- **Claude Code** — local usage is best-effort and cost is always unknown; use
  token totals and parse counters, not estimated dollars.
- **Ollama** — `models` describes installed models and `running` describes the
  current daemon state. Do not infer VRAM use from model size alone.
- **Changes** — repository state is metadata only; never request or reconstruct
  diff content when this section is available.
- **Usage** — compare provider values separately. A budget warning concerns
  observed tokens, not provider billing or subscription spend.
- **Capacity** — treat `blocked` as a hard resource constraint and `warning` as
  contention; it is not a model-fit predictor.
- **Explain** — relay the static meaning and suggestion, but keep the original
  warning message as the source of truth.
- **GPU** — temperatures at or above 85 C deserve attention. Do not claim
  throttling unless the hardware exposes a limit.
- **Tokens** — compare against the per-day breakdown. `cost_usd` is only the
  value reported by opencode; do not infer that zero means local.

## When the opencode section says `available: false`

It carries a `reason`. The schema is pinned to a tested opencode version and
degrades deliberately instead of guessing — so an upgrade that moved the schema
reports unavailable rather than a wrong number. Relay the reason; do not try to
query the DB yourself to work around it.

## Installing it elsewhere

If `agentbox` is not on PATH: `git clone https://github.com/ceccode/agentbox &&
cd agentbox && ./install.sh` (Python 3.10+, stdlib only, symlinks to `~/bin`).
