#!/usr/bin/env bash
# Verifier Buddy installer. GitHub is the single source of truth:
#
#   curl -fsSL https://raw.githubusercontent.com/rickyortega213-cmyk/scraper/claude/loving-lamport-9qut59/verifier-buddy/install.sh | bash
#
# It clones the repo into ~/.verifier-buddy (or reuses the checkout it is run
# from) and puts a `verifier` launcher in ~/.local/bin. Every time you type
# `verifier`, the launcher pulls the latest commit from GitHub first, so a
# change pushed to the branch shows up in your terminal on the next run.
#
#   bash install.sh --uninstall     remove the launcher (keeps the clone)
#
# Overrides:  VERIFIER_BRANCH, VERIFIER_REPO, VERIFIER_HOME (clone location)
set -euo pipefail

REPO_URL="${VERIFIER_REPO:-https://github.com/rickyortega213-cmyk/scraper.git}"
BRANCH="${VERIFIER_BRANCH:-claude/loving-lamport-9qut59}"
REPO_DIR="${VERIFIER_HOME:-$HOME/.verifier-buddy}"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/verifier"
INSTALL_URL="https://raw.githubusercontent.com/rickyortega213-cmyk/scraper/$BRANCH/verifier-buddy/install.sh"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--uninstall" ]]; then
  if [[ -e "$LAUNCHER" ]]; then rm -f "$LAUNCHER"; say "removed $LAUNCHER"; fi
  say "clone left in place: $REPO_DIR   (delete it with: rm -rf \"$REPO_DIR\")"
  say "saved API key left in: ~/.config/verifier-buddy"
  exit 0
fi
[[ -z "${1:-}" ]] || die "unknown option: $1"

command -v git >/dev/null 2>&1 || die "git is required (on a Mac: xcode-select --install)"
command -v python3 >/dev/null 2>&1 || die "python3 (3.9 or newer) is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "python3 3.9 or newer is required (you have $(python3 --version 2>&1))"

# Running from inside a checkout of the repo? Then that checkout is the source.
SELF="${BASH_SOURCE[0]:-}"
if [[ -n "$SELF" && -f "$SELF" ]]; then
  top="$(git -C "$(dirname "$SELF")" rev-parse --show-toplevel 2>/dev/null || true)"
  if [[ -n "$top" && -f "$top/verifier-buddy/verifier" ]]; then
    REPO_DIR="$top"
  fi
fi

if [[ -f "$REPO_DIR/verifier-buddy/verifier" ]]; then
  say "using checkout at $REPO_DIR"
  git -C "$REPO_DIR" pull --ff-only --quiet || say "  (could not pull; continuing with what is there)"
else
  [[ ! -e "$REPO_DIR" ]] || die "$REPO_DIR exists but is not a Verifier Buddy checkout; move it or set VERIFIER_HOME"
  say "cloning $REPO_URL ($BRANCH) → $REPO_DIR"
  git clone --quiet --branch "$BRANCH" --single-branch "$REPO_URL" "$REPO_DIR"
fi
chmod 0755 "$REPO_DIR/verifier-buddy/verifier"

mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" <<'EOF'
#!/usr/bin/env bash
# Verifier Buddy launcher (written by install.sh). GitHub is the source of
# truth: each run pulls the latest commit before starting. Skip the check
# with VERIFIER_NO_UPDATE=1.
REPO_DIR="__REPO_DIR__"
SCRIPT="$REPO_DIR/verifier-buddy/verifier"
if [[ ! -f "$SCRIPT" ]]; then
  echo "verifier: $SCRIPT is missing. Re-install with:" >&2
  echo "  curl -fsSL __INSTALL_URL__ | bash" >&2
  exit 1
fi
if [[ "${VERIFIER_NO_UPDATE:-}" != "1" ]] && command -v git >/dev/null 2>&1; then
  before="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null)"
  if out="$(GIT_TERMINAL_PROMPT=0 git -C "$REPO_DIR" \
              -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=8 \
              pull --ff-only --quiet 2>&1)"; then
    after="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null)"
    [[ "$before" == "$after" ]] || export VERIFIER_UPDATED="$before → $after"
  else
    case "$out" in
      *"local changes"*|*"would be overwritten"*|*"fast-forward"*|*"diverg"*)
        echo "  ! GitHub has a newer version but local edits in $REPO_DIR block the update." >&2
        echo "    Push them:     git -C \"$REPO_DIR\" add -A && git -C \"$REPO_DIR\" commit -m 'tweak' && git -C \"$REPO_DIR\" push" >&2
        echo "    or drop them:  git -C \"$REPO_DIR\" reset --hard origin/$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)" >&2 ;;
      *) echo "  (could not reach GitHub to check for updates; running the local version)" >&2 ;;
    esac
  fi
  VERIFIER_REV="$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null)"
  export VERIFIER_REV
fi
exec python3 "$SCRIPT" "$@"
EOF
# Substitute paths (sed keeps the quoted heredoc above readable).
esc() { printf '%s' "$1" | sed 's/[&|]/\\&/g'; }
sed -i.bak -e "s|__REPO_DIR__|$(esc "$REPO_DIR")|" -e "s|__INSTALL_URL__|$(esc "$INSTALL_URL")|" "$LAUNCHER"
rm -f "$LAUNCHER.bak"
chmod 0755 "$LAUNCHER"
say "installed launcher → $LAUNCHER  (source: $REPO_DIR, branch: $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD))"

# Make sure ~/.local/bin is on PATH for future shells.
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
    if [[ -z "$added" && ! -f "$HOME/.zshrc" && ! -f "$HOME/.bashrc" ]]; then
      printf '\n# verifier buddy\n%s\n' "$LINE" >> "$HOME/.zshrc"
      added=" $HOME/.zshrc"
    fi
    say "added $BIN_DIR to PATH in:$added"
    say "open a new terminal (or run:  $LINE ) and then type:  verifier"
    ;;
esac

say ""
say "done - type:  verifier"
say "(it checks GitHub for a newer version every time it starts)"
