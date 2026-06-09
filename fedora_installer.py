#!/usr/bin/env python3
"""
Fedora Installer — universal GUI installer for .rpm, .deb, .flatpak,
.AppImage, .tar.*, and .zip files on Fedora/GNOME.
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, Adw, Gio, GLib, Gdk
import subprocess
import threading
import os
import sys
import re
import signal
import urllib.request

APP_ID = "io.github.kinglukainzy_ai.FedoraInstaller"
VERSION_FILE = "/usr/local/lib/fedora-installer/VERSION"
try:
    with open(VERSION_FILE) as _vf:
        VERSION = _vf.read().strip()
except OSError:
    VERSION = "0.0.0"

VERSION_URL = (
    "https://raw.githubusercontent.com/kinglukainzy-ai/Fedora-installer-/main/VERSION"
)


def _log_output(text: str, log, max_lines: int = 20):
    """Log subprocess output, collapsing excessive lines into a summary."""
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        for l in lines[:max_lines]:
            log(l)
        log(f"… ({len(lines) - max_lines} more lines suppressed)")
    else:
        for l in lines:
            log(l)


class CancelledError(Exception):
    """Raised when the user cancels an in-progress installation."""


class CancelToken:
    """Thread-safe cancellation token that can kill the active subprocess."""

    def __init__(self):
        self._cancelled = False
        self._lock = threading.Lock()
        self._active_proc: subprocess.Popen | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self):
        """Set the cancel flag and kill any running subprocess."""
        with self._lock:
            self._cancelled = True
            proc = self._active_proc
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass

    def check(self):
        """Raise CancelledError if cancellation was requested."""
        if self._cancelled:
            raise CancelledError("Installation cancelled by user.")

    def register(self, proc: subprocess.Popen):
        """Register a subprocess so it can be killed on cancel."""
        with self._lock:
            self._active_proc = proc
            if self._cancelled:
                try:
                    proc.kill()
                except OSError:
                    pass

    def unregister(self):
        with self._lock:
            self._active_proc = None


def cancellable_run(cmd: list[str], token: CancelToken | None = None, **kwargs):
    """Run a subprocess, killing it if the token is cancelled.

    Uses Popen internally so the process can be terminated mid-flight.
    Returns a CompletedProcess-like object.
    """
    if token:
        token.check()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if token:
        token.register(proc)
    try:
        stdout, stderr = proc.communicate()
    finally:
        if token:
            token.unregister()
    if token:
        token.check()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


# ─────────────────────────── backend installer ───────────────────────────────

def detect_type(path: str) -> str:
    name = os.path.basename(path).lower()
    if name.endswith(".rpm"):      return "rpm"
    if name.endswith(".deb"):      return "deb"
    if name.endswith(".flatpak"):  return "flatpak"
    if name.endswith(".appimage"): return "appimage"
    for ext in (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tar"):
        if name.endswith(ext):     return "tarball"
    if name.endswith(".zip"):      return "zip"
    return "unknown"


def app_name_from_path(path: str) -> str:
    """Derive a clean app name from the file path."""
    base = os.path.basename(path)
    # strip all known extensions
    for ext in (".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tgz",
                ".rpm", ".deb", ".flatpak", ".appimage", ".zip", ".tar"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    # strip version-like suffixes: -1.2.3, _x86_64, -linux, etc.
    base = re.sub(r"[-_](v?\d[\d.\-]*).*$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[-_](x86_64|amd64|arm64|linux|fedora).*$", "", base, flags=re.IGNORECASE)
    return base or "app"


def create_desktop_entry(app_name: str, exec_path: str, icon_path: str | None, log):
    """Write a .desktop file so the app appears in GNOME's launcher."""
    desktop_dir = os.path.expanduser("~/.local/share/applications")
    os.makedirs(desktop_dir, exist_ok=True)
    icon_line = f"Icon={icon_path}" if icon_path else "Icon=application-x-executable"
    entry = f"""[Desktop Entry]
Name={app_name}
Exec={exec_path}
{icon_line}
Type=Application
Categories=Utility;
Terminal=false
"""
    desktop_file = os.path.join(desktop_dir, f"{app_name.lower().replace(' ', '-')}.desktop")
    with open(desktop_file, "w") as f:
        f.write(entry)
    os.chmod(desktop_file, 0o755)
    subprocess.run(["update-desktop-database", desktop_dir], capture_output=True)
    log(f"✔ Desktop entry created: {desktop_file}")


