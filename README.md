# agentbox

One file. No dependencies. Tells you what your Linux AI box is doing.

Built for a laptop running headless as an agent environment — opencode,
llama.cpp, ollama — checked on over SSH. Text only, no GUI, no daemon.

```
$ agentbox
── francesco-MACHC-WAX9  ·  2026-08-16T22:30:00Z
STATUS  OK

CPU   [########............]  42.0%  8 cores   load 6.04/3.85/2.68 (0.76x per core)
      up 5d12h   57°C
RAM   [########............]  41.0%  5.8G / 14.0G   (9.1G available)
SWAP  [....................]   0.0%  728.0K / 4.0G
GPU   [....................]   0.0%  NVIDIA GeForce MX250   57°C   5W/5W
VRAM  [....................]   0.1%  2.0M / 2.0G
DISK  [#...................]   6.0%  /  28.0G / 468.0G   (417.0G free)

TOP PROCESSES
  118.0%cpu   817.0M rss    44263  opencode
   47.0%cpu   793.0M rss    55634  opencode

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
agentbox cpu | mem | gpu | disk | services | procs | opencode
agentbox --json             # machine readable — this is the real interface
agentbox --jsonl            # one compact JSON object per line, for logging
agentbox --json opencode
agentbox --watch 5          # refresh every 5s (foreground loop, not a daemon)
agentbox --days 30          # opencode token window
agentbox --redact           # remove host, network, process and session identifiers
agentbox --plain            # no Unicode decorations or terminal control sequences
```

`--no-titles` remains an alias for `--redact`. Use `--jsonl --watch 5` for a
machine-readable stream; `--json --watch` is rejected because concatenated
formatted JSON is not a valid stream.

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

## Tests

```bash
python3 -m unittest discover -s tests
```

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
