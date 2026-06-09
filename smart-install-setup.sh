#!/usr/bin/env bash
# smart-install-setup.sh — run once to set up Fedora Installer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Fedora Installer — Setup ==="
echo ""

# ── dependencies ──────────────────────────────────────────────────────────────
echo "▸ Installing system dependencies…"
sudo dnf install -y \
    python3 \
    python3-gobject \
    python3-gobject-base \
    libadwaita \
    gtk4 \
    flatpak \
    unzip \
    tar \
    libnotify || true

# alien is optional (for .deb support)
echo ""
read -rp "Install 'alien' for .deb → .rpm conversion? [y/N] " ans
if [[ "${ans,,}" == "y" ]]; then
    sudo dnf install -y alien && echo "✔ alien installed"
else
    echo "  Skipped alien — .deb files won't be supported."
fi

# ── install app ───────────────────────────────────────────────────────────────
echo ""
echo "▸ Installing Fedora Installer to /usr/local/lib/fedora-installer…"
sudo mkdir -p /usr/local/lib/fedora-installer
sudo cp "$SCRIPT_DIR/fedora_installer.py" /usr/local/lib/fedora-installer/

# ── CLI launcher ──────────────────────────────────────────────────────────────
echo "▸ Creating CLI launcher at /usr/local/bin/fedora-installer…"
sudo tee /usr/local/bin/fedora-installer > /dev/null << 'EOF'
#!/usr/bin/env bash
exec python3 /usr/local/lib/fedora-installer/fedora_installer.py "$@"
EOF
sudo chmod +x /usr/local/bin/fedora-installer

echo "▸ Installing CLI installer to ~/.local/bin/smart-install.sh…"
mkdir -p ~/.local/bin
cp "$SCRIPT_DIR/smart-install.sh" ~/.local/bin/smart-install.sh
chmod +x ~/.local/bin/smart-install.sh

# ── desktop entry ─────────────────────────────────────────────────────────────
echo "▸ Creating desktop entry…"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/fedora-installer.desktop << 'EOF'
[Desktop Entry]
Name=Fedora Installer
Comment=Install .rpm, .deb, .flatpak, .AppImage, .tar and .zip files with one click
Exec=fedora-installer %f
Icon=system-software-install
Type=Application
Categories=System;PackageManager;
MimeType=application/x-rpm;application/vnd.debian.binary-package;application/vnd.flatpak;application/x-tar;application/gzip;application/zip;
StartupNotify=true
EOF
update-desktop-database ~/.local/share/applications 2>/dev/null || true

# ── Nautilus right-click script ───────────────────────────────────────────────
echo "▸ Installing Nautilus right-click script…"
mkdir -p ~/.local/share/nautilus/scripts
cat > ~/.local/share/nautilus/scripts/"Smart Install" << 'SCRIPT'
#!/usr/bin/env bash
# Right-click → Scripts → Smart Install
IFS=$'\n'
for filepath in $NAUTILUS_SCRIPT_SELECTED_FILE_PATHS; do
    fedora-installer "$filepath"
done
SCRIPT
chmod +x ~/.local/share/nautilus/scripts/"Smart Install"

# restart Nautilus to pick up the script
nautilus -q 2>/dev/null && sleep 1 && nautilus --no-desktop &

echo ""
echo "✅ All done!"
echo ""
echo "  Launch: fedora-installer"
echo "  Or: right-click any installer file → Scripts → Smart Install"
echo "  Or: open the app from your GNOME launcher"
echo ""
echo "  Restart Nautilus if the right-click menu doesn't appear:"
echo "    nautilus -q && nautilus &"
