#!/usr/bin/env bash
# Fedora Installer — Universal app installer for Fedora/GNOME
# Copyright (C) 2026 Lukeman Nana Yaw Quansah <kinglukainzy@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

# smart-install.sh — CLI installer (no GUI required)
# Usage:
#   smart-install.sh /path/to/file.rpm
#   smart-install.sh /path/to/app.tar.gz myapp
#   smart-install.sh /path/to/app.deb --deb-method alien
#   smart-install.sh /path/to/app.deb --deb-method distrobox
set -euo pipefail

FILE="${1:-}"
APP_NAME_OVERRIDE="${2:-}"
DEB_METHOD=""

# Parse --deb-method flag (can appear anywhere after the file)
for arg in "$@"; do
    case "$arg" in
        --deb-method=*) DEB_METHOD="${arg#*=}" ;;
        --deb-method)   : ;;  # value handled below
    esac
done
# Handle "--deb-method alien" (space-separated)
for i in "${!@}"; do
    if [[ "${!i}" == "--deb-method" ]]; then
        next=$((i+1))
        DEB_METHOD="${!next:-}"
    fi
done 2>/dev/null || true

# Fall back to saved config if no flag
if [[ -z "$DEB_METHOD" ]]; then
    CONFIG_FILE="$HOME/.config/fedora-installer/config.json"
    if [[ -f "$CONFIG_FILE" ]]; then
        DEB_METHOD=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('deb_method','distrobox'))" "$CONFIG_FILE" 2>/dev/null || echo "distrobox")
    else
        DEB_METHOD="distrobox"
    fi
fi

if [[ -z "$FILE" ]]; then
    echo "Usage: smart-install.sh <file> [app-name] [--deb-method distrobox|alien]"
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

echo "File:     $BASENAME"
echo "App name: $APP_NAME"
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

echo "Type: $TYPE"

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

write_receipt() {
    local app="$1" type="$2" install_dir="${3:-null}" symlink="${4:-null}" desktop_entry="${5:-null}" icon="${6:-null}" pkg_name="${7:-null}"
    local receipts_dir="$HOME/.local/share/fedora-installer/receipts"
    mkdir -p "$receipts_dir"
    local receipt_file="$receipts_dir/${app,,}.json"
    receipt_file="${receipt_file// /-}"
    
    json_val() {
        if [[ "$1" == "null" || -z "$1" ]]; then
            echo "null"
        else
            echo "\"$1\""
        fi
    }
    
    cat > "$receipt_file" << EOF
{
  "app_name": "$app",
  "install_type": "$type",
  "installed_at": "$(date -Iseconds)",
  "paths": {
    "install_dir": $(json_val "$install_dir"),
    "symlink": $(json_val "$symlink"),
    "desktop_entry": $(json_val "$desktop_entry"),
    "icon": $(json_val "$icon")
  },
  "package_name": $(json_val "$pkg_name")
}
EOF
}