def find_icon(directory: str) -> str | None:
    # Fix #7: increased maxdepth equivalent — os.walk is unbounded, but we
    # now prefer hicolor/256x256 paths and fall back to any .png/.svg/.xpm
    best = None
    for root, _, files in os.walk(directory):
        depth = root[len(directory):].count(os.sep)
        if depth > 8:
            continue
        for f in files:
            if f.lower().endswith((".png", ".svg", ".xpm")):
                full = os.path.join(root, f)
                # prefer icons in standard hicolor or pixmaps directories
                if any(p in root for p in ("hicolor", "pixmaps", "icons")):
                    return full
                if best is None:
                    best = full
    return best


# Fix #6: excluded installer/setup/uninstall/config scripts from executable search
_EXEC_EXCLUDE = re.compile(
    r"(install|setup|uninstall|uninst|configure|config|postinst|prerm)(\.sh)?$",
    re.IGNORECASE,
)

def find_executable(directory: str) -> str | None:
    """Find the most likely main executable in an extracted directory."""
    app_base = os.path.basename(directory).lower()
    candidates = []
    for root, _, files in os.walk(directory):
        depth = root[len(directory):].count(os.sep)
        for f in files:
            # Fix #6: skip installer/setup/uninstall/config scripts
            if _EXEC_EXCLUDE.match(f):
                continue
            full = os.path.join(root, f)
            if os.access(full, os.X_OK) and not re.search(r"\.so(\.\d+)*$", f):
                score = 0
                fname = f.lower()
                
                # 1. Name matching
                if fname in (app_base, app_base.replace("-", ""), app_base.replace("_", "")):
                    score += 100
                elif app_base in fname:
                    score += 50
                
                # 2. Location
                if "bin" in root.split(os.sep):
                    score += 30
                
                # 3. Depth penalty
                score -= depth * 2
                
                # 4. Prefer binaries over shell script wrappers
                if not fname.endswith(".sh"):
                    score += 5
                
                candidates.append((score, full))
    if candidates:
        return sorted(candidates, key=lambda x: -x[0])[0][1]
    return None


