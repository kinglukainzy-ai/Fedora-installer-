# Fedora Installer 🐧📦

A universal GUI installer for Fedora/GNOME. Drop any installer file — it handles the rest.

## What it installs

| File type | How |
|---|---|
| `.rpm` | `dnf install` |
| `.deb` | Converted via `alien` → dnf |
| `.flatpak` | `flatpak install --user` |
| `.AppImage` | Copied to `~/.local/bin`, made executable |
| `.tar.gz / .tar.xz / .zip / etc.` | Extracted to `/opt/appname`, symlinked, desktop entry created |

For archives it also:
- Runs a bundled `install.sh` if one exists
- Auto-detects the main executable
- Creates a `.desktop` entry so the app appears in GNOME's launcher
- Refreshes GNOME's icon cache automatically

## Install

```bash
git clone https://github.com/kinglukainzy-ai/Fedora-installer-
cd Fedora-installer-
chmod +x smart-install-setup.sh
bash smart-install-setup.sh
```

## Usage

**GUI:**
```bash
fedora-installer
```
Open the app, drag a file onto the window (or browse), hit **Install**.

**CLI:**
```bash
smart-install.sh ~/Downloads/someapp.tar.gz
smart-install.sh ~/Downloads/thing.AppImage myapp  # override app name
```

**Right-click in Files:**  
Right-click any installer file → Scripts → Smart Install

## Requirements

- Fedora 38+ with GNOME
- Python 3.11+, GTK4, libadwaita, python3-gobject (setup script installs these)
- `alien` (optional, for `.deb` support)

## License

MIT
