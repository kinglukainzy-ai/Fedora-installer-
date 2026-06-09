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

APP_ID = "io.github.kinglukainzy_ai.FedoraInstaller"
VERSION = "1.0.0"


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
    for root, _, files in os.walk(directory):
        for f in files:
            if f.lower().endswith((".png", ".svg", ".xpm")):
                return os.path.join(root, f)
    return None


def find_executable(directory: str) -> str | None:
    """Find the most likely main executable in an extracted directory."""
    app_base = os.path.basename(directory).lower()
    candidates = []
    for root, _, files in os.walk(directory):
        for f in files:
            full = os.path.join(root, f)
            if os.access(full, os.X_OK) and not f.endswith((".so", ".so.0")):
                score = 0
                if f.lower() in (app_base, app_base.replace("-", ""), app_base.replace("_", "")):
                    score = 10
                elif "bin" in root:
                    score = 5
                candidates.append((score, full))
    if candidates:
        return sorted(candidates, key=lambda x: -x[0])[0][1]
    return None


def install_file(path: str, app_name_override: str | None, log, sudo_password: str | None = None):
    """
    Core installer. Calls log(str) for progress. Raises on fatal error.
    sudo_password: if provided, piped into sudo -S.
    """
    ftype = detect_type(path)
    app_name = app_name_override or app_name_from_path(path)
    log(f"📦 File type detected: {ftype}")
    log(f"🏷  App name: {app_name}")

    def sudo_run(cmd: list[str], **kwargs):
        if sudo_password:
            proc = subprocess.run(
                ["sudo", "-S"] + cmd,
                input=(sudo_password + "\n").encode(),
                capture_output=True,
                **kwargs,
            )
        else:
            proc = subprocess.run(["sudo"] + cmd, capture_output=True, **kwargs)
        return proc

    # ── RPM ──────────────────────────────────────────────────────────────────
    if ftype == "rpm":
        log("⚙️  Installing via dnf…")
        proc = sudo_run(["dnf", "install", "-y", path])
        log(proc.stdout.decode(errors="replace"))
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        log("✅ RPM installed.")

    # ── DEB ──────────────────────────────────────────────────────────────────
    elif ftype == "deb":
        log("⚙️  Converting .deb → .rpm via alien…")
        conv = subprocess.run(["alien", "--to-rpm", "--scripts", path], capture_output=True)
        log(conv.stdout.decode(errors="replace"))
        if conv.returncode != 0:
            raise RuntimeError(
                "alien failed (is it installed? run: sudo dnf install -y alien)\n"
                + conv.stderr.decode(errors="replace")
            )
        rpm_out = [f for f in os.listdir(".") if f.endswith(".rpm")]
        if not rpm_out:
            raise RuntimeError("alien did not produce an .rpm file.")
        rpm_path = os.path.abspath(rpm_out[0])
        log(f"⚙️  Installing converted RPM: {rpm_path}")
        proc = sudo_run(["dnf", "install", "-y", rpm_path])
        log(proc.stdout.decode(errors="replace"))
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        log("✅ .deb converted and installed.")

    # ── FLATPAK ───────────────────────────────────────────────────────────────
    elif ftype == "flatpak":
        log("⚙️  Installing Flatpak bundle…")
        proc = subprocess.run(
            ["flatpak", "install", "--user", "--noninteractive", path],
            capture_output=True,
        )
        log(proc.stdout.decode(errors="replace"))
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        log("✅ Flatpak installed.")

    # ── APPIMAGE ──────────────────────────────────────────────────────────────
    elif ftype == "appimage":
        dest_dir = os.path.expanduser("~/.local/bin")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{app_name}.AppImage")
        import shutil
        shutil.copy2(path, dest)
        os.chmod(dest, 0o755)
        log(f"⚙️  Copied to {dest}")
        create_desktop_entry(app_name, dest, None, log)
        log("✅ AppImage installed.")

    # ── TARBALL / ZIP ─────────────────────────────────────────────────────────
    elif ftype in ("tarball", "zip"):
        install_dir = f"/opt/{app_name}"
        log(f"⚙️  Extracting to {install_dir}…")
        proc = sudo_run(["mkdir", "-p", install_dir])
        if proc.returncode != 0:
            raise RuntimeError(f"Cannot create {install_dir}: {proc.stderr.decode()}")

        if ftype == "tarball":
            proc = sudo_run(["tar", "--strip-components=1", "-xf", path, "-C", install_dir])
        else:
            proc = subprocess.run(
                ["unzip", "-o", path, "-d", install_dir],
                capture_output=True,
            )

        log(proc.stdout.decode(errors="replace") if hasattr(proc, "stdout") else "")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace") if hasattr(proc, "stderr") else "extraction failed")

        # look for bundled install.sh
        install_sh = os.path.join(install_dir, "install.sh")
        if os.path.exists(install_sh):
            log("⚙️  Found install.sh — running it…")
            subprocess.run(["bash", install_sh], cwd=install_dir)
        else:
            exe = find_executable(install_dir)
            if exe:
                link = f"/usr/local/bin/{app_name}"
                sudo_run(["ln", "-sf", exe, link])
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
    subprocess.run(
        ["bash", "-c", "update-desktop-database ~/.local/share/applications 2>/dev/null; "
                        "killall -HUP gnome-shell 2>/dev/null || true"],
        capture_output=True,
    )
    log("🔄 GNOME launcher refreshed.")


