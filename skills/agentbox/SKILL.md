---
name: agentbox
description: Inspect this Linux machine's health and opencode token usage. Use whenever asked how the box is doing, whether it's overloaded, how much RAM/disk/GPU is left, what's eating CPU, which services are up, how many tokens opencode burned, or which sessions ran recently. Also use before starting heavy work to check there is headroom.
---

# agentbox

`agentbox` is a single-file probe for this machine. It reads `/proc`, sysfs,
`nvidia-smi`, `systemctl`, and opencode's SQLite DB, and prints one snapshot.

## Rule: always use `--json`

The text output is a pretty-printer over the same dict. `--json` is the real
interface — parse it. Never read `/proc`, run `nvidia-smi`, `free`, `top`, or
`systemctl` by hand to answer these questions; agentbox already did it, more
carefully, in one pass.

```bash
agentbox --json                 # everything
agentbox --json opencode        # just token accounting
agentbox --json --days 30       # widen the opencode window (default 7)
```

## Sections

Pass one positional section to narrow the snapshot:

| Section | Contents |
|---|---|
| `status` (default), `all` | everything |
| `cpu`, `procs` | usage, load, per-core load, uptime, temp, top processes |
| `mem`, `ram` | RAM + swap |
| `gpu` | NVIDIA (via `nvidia-smi`) or AMD (via sysfs) |
| `disk` | `/` and `/home` |
| `services`, `svc` | systemd running/failed + watchlist + listening TCP ports |
| `opencode`, `oc`, `tokens` | tokens, cost, sessions, per-model/per-day, todos, live agent processes |

## Flags

- `--json` — machine-readable output. Use this.
- `--days N` — opencode token window in days (default `7`).
- `--no-titles` — scrub session titles, directories, todo text, DB path, and
  cmdlines. Use it for anything that gets logged, pasted publicly, or shared.
- `--watch N` — foreground redraw loop every N seconds. **Never run this**; it
  does not terminate. It is for a human at a terminal.

Env: `AGENTBOX_OPENCODE_DB` overrides the opencode DB path and is authoritative
— if it points at a missing file, the opencode section reports unavailable
rather than falling back.

## Reading the output

Report in plain language, then flag anything concerning:

- **CPU** — `load_per_core` sustained above ~1.0 means saturated. A single spike
  is not a problem; check `uptime_seconds` and the load triple (1/5/15 min) to
  tell a burst from a trend.
- **RAM** — judge by `available`, not `free`. Cache is not pressure. Swap in use
  with low available RAM is real pressure.
- **Disk** — over 85% used is worth raising unprompted.
- **GPU** — a temperature near its cap alongside a power draw pinned at its
  limit means thermal throttling.
- **Tokens** — compare against the per-day breakdown. `cost` is `0` for local
  models, so the dollar figure only means anything for cloud providers; say so
  rather than reporting "$0" as if it were a saving.

## When the opencode section says `available: false`

It carries a `reason`. The schema is pinned to a tested opencode version and
degrades deliberately instead of guessing — so an upgrade that moved the schema
reports unavailable rather than a wrong number. Relay the reason; do not try to
query the DB yourself to work around it.

## Installing it elsewhere

If `agentbox` is not on PATH: `git clone https://github.com/ceccode/agentbox &&
cd agentbox && ./install.sh` (Python 3.10+, stdlib only, symlinks to `~/bin`).
