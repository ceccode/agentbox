#!/usr/bin/env bash
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/agentbox.py"
DEST="${1:-$HOME/bin}"
mkdir -p "$DEST"
chmod +x "$SRC"
ln -sf "$SRC" "$DEST/agentbox"
echo "linked $DEST/agentbox -> $SRC"
python3 -c 'import sys,sqlite3; assert sys.version_info>=(3,10), "need Python 3.10+"; print("python", sys.version.split()[0], "sqlite", sqlite3.sqlite_version, "ok")'
case ":$PATH:" in *":$DEST:"*) ;; *) echo "note: $DEST is not on PATH";; esac
