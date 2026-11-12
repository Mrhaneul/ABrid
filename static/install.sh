#!/bin/sh
# ABrid installer (macOS / Linux):   curl -fsSL __ABRID_URL__/install | sh
# Connects this computer's Claude Code and/or Codex to ABrid. You approve it once in the browser (GitHub login).
set -eu
URL="__ABRID_URL__"
DIR="$HOME/.abrid"

say() { printf '%s\n' "$*"; }
command -v python3 >/dev/null 2>&1 || { say "ABrid needs python3. Install it, then run this again."; exit 1; }
HAS_CLAUDE=0; command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1
HAS_CODEX=0;  command -v codex  >/dev/null 2>&1 && HAS_CODEX=1
if [ "$HAS_CLAUDE" = 0 ] && [ "$HAS_CODEX" = 0 ]; then
  say "Neither Claude Code (claude) nor Codex (codex) was found on this computer. Install one, then run this again."; exit 1
fi

mkdir -p "$DIR/bin"; chmod 700 "$DIR"
curl -fsSL "$URL/channel.py" -o "$DIR/abrid_channel.py"

# 1. approve this computer in the browser
J=$(curl -fsS -X POST "$URL/api/link/start")
field() { printf '%s' "$1" | python3 -c "import json,sys; print(json.load(sys.stdin)['$2'])"; }
CODE=$(field "$J" code); POLL=$(field "$J" poll); LINK=$(field "$J" link)
say ""
say "  Open this link, log in with GitHub, and click Approve:"
say ""
say "    $LINK"
say ""
say "  The page should show the code  $CODE"
say ""
(open "$LINK" || xdg-open "$LINK") >/dev/null 2>&1 || true
say "  Waiting for approval..."
i=0
while :; do
  R=$(curl -fsS "$URL/api/link/poll?poll=$POLL" 2>/dev/null) || { say "  The link expired. Run the installer again."; exit 1; }
  case "$R" in *'"key"'*) break ;; esac
  i=$((i + 1)); [ "$i" -gt 300 ] && { say "  Timed out waiting for approval. Run the installer again."; exit 1; }
  sleep 2
done
umask 077
printf '%s' "$R" | python3 -c 'import json,sys; d=json.load(sys.stdin); json.dump({"url": d["url"], "key": d["key"], "login": d["login"]}, open(sys.argv[1], "w"))' "$DIR/config.json"
LOGIN=$(field "$R" login)
umask 022

# 2. register the local ABrid server with Claude Code and/or Codex
if [ "$HAS_CLAUDE" = 1 ]; then
  claude mcp remove --scope user abrid >/dev/null 2>&1 || true
  claude mcp remove --scope user abrid-notify >/dev/null 2>&1 || true
  claude mcp add --scope user abrid -- python3 "$DIR/abrid_channel.py" >/dev/null
fi
if [ "$HAS_CODEX" = 1 ]; then
  codex mcp remove abrid >/dev/null 2>&1 || true
  codex mcp add abrid -- python3 "$DIR/abrid_channel.py" >/dev/null
fi

# 3. the `abrid` command: Claude Code with ABrid notifications on
cat > "$DIR/bin/abrid" <<'EOF'
#!/bin/sh
# Claude Code with ABrid notifications (new messages arrive in the session by themselves).
exec claude --dangerously-load-development-channels server:abrid "$@"
EOF
chmod 755 "$DIR/bin/abrid"
case ":$PATH:" in
  *":$DIR/bin:"*) ON_PATH=1 ;;
  *) ON_PATH=0
     LINE='export PATH="$HOME/.abrid/bin:$PATH"  # ABrid'
     RCS=""
     for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do if [ -f "$rc" ]; then RCS="$RCS $rc"; fi; done
     if [ -z "$RCS" ]; then
       case "${SHELL:-}" in *zsh) RCS="$HOME/.zshrc" ;; *) RCS="$HOME/.bashrc" ;; esac
     fi
     for rc in $RCS; do grep -qs ".abrid/bin" "$rc" || printf '\n%s\n' "$LINE" >> "$rc"; done ;;
esac

say ""
say "  Done. Connected as $LOGIN."
if [ "$HAS_CLAUDE" = 1 ]; then
  say ""
  say "  Claude Code: start it with   abrid   (instead of claude)."
  say "  New ABrid messages then arrive in the session by themselves. The first time, Claude Code warns"
  say "  about development channels: choose \"I am using this for local development\"."
  if [ "$ON_PATH" = 0 ]; then say "  (Open a new terminal first, or run:  $DIR/bin/abrid )"; fi
fi
if [ "$HAS_CODEX" = 1 ]; then
  say ""
  say "  Codex: the ABrid tools are ready (no notifications in Codex; ask it to keep calling wait_for_messages)."
fi
say ""
say "  Watch and step in: $URL/"
