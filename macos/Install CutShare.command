#!/bin/bash
# Double-click to install CutShare and its Finder Quick Actions.
# (If macOS blocks it: right-click > Open, or run `bash "Install CutShare.command"`.)
set -e
cd "$(dirname "$0")/.."
PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "python3 not found. Install Apple's command line tools with: xcode-select --install"
  exit 1
fi
PY="$("$PY" -c 'import sys; print(sys.executable)')"
echo "Installing CutShare with $PY"
rm -rf build *.egg-info
"$PY" -m pip install --user --force-reinstall --no-deps --quiet . || \
  "$PY" -m pip install --user --force-reinstall --no-deps --break-system-packages --quiet .
"$PY" -c 'import cutshare; print("CutShare", cutshare.__version__, "installed")'

mkdir -p "$HOME/.cutshare"
echo "Adding Finder Quick Actions"
"$PY" macos/make_quick_actions.py "$PY"
/System/Library/CoreServices/pbs -flush >/dev/null 2>&1 || true

cat <<MSG

Done. In Finder:
  - right-click an exported .fcpxml  > Quick Actions > CutShare – Submit to segment
  - right-click the .cutshare folder > Quick Actions > Open a segment / Assemble master / Status

Don't see them? System Settings > Privacy & Security > Extensions > Finder,
and tick the CutShare items. Then relaunch Finder (Option-right-click its Dock icon > Relaunch).
MSG