case "$TYPE" in
    rpm)
        echo "▸ Installing via dnf…"
        PKG_NAME=$(rpm -qp --qf "%{NAME}" "$FILE" 2>/dev/null || true)
        sudo dnf install -y "$FILE"
        echo "✅ RPM installed."
        write_receipt "$APP_NAME" "rpm" "null" "null" "null" "null" "$PKG_NAME"
        ;;

    deb)
        install_deb_distrobox() {
            echo "▸ Installing .deb via distrobox…"
            if ! command -v distrobox &>/dev/null; then
                echo "▸ distrobox not found — installing…"
                sudo dnf install -y distrobox podman
            elif ! command -v podman &>/dev/null && ! command -v docker &>/dev/null; then
                echo "▸ podman not found — installing…"
                sudo dnf install -y podman
            fi
            local CONTAINER_NAME="fedora-installer-debian"
            if ! distrobox list 2>/dev/null | grep -q "$CONTAINER_NAME"; then
                echo "▸ Creating Debian container '$CONTAINER_NAME' (first time only)…"
                distrobox create --name "$CONTAINER_NAME" \
                    --image quay.io/toolbx-images/debian-toolbox:testing --yes
            fi
            echo "▸ Installing $BASENAME inside container…"
            distrobox enter "$CONTAINER_NAME" -- bash -c "
                sudo apt-get update -qq &&
                sudo apt-get install -y \'$FILE\' 2>&1
            "
            echo "▸ Exporting app to host launcher…"
            distrobox enter "$CONTAINER_NAME" -- bash -c "
                distrobox-export --app \'$APP_NAME\' 2>/dev/null || true
            "
            echo "✅ .deb installed via distrobox."
        }

        install_deb_alien() {
            echo "▸ Installing .deb via alien…"
            if ! command -v alien &>/dev/null; then
                echo "▸ alien not found — installing…"
                sudo dnf install -y alien
            fi
            local TMP_DIR
            TMP_DIR=$(mktemp -d --tmpdir=/var/tmp)
            (cd "$TMP_DIR" && alien --to-rpm --scripts "$FILE")
            local rpm_files=("$TMP_DIR"/*.rpm)
            if [[ ! -e "${rpm_files[0]}" ]]; then
                rm -rf "$TMP_DIR"
                echo "❌ alien did not produce an .rpm file."
                return 1
            fi
            local rpm_path="${rpm_files[0]}"
            echo "▸ Installing converted RPM: $rpm_path"
            sudo dnf install -y "$rpm_path"
            rm -rf "$TMP_DIR"
            echo "✅ .deb converted and installed via alien."
        }

        if [[ "$DEB_METHOD" == "alien" ]]; then
            if ! install_deb_alien; then
                echo "⚠️  alien failed — retrying with distrobox…"
                install_deb_distrobox
            fi
        else
            install_deb_distrobox
        fi
        write_receipt "$APP_NAME" "deb" "null" "null" "null" "null" "$APP_NAME"
        ;;

    flatpak)
        echo "▸ Installing Flatpak bundle…"
        PKG_NAME=$(flatpak info --show-metadata "$FILE" 2>/dev/null | grep -E '^name=' | cut -d= -f2 || true)
        flatpak install --user --noninteractive "$FILE"
        echo "✅ Flatpak installed."
        write_receipt "$APP_NAME" "flatpak" "null" "null" "null" "null" "$PKG_NAME"
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
        TMP_DIR=$(mktemp -d --tmpdir=/var/tmp)
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
        
        clean_name="${APP_NAME,,}"
        clean_name="${clean_name// /-}"
        write_receipt "$APP_NAME" "appimage" "$DEST" "null" "$HOME/.local/share/applications/${clean_name}.desktop" "$icon_path" "null"
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
        symlink_path=""
        desktop_path=""
        icon_path=""
        if [[ -f "$INSTALL_SH" ]]; then
            echo "▸ Found install.sh — running…"
            (cd "$INSTALL_DIR" && sudo bash install.sh)
        else
            EXE=$(find_executable "$INSTALL_DIR")
            if [[ -n "$EXE" ]]; then
                sudo ln -sf "$EXE" "/usr/local/bin/$APP_NAME"
                echo "▸ Symlinked: /usr/local/bin/$APP_NAME → $EXE"
                symlink_path="/usr/local/bin/$APP_NAME"
            else
                EXE="$INSTALL_DIR"
            fi
            
            ICON=$(find_icon "$INSTALL_DIR")
            icon_path="$ICON"
            create_desktop_entry "$APP_NAME" "$EXE" "$ICON"
            clean_name="${APP_NAME,,}"
            clean_name="${clean_name// /-}"
            desktop_path="$HOME/.local/share/applications/${clean_name}.desktop"
        fi
        echo "✅ Archive installed."
        write_receipt "$APP_NAME" "$TYPE" "$INSTALL_DIR" "$symlink_path" "$desktop_path" "$icon_path" "null"
        ;;
esac

# ── refresh GNOME ─────────────────────────────────────────────────────────────
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
echo "Desktop database updated. On Wayland, log out and back in for the launcher icon to appear."

# ── success notification ──────────────────────────────────────────────────────
notify_success "$APP_NAME"
trap - EXIT   # clear the error trap — we finished cleanly
