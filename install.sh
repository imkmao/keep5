#!/bin/sh
# keep5 installer — fetch the single script and put it on your PATH.
# It's short on purpose: read it before you pipe it to a shell.
#
#   curl -fsSL https://keep5.pages.dev/install.sh | sh
#
# Installs the one file to ~/.local/share/keep5/ and links it into ~/.local/bin
# (no sudo). It does NOT touch your token — that's the separate `keep5 setup`
# step below. Override locations with KEEP5_HOME / KEEP5_BIN.
set -eu

RAW="https://raw.githubusercontent.com/imkmao/keep5/main/keep5.py"
SHARE="${KEEP5_HOME:-$HOME/.local/share/keep5}"
BIN="${KEEP5_BIN:-$HOME/.local/bin}"

mkdir -p "$SHARE" "$BIN"
echo "downloading keep5..."
curl -fsSL "$RAW" -o "$SHARE/keep5.py"
chmod +x "$SHARE/keep5.py"
ln -sf "$SHARE/keep5.py" "$BIN/keep5"
echo "installed: $BIN/keep5"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "note: $BIN is not on your PATH — add it, e.g.:"
     echo "      echo 'export PATH=\"$BIN:\$PATH\"' >> ~/.zshrc && exec \$SHELL" ;;
esac

echo
echo "next:  keep5 setup    # paste your token (from: claude setup-token)"
echo "       keep5 enable   # start the background job"
