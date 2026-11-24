#!/usr/bin/env bash
# One-line installer for promptwatch-buster:
#   curl -fsSL https://raw.githubusercontent.com/Manasbh/promptwatch-buster/main/install.sh | bash
# Installs it as a Claude Code plugin when the `claude` CLI is available, otherwise as a plain skill.
set -euo pipefail
REPO="Manasbh/promptwatch-buster"
NAME="promptwatch-buster"

if command -v claude >/dev/null 2>&1; then
  echo "→ Installing $NAME as a Claude Code plugin"
  claude plugin marketplace add "$REPO" >/dev/null 2>&1 || claude plugin marketplace update "$NAME" >/dev/null 2>&1 || true
  claude plugin install "$NAME@$NAME"
  echo "  Update later with: claude plugin update $NAME@$NAME"
else
  echo "→ claude CLI not found; installing $NAME as a skill in ~/.claude/skills"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/main" | tar -xz -C "$tmp"
  dest="$HOME/.claude/skills/$NAME"
  mkdir -p "$HOME/.claude/skills"
  if [ -e "$dest" ]; then
    mv "$dest" "$dest.bak-$(date +%Y%m%d%H%M%S)"   # keep any local edits
    echo "  Existing copy backed up next to it"
  fi
  cp -R "$tmp/$NAME-main/skills/$NAME" "$dest"
  echo "  Installed to $dest"
fi

cat <<'EOF'

✔ Done. Next:
  1. Get a Renderdata API key (free credits): https://platform.renderdata.cloud/login
  2. export RENDERDATA_API_KEY=your_key      (add it to ~/.bashrc or ~/.zshrc to keep it)
  3. Restart Claude Code and type /promptwatch-buster
EOF
