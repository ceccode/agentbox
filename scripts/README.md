# scripts

## Give agentbox to your agent

The shortest path needs no clone of the skill at all:

```bash
npx skills add ceccode/agentbox        # add -g for user-level instead of project
```

The [`skills` CLI](https://github.com/vercel-labs/skills) reads
`skills/agentbox/SKILL.md` from this repo and links it into every agent it
detects (Claude Code, opencode, Cursor, Codex, ...). `npx skills update`
refreshes it. The binary still has to be on PATH, so do step 1 below either way.

The scripts here are the no-Node alternative. Two steps, in order.

### 1. Install the binary

From the repo root:

```bash
./install.sh
```

Symlinks `agentbox.py` to `~/bin/agentbox`. Make sure `~/bin` is on your PATH —
the script warns you if it isn't. Check with `agentbox --json | head`.

### 2. Install the skill

```bash
./scripts/install-agent-skill.sh
```

This symlinks `skills/agentbox/` into `~/.claude/skills/agentbox`.

**One location, both tools.** opencode 1.18.18 discovers skills in
`.opencode/skills/`, `.claude/skills/`, and `.agents/skills/` — project-local
(walking up to the git worktree root) and global under `$HOME`. Claude Code uses
`~/.claude/skills/` and `.claude/skills/`. The overlap means a single
`~/.claude/skills/agentbox/SKILL.md` is seen by both, with nothing duplicated
and nothing to keep in sync.

Scoping it to one project instead:

```bash
./scripts/install-agent-skill.sh ~/projects/whatever
```

Restart opencode / Claude Code, then confirm with `/skills`.

## Why a skill and not an agent

An opencode **agent** (`.opencode/agent/*.md`) is a persona — its own model,
temperature, permissions, and system prompt, switched into with Tab or invoked
with `@name`. agentbox isn't a persona; it's a tool the agent you're already
talking to should know how to use. That is what skills are for: an instruction
fragment loaded on demand, when the conversation calls for it.

Agents are also not portable. opencode's frontmatter (`mode`, `permission`,
`temperature`) and Claude Code's subagent format are different schemas, so an
agent means writing and maintaining two files. The skill format is shared.

## Alternative: AGENTS.md

If you'd rather have the instructions always in context instead of loaded on
demand, skip the skill and paste this into your project's `AGENTS.md` (opencode
reads `AGENTS.md`, falling back to `CLAUDE.md`; Claude Code reads `CLAUDE.md`):

```markdown
## Machine monitoring
Run `agentbox --json` for a full system snapshot, or `agentbox --json opencode`
for token usage. Always use --json and parse it — never read `/proc`, run
`top`, `nvidia-smi`, or provider databases by hand. Summarize in plain language
and flag anything concerning: high sustained CPU, low available RAM, disk over
85%, read-only operational filesystems, GPU temperature warnings where
supported, partial provider accounting, or runaway token consumption. Treat
`availability: "unsupported"` as a platform limit rather than a failure.
```

Cheaper to set up, but it costs context on every single turn whether or not the
machine is the subject. The skill costs one line in the tool listing until it's
actually needed. Prefer the skill; use `AGENTS.md` if you check the box
constantly.

## Uninstall

```bash
rm ~/.claude/skills/agentbox        # or <project>/.claude/skills/agentbox
rm ~/bin/agentbox
```

If you installed with the `skills` CLI: `npx skills remove agentbox`.
