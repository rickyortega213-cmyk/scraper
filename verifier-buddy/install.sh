#!/usr/bin/env bash
# Installs the `verifier` command so you can type `verifier` in any terminal.
#
#   bash install.sh            copy to ~/.local/bin (no sudo)
#   bash install.sh --link     symlink instead of copy: edits to this folder's
#                              `verifier` take effect immediately
#   bash install.sh --system   install to /usr/local/bin (uses sudo)
#   bash install.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/verifier"
BIN_DIR="$HOME/.local/bin"
SUDO=""
LINK=""

for arg in "$@"; do
  case "$arg" in
    --system) BIN_DIR="/usr/local/bin"; [[ $EUID -eq 0 ]] || SUDO="sudo" ;;
    --link) LINK=1 ;;
    --uninstall) ;;
    *) echo "unknown option: $arg" >&2; exit 1 ;;
  esac
done

if [[ " $* " == *" --uninstall "* ]]; then
  for dir in "$HOME/.local/bin" /usr/local/bin; do
    if [[ -e "$dir/verifier" ]]; then
      if [[ -w "$dir" ]]; then rm -f "$dir/verifier"; else sudo rm -f "$dir/verifier"; fi
      echo "removed $dir/verifier"
    fi
  done
  echo "(your saved API key is in ~/.config/verifier-buddy — delete it if you want)"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (3.9 or newer). Install it and re-run." >&2
  exit 1
fi
python3 - <<'PY' || { echo "python3 3.9 or newer is required" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
PY

$SUDO mkdir -p "$BIN_DIR"
if [[ -n "$LINK" ]]; then
  chmod 0755 "$SRC"
  $SUDO ln -sfn "$SRC" "$BIN_DIR/verifier"
  echo "linked → $BIN_DIR/verifier → $SRC  (edits to that file are live)"
else
  $SUDO install -m 0755 "$SRC" "$BIN_DIR/verifier"
  echo "installed → $BIN_DIR/verifier  (re-run this installer after editing $SRC)"
fi

# Make sure the bin dir is on PATH for future shells.
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    LINE="export PATH=\"$BIN_DIR:\$PATH\""
    added=""
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
      if [[ -f "$rc" ]] && ! grep -qF "$BIN_DIR" "$rc"; then
        printf '\n# verifier buddy\n%s\n' "$LINE" >> "$rc"
        added="$added $rc"
      fi
    done
    if [[ -n "$added" ]]; then
      echo "added $BIN_DIR to PATH in:$added"
      echo "open a new terminal (or run:  $LINE ) and then type:  verifier"
    else
      echo "add this to your shell profile so 'verifier' is found:  $LINE"
    fi
    ;;
esac

echo
echo "done - type:  verifier"
