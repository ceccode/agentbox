# agentbox

One file. No dependencies. Tells you what your Linux or macOS AI box is doing.

Built for a laptop running as an agent environment — opencode, Claude Code,
llama.cpp, ollama — checked locally or over SSH. Text only, no GUI, no daemon.

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

Python 3.10+, stdlib only. Pick one:

```bash
# single file, no clone
mkdir -p ~/bin && curl -fsSL https://raw.githubusercontent.com/ceccode/agentbox/main/agentbox.py -o ~/bin/agentbox && chmod +x ~/bin/agentbox
```

```bash
# clone, symlink follows git pull
git clone https://github.com/ceccode/agentbox && cd agentbox && ./install.sh
```

Make sure `~/bin` is on your PATH, then check with `agentbox --json | head`.

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
agentbox changes            # current repository state, without diff content
agentbox usage              # token trend and configured daily budgets
agentbox capacity           # deterministic readiness checks
agentbox explain            # explanations and suggested actions for warnings
```

Read `status` and `warnings` first; `--json` is the real interface and the
text view is a pretty-printer over the same data. Section availability states
(`available`, `unsupported`, `unavailable`, `partial`, `unverified`), exit
codes, configuration and platform semantics are in
[`docs/reference.md`](docs/reference.md).

## Give it to your agent

The skill lives in [`skills/agentbox/SKILL.md`](skills/agentbox/SKILL.md).
It teaches the agent you already use (Claude Code, opencode, Cursor, Codex and
the other agents the `skills` CLI supports) to run `agentbox --json` instead of
poking at `/proc`, `top` or provider databases by hand.

```bash
npx skills add ceccode/agentbox
```

Add `-g` for a user-level install instead of the current project. The skill
needs the `agentbox` binary on PATH (see Install); it only tells the agent how
to call it.

Without Node, from a clone:

```bash
./scripts/install-agent-skill.sh          # ~/.claude/skills/agentbox, read by opencode and Claude Code
./scripts/install-agent-skill.sh ~/proj   # project-scoped
```

Restart the agent and ask it how the box is doing. Prompts to try are in
[`docs/test-prompts.md`](docs/test-prompts.md); why this is a skill and not an
agent, and the `AGENTS.md` alternative, are in
[`scripts/README.md`](scripts/README.md).

No MCP server, no subagent, no daemon.

## Development

```bash
python3 -m unittest discover -s tests
```

CI runs the same suite on Ubuntu and macOS with Python 3.10 and 3.13, then
byte-compiles `agentbox.py`. Tests use fixtures only, so they do not depend
on the host's `/proc`, providers or clock.

`tests/test_docs.py` diffs `SKILL.md` against the argument parser in both
directions: every flag and section the skill names must exist, and every flag
and section the CLI accepts must be documented. Rename or add a flag and the
build tells you which doc to update.

## License

MIT
