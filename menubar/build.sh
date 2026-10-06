#!/usr/bin/env bash
# Build MuseBar.app (108KB native Swift menu bar switcher).
# Usage: ./build.sh [destdir]   (default: this directory)
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$SRC}"
APP="$DEST/MuseBar.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
swiftc -O -parse-as-library -o "$TMP/MuseBar" "$SRC/MuseBar.swift"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$TMP/MuseBar" "$APP/Contents/MacOS/MuseBar"
cp "$SRC/Info.plist" "$APP/Contents/Info.plist"
codesign --force -s - "$APP"
echo "built $APP"
