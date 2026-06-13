#!/usr/bin/env bash
# setup.sh — run once to set up Fedora Installer
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Fedora Installer — Setup ==="
echo ""

# ── dependencies ──────────────────────────────────────────────────────────────
echo "▸ Installing system dependencies…"
sudo dnf install -y --skip-unavailable \
    python3 \
    python3-gobject \
    python3-gobject-base \
    libadwaita \
    gtk4 \
    flatpak \
    unzip \
    tar \
    curl \
    git \
    zenity \
    gnome-terminal \
    libnotify || true

# distrobox for .deb support
if [[ -t 0 ]]; then
    read -rp "Install 'distrobox' for .deb support? [Y/n] " ans
else
    ans="y"
fi
if [[ ! "$ans" =~ ^[Nn]$ ]]; then
    sudo dnf install -y distrobox podman && echo "✔ distrobox installed"
else
    echo "  Skipped distrobox — default .deb support won't be available unless you choose alien."
fi


# ── install app ───────────────────────────────────────────────────────────────
echo ""
echo "▸ Installing Fedora Installer to /usr/local/lib/fedora-installer…"
sudo mkdir -p /usr/local/lib/fedora-installer
sudo cp "$SCRIPT_DIR/fedora_installer.py" /usr/local/lib/fedora-installer/
sudo chmod 644 /usr/local/lib/fedora-installer/fedora_installer.py
sudo cp "$SCRIPT_DIR/VERSION" /usr/local/lib/fedora-installer/VERSION

# ── CLI launcher ──────────────────────────────────────────────────────────────
echo "▸ Creating CLI launcher at /usr/local/bin/fedora-installer…"
sudo tee /usr/local/bin/fedora-installer > /dev/null << 'LAUNCHER'
#!/usr/bin/env bash
# Fedora Installer launcher — supports --update and --version flags
set -euo pipefail

INSTALL_DIR="/usr/local/lib/fedora-installer"
REPO_URL="https://github.com/kinglukainzy-ai/Fedora-installer-"
VERSION_URL="https://raw.githubusercontent.com/kinglukainzy-ai/Fedora-installer-/main/VERSION"

if [[ "${1:-}" == "--version" ]]; then
    CURRENT=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo "unknown")
    echo "Fedora Installer v$CURRENT"
    exit 0
fi

if [[ "${1:-}" == "--update" ]]; then
    CURRENT=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo "0.0.0")
    echo "Current version: $CURRENT"
    echo "Checking for updates…"

    LATEST=$(curl -fsSL --connect-timeout 10 "$VERSION_URL" 2>/dev/null | tr -d '[:space:]') || {
        echo "❌ Could not reach GitHub. Check your internet connection."
        exit 1
    }

    if [[ -z "$LATEST" ]]; then
        echo "❌ Could not read remote version."
        exit 1
    fi

    if [[ "$LATEST" == "$CURRENT" ]]; then
        echo "✅ Already up to date ($CURRENT)."
        exit 0
    fi

    echo "Updating $CURRENT → $LATEST …"
    TMP=$(mktemp -d --tmpdir=/var/tmp)
    trap 'rm -rf "$TMP"' EXIT

    git clone --depth=1 "$REPO_URL" "$TMP/repo"
    bash "$TMP/repo/setup.sh"

    echo ""
    echo "✅ Updated to v$LATEST."
    exit 0
fi

exec python3 "$INSTALL_DIR/fedora_installer.py" "$@"
LAUNCHER
sudo chmod +x /usr/local/bin/fedora-installer

echo "▸ Installing CLI installer to ~/.local/bin/smart-install.sh…"
mkdir -p ~/.local/bin
cp "$SCRIPT_DIR/smart-install.sh" ~/.local/bin/smart-install.sh
chmod +x ~/.local/bin/smart-install.sh

# ── desktop entry ─────────────────────────────────────────────────────────────
# Fix ownership if a prior sudo created these directories as root
for dir in ~/.local/share/icons ~/.local/share/applications ~/.local/share/nautilus/scripts; do
    mkdir -p "$dir"
    if [[ -d "$dir" && ! -w "$dir" ]]; then
        sudo chown -R "$(whoami)":"$(whoami)" "$dir"
    fi
done

echo "▸ Installing application icon…"
cp "$SCRIPT_DIR/fedora-installer.png" ~/.local/share/icons/fedora-installer.png

echo "▸ Creating desktop entry…"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/io.github.kinglukainzy_ai.FedoraInstaller.desktop << 'EOF'
[Desktop Entry]
Name=Fedora Installer
Comment=Install .rpm, .deb, .flatpak, .AppImage, .tar and .zip files with one click
Exec=fedora-installer %f
Icon=fedora-installer
Type=Application
Categories=System;PackageManager;
MimeType=application/x-rpm;application/vnd.debian.binary-package;application/vnd.flatpak;application/x-tar;application/gzip;application/zip;
StartupNotify=true
EOF
update-desktop-database ~/.local/share/applications 2>/dev/null || true

# ── Nautilus right-click script ───────────────────────────────────────────────
echo "▸ Installing Nautilus right-click script…"
mkdir -p ~/.local/share/nautilus/scripts
cp "$SCRIPT_DIR/Smart Install" ~/.local/share/nautilus/scripts/"Smart Install"
chmod +x ~/.local/share/nautilus/scripts/"Smart Install"

# restart Nautilus to pick up the script
nautilus -q 2>/dev/null && sleep 1 && nautilus &

echo ""
echo "✅ All done!"
echo ""
echo "  Launch: fedora-installer"
echo "  Or: right-click any installer file → Scripts → Smart Install"
echo "  Or: open the app from your GNOME launcher"
echo ""
echo "  ⚠️  On Wayland (default since Fedora 34): log out and back in"
echo "     for the launcher icon to appear."
echo ""
echo "  Restart Nautilus if the right-click menu doesn't appear:"
echo "    nautilus -q && nautilus &"
