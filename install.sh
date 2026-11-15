#!/bin/bash
# CutShare installer for macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/ryanmarkgenova/cutshare/main/install.sh | bash
#
# Installs into ~/Library/Application Support/CutShare (no pip, no admin password),
# adds a `cutshare` command, and adds + switches on the Finder Quick Actions.
# Run it again any time to update. Uninstall: add `-s -- --uninstall` after bash.
set -euo pipefail

REPO="ryanmarkgenova/cutshare"
REF="${CUTSHARE_REF:-main}"
DEST="$HOME/Library/Application Support/CutShare"
BIN_DIR="$HOME/.local/bin"
SERVICES="$HOME/Library/Services"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

titles() {
  # menu titles of the Quick Actions (kept in sync with macos/make_quick_actions.py)
  printf '%s\n' "CutShare – New project from this export" "CutShare – Submit to segment" \
    "CutShare – Open a segment" "CutShare – Assemble master" "CutShare – Status"
}

uninstall() {
  say "Removing CutShare"
  rm -rf "$DEST"
  rm -f "$BIN_DIR/cutshare"
  while IFS= read -r t; do rm -rf "$SERVICES/$t.workflow"; done < <(titles)
  [ -x /System/Library/CoreServices/pbs ] && /System/Library/CoreServices/pbs -flush >/dev/null 2>&1 || true
  say "Done. Your projects and ~/.cutshare settings were left alone."
}

if [ "${1:-}" = "--uninstall" ]; then uninstall; exit 0; fi

# 1. Python 3.9+ (Apple's, from the Command Line Tools, is fine)
PY=""
for cand in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$cand" ] && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  [ "$(uname)" = "Darwin" ] && xcode-select --install >/dev/null 2>&1 || true
  fail "CutShare needs Python 3.9+. A window to install Apple's Command Line Tools should have opened; finish it, then run this again."
fi
PY="$("$PY" -c 'import sys; print(sys.executable)')"

# 2. Get the code: this folder if run from a download, otherwise GitHub
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/cutshare/cli.py" ]; then
  SRC="$HERE"
else
  say "Downloading CutShare ($REF)"
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$REF" | tar -xz -C "$TMP"
  SRC="$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)"
fi

# 3. Copy it in place. Pure standard library, so nothing to pip install.
say "Installing to $DEST"
mkdir -p "$DEST/bin"
rm -rf "$DEST/lib" "$DEST/macos"
mkdir -p "$DEST/lib"
cp -R "$SRC/cutshare" "$DEST/lib/cutshare"
cp -R "$SRC/macos" "$DEST/macos"
cat > "$DEST/bin/cutshare" <<LAUNCH
#!/bin/bash
export PYTHONPATH="$DEST/lib\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m cutshare "\$@"
LAUNCH
chmod +x "$DEST/bin/cutshare"

mkdir -p "$BIN_DIR"
ln -sf "$DEST/bin/cutshare" "$BIN_DIR/cutshare"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) for rc in "$HOME/.zshrc" "$HOME/.bash_profile"; do
       if [ -f "$rc" ] || [ "$rc" = "$HOME/.zshrc" ]; then
         grep -qs 'CutShare' "$rc" || printf '\n# CutShare\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$rc"
       fi
     done ;;
esac
VERSION="$("$DEST/bin/cutshare" --version 2>/dev/null || echo cutshare)"

# 4. Finder Quick Actions, switched on
mkdir -p "$HOME/.cutshare"
if [ "$(uname)" = "Darwin" ]; then
  say "Adding Finder Quick Actions"
  "$PY" "$DEST/macos/make_quick_actions.py" "$DEST/bin/cutshare" "$SERVICES" >/dev/null
  while IFS= read -r t; do
    defaults write pbs NSServicesStatus -dict-add "\"(null) - $t - runWorkflowAsService\"" \
      '{enabled_context_menu = 1; enabled_services_menu = 1; presentation_modes = {ContextMenu = 1; ServicesMenu = 1;};}' \
      >/dev/null 2>&1 || true
  done < <(titles)
  /System/Library/CoreServices/pbs -flush >/dev/null 2>&1 || true
  killall Finder >/dev/null 2>&1 || true
fi

echo
say "Installed $VERSION"
cat <<MSG

In Finder, right-click:
  an exported .fcpxml    > Quick Actions > CutShare – New project / Submit to segment
  a .cutshare folder     > Quick Actions > CutShare – Open a segment / Assemble master / Status

Terminal: open a new window and run  cutshare --help
Update:   run the same install command again
MSG
