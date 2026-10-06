#!/usr/bin/env bash
# Install Muse Spark 1.3 for Codex.
#
# Copies the shim + Desktop toggle to ~/.codex/muse-shim, builds the model
# catalog, writes CLI profiles, adds the `muse` provider to config.toml,
# and (macOS) installs launch agents and builds the MuseBar menu app.
#
# Usage: ./install.sh [--no-agents]
# Env:   CODEX_HOME (default ~/.codex)
#        META_API_KEY_FILE (default ~/.config/muse/meta_api_key)
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SHIM_DIR="$CODEX_HOME/muse-shim"
KEY_FILE="${META_API_KEY_FILE:-$HOME/.config/muse/meta_api_key}"
AGENTS=1
for arg in "$@"; do
    [ "$arg" = "--no-agents" ] && AGENTS=0
done

command -v python3 >/dev/null 2>&1 || { echo "error: python3 required"; exit 1; }
[ -f "$KEY_FILE" ] || echo "note: key file not found yet: $KEY_FILE (create it before first use)"

mkdir -p "$SHIM_DIR" "$CODEX_HOME/model-catalogs"
cp "$REPO/shim/shim.py" "$SHIM_DIR/shim.py"
cp "$REPO/desktop-toggle/muse-desktop" "$SHIM_DIR/muse-desktop"
cp "$REPO/catalog/build_muse_catalog.py" "$SHIM_DIR/build_muse_catalog.py"
chmod +x "$SHIM_DIR/muse-desktop"
echo "installed shim + toggle -> $SHIM_DIR"

if command -v codex >/dev/null 2>&1; then
    codex debug models > /tmp/bundled_models.json
    python3 "$REPO/catalog/build_muse_catalog.py" /tmp/bundled_models.json
else
    echo "warning: codex CLI not found; catalog not built"
fi

for p in muse muse-contributor muse-fast muse-lean; do
    sed "s|__CODEX_HOME__|$CODEX_HOME|g" "$REPO/profiles/$p.config.toml" \
        > "$CODEX_HOME/$p.config.toml"
done
echo "installed profiles -> $CODEX_HOME/muse*.config.toml"

touch "$CODEX_HOME/config.toml"
python3 - "$CODEX_HOME/config.toml" "$CODEX_HOME" "$KEY_FILE" <<'EOF'
import re
import sys
path, codex_home, key_file = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as f:
    text = f.read()
lines = text.splitlines(keepends=True)
if not re.search(r"(?m)^\s*model_catalog_json\s*=", text):
    cat = 'model_catalog_json = "%s/model-catalogs/muse-models.json"\n' % codex_home
    idx = next((i for i, l in enumerate(lines) if re.match(r"\s*\[", l)), len(lines))
    lines.insert(idx, cat)
    print("added model_catalog_json")
if not re.search(r"(?m)^\s*\[model_providers\.muse\]", text):
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append("\n[model_providers.muse]\n")
    lines.append('base_url = "http://127.0.0.1:18199/v1"\n')
    lines.append('wire_api = "responses"\n')
    lines.append("\n[model_providers.muse.auth]\n")
    lines.append('command = "/bin/cat"\n')
    lines.append('args = ["%s"]\n' % key_file)
    lines.append("timeout_ms = 5000\n")
    lines.append("refresh_interval_ms = 300000\n")
    print("added [model_providers.muse]")
with open(path, "w") as f:
    f.writelines(lines)
EOF

if [ "$AGENTS" = 1 ] && [ "$(uname)" = "Darwin" ]; then
    AGENT_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$AGENT_DIR"
    for plist in ai.muse.codex-shim ai.muse.menubar; do
        sed "s|__HOME__|$HOME|g" "$REPO/launchd/$plist.plist" > "$AGENT_DIR/$plist.plist"
        launchctl bootout "gui/$(id -u)/$plist" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$AGENT_DIR/$plist.plist"
        echo "loaded $plist"
    done
    if command -v swiftc >/dev/null 2>&1; then
        bash "$REPO/menubar/build.sh" "$SHIM_DIR"
        open -a "$SHIM_DIR/MuseBar.app" || true
    else
        echo "warning: swiftc not found; menu bar app not built"
    fi
    sleep 2
    curl -s -m 5 http://127.0.0.1:18199/healthz && echo "  <- shim alive"
else
    echo "agents skipped (pass no --no-agents on macOS to install them)"
    echo "run the shim manually: python3 $SHIM_DIR/shim.py"
fi

cat <<'EOF'

Next steps:
  1. echo -n '<meta-api-key>' > ~/.config/muse/meta_api_key && chmod 600 ~/.config/muse/meta_api_key
  2. codex --profile muse exec "Reply with exactly: muse-ok"
  3. Desktop app: pick a mode (~/.codex/muse-shim/muse-desktop list) or use MuseBar
  4. After a Codex upgrade, rebuild the catalog (see README)
EOF