# ─────────────────────────── GTK4 UI ─────────────────────────────────────────

class InstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Fedora Installer")
        self.set_default_size(560, 600)
        self.set_resizable(True)

        self._file_path: str | None = None
        self._installing = False

        # ── root box ──────────────────────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(root)

        # ── header bar ────────────────────────────────────────────────────────
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        root.append(header)

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

        # ── install button ────────────────────────────────────────────────────
        self.install_btn = Gtk.Button(label="Install")
        self.install_btn.set_halign(Gtk.Align.CENTER)
        self.install_btn.add_css_class("pill")
        self.install_btn.add_css_class("suggested-action")
        self.install_btn.set_sensitive(False)
        self.install_btn.connect("clicked", self._on_install)
        content.append(self.install_btn)

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
        self._file_path = path
        name = os.path.basename(path)
        ftype = detect_type(path)
        self.file_row.set_title(name)
        self.file_row.set_subtitle(f"Type: {ftype}  ·  {os.path.getsize(path) // 1024} KB")
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
            adj.set_value(adj.get_upper())
        GLib.idle_add(_append)

    # ── install ───────────────────────────────────────────────────────────────

    def _on_install(self, btn):
        if not self._file_path or self._installing:
            return
        self._installing = True
        self.install_btn.set_sensitive(False)
        self.progress.set_visible(True)
        app_name_override = self.name_entry.get_text().strip() or None

        # pulse the progress bar on a timer
        self._pulse_id = GLib.timeout_add(100, self._pulse)

        thread = threading.Thread(
            target=self._install_thread,
            args=(self._file_path, app_name_override),
            daemon=True,
        )
        thread.start()

    def _pulse(self):
        self.progress.pulse()
        return self._installing

    def _install_thread(self, path, app_name_override):
        try:
            install_file(path, app_name_override, self._log)
            GLib.idle_add(self._install_done, True, None)
        except Exception as e:
            GLib.idle_add(self._install_done, False, str(e))

    def _install_done(self, success: bool, error: str | None):
        self._installing = False
        self.progress.set_visible(False)
        self.install_btn.set_sensitive(True)

        if success:
            self._log("🎉 Installation complete!")
            dialog = Adw.MessageDialog(
                transient_for=self,
                heading="Installed!",
                body=f"The app is now available in your GNOME launcher.",
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


class FedoraInstallerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        win = InstallerWindow(application=app)
        win.present()


def main():
    app = FedoraInstallerApp()
    # allow a file path as CLI argument
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        # open the window then pre-select the file
        original_activate = app._on_activate
        path_arg = sys.argv[1]
        def activate_with_file(a):
            win = InstallerWindow(application=a)
            win._set_file(path_arg)
            win.present()
        app.connect("activate", activate_with_file)
        sys.exit(app.run([]))
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
