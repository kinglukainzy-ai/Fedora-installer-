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
        --icon="fedora-installer" \
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
    APP_NAME="$(echo "$BASENAME" | sed -E 's/\.(tar\.gz|tar\.xz|tar\.bz2|tar\.zst|tgz|rpm|deb|flatpak|appimage|zip|tar)$//I')"
    # strip version-like suffixes: -1.2.3, _x86_64, -linux, etc.
    APP_NAME="$(echo "$APP_NAME" | sed -E 's/[-_](v?[0-9][0-9._-]*).*$//I' | sed -E 's/[-_](x86_64|amd64|arm64|linux|fedora).*$//I')"
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
# shellcheck disable=SC2154  # rc is assigned inside the trap via rc=$?
trap 'rc=$?; if [[ $rc -ne 0 ]]; then notify_failure "${APP_NAME:-}" "Something went wrong. Check the terminal for details."; fi; if [[ -n "${TMP_DIR:-}" && -d "$TMP_DIR" ]]; then rm -rf "$TMP_DIR"; fi' EXIT

find_icon() {
    local dir="$1"
    # Find all png, svg, xpm files up to depth 8
    # We want to prefer hicolor, pixmaps, icons directories, then fall back to best
    local best=""
    while IFS= read -r -d '' file; do
        # check if path contains hicolor, pixmaps, or icons
        if [[ "$file" =~ /(hicolor|pixmaps|icons)/ ]]; then
            echo "$file"
            return 0
        fi
        if [[ -z "$best" ]]; then
            best="$file"
        fi
    done < <(find "$dir" -maxdepth 8 -type f \( -iname "*.png" -o -iname "*.svg" -o -iname "*.xpm" \) -print0 2>/dev/null)
    
    echo "$best"
}

find_executable() {
    local dir="$1"
    local app_base
    app_base=$(basename "$dir" | tr '[:upper:]' '[:lower:]')
    local best_exe=""
    local best_score=-9999

    # We want to walk the directory
    # Exclude files matching: (install|setup|uninstall|uninst|configure|config|postinst|prerm)(\.sh)?$ (case-insensitive)
    while IFS= read -r -d '' file; do
        local fname
        fname=$(basename "$file")
        local fname_lower
        fname_lower=$(echo "$fname" | tr '[:upper:]' '[:lower:]')
        
        # Skip if matching exclusions
        if [[ "$fname_lower" =~ ^(install|setup|uninstall|uninst|configure|config|postinst|prerm)(\.sh)?$ ]]; then
            continue
        fi
        
        # Skip if it is a shared library (.so, .so.1, etc.)
        if [[ "$fname_lower" =~ \.so(\.[0-9]+)*$ ]]; then
            continue
        fi

        # Check if file is executable
        if [[ -x "$file" && -f "$file" ]]; then
            local score=0
            local app_base_no_hyphen="${app_base//-/}"
            local app_base_no_under="${app_base//_/}"

            # 1. Name matching
            if [[ "$fname_lower" == "$app_base" || "$fname_lower" == "$app_base_no_hyphen" || "$fname_lower" == "$app_base_no_under" ]]; then
                score=$((score + 100))
            elif [[ "$fname_lower" == *"$app_base"* ]]; then
                score=$((score + 50))
            fi
            
            # 2. Location
            if [[ "$file" =~ /bin/ ]]; then
                score=$((score + 30))
            fi
            
            # 3. Depth penalty
            local rel_path="${file#"$dir"}"
            local slash_count
            local temp="${rel_path//[^\/]/}"
            slash_count="${#temp}"
            local depth=$((slash_count - 1))
            if (( depth < 0 )); then depth=0; fi
            score=$((score - (depth * 2)))
            
            # 4. Prefer binaries over shell script wrappers
            if [[ "$fname_lower" != *.sh ]]; then
                score=$((score + 5))
            fi
            
            if (( score > best_score )); then
                best_score=$score
                best_exe="$file"
            fi
        fi
    done < <(find "$dir" -type f -print0 2>/dev/null)

    echo "$best_exe"
}

create_desktop_entry() {
    local name="$1" exec="$2" icon="${3:-}"
    local desktop_dir="$HOME/.local/share/applications"
    mkdir -p "$desktop_dir"
    local icon_line="Icon=application-x-executable"
    [[ -n "$icon" ]] && icon_line="Icon=$icon"
    local clean_name="${name,,}"
    clean_name="${clean_name// /-}"
    cat > "$desktop_dir/${clean_name}.desktop" << EOF
[Desktop Entry]
Name=$name
Exec=$exec
$icon_line
Type=Application
Categories=Utility;
Terminal=false
EOF
    chmod 755 "$desktop_dir/${clean_name}.desktop"
    update-desktop-database "$desktop_dir" 2>/dev/null || true
    echo "✔ Desktop entry: $desktop_dir/${clean_name}.desktop"
}

