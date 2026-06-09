#!/usr/bin/env bash
# smart-install.sh — CLI installer (no GUI required)
# Usage:
#   smart-install.sh /path/to/file.rpm
#   smart-install.sh /path/to/app.tar.gz myapp
set -euo pipefail

FILE="${1:-}"
APP_NAME_OVERRIDE="${2:-}"

if [[ -z "$FILE" ]]; then
    echo "Usage: smart-install.sh <file> [app-name]"
    exit 1
fi

if [[ ! -f "$FILE" ]]; then
    echo "File not found: $FILE"
    exit 1
fi

BASENAME="$(basename "$FILE")"
LOWER="${BASENAME,,}"

# ── notify helper ─────────────────────────────────────────────────────────────
notify_success() {
    local app="$1"
    # notify-send works on the user's desktop session
    DISPLAY="${DISPLAY:-:0}" \
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
    notify-send \
        --app-name="Fedora Installer" \
        --icon="system-software-install" \
        --urgency=normal \
        "✅ $app installed" \
        "$app is ready — find it in your app launcher." 2>/dev/null || true
}

notify_failure() {
    local app="$1" reason="$2"
    DISPLAY="${DISPLAY:-:0}" \
    DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}" \
    notify-send \
        --app-name="Fedora Installer" \
        --icon="dialog-error" \
        --urgency=critical \
        "❌ Failed to install $app" \
        "$reason" 2>/dev/null || true
}

# ── derive app name ───────────────────────────────────────────────────────────
if [[ -n "$APP_NAME_OVERRIDE" ]]; then
    APP_NAME="$APP_NAME_OVERRIDE"
else
    APP_NAME="$BASENAME"
    for ext in .tar.gz .tar.xz .tar.bz2 .tar.zst .tgz .rpm .deb .flatpak .appimage .zip .tar; do
        APP_NAME="${APP_NAME%"$ext"}"
    done
    APP_NAME="$(echo "$APP_NAME" | sed -E 's/[-_](v?[0-9][0-9._-]*).*$//' | sed -E 's/[-_](x86_64|amd64|arm64|linux|fedora).*$//')"
fi
APP_NAME="${APP_NAME:-app}"

echo "📦 File:     $BASENAME"
echo "🏷  App name: $APP_NAME"
echo ""

# ── detect type ───────────────────────────────────────────────────────────────
TYPE=""
[[ "$LOWER" == *.rpm ]]      && TYPE=rpm
[[ "$LOWER" == *.deb ]]      && TYPE=deb
[[ "$LOWER" == *.flatpak ]]  && TYPE=flatpak
[[ "$LOWER" == *.appimage ]] && TYPE=appimage
for tarext in .tar.gz .tgz .tar.xz .tar.bz2 .tar.zst .tar; do
    [[ "$LOWER" == *"$tarext" ]] && TYPE=tarball && break
done
[[ "$LOWER" == *.zip ]] && TYPE=zip

if [[ -z "$TYPE" ]]; then
    notify_failure "$APP_NAME" "Unrecognised file type: $BASENAME"
    echo "❌ Unrecognised file type: $BASENAME"
    echo "   Supported: .rpm  .deb  .flatpak  .AppImage  .tar.*  .zip"
    exit 1
fi

echo "⚙️  Type: $TYPE"

# ── trap: send failure notification if script exits unexpectedly ──────────────
trap 'if [[ $? -ne 0 ]]; then notify_failure "$APP_NAME" "Something went wrong. Check the terminal for details."; fi' EXIT

create_desktop_entry() {
    local name="$1" exec="$2" icon="${3:-}"
    local desktop_dir="$HOME/.local/share/applications"
    mkdir -p "$desktop_dir"
    local icon_line="Icon=application-x-executable"
    [[ -n "$icon" ]] && icon_line="Icon=$icon"
    cat > "$desktop_dir/${name,,}.desktop" << EOF
[Desktop Entry]
Name=$name
Exec=$exec
$icon_line
Type=Application
Categories=Utility;
Terminal=false
EOF
    chmod 755 "$desktop_dir/${name,,}.desktop"
    update-desktop-database "$desktop_dir" 2>/dev/null || true
    echo "✔ Desktop entry: $desktop_dir/${name,,}.desktop"
}

case "$TYPE" in
    rpm)
        echo "▸ Installing via dnf…"
        sudo dnf install -y "$FILE"
        echo "✅ RPM installed."
        ;;

    deb)
        echo "▸ Converting .deb → .rpm via alien…"
        cd /tmp
        alien --to-rpm --scripts "$FILE"
        RPM_OUT=$(ls -t *.rpm | head -1)
        echo "▸ Installing $RPM_OUT via dnf…"
        sudo dnf install -y "/tmp/$RPM_OUT"
        rm -f "/tmp/$RPM_OUT"
        echo "✅ .deb converted and installed."
        ;;

    flatpak)
        echo "▸ Installing Flatpak bundle…"
        flatpak install --user --noninteractive "$FILE"
        echo "✅ Flatpak installed."
        ;;

    appimage)
        DEST_DIR="$HOME/.local/bin"
        mkdir -p "$DEST_DIR"
        DEST="$DEST_DIR/$APP_NAME.AppImage"
        cp "$FILE" "$DEST"
        chmod +x "$DEST"
        echo "▸ Copied to $DEST"
        create_desktop_entry "$APP_NAME" "$DEST"
        echo "✅ AppImage installed."
        ;;

    tarball|zip)
        INSTALL_DIR="/opt/$APP_NAME"
        echo "▸ Extracting to $INSTALL_DIR…"
        sudo mkdir -p "$INSTALL_DIR"

        if [[ "$TYPE" == "tarball" ]]; then
            sudo tar --strip-components=1 -xf "$FILE" -C "$INSTALL_DIR"
        else
            unzip -o "$FILE" -d "$INSTALL_DIR"
        fi

        if [[ -f "$INSTALL_DIR/install.sh" ]]; then
            echo "▸ Found install.sh — running…"
            bash "$INSTALL_DIR/install.sh"
        else
            EXE=$(find "$INSTALL_DIR" -maxdepth 4 -type f -executable \
                ! -name "*.so" ! -name "*.so.*" \
                | head -1)
            if [[ -n "$EXE" ]]; then
                sudo ln -sf "$EXE" "/usr/local/bin/$APP_NAME"
                echo "▸ Symlinked: /usr/local/bin/$APP_NAME → $EXE"
            fi
            ICON=$(find "$INSTALL_DIR" -maxdepth 4 -type f \
                \( -name "*.png" -o -name "*.svg" \) | head -1)
            create_desktop_entry "$APP_NAME" "${EXE:-$INSTALL_DIR}" "$ICON"
        fi
        echo "✅ Archive installed."
        ;;
esac

# ── refresh GNOME ─────────────────────────────────────────────────────────────
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
killall -HUP gnome-shell 2>/dev/null || true
echo "🔄 GNOME launcher refreshed."

# ── success notification ──────────────────────────────────────────────────────
notify_success "$APP_NAME"
trap - EXIT   # clear the error trap — we finished cleanly