def install_file(path: str, app_name_override: str | None, log,
                 sudo_password: str | None = None,
                 cancel_token: CancelToken | None = None):
    """
    Core installer. Calls log(str) for progress. Raises on fatal error.
    sudo_password: if provided, piped into sudo -S.
    cancel_token: if provided, checked between steps and used to kill subprocesses.
    """
    ftype = detect_type(path)
    app_name = app_name_override or app_name_from_path(path)
    # Sanitize: replace whitespace with hyphens for safe filesystem paths
    app_name = re.sub(r'\s+', '-', app_name)
    log(f"📦 File type detected: {ftype}")
    log(f"🏷  App name: {app_name}")

    def sudo_run(cmd: list[str], **kwargs):
        if cancel_token:
            cancel_token.check()
        if sudo_password:
            # sudo_run uses Popen directly to support cancellation
            full_cmd = ["sudo", "-S"] + cmd
            proc = subprocess.Popen(
                full_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
            )
            if cancel_token:
                cancel_token.register(proc)
            try:
                stdout, stderr = proc.communicate(input=(sudo_password + "\n").encode())
            finally:
                if cancel_token:
                    cancel_token.unregister()
            return subprocess.CompletedProcess(full_cmd, proc.returncode, stdout, stderr)
        else:
            return cancellable_run(["sudo"] + cmd, token=cancel_token, **kwargs)
        return proc

    # ── RPM ──────────────────────────────────────────────────────────────────
    if ftype == "rpm":
        log("⚙️  Installing via dnf…")
        proc = sudo_run(["dnf", "install", "-y", path])
        _log_output(proc.stdout.decode(errors="replace"), log)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        if cancel_token:
            cancel_token.check()
        log("✅ RPM installed.")

    # ── DEB ──────────────────────────────────────────────────────────────────
    elif ftype == "deb":
        import tempfile, shutil
        log("⚙️  Converting .deb → .rpm via alien…")
        # Fix #4: use a temp dir so we never pollute CWD or fail on read-only dirs
        with tempfile.TemporaryDirectory() as tmpdir:
            conv = cancellable_run(
                ["alien", "--to-rpm", "--scripts", path],
                token=cancel_token,
                cwd=tmpdir,
            )
            _log_output(conv.stdout.decode(errors="replace"), log)
            if conv.returncode != 0:
                raise RuntimeError(
                    "alien failed (is it installed? run: sudo dnf install -y alien)\n"
                    + conv.stderr.decode(errors="replace")
                )
            rpm_files = [f for f in os.listdir(tmpdir) if f.endswith(".rpm")]
            if not rpm_files:
                raise RuntimeError("alien did not produce an .rpm file.")
            rpm_path = os.path.join(tmpdir, rpm_files[0])
            log(f"⚙️  Installing converted RPM: {rpm_path}")
            proc = sudo_run(["dnf", "install", "-y", rpm_path])
            _log_output(proc.stdout.decode(errors="replace"), log)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode(errors="replace"))
        log("✅ .deb converted and installed.")

    # ── FLATPAK ───────────────────────────────────────────────────────────────
    elif ftype == "flatpak":
        log("⚙️  Installing Flatpak bundle…")
        proc = cancellable_run(
            ["flatpak", "install", "--user", "--noninteractive", path],
            token=cancel_token,
        )
        _log_output(proc.stdout.decode(errors="replace"), log)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        if cancel_token:
            cancel_token.check()
        log("✅ Flatpak installed.")

    # ── APPIMAGE ──────────────────────────────────────────────────────────────
    elif ftype == "appimage":
        import tempfile, shutil
        dest_dir = os.path.expanduser("~/.local/bin")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{app_name}.AppImage")
        shutil.copy2(path, dest)
        os.chmod(dest, 0o755)
        log(f"⚙️  Copied to {dest}")

        # Fix #2: extract icon in a secure temp dir, not CWD
        # Fix #3: detect actual icon extension (.png or .svg) dynamically
        icon_path = None
        with tempfile.TemporaryDirectory() as tmpdir:
            extract = cancellable_run(
                [dest, "--appimage-extract"],
                token=cancel_token,
                cwd=tmpdir,
            )
            if extract.returncode == 0:
                squash = os.path.join(tmpdir, "squashfs-root")
                raw_icon = find_icon(squash)
                if raw_icon:
                    ext = os.path.splitext(raw_icon)[1]  # preserves .svg or .png
                    icon_dest = os.path.join(
                        os.path.expanduser("~/.local/share/icons"),
                        f"{app_name}{ext}",
                    )
                    os.makedirs(os.path.dirname(icon_dest), exist_ok=True)
                    shutil.copy2(raw_icon, icon_dest)
                    icon_path = icon_dest
                    log(f"⚙️  Icon extracted: {icon_dest}")
            else:
                log("⚠️  Could not extract icon from AppImage (non-fatal)")

        create_desktop_entry(app_name, dest, icon_path, log)
        log("✅ AppImage installed.")

    # ── TARBALL / ZIP ─────────────────────────────────────────────────────────
    elif ftype in ("tarball", "zip"):
        import tempfile, shutil
        log("⚙️  Extracting archive…")

        # Pre-flight disk space check — catch full disks before starting
        archive_size = os.path.getsize(path)
        stat_tmp = os.statvfs("/var/tmp")
        stat_opt = os.statvfs("/opt")
        free_tmp = stat_tmp.f_bavail * stat_tmp.f_frsize
        free_opt = stat_opt.f_bavail * stat_opt.f_frsize
        needed = archive_size * 3
        if free_tmp < needed or free_opt < needed:
            raise RuntimeError(
                f"Not enough disk space. Need ~{needed // (1024**2)} MB free in both "
                f"/var/tmp ({free_tmp // (1024**2)} MB available) and "
                f"/opt ({free_opt // (1024**2)} MB available)."
            )

        # Extract to /var/tmp (real disk) instead of /tmp (tmpfs, RAM-limited)
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmpdir:
            if ftype == "tarball":
                # Fix #8: use plain -xf — GNU tar auto-detects compression
                proc = cancellable_run(
                    ["tar", "-xf", path, "-C", tmpdir],
                    token=cancel_token,
                )
            else:
                proc = cancellable_run(
                    ["unzip", "-o", path, "-d", tmpdir],
                    token=cancel_token,
                )

            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.decode(errors="replace")[:500] or "extraction failed")

            # Fix #1: determine the real content root
            top_items = os.listdir(tmpdir)
            if len(top_items) == 1 and os.path.isdir(os.path.join(tmpdir, top_items[0])):
                content_root = os.path.join(tmpdir, top_items[0])
            else:
                content_root = tmpdir

            install_dir = f"/opt/{app_name}"
            log(f"⚙️  Installing to {install_dir}…")
            proc2 = sudo_run(["mkdir", "-p", install_dir])
            if proc2.returncode != 0:
                raise RuntimeError(f"Cannot create {install_dir}: {proc2.stderr.decode()}")

            if cancel_token:
                cancel_token.check()
            # copy content_root into install_dir
            copy_proc = cancellable_run(
                ["cp", "-a", content_root + "/.", install_dir + "/"],
                token=cancel_token,
            )
            if copy_proc.returncode != 0:
                # fallback: try with sudo
                copy_proc = sudo_run(["cp", "-a", content_root + "/.", install_dir + "/"])
                if copy_proc.returncode != 0:
                    raise RuntimeError(copy_proc.stderr.decode(errors="replace") or "copy failed")

        # look for bundled install.sh
        install_sh = os.path.join(install_dir, "install.sh")
        if os.path.exists(install_sh):
            log("⚙️  Found install.sh — running it…")
            proc_sh = cancellable_run(
                ["bash", install_sh], token=cancel_token,
                cwd=install_dir,
            )
            if proc_sh.returncode != 0:
                log(f"⚠️  install.sh exited with code {proc_sh.returncode}")
                _log_output(proc_sh.stderr.decode(errors="replace"), log)
            else:
                _log_output(proc_sh.stdout.decode(errors="replace"), log)
        else:
            exe = find_executable(install_dir)
            if exe:
                link = f"/usr/local/bin/{app_name}"
                ln_proc = sudo_run(["ln", "-sf", exe, link])
                if ln_proc.returncode != 0:
                    log(f"⚠️  Symlink failed: {ln_proc.stderr.decode(errors='replace')}")
                else:
                    log(f"⚙️  Symlinked executable → {link}")
            else:
                exe = install_dir

            icon = find_icon(install_dir)
            create_desktop_entry(app_name, exe or install_dir, icon, log)

        log("✅ Archive installed.")

    else:
        raise RuntimeError(
            f"Unrecognised file type for: {os.path.basename(path)}\n"
            "Supported: .rpm  .deb  .flatpak  .AppImage  .tar.*  .zip"
        )

    # Refresh GNOME shell icon cache
    if cancel_token:
        cancel_token.check()
    subprocess.run(
        ["update-desktop-database",
         os.path.expanduser("~/.local/share/applications")],
        capture_output=True,
    )
    log("🔄 Desktop database updated. On Wayland, log out and back in "
        "for the launcher icon to appear.")


