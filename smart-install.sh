#!/usr/bin/env bash
# =============================================================================
# smart-install.sh — Universal app installer for Fedora
# Usage: smart-install.sh /path/to/file [app-name]
# =============================================================================

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; exit 1; }

# ── Args ──────────────────────────────────────────────────────────────────────
[[ $# -lt 1 ]] && error "Usage: smart-install.sh /path/to/file [app-name]"

FILE="$1"
[[ ! -f "$FILE" ]] && error "File not found: $FILE"

FILE_ABS="$(realpath "$FILE")"
BASENAME="$(basename "$FILE_ABS")"
EXT="${BASENAME##*.}"
NAME_HINT="${2:-}"   # optional second arg overrides detected app name

# ── Detect app name ───────────────────────────────────────────────────────────
detect_name() {
    local raw="$1"
    # Strip common suffixes: version numbers, arch, extension
    echo "$raw" | sed -E \
        's/\.(tar\.(gz|xz|bz2|zst)|tgz|rpm|deb|flatpak|AppImage|appimage|zip)$//' |
        sed -E 's/[-_][0-9]+(\.[0-9]+)*[-_]?(x86_64|amd64|arm64|linux)?$//' |
        sed -E 's/[-_](setup|installer|linux|amd64|x86_64)//gI' |
        tr '[:upper:]' '[:lower:]' |
        tr -s '-_' '-'
}

APP_NAME="${NAME_HINT:-$(detect_name "$BASENAME")}"
INSTALL_DIR="/opt/$APP_NAME"

# ── Desktop entry helper ──────────────────────────────────────────────────────
create_desktop_entry() {
    local name="$1"
    local exec_path="$2"
    local icon_path="${3:-}"
    local desktop_file="$HOME/.local/share/applications/${name}.desktop"

    mkdir -p "$HOME/.local/share/applications"

    cat > "$desktop_file" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=$(echo "$name" | sed 's/-/ /g' | sed 's/\b./\u&/g')
Exec=$exec_path
Icon=${icon_path:-application-x-executable}
Terminal=false
Categories=Utility;
EOF

    chmod +x "$desktop_file"
    # Notify GNOME Shell to pick it up
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
    success "Desktop entry created: $desktop_file"
}

# Find the main executable inside an extracted dir
find_executable() {
    local dir="$1"
    local name="$2"

    # Exact name match first
    local bin
    bin=$(find "$dir" -maxdepth 3 -type f -iname "$name" 2>/dev/null | head -1)
    [[ -n "$bin" ]] && { echo "$bin"; return; }

    # Any executable in bin/ subdir
    bin=$(find "$dir/bin" -maxdepth 1 -type f -executable 2>/dev/null | head -1)
    [[ -n "$bin" ]] && { echo "$bin"; return; }

    # Any executable file at top level or one level deep
    bin=$(find "$dir" -maxdepth 2 -type f -executable ! -name "*.so*" ! -name "*.py" 2>/dev/null | head -1)
    [[ -n "$bin" ]] && { echo "$bin"; return; }

    echo ""
}

# Find icon inside extracted dir
find_icon() {
    local dir="$1"
    find "$dir" -maxdepth 4 \( -iname "*.png" -o -iname "*.svg" \) 2>/dev/null | head -1
}

# ── Installers ────────────────────────────────────────────────────────────────

install_rpm() {
    info "Detected: RPM package"
    sudo dnf install -y "$FILE_ABS"
    success "Installed via DNF: $APP_NAME"
    # DNF handles .desktop entries automatically
}

install_deb() {
    info "Detected: DEB package — converting with alien"
    if ! command -v alien &>/dev/null; then
        warn "alien not found. Installing..."
        sudo dnf install -y alien
    fi
    local rpm_out
    rpm_out=$(sudo alien --to-rpm --scripts "$FILE_ABS" 2>&1 | grep -oP '\S+\.rpm' | tail -1)
    [[ -z "$rpm_out" ]] && error "alien conversion failed."
    sudo dnf install -y "$rpm_out"
    success "DEB converted and installed: $APP_NAME"
}

install_flatpak() {
    info "Detected: Flatpak bundle"
    flatpak install --user -y "$FILE_ABS"
    success "Flatpak installed: $APP_NAME"
}

install_appimage() {
    info "Detected: AppImage"
    local dest="$HOME/.local/bin/${APP_NAME}.AppImage"
    mkdir -p "$HOME/.local/bin"
    cp "$FILE_ABS" "$dest"
    chmod +x "$dest"

    # Try to extract icon from AppImage
    local icon=""
    local tmp_dir
    tmp_dir=$(mktemp -d)
    if "$dest" --appimage-extract &>/dev/null; then
        icon=$(find squashfs-root -maxdepth 3 \( -iname "*.png" -o -iname "*.svg" \) 2>/dev/null | head -1)
        [[ -n "$icon" ]] && cp "$icon" "$HOME/.local/share/icons/${APP_NAME}.${icon##*.}" 2>/dev/null || true
        rm -rf squashfs-root
    fi
    rm -rf "$tmp_dir"

    create_desktop_entry "$APP_NAME" "$dest" "$HOME/.local/share/icons/${APP_NAME}.png"
    success "AppImage installed: $dest"
}

install_tarball() {
    local compression="$1"
    info "Detected: Tarball ($compression)"

    # Peek inside to check for single top-level dir
    local top_dir
    top_dir=$(tar -t${compression}f "$FILE_ABS" 2>/dev/null | head -1 | cut -d/ -f1)

    local tmp_dir
    tmp_dir=$(mktemp -d)
    info "Extracting to $tmp_dir ..."
    tar -x${compression}f "$FILE_ABS" -C "$tmp_dir"

    local src_dir="$tmp_dir/$top_dir"
    [[ ! -d "$src_dir" ]] && src_dir="$tmp_dir"

    # Check for bundled installer scripts first
    for script in install.sh INSTALL.sh setup.sh; do
        if [[ -f "$src_dir/$script" ]]; then
            info "Found bundled installer: $script — running it"
            chmod +x "$src_dir/$script"
            (cd "$src_dir" && sudo ./"$script")
            rm -rf "$tmp_dir"
            success "Installed via bundled script."
            return
        fi
    done

    # Move to /opt
    if [[ -d "$INSTALL_DIR" ]]; then
        warn "$INSTALL_DIR already exists. Removing old version..."
        sudo rm -rf "$INSTALL_DIR"
    fi
    sudo mv "$src_dir" "$INSTALL_DIR"
    sudo chown -R root:root "$INSTALL_DIR"
    rm -rf "$tmp_dir"

    # Find executable and icon
    local exec_bin icon_file
    exec_bin=$(find_executable "$INSTALL_DIR" "$APP_NAME")
    icon_file=$(find_icon "$INSTALL_DIR")

    if [[ -z "$exec_bin" ]]; then
        warn "No executable found in $INSTALL_DIR. You may need to set it manually."
        warn "Desktop entry will point to $INSTALL_DIR — edit it if needed."
        exec_bin="$INSTALL_DIR"
    else
        sudo chmod +x "$exec_bin"
        # Symlink to /usr/local/bin so it's in PATH
        sudo ln -sf "$exec_bin" "/usr/local/bin/$APP_NAME"
        success "Symlinked: /usr/local/bin/$APP_NAME → $exec_bin"
    fi

    create_desktop_entry "$APP_NAME" "$exec_bin" "${icon_file:-}"
    success "Tarball installed to $INSTALL_DIR"
}

install_zip() {
    info "Detected: ZIP archive"
    if ! command -v unzip &>/dev/null; then
        sudo dnf install -y unzip
    fi

    local tmp_dir
    tmp_dir=$(mktemp -d)
    unzip -q "$FILE_ABS" -d "$tmp_dir"

    # Same logic as tarball
    local src_dir="$tmp_dir"
    local inner
    inner=$(ls "$tmp_dir" | head -1)
    [[ -d "$tmp_dir/$inner" ]] && src_dir="$tmp_dir/$inner"

    for script in install.sh INSTALL.sh setup.sh; do
        if [[ -f "$src_dir/$script" ]]; then
            info "Found bundled installer: $script — running it"
            chmod +x "$src_dir/$script"
            (cd "$src_dir" && sudo ./"$script")
            rm -rf "$tmp_dir"
            success "Installed via bundled script."
            return
        fi
    done

    [[ -d "$INSTALL_DIR" ]] && sudo rm -rf "$INSTALL_DIR"
    sudo mv "$src_dir" "$INSTALL_DIR"
    sudo chown -R root:root "$INSTALL_DIR"
    rm -rf "$tmp_dir"

    local exec_bin icon_file
    exec_bin=$(find_executable "$INSTALL_DIR" "$APP_NAME")
    icon_file=$(find_icon "$INSTALL_DIR")

    [[ -n "$exec_bin" ]] && sudo chmod +x "$exec_bin" && \
        sudo ln -sf "$exec_bin" "/usr/local/bin/$APP_NAME"

    create_desktop_entry "$APP_NAME" "${exec_bin:-$INSTALL_DIR}" "${icon_file:-}"
    success "ZIP installed to $INSTALL_DIR"
}

# ── Main dispatch ─────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}smart-install.sh${RESET} — installing ${CYAN}$APP_NAME${RESET}"
echo -e "  File : $FILE_ABS"
echo -e "  Type : .$EXT"
echo ""

# Full filename-based detection (handles double extensions like .tar.gz)
case "$FILE_ABS" in
    *.rpm)                install_rpm ;;
    *.deb)                install_deb ;;
    *.flatpak)            install_flatpak ;;
    *.AppImage|*.appimage) install_appimage ;;
    *.tar.gz|*.tgz)      install_tarball z ;;
    *.tar.xz)             install_tarball J ;;
    *.tar.bz2)            install_tarball j ;;
    *.tar.zst)            install_tarball '--zstd' ;;
    *.tar)                install_tarball '' ;;
    *.zip)                install_zip ;;
    *)
        # Last resort: check MIME type
        MIME=$(file --mime-type -b "$FILE_ABS")
        case "$MIME" in
            application/x-rpm)         install_rpm ;;
            application/vnd.flatpak*)  install_flatpak ;;
            application/x-executable|application/x-elf) install_appimage ;;
            application/gzip)          install_tarball z ;;
            application/x-xz)         install_tarball J ;;
            application/x-bzip2)      install_tarball j ;;
            application/zip)           install_zip ;;
            *)
                error "Unsupported file type: $MIME ($BASENAME)\nSupported: .rpm .deb .flatpak .AppImage .tar.gz .tar.xz .tar.bz2 .tar.zst .zip"
                ;;
        esac
        ;;
esac

echo ""
success "Done! '$APP_NAME' should now appear in your app launcher."
echo ""
