#!/bin/bash
# User-space installer.  Needs no root: everything lands under $HOME.
set -euo pipefail

INSTALL_PATH="${INSTALL_DIRECTORY:-$HOME/.local/share/adobe-connect}"
FLASH_DIR="$HOME/.local/lib/flash"
FLASH_URL="https://github.com/darktohka/clean-flash-builds/releases/download/v1.7/flash_player_patched_ppapi_linux.x86_64.tar.gz"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Flash Player ------------------------------------------------------------
# Adobe Connect's classic meeting client is a Flash application, so the plugin
# is still required.  Skip the download when it is already in place.
if [ -f "$FLASH_DIR/libpepflashplayer.so" ] && [ -z "${FORCE_FLASH_DOWNLOAD:-}" ]; then
    echo "Flash Player already present in $FLASH_DIR, skipping download."
else
    echo "Downloading Flash Player..."
    TMP_FLASH="$(mktemp -d)"
    trap 'rm -rf "$TMP_FLASH"' EXIT
    wget -q --show-progress "$FLASH_URL" -O "$TMP_FLASH/flash.tar.gz"
    tar -xzf "$TMP_FLASH/flash.tar.gz" -C "$TMP_FLASH"
    mkdir -p "$FLASH_DIR"
    cp "$TMP_FLASH/libpepflashplayer.so" "$FLASH_DIR/"
fi

# --- Application files -------------------------------------------------------
echo "Installing to $INSTALL_PATH..."
rm -rf "$INSTALL_PATH"
mkdir -p "$INSTALL_PATH"

# Copy everything except the installer and the templates that need expanding.
# Earlier versions ran `sed -i` over open.sh and connect.desktop in the source
# tree, which consumed the INSTALL_PATH placeholder: a second run - or a run
# with a different INSTALL_DIRECTORY - silently kept the first run's path.
# Generating the files instead leaves the source tree untouched and repeatable.
for entry in "$SRC_DIR"/* "$SRC_DIR"/.[!.]*; do
    [ -e "$entry" ] || continue
    case "$(basename "$entry")" in
        install.sh|open.sh|connect.desktop|tools|.git|.github|.vscode|CEF|include) continue ;;
    esac
    cp -r "$entry" "$INSTALL_PATH"/
done

sed "s|INSTALL_PATH|$INSTALL_PATH|g" "$SRC_DIR/open.sh" > "$INSTALL_PATH/open.sh"
chmod +x "$INSTALL_PATH/open.sh"
if [ -f "$INSTALL_PATH/connect" ]; then
    chmod +x "$INSTALL_PATH/connect"
else
    echo "Note: the 'connect' binary is not here yet - run ./make.sh first," >&2
    echo "      or install from a release tarball." >&2
fi

# --- URL scheme handler ------------------------------------------------------
echo "Registering the connectpro:// URL handler..."
mkdir -p "$HOME/.local/share/applications"
sed "s|INSTALL_PATH|$INSTALL_PATH|g" "$SRC_DIR/connect.desktop" \
    > "$HOME/.local/share/applications/connect.desktop"

xdg-mime default connect.desktop x-scheme-handler/connectpro || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

echo "Done. Launch a meeting with:"
echo "  $INSTALL_PATH/connect \"connectpro://YOUR_MEETING_URL\""
