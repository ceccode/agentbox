#!/usr/bin/env bash
# Install the agentbox skill so opencode and Claude Code can both find it.
#
# Both tools read skills from ~/.claude/skills/<name>/SKILL.md, so one symlink
# covers both. Project-scoped installs land in <project>/.claude/skills/, which
# both tools also read.
#
#   ./scripts/install-agent-skill.sh              # global: ~/.claude/skills
#   ./scripts/install-agent-skill.sh ~/some/proj  # that project's .claude/skills
#
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/skills/agentbox"
[ -f "$SRC/SKILL.md" ] || { echo "error: $SRC/SKILL.md not found" >&2; exit 1; }

if [ $# -eq 0 ]; then
  DEST="$HOME/.claude/skills"
  SCOPE="global (every project, opencode + Claude Code)"
else
  [ -d "$1" ] || { echo "error: not a directory: $1" >&2; exit 1; }
  DEST="$(cd "$1" && pwd)/.claude/skills"
  SCOPE="project $1"
fi

mkdir -p "$DEST"
if [ -e "$DEST/agentbox" ] && [ ! -L "$DEST/agentbox" ]; then
  echo "error: $DEST/agentbox exists and is not a symlink — remove it first" >&2
  exit 1
fi
ln -sfn "$SRC" "$DEST/agentbox"
echo "linked $DEST/agentbox -> $SRC"
echo "scope: $SCOPE"

if ! command -v agentbox >/dev/null 2>&1; then
  echo
  echo "warning: 'agentbox' is not on PATH — the skill will not work until it is."
  echo "         run ./install.sh from the repo root."
fi

cat <<'EOF'

Next:
  opencode      restart it; ask "how is this box doing?"
                verify with: /skills  (agentbox should be listed)
  Claude Code   restart it; ask the same. verify with: /skills
EOF
