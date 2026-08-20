# agentbox

One file. No dependencies. Tells you what your Linux AI box is doing.

Built for a laptop running headless as an agent environment — opencode,
Claude Code, llama.cpp, ollama — checked on over SSH. Text only, no GUI, no daemon.

```
$ agentbox
── francesco-MACHC-WAX9  ·  2026-08-16T22:30:00Z
STATUS  OK

CPU   [########............]  42.0%  8 cores   load 6.04/3.85/2.68 (0.76x per core)
      up 5d12h   57°C
RAM   [########............]  41.0%  5.8G / 14.0G   (9.1G available)
SWAP  [....................]   0.0%  728.0K / 4.0G
PSI   cpu some 0.3%   memory full 0.0%   io full 0.1%  (avg10)
GPU   [....................]   0.0%  NVIDIA GeForce MX250   57°C   5W/5W
VRAM  [....................]   0.1%  2.0M / 2.0G
DISK  [#...................]   6.0%  /  28.0G / 468.0G   (417.0G free)
      ext4 on /dev/nvme0n1p2   inodes 1.0%   I/O 0B/s read 0B/s write

TOP PROCESSES
  118.0%cpu   817.0M rss    44263  opencode
   47.0%cpu   793.0M rss    55634  opencode

AGENTS  opencode 2   claude 1

SERVICES  28 running
  ● ollama                 active
  ● docker                 active

OPENCODE  (last 7d)   v1.18.18
  > opencode  pid 44263   pts/0     up 29m     817.0M rss
  > opencode  pid 55634   pts/4     up 16m     793.0M rss
  tokens: 246.8k input+output (in 216.6k / out 30.2k / reason 1.2k)
          cache read 1.3M, write 0   80 turns   $0 reported
    opencode/big-pickle                   246.8k   80 turns
  by day:
    2026-08-16  [########################]    246.8k
  sessions (2 in window, showing 2):
      16m ago    84.7k tok   build/big-pickle       Opencode setup for Ubuntu
      29m ago    71.0k tok   build/big-pickle       Setting up remote access
  open todos:
    * Wire agentbox into AGENTS.md
```

## Install

Python 3.10+, stdlib only.

```bash
git clone https://github.com/ceccode/agentbox && cd agentbox
./install.sh          # symlinks to ~/bin/agentbox
```

## Usage

```bash
agentbox                    # full snapshot
agentbox cpu | mem | gpu | disk | services | procs | agents | pressure | opencode
agentbox --json             # machine readable — this is the real interface
agentbox --jsonl            # one compact JSON object per line, for logging
agentbox --json opencode
agentbox --watch 5          # refresh every 5s (foreground loop, not a daemon)
agentbox --days 30          # opencode token window
agentbox --redact           # remove host, network, process and session identifiers
agentbox --plain            # no Unicode decorations or terminal control sequences
agentbox --check            # exit 1 when warnings are present
agentbox --deep disk        # inspect known AI storage directories
agentbox claude             # Claude Code local usage, when available
agentbox ollama             # installed and running Ollama models
agentbox changes             # current repository state, without diff content
agentbox usage               # token trend and configured daily budgets
agentbox capacity            # deterministic readiness checks
agentbox explain             # explanations and suggested actions for warnings
```

The JSON contract is versioned with the top-level `schema_version` field.
Current value: `1`. Consumers should read `status` and `warnings` before using
section data, and should treat `available: false`, `partial`, `unknown`, and
`data_confidence: "unverified"` as non-healthy data states.

`--no-titles` remains an alias for `--redact`. Use `--jsonl --watch 5` for a
machine-readable stream; `--json --watch` is rejected because concatenated
formatted JSON is not a valid stream.

`--check` maps the same warning contract used by JSON to process exit codes:
`0` means healthy, `1` means warnings (including `config_invalid`), and
argparse errors use `2`.

`capacity` propagates blocked and unknown resource checks into the same warning
contract, so `agentbox --check capacity` is safe to use in CI or before starting
another agent.

`--deep disk` scans known AI storage roots (`~/.ollama`, `~/.claude`, opencode,
Hugging Face, llama.cpp and Docker) with bounded `du` scans. It is opt-in and
does not follow other filesystems.

Edit `WATCHED_UNITS` at the top of the file for your machine.

## Give it to your agent

```bash
./scripts/install-agent-skill.sh
```

Symlinks `skills/agentbox/` into `~/.claude/skills/agentbox`. **opencode and
Claude Code both read that path**, so one file serves both — nothing duplicated,
nothing to keep in sync. Restart your agent and ask it how the box is doing;
`/skills` lists it. Pass a project directory to scope it there instead.

A skill, not an agent: agentbox isn't a persona to switch into, it's a tool the
agent you're already talking to should know how to use — loaded on demand
instead of sitting in context every turn. Details and the `AGENTS.md`
alternative are in [`scripts/README.md`](scripts/README.md).

No MCP server, no subagent, no daemon.

## Notes

- **opencode schema verified against 1.18.18.** The DB is read-only (`mode=ro`);
  `AGENTBOX_OPENCODE_DB` overrides the path. Required tables and columns are
  checked before querying; a different opencode version is reported as a
  warning because semantic changes cannot be detected automatically.
- Tokens are summed from `part` step-finish rows, not the `session.tokens_*`
  columns (those hold the last turn only). Rationale in `collect_opencode()`.
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
  produce `config_invalid` instead of silently disabling a threshold:

  ```json
  {
    "disk_warning_pct": 85,
    "inode_warning_pct": 85,
    "usage": {
      "opencode_daily_tokens": 500000,
      "claude_daily_tokens": 300000
    }
  }
  ```

## Tests

```bash
python3 -m unittest discover -s tests
```

Prompt examples for testing the shared opencode and Claude Code skill are in
[`docs/test-prompts.md`](docs/test-prompts.md).

Two kinds of drift are caught here rather than in the field:

- The fixture DB is built from 1.18.18's exact schema, so an opencode upgrade
  that moves the schema fails here instead of quietly reporting garbage.
- `SKILL.md` promises an agent a specific set of flags and sections, and the
  agent runs them without checking. `tests/test_docs.py` diffs that promise
  against `build_parser()` in both directions, so renaming a flag — or adding
  one and forgetting to document it — fails here instead of handing the agent
  a command that exits non-zero for no visible reason.

## License

MIT
