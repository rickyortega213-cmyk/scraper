#!/usr/bin/env bash
# Exercises install.sh and the generated launcher against a local clone of
# this repo standing in for GitHub:  bash verifier-buddy/tests/test_install.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
BRANCH="$(git -C "$SRC_REPO" rev-parse --abbrev-ref HEAD)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# "GitHub": a bare copy of the current branch we can push new commits to.
ORIGIN="$WORK/origin.git"
git clone --quiet --bare --branch "$BRANCH" "$SRC_REPO" "$ORIGIN"
git -C "$ORIGIN" symbolic-ref HEAD "refs/heads/$BRANCH"

export HOME="$WORK/home"; mkdir -p "$HOME"; touch "$HOME/.zshrc"
export VERIFIER_REPO="file://$ORIGIN" VERIFIER_BRANCH="$BRANCH"
export VERIFIER_CONFIG_DIR="$WORK/cfg" NO_COLOR=1
unset VERIFIER_HOME
export PATH="/usr/bin:/bin:/usr/local/bin"     # launcher must not rely on our PATH

pass() { echo "ok    $1"; }
fail() { echo "FAIL  $1"; exit 1; }

# 1. piped install, like `curl … | bash`
cat "$SRC_REPO/verifier-buddy/install.sh" | bash >"$WORK/install.log" 2>&1 || { cat "$WORK/install.log"; fail "install"; }
[[ -x "$HOME/.local/bin/verifier" ]] || fail "launcher not created"
[[ -f "$HOME/.verifier-buddy/verifier-buddy/verifier" ]] || fail "clone missing"
grep -q '.local/bin' "$HOME/.zshrc" || fail "PATH not added to .zshrc"
pass "piped install creates clone, launcher and PATH entry"

# 2. launcher runs the tool and reports the revision
out="$("$HOME/.local/bin/verifier" --version)"
[[ "$out" == "verifier buddy "* ]] || fail "launcher did not run the tool: $out"
pass "launcher runs verifier"

# 3. a new commit on "GitHub" is picked up on the next run
scratch="$WORK/scratch"
git clone --quiet --branch "$BRANCH" "$ORIGIN" "$scratch"
sed -i.bak 's/^__version__ = "1.0.0"/__version__ = "1.0.1"/' "$scratch/verifier-buddy/verifier"; rm -f "$scratch/verifier-buddy/verifier.bak"
git -C "$scratch" -c user.name=t -c user.email=t@t commit --quiet -am "bump"
git -C "$scratch" push --quiet origin "$BRANCH"
out="$("$HOME/.local/bin/verifier" --version)"
[[ "$out" == "verifier buddy 1.0.1" ]] || fail "update not picked up: $out"
# banner reports the update (need a key prompt to bail out: feed EOF)
banner="$(printf '' | "$HOME/.local/bin/verifier" 2>&1 || true)"
grep -q "rev $(git -C "$ORIGIN" rev-parse --short HEAD)" <<<"$banner" || fail "banner lacks rev: $banner"
pass "new commit on GitHub is live on the next run (1.0.0 → 1.0.1)"

# 4. second run without changes: no update note, still works
out="$("$HOME/.local/bin/verifier" --version 2>&1)"
[[ "$out" == "verifier buddy 1.0.1" ]] || fail "second run broke: $out"
pass "no-op update is quiet"

# 5. local edits block the update with a clear hint, but the tool still runs
echo "# local tweak" >> "$HOME/.verifier-buddy/verifier-buddy/verifier"
sed -i.bak 's/^__version__ = "1.0.1"/__version__ = "1.0.2"/' "$scratch/verifier-buddy/verifier"; rm -f "$scratch/verifier-buddy/verifier.bak"
git -C "$scratch" -c user.name=t -c user.email=t@t commit --quiet -am "bump again"
git -C "$scratch" push --quiet origin "$BRANCH"
out="$("$HOME/.local/bin/verifier" --version 2>&1)"
grep -q "local edits" <<<"$out" || fail "no local-edit warning: $out"
grep -q "verifier buddy 1.0.1" <<<"$out" || fail "tool did not run with local edits: $out"
git -C "$HOME/.verifier-buddy" checkout --quiet -- .
out="$("$HOME/.local/bin/verifier" --version 2>&1)"
[[ "$out" == "verifier buddy 1.0.2" ]] || fail "update after dropping edits failed: $out"
pass "local edits warn instead of breaking; update resumes once dropped"

# 6. offline: unreachable origin still runs the local version
git -C "$HOME/.verifier-buddy" remote set-url origin "file://$WORK/does-not-exist.git"
out="$("$HOME/.local/bin/verifier" --version 2>&1)"
grep -q "could not reach GitHub" <<<"$out" || fail "no offline note: $out"
grep -q "verifier buddy 1.0.2" <<<"$out" || fail "did not run offline: $out"
git -C "$HOME/.verifier-buddy" remote set-url origin "file://$ORIGIN"
pass "offline run still works"

# 7. VERIFIER_NO_UPDATE skips the check; re-install is idempotent; uninstall
out="$(VERIFIER_NO_UPDATE=1 "$HOME/.local/bin/verifier" --version 2>&1)"
[[ "$out" == "verifier buddy 1.0.2" ]] || fail "NO_UPDATE run: $out"
cat "$SRC_REPO/verifier-buddy/install.sh" | bash >/dev/null 2>&1 || fail "re-install"
[[ "$(grep -c '.local/bin' "$HOME/.zshrc")" == "1" ]] || fail "PATH line duplicated"
bash "$HOME/.verifier-buddy/verifier-buddy/install.sh" --uninstall >/dev/null
[[ ! -e "$HOME/.local/bin/verifier" ]] || fail "uninstall left launcher"
pass "no-update flag, idempotent re-install, uninstall"

# 8. running the installer from inside a checkout uses that checkout
git clone --quiet --branch "$BRANCH" "$ORIGIN" "$WORK/mine"
bash "$WORK/mine/verifier-buddy/install.sh" >"$WORK/install2.log" 2>&1 || { cat "$WORK/install2.log"; fail "checkout install"; }
grep -q "REPO_DIR=\"$WORK/mine\"" "$HOME/.local/bin/verifier" || fail "launcher not pointed at checkout"
pass "installer inside a checkout uses that checkout"

echo "all install tests passed"
