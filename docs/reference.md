# Reference

Details that do not belong in the README: the JSON contract, platform notes,
configuration, and the feature matrix. The README covers install and usage.

## JSON contract

The JSON contract is versioned with the top-level `schema_version` field.
Current value: `1`. This release keeps the existing fields and adds availability
metadata, so the version remains unchanged. Consumers should read `status` and
`warnings` before using section data, and should treat `available: false`,
`availability: "partial"`, `unknown`, and `data_confidence: "unverified"` as
non-healthy data states. `availability: "unsupported"` means the platform does
not expose that feature; it is not automatically a machine-health warning.

Every snapshot includes `platform.system` and capability flags. Availability
states use one vocabulary:

| State | Meaning |
|---|---|
| `available` | The measurement was read and has normal semantics. Zero can be a valid value. |
| `unsupported` | The feature is not provided by this platform, such as Linux PSI on macOS. |
| `unavailable` | The feature should be readable, but the source is missing, denied or malformed. |
| `partial` | Some data was read, but records or subprocesses failed. Totals may be incomplete. |
| `unverified` | The source is readable, but version or schema semantics are not verified. |

`--no-titles` remains an alias for `--redact`. Use `--jsonl --watch 5` for a
machine-readable stream; `--json --watch` is rejected because concatenated
formatted JSON is not a valid stream.

`--check` maps the same warning contract used by JSON to process exit codes:
`0` means healthy, `1` means warnings (including `config_invalid`), and
argparse errors use `2`.

`capacity` lists the checks it ran and propagates blocked, contended and unknown
resource checks into the same warning contract, so `agentbox --check capacity`
is safe to use in CI or before starting another agent. It checks available RAM,
CPU contention, disk free space, read-only filesystems, and Linux I/O PSI when
applicable. It does not predict whether a specific model will fit or start.

`--deep disk` scans known AI storage roots (`~/.ollama`, `~/.claude`, opencode,
Hugging Face, llama.cpp and Docker) with bounded `du` scans. It is opt-in and
does not follow other filesystems.

Edit `WATCHED_UNITS` at the top of `agentbox.py` for your Linux machine.
Optional configuration lives at `~/.config/agentbox/config.json` (see below).

## Platform notes and semantics

- **Linux/macOS support.** Linux uses `/proc`, `/sys`, `systemctl`, `ss`,
  `statvfs`, `nvidia-smi`/amdgpu sysfs and provider files. macOS uses `top`,
  `ps`, `sysctl`, `vm_stat`, `statvfs`, `mount`, `lsof` and the same provider
  files. Apple GPU, temperature sensors, launchd parity and Linux PSI on macOS
  are explicitly unsupported for now.
- **CPU semantics.** Linux CPU is sampled from `/proc/stat` over the measured
  interval. macOS CPU is sampled from `top -l 2`; `sample_seconds` records the
  actual elapsed sampling time.
- **Memory semantics.** Linux reports `MemAvailable`. macOS reports
  `available_bytes` as free + inactive + speculative pages from `vm_stat`; this
  is not treated as Linux `MemAvailable`.
- **opencode schema verified against 1.18.18.** The DB is read-only (`mode=ro`);
  `AGENTBOX_OPENCODE_DB` overrides the path. Required tables and columns are
  checked before querying; a different opencode version is reported as a
  warning because semantic changes cannot be detected automatically.
- Tokens are summed from `part` step-finish rows, not the `session.tokens_*`
  columns (those hold the last turn only). Corrupt or duplicate records set
  `partial` and diagnostic parse counters instead of being silently presented
  as complete totals.
- Local models report `cost: 0`, so the dollar figure only means something for
  cloud providers. Use `ccusage` or `tokscale` for real spend accounting.
- `--redact` strips host, network, process and session identifiers. Use it for
  anything public or logged: `agentbox --jsonl --redact >> metrics.jsonl`.
- PSI comes from `/proc/pressure`; disk metadata comes from mountinfo, statvfs
  and diskstats. No elevated privileges or extra dependencies are required.
- Claude Code usage is parsed locally from JSONL usage fields only; prompts,
  tool output and project paths are never returned. Its local format is
  best-effort and cost is deliberately not estimated.
- `ollama` uses `ollama list` and `ollama ps`; `changes` reports only repository
  metadata and numstat, never diff content.
- `usage` compares today with the observed window and optional daily budgets.
  `capacity` is deterministic and does not claim that a specific model will fit.
  `explain` uses static explanations, never an embedded LLM.
- Configuration is optional at `~/.config/agentbox/config.json`. Invalid values
  produce `config_invalid` instead of silently disabling a threshold. Typos in
  expected providers are rejected:

  ```json
  {
    "disk_warning_pct": 85,
    "inode_warning_pct": 85,
    "usage": {
      "opencode_daily_tokens": 500000,
      "claude_daily_tokens": 300000
    },
    "expected_providers": ["opencode", "claude"],
    "expected_services": ["ollama"]
  }
  ```

## Feature matrix

| Section | Linux | macOS |
|---|---|---|
| CPU/processes | `/proc/stat`, `/proc`, loadavg | `top`, `ps`, `sysctl`, loadavg |
| Memory | `/proc/meminfo` | `vm_stat`, `sysctl` |
| Disk | mountinfo, statvfs, diskstats | mount, statvfs |
| Agents | executable names from `/proc` | executable names from `ps` |
| Providers | opencode, Claude Code, Ollama | opencode, Claude Code, Ollama |
| Services | systemd + `ss` listeners | systemd unsupported; listeners via `lsof` when present |
| Pressure | Linux PSI | unsupported |
| GPU | NVIDIA/AMD best effort | unsupported |
