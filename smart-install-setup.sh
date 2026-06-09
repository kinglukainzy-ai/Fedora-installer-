#!/usr/bin/env bash
# =============================================================================
# smart-install-setup.sh — One-time setup
# Run once after downloading smart-install.sh and "Smart Install"
# =============================================================================

set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

echo -e "${BOLD}Setting up smart-install...${RESET}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Install main script to ~/.local/bin
mkdir -p "$HOME/.local/bin"
cp "$SCRIPT_DIR/smart-install.sh" "$HOME/.local/bin/smart-install.sh"
chmod +x "$HOME/.local/bin/smart-install.sh"
echo -e "${GREEN}[OK]${RESET} Installed: ~/.local/bin/smart-install.sh"

# 2. Ensure ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc" 2>/dev/null || true
    echo -e "${GREEN}[OK]${RESET} Added ~/.local/bin to PATH in .bashrc"
fi

# 3. Install Nautilus script
NAUTILUS_SCRIPTS="$HOME/.local/share/nautilus/scripts"
mkdir -p "$NAUTILUS_SCRIPTS"
cp "$SCRIPT_DIR/Smart Install" "$NAUTILUS_SCRIPTS/Smart Install"
chmod +x "$NAUTILUS_SCRIPTS/Smart Install"
echo -e "${GREEN}[OK]${RESET} Installed Nautilus script: ~/...nautilus/scripts/Smart Install"

# 4. Install alien (for .deb support) if not present
if ! command -v alien &>/dev/null; then
    echo -e "${CYAN}[INFO]${RESET} Installing alien (for .deb support)..."
    sudo dnf install -y alien 2>/dev/null || \
        echo -e "  Skipped alien install — add manually if you need .deb support."
fi

echo ""
echo -e "${BOLD}All done!${RESET}"
echo ""
echo "  CLI usage:"
echo "    smart-install.sh /path/to/file.tar.gz"
echo "    smart-install.sh /path/to/file.AppImage myapp"
echo ""
echo "  Right-click usage:"
echo "    Right-click any installer file in Nautilus → Scripts → Smart Install"
echo ""
echo "  Restart Nautilus to pick up the new script:"
echo "    nautilus -q && nautilus &"
echo ""