case "$TYPE" in
    rpm)
        echo "▸ Installing via dnf…"
        sudo dnf install -y "$FILE"
        echo "✅ RPM installed."
        ;;

    deb)
        echo "▸ Converting .deb → .rpm via alien…"
        TMP_DIR=$(mktemp -d)
        
        # Run alien with cwd in TMP_DIR
        (cd "$TMP_DIR" && alien --to-rpm --scripts "$FILE")
        
        # Check if alien produced an .rpm file in TMP_DIR
        rpm_files=("$TMP_DIR"/*.rpm)
        if [[ ! -e "${rpm_files[0]}" ]]; then
            rm -rf "$TMP_DIR"
            echo "❌ alien did not produce an .rpm file."
            exit 1
        fi
        
        rpm_path="${rpm_files[0]}"
        echo "▸ Installing converted RPM: $rpm_path via dnf…"
        sudo dnf install -y "$rpm_path"
        
        rm -rf "$TMP_DIR"
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

        # Try to extract icon from AppImage
        icon_path=""
        TMP_DIR=$(mktemp -d)
        # Execute --appimage-extract inside TMP_DIR to avoid junk files in CWD
        if (cd "$TMP_DIR" && "$DEST" --appimage-extract &>/dev/null); then
            raw_icon=$(find_icon "$TMP_DIR/squashfs-root")
            if [[ -n "$raw_icon" ]]; then
                # Get the extension of the icon
                ext="${raw_icon##*.}"
                icon_dest_dir="$HOME/.local/share/icons"
                mkdir -p "$icon_dest_dir"
                icon_dest="$icon_dest_dir/$APP_NAME.$ext"
                cp "$raw_icon" "$icon_dest"
                icon_path="$icon_dest"
                echo "▸ Icon extracted: $icon_dest"
            fi
        else
            echo "⚠️  Could not extract icon from AppImage (non-fatal)"
        fi
        rm -rf "$TMP_DIR"

        create_desktop_entry "$APP_NAME" "$DEST" "$icon_path"
        echo "✅ AppImage installed."
        ;;

    tarball|zip)
        # Pre-flight disk space check
        archive_size=$(stat -c%s "$FILE")
        free_tmp=$(df --output=avail -B1 /var/tmp | tail -1)
        free_opt=$(df --output=avail -B1 /opt | tail -1)
        needed=$(( archive_size * 3 ))
        if (( free_tmp < needed || free_opt < needed )); then
            echo "❌ Not enough disk space."
            echo "   Need ~$(( needed / 1024 / 1024 )) MB free in both /var/tmp and /opt."
            echo "   /var/tmp has $(( free_tmp / 1024 / 1024 )) MB, /opt has $(( free_opt / 1024 / 1024 )) MB."
            exit 1
        fi

        # Extract to /var/tmp (real disk) instead of /tmp (tmpfs, RAM-limited)
        TMP_DIR=$(mktemp -d --tmpdir=/var/tmp)
        echo "▸ Extracting archive to temporary directory…"
        
        if [[ "$TYPE" == "tarball" ]]; then
            tar -xf "$FILE" -C "$TMP_DIR"
        else
            unzip -o "$FILE" -d "$TMP_DIR" >/dev/null
        fi

        # Determine the real content root
        # Check if there is exactly one item in TMP_DIR and it is a directory
        top_items=("$TMP_DIR"/*)
        # Handle cases where glob didn't match anything
        if [[ -e "${top_items[0]}" ]]; then
            if [[ ${#top_items[@]} -eq 1 && -d "${top_items[0]}" ]]; then
                CONTENT_ROOT="${top_items[0]}"
            else
                CONTENT_ROOT="$TMP_DIR"
            fi
        else
            CONTENT_ROOT="$TMP_DIR"
        fi

        INSTALL_DIR="/opt/$APP_NAME"
        echo "▸ Installing to $INSTALL_DIR…"
        sudo mkdir -p "$INSTALL_DIR"

        # Copy content_root to install_dir
        # We need to copy hidden files too, so we use CONTENT_ROOT/.
        # Since /opt/$APP_NAME is owned by root, we use sudo cp -a.
        sudo cp -a "$CONTENT_ROOT/." "$INSTALL_DIR/"

        # Clean up temp dir
        rm -rf "$TMP_DIR"

        # Look for bundled install.sh
        INSTALL_SH="$INSTALL_DIR/install.sh"
        if [[ -f "$INSTALL_SH" ]]; then
            echo "▸ Found install.sh — running…"
            (cd "$INSTALL_DIR" && sudo bash install.sh)
        else
            EXE=$(find_executable "$INSTALL_DIR")
            if [[ -n "$EXE" ]]; then
                sudo ln -sf "$EXE" "/usr/local/bin/$APP_NAME"
                echo "▸ Symlinked: /usr/local/bin/$APP_NAME → $EXE"
            else
                EXE="$INSTALL_DIR"
            fi
            
            ICON=$(find_icon "$INSTALL_DIR")
            create_desktop_entry "$APP_NAME" "$EXE" "$ICON"
        fi
        echo "✅ Archive installed."
        ;;
esac

# ── refresh GNOME ─────────────────────────────────────────────────────────────
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "🔄 Desktop database updated. On Wayland, log out and back in for the launcher icon to appear."

# ── success notification ──────────────────────────────────────────────────────
notify_success "$APP_NAME"
trap - EXIT   # clear the error trap — we finished cleanly