# ─────────────────────────── GTK4 UI ─────────────────────────────────────────

class InstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Fedora Installer")
        self.set_default_size(560, 600)
        self.set_resizable(True)

        self._file_path: str | None = None
        self._installing = False
        self._cancel_token: CancelToken | None = None

        # ── root box ──────────────────────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(root)

        # ── header bar ────────────────────────────────────────────────────────
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        root.append(header)

        # ── update banner (hidden until a newer version is detected) ──────────
        self.update_banner = Adw.Banner()
        self.update_banner.set_button_label("How to update")
        self.update_banner.set_revealed(False)
        self.update_banner.connect("button-clicked", self._on_update_banner_clicked)
        root.append(self.update_banner)

        # ── content ───────────────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        root.append(scroll)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_margin_top(32)
        content.set_margin_bottom(24)
        content.set_margin_start(32)
        content.set_margin_end(32)
        scroll.set_child(content)

        # ── drop target ───────────────────────────────────────────────────────
        self.drop_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.drop_box.set_halign(Gtk.Align.FILL)
        self.drop_box.add_css_class("drop-area")
        self.drop_box.set_size_request(-1, 160)

        drop_icon = Gtk.Image.new_from_icon_name("folder-download-symbolic")
        drop_icon.set_pixel_size(48)
        drop_icon.add_css_class("dim-label")
        self.drop_box.append(drop_icon)

        self.drop_label = Gtk.Label(label="Drop your installer file here")
        self.drop_label.add_css_class("title-2")
        self.drop_box.append(self.drop_label)

        sub = Gtk.Label(label=".rpm  ·  .deb  ·  .flatpak  ·  .AppImage  ·  .tar.*  ·  .zip")
        sub.add_css_class("dim-label")
        self.drop_box.append(sub)

        # drag-and-drop
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("enter", self._on_drag_enter)
        drop_target.connect("leave", self._on_drag_leave)
        self.drop_box.add_controller(drop_target)

        content.append(self.drop_box)

        # ── browse button ─────────────────────────────────────────────────────
        browse_btn = Gtk.Button(label="Browse for file…")
        browse_btn.set_halign(Gtk.Align.CENTER)
        browse_btn.add_css_class("pill")
        browse_btn.connect("clicked", self._on_browse)
        content.append(browse_btn)

        # ── selected file row ─────────────────────────────────────────────────
        self.file_row = Adw.ActionRow()
        self.file_row.set_title("No file selected")
        self.file_row.set_subtitle("Choose or drop an installer file above")
        self.file_row.add_css_class("card")
        content.append(self.file_row)

        # ── app name entry ────────────────────────────────────────────────────
        name_group = Adw.PreferencesGroup(title="App name (optional)")
        self.name_entry = Adw.EntryRow(title="Override app name")
        name_group.add(self.name_entry)
        content.append(name_group)

        # ── sudo password entry ───────────────────────────────────────────────
        pwd_group = Adw.PreferencesGroup(title="Sudo password (if needed)")
        self.pwd_entry = Adw.PasswordEntryRow(title="Password for sudo operations")
        pwd_group.add(self.pwd_entry)
        content.append(pwd_group)

        # ── install / cancel button row ────────────────────────────────────────
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.set_halign(Gtk.Align.CENTER)
        content.append(btn_row)

        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.add_css_class("pill")
        self.install_btn.add_css_class("suggested-action")
        self.install_btn.set_sensitive(False)
        self.install_btn.connect("clicked", self._on_install)
        btn_row.append(self.install_btn)

        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.add_css_class("pill")
        self.cancel_btn.add_css_class("destructive-action")
        self.cancel_btn.set_visible(False)
        self.cancel_btn.connect("clicked", self._on_cancel)
        btn_row.append(self.cancel_btn)

        # ── progress ──────────────────────────────────────────────────────────
        self.progress = Gtk.ProgressBar()
        self.progress.set_pulse_step(0.05)
        self.progress.set_visible(False)
        content.append(self.progress)

        # ── log output ────────────────────────────────────────────────────────
        log_frame = Gtk.Frame()
        log_frame.add_css_class("card")
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_size_request(-1, 160)
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_margin_start(8)
        self.log_view.set_margin_end(8)
        self.log_view.set_margin_top(8)
        self.log_view.set_margin_bottom(8)
        self.log_buffer = self.log_view.get_buffer()
        log_scroll.set_child(self.log_view)
        log_frame.set_child(log_scroll)
        content.append(log_frame)

        # ── CSS ───────────────────────────────────────────────────────────────
        css = Gtk.CssProvider()
        css.load_from_data(b"""
.drop-area {
    border-radius: 16px;
    border: 2px dashed alpha(@accent_color, 0.4);
    background: alpha(@accent_color, 0.04);
    padding: 24px;
    transition: border-color 200ms, background 200ms;
}
.drop-area.drag-over {
    border-color: @accent_color;
    background: alpha(@accent_color, 0.10);
}
""")
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # ── background update check ───────────────────────────────────────────
        threading.Thread(target=self._check_for_update, daemon=True).start()

    # ── update check ──────────────────────────────────────────────────────────

    def _check_for_update(self):
        """Non-blocking background check — silently ignored on any failure."""
        try:
            req = urllib.request.Request(VERSION_URL, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                latest = resp.read().decode().strip()
            if latest and latest != VERSION:
                GLib.idle_add(self._show_update_banner, latest)
        except Exception:
            pass  # network down, timeout, 404 — all fine, just skip

    def _show_update_banner(self, latest: str):
        self.update_banner.set_title(
            f"A new version is available: v{latest}  (you have v{VERSION})"
        )
        self.update_banner.set_revealed(True)
        return False

    def _on_update_banner_clicked(self, banner):
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Update Fedora Installer",
            body=(
                "Run this command in a terminal:\n\n"
                "  fedora-installer --update\n\n"
                "This will download the latest version from GitHub "
                "and re-run the setup script automatically."
            ),
        )
        dialog.add_response("ok", "Got it")
        dialog.present()

    # ── drag-and-drop callbacks ───────────────────────────────────────────────

    def _on_drag_enter(self, target, x, y):
        self.drop_box.add_css_class("drag-over")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, target):
        self.drop_box.remove_css_class("drag-over")

    def _on_drop(self, target, value, x, y):
        self.drop_box.remove_css_class("drag-over")
        if isinstance(value, Gio.File):
            self._set_file(value.get_path())
        return True

    # ── browse ────────────────────────────────────────────────────────────────

    def _on_browse(self, btn):
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose installer file")
        f = Gtk.FileFilter()
        f.set_name("Installer files")
        for pat in ("*.rpm", "*.deb", "*.flatpak", "*.AppImage", "*.appimage",
                    "*.tar.gz", "*.tar.xz", "*.tar.bz2", "*.tar.zst", "*.tgz", "*.zip", "*.tar"):
            f.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dialog.set_filters(store)
        dialog.open(self, None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        try:
            f = dialog.open_finish(result)
            if f:
                self._set_file(f.get_path())
        except GLib.Error:
            pass

    # ── state helpers ─────────────────────────────────────────────────────────

    def _set_file(self, path: str):
        if not path or not os.path.isfile(path):
            self._log(f"❌ Error: Invalid or non-existent file path: {path}")
            return
        self._file_path = path
        name = os.path.basename(path)
        ftype = detect_type(path)
        self.file_row.set_title(name)
        try:
            size_kb = os.path.getsize(path) // 1024
            size_str = f"  ·  {size_kb} KB"
        except OSError:
            size_str = ""
        self.file_row.set_subtitle(f"Type: {ftype}{size_str}")
        self.drop_label.set_text(name)
        suggested = app_name_from_path(path)
        self.name_entry.set_text(suggested)
        self.install_btn.set_sensitive(True)
        self._log(f"Selected: {path}")

    def _log(self, text: str):
        def _append():
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end, text.strip() + "\n")
            # scroll to bottom
            adj = self.log_view.get_parent().get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
        GLib.idle_add(_append)

    # ── install ───────────────────────────────────────────────────────────────

    def _on_install(self, btn):
        if not self._file_path or self._installing:
            return
        self._installing = True
        self._cancel_token = CancelToken()
        self.install_btn.set_sensitive(False)
        self.cancel_btn.set_visible(True)
        self.cancel_btn.set_sensitive(True)
        self.progress.set_visible(True)
        app_name_override = self.name_entry.get_text().strip() or None
        sudo_password = self.pwd_entry.get_text().strip() or None

        # pulse the progress bar on a timer
        self._pulse_id = GLib.timeout_add(100, self._pulse)

        thread = threading.Thread(
            target=self._install_thread,
            args=(self._file_path, app_name_override, sudo_password),
            daemon=True,
        )
        thread.start()

    def _on_cancel(self, btn):
        """Cancel button handler — kill the running subprocess and abort."""
        if self._cancel_token and self._installing:
            self._log("⛔ Cancelling installation…")
            self.cancel_btn.set_sensitive(False)
            self._cancel_token.cancel()

    def _pulse(self):
        self.progress.pulse()
        return self._installing

    def _install_thread(self, path, app_name_override, sudo_password=None):
        try:
            install_file(path, app_name_override, self._log,
                         sudo_password=sudo_password,
                         cancel_token=self._cancel_token)
            GLib.idle_add(self._install_done, True, None)
        except CancelledError:
            self._cleanup_partial(path, app_name_override, sudo_password)
            GLib.idle_add(self._install_cancelled)
        except Exception as e:
            GLib.idle_add(self._install_done, False, str(e))

    def _cleanup_partial(self, path, app_name_override, sudo_password):
        ftype = detect_type(path)
        app_name = app_name_override or app_name_from_path(path)
        app_name = re.sub(r'\s+', '-', app_name)
        
        self._log("🧹 Cleaning up partial installation...")
        def cleanup_sudo(cmd: list[str]):
            if sudo_password:
                subprocess.run(["sudo", "-S"] + cmd, input=(sudo_password + "\n").encode(), capture_output=True)
            else:
                subprocess.run(["sudo"] + cmd, capture_output=True)

        if ftype in ("rpm", "deb"):
            cleanup_sudo(["dnf", "remove", "-y", app_name])
        elif ftype == "appimage":
            dest = os.path.expanduser(f"~/.local/bin/{app_name}.AppImage")
            try: os.remove(dest)
            except OSError: pass
            desktop_file = os.path.expanduser(f"~/.local/share/applications/{app_name.lower().replace(' ', '-')}.desktop")
            try: os.remove(desktop_file)
            except OSError: pass
        elif ftype in ("tarball", "zip"):
            cleanup_sudo(["rm", "-rf", f"/opt/{app_name}"])
            link = f"/usr/local/bin/{app_name}"
            cleanup_sudo(["rm", "-f", link])
            desktop_file = os.path.expanduser(f"~/.local/share/applications/{app_name.lower().replace(' ', '-')}.desktop")
            try: os.remove(desktop_file)
            except OSError: pass

    def _install_done(self, success: bool, error: str | None):
        self._installing = False
        self._cancel_token = None
        self.progress.set_visible(False)
        self.cancel_btn.set_visible(False)
        self.install_btn.set_sensitive(True)

        if success:
            self._log("🎉 Installation complete!")
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Installed!",
                body="The app is now available in your GNOME launcher.",
            )
            dialog.add_response("ok", "Great!")
            dialog.present()
        else:
            self._log(f"❌ Error: {error}")
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Installation failed",
                body=error or "Unknown error",
            )
            dialog.add_response("ok", "OK")
            dialog.present()
        return False

    def _install_cancelled(self):
        """Called on the main thread when the install thread exits via cancel."""
        self._installing = False
        self._cancel_token = None
        self.progress.set_visible(False)
        self.cancel_btn.set_visible(False)
        self.install_btn.set_sensitive(True)
        self._log("⛔ Installation cancelled.")
        dialog = Adw.MessageDialog(
            transient_for=self,
            heading="Cancelled",
            body="The installation was cancelled. Partial files may remain.",
        )
        dialog.add_response("ok", "OK")
        dialog.present()
        return False


class FedoraInstallerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.path_arg = None

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            win = InstallerWindow(application=self)
            if self.path_arg:
                win._set_file(self.path_arg)
        win.present()


def main():
    app = FedoraInstallerApp()
    # allow a file path as CLI argument
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        app.path_arg = sys.argv[1]
        sys.exit(app.run([sys.argv[0]]))
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()