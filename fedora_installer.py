#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Fedora Installer — universal GUI installer for .rpm, .deb, .flatpak,
.AppImage, .tar.*, and .zip files on Fedora/GNOME.
"""

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

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, Adw, Gio, GLib, Gdk
import subprocess
import threading
import os
import sys
import re
import shlex
import urllib.request
import json
import shutil
import tempfile
from datetime import datetime
import gc
import queue as _queue

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

# ── Bounded log sink ──────────────────────────────────────────────────────────
# ROOT CAUSE OF 5 GB RAM SPIKE:
# The old design called GLib.idle_add(_append) once per log line. Install
# threads can produce thousands of lines per second (dnf, flatpak). Each call
# allocates a closure object that holds the line string and sits in GLib's idle
# queue until the GTK main loop gets to drain it. When the main loop is busy
# (e.g. updating widgets), the queue grows without bound — thousands of live
# closure objects each holding a heap string. The TextBuffer also grew without
# any trimming, so every character was retained in memory for the lifetime of
# the process.
#
# Fix: BoundedLogSink decouples the producer (install thread) from the
# consumer (GTK main loop) with a bounded queue:
#   • The install thread pushes lines into a thread-safe deque (at most
#     MAX_QUEUED_LINES entries; older entries are silently dropped).
#   • A single GLib.timeout_add(50 ms) timer drains the deque in a batch
#     on the main thread — one GTK round-trip per 50 ms instead of one per line.
#   • The TextBuffer is capped at MAX_BUFFER_LINES: when inserting would exceed
#     the cap, the oldest lines are deleted first (O(lines_to_drop) gtk ops).
#   • The timer is self-cancelling when the sink is closed.

_MAX_QUEUED_LINES = 500    # max pending lines in the thread→GTK queue
_MAX_BUFFER_LINES = 500    # max lines kept in the visible TextBuffer


class BoundedLogSink:
    """Thread-safe, memory-bounded log sink for GTK TextBuffer.

    Call push(text) from any thread.  A GLib timer drains the queue on the
    main thread in batches, keeping the TextBuffer at most _MAX_BUFFER_LINES
    lines long.  Call close() when the operation is done to stop the timer.
    """

    def __init__(self, buf: "Gtk.TextBuffer", adj: "Gtk.Adjustment"):
        self._buf = buf
        self._adj = adj
        self._q: "_queue.SimpleQueue[str | None]" = _queue.SimpleQueue()
        self._closed = False
        self._timer_id = GLib.timeout_add(50, self._drain)

    def push(self, text: str) -> None:
        """Enqueue a log line from any thread (non-blocking, drops if full)."""
        if self._closed:
            return
        # SimpleQueue is unbounded by default; enforce the cap ourselves by
        # checking approximate size. qsize() is O(1) on SimpleQueue.
        try:
            if self._q.qsize() < _MAX_QUEUED_LINES:
                self._q.put_nowait(text.strip())
            # else: silently drop — the UI would freeze trying to render
            # thousands of lines anyway
        except Exception:
            pass

    def close(self) -> None:
        """Stop draining. Safe to call multiple times."""
        self._closed = True
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _drain(self) -> bool:
        """Called on the GTK main thread every 50 ms."""
        if self._closed:
            self._timer_id = None
            return False  # cancel the timer

        buf = self._buf
        adj = self._adj

        # Drain up to 100 lines per tick to keep the UI responsive
        lines_written = 0
        while lines_written < 100:
            try:
                line = self._q.get_nowait()
            except _queue.Empty:
                break
            end = buf.get_end_iter()
            buf.insert(end, line + "\n")
            lines_written += 1

        if lines_written:
            # Trim the buffer if it exceeds the cap
            n = buf.get_line_count()
            if n > _MAX_BUFFER_LINES:
                drop = n - _MAX_BUFFER_LINES
                start = buf.get_start_iter()
                cut = buf.get_iter_at_line(drop)
                buf.delete(start, cut)

            # Scroll to the bottom
            adj.set_value(adj.get_upper())

        return True  # keep the timer alive


def _append_capped(buf: bytearray, chunk: bytes, cap: int) -> None:
    """Append bytes while keeping only the first cap bytes."""
    if len(buf) >= cap:
        return
    remaining = cap - len(buf)
    buf.extend(chunk[:remaining])


def _stream_pipe(pipe, log=None, *, max_lines: int = 200,
                 capture: bytearray | None = None,
                 capture_limit: int = 64 * 1024,
                 cancel_token=None) -> tuple[int, int]:
    """Drain one subprocess pipe.

    Returns (logged_lines, suppressed_lines).  If capture is provided, only the
    first capture_limit bytes are retained while the rest is still drained.
    """
    logged = 0
    suppressed = 0
    for raw_line in iter(pipe.readline, b""):
        if cancel_token and cancel_token.cancelled:
            break
        if capture is not None:
            _append_capped(capture, raw_line, capture_limit)
        if log is None:
            continue
        if logged < max_lines:
            log(raw_line.decode(errors="replace").rstrip("\n"))
            logged += 1
        else:
            suppressed += 1
    return logged, suppressed


def _stream_output(proc, log, cancel_token=None, max_lines: int = 200):
    """Stream subprocess stdout/stderr concurrently with bounded memory use."""
    stderr_buf = bytearray()
    results: dict[str, tuple[int, int]] = {}

    def _drain_stdout():
        results["stdout"] = _stream_pipe(
            proc.stdout, log, max_lines=max_lines, cancel_token=cancel_token
        )

    def _drain_stderr():
        results["stderr"] = _stream_pipe(
            proc.stderr, log, max_lines=max_lines, capture=stderr_buf,
            cancel_token=cancel_token
        )

    threads = [
        threading.Thread(target=_drain_stdout, daemon=True),
        threading.Thread(target=_drain_stderr, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    suppressed = sum(item[1] for item in results.values())
    if suppressed:
        log(f"… ({suppressed} more lines suppressed)")
    return stderr_buf.decode(errors="replace")


def _capture_capped_output(proc, capture_limit: int = 64 * 1024):
    """Drain stdout/stderr concurrently and retain only capped output bytes."""
    stdout_buf = bytearray()
    stderr_buf = bytearray()

    threads = [
        threading.Thread(
            target=_stream_pipe,
            args=(proc.stdout,),
            kwargs={"capture": stdout_buf, "capture_limit": capture_limit},
            daemon=True,
        ),
        threading.Thread(
            target=_stream_pipe,
            args=(proc.stderr,),
            kwargs={"capture": stderr_buf, "capture_limit": capture_limit},
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    proc.wait()
    for thread in threads:
        thread.join()
    return bytes(stdout_buf), bytes(stderr_buf)


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


def cancellable_run(cmd: list[str], token: CancelToken | None = None,
                    log=None, **kwargs):
    """Run a subprocess, killing it if the token is cancelled.

    Uses Popen internally so the process can be terminated mid-flight.
    If *log* is provided, stdout/stderr are streamed line-by-line through
    _stream_output.  When log is None, stdout/stderr are still drained
    concurrently but captured output is capped.
    Returns a CompletedProcess-like object.
    """
    if token:
        token.check()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    if token:
        token.register(proc)
    try:
        if log is not None:
            stderr_str = _stream_output(proc, log, cancel_token=token)
            proc.wait()
            stdout = b""
            stderr = stderr_str.encode(errors="replace")
        else:
            stdout, stderr = _capture_capped_output(proc)
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
    return desktop_file


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


# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_DIR  = os.path.expanduser("~/.config/fedora-installer")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "deb_method":        None,   # None = not chosen yet (triggers first-launch dialog)
    "first_launch_done": False,
    "last_tab":          0,      # 0 = Install, 1 = Installed
}

def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        for k, v in DEFAULTS.items():
            data.setdefault(k, v)
        return data
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)

def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def send_notification(summary: str, body: str = "", urgency: str = "normal") -> None:
    """Fire a desktop notification via notify-send (non-blocking, best-effort)."""
    try:
        cmd = [
            "notify-send",
            "--app-name", "Fedora Installer",
            "--icon", "system-software-install",
            "--urgency", urgency,
            summary,
        ]
        if body:
            cmd.append(body)
        # Fix Bug 10: Popen without wait() leaves a zombie process entry in the
        # process table for each notification. Use subprocess.run with a short
        # timeout inside a daemon thread so the UI never blocks.
        def _fire():
            try:
                subprocess.run(cmd, close_fds=True, timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        threading.Thread(target=_fire, daemon=True).start()
    except Exception:
        pass  # notify-send not available — silently skip


def get_deb_method() -> str:
    """Return saved deb method, defaulting to distrobox."""
    return load_config().get("deb_method") or "distrobox"


def write_receipt(app_name: str, install_type: str, paths: dict, package_name: str | None = None):
    """Write a JSON receipt of the installation to ~/.local/share/fedora-installer/receipts/"""
    receipts_dir = os.path.expanduser("~/.local/share/fedora-installer/receipts")
    os.makedirs(receipts_dir, exist_ok=True)
    receipt = {
        "app_name": app_name,
        "install_type": install_type,
        "installed_at": datetime.now().isoformat(),
        "paths": {
            "install_dir": paths.get("install_dir"),
            "symlink": paths.get("symlink"),
            "desktop_entry": paths.get("desktop_entry"),
            "icon": paths.get("icon")
        },
        "package_name": package_name
    }
    receipt_file = os.path.join(receipts_dir, f"{app_name.lower().replace(' ', '-')}.json")
    with open(receipt_file, "w") as f:
        json.dump(receipt, f, indent=2)


def install_file(path: str, app_name_override: str | None, log,
                 sudo_password: str | None = None,
                 cancel_token: CancelToken | None = None,
                 deb_method: str | None = None):
    """
    Core installer. Calls log(str) for progress. Raises on fatal error.
    sudo_password: if provided, piped into sudo -S.
    cancel_token: if provided, checked between steps and used to kill subprocesses.
    deb_method: "distrobox" or "alien". Falls back to saved config if None.
    """
    if deb_method is None:
        deb_method = get_deb_method()
    ftype = detect_type(path)
    app_name = app_name_override or app_name_from_path(path)
    # Sanitize: replace whitespace with hyphens for safe filesystem paths
    app_name = re.sub(r'\s+', '-', app_name)
    log(f"File type detected: {ftype}")
    log(f"App name: {app_name}")

    def sudo_run(cmd: list[str], stream=False, **kwargs):
        """Run a command under sudo.  When stream=True, pipe output
        through _stream_output so it never buffers fully in RAM."""
        if cancel_token:
            cancel_token.check()
        if sudo_password:
            full_cmd = ["sudo", "-S"] + cmd
            proc = subprocess.Popen(
                full_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
            )
            if cancel_token:
                cancel_token.register(proc)
            try:
                # Write the password, then stream
                proc.stdin.write((sudo_password + "\n").encode())
                proc.stdin.flush()
                proc.stdin.close()
                if stream:
                    stderr_str = _stream_output(proc, log, cancel_token=cancel_token)
                    proc.wait()
                    return subprocess.CompletedProcess(full_cmd, proc.returncode, b"", stderr_str.encode(errors="replace"))
                else:
                    stdout, stderr = _capture_capped_output(proc)
                    return subprocess.CompletedProcess(full_cmd, proc.returncode, stdout, stderr)
            finally:
                if cancel_token:
                    cancel_token.unregister()
        else:
            if stream:
                return cancellable_run(["sudo"] + cmd, token=cancel_token, log=log, **kwargs)
            return cancellable_run(["sudo"] + cmd, token=cancel_token, **kwargs)

    # ── RPM ──────────────────────────────────────────────────────────────────
    if ftype == "rpm":
        log("Installing via dnf…")
        package_name = None
        try:
            pkg_proc = cancellable_run(["rpm", "-qp", "--qf", "%{NAME}", path], token=cancel_token)
            if pkg_proc.returncode == 0:
                package_name = pkg_proc.stdout.decode(errors="replace").strip()
                log(f"Queried RPM package name: {package_name}")
        except Exception as e:
            log(f"⚠️  Could not query RPM package name: {e}")

        proc = sudo_run(["dnf", "install", "-y", path], stream=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        if cancel_token:
            cancel_token.check()
        log("✅ RPM installed.")
        write_receipt(app_name, "rpm", {}, package_name)

    # ── DEB ──────────────────────────────────────────────────────────────────
    elif ftype == "deb":
        def _install_deb_distrobox():
            CONTAINER = "fedora-installer-debian"
            log("▸ Installing .deb via distrobox…")
            if subprocess.run(["which", "distrobox"], capture_output=True).returncode != 0:
                log("▸ distrobox not found — installing…")
                proc = sudo_run(["dnf", "install", "-y", "distrobox", "podman"], stream=True)
                if proc.returncode != 0:
                    raise RuntimeError("Failed to install distrobox.\n" + proc.stderr.decode(errors="replace"))
            existing = subprocess.run(["distrobox", "list"], capture_output=True, text=True)
            if CONTAINER not in existing.stdout:
                log(f"▸ Creating Debian container '{CONTAINER}' (first time only — may take a minute)…")
                proc = cancellable_run(
                    ["distrobox", "create", "--name", CONTAINER,
                     "--image", "quay.io/toolbx-images/debian-toolbox:testing", "--yes"],
                    token=cancel_token, log=log
                )
                if proc.returncode != 0:
                    raise RuntimeError("Failed to create distrobox container.\n" + proc.stderr.decode(errors="replace"))
            log(f"▸ Installing {os.path.basename(path)} inside container…")
            install_cmd = f"sudo apt-get update -qq && sudo apt-get install -y {shlex.quote(path)}"
            proc = cancellable_run(
                ["distrobox", "enter", CONTAINER, "--", "bash", "-c", install_cmd],
                token=cancel_token, log=log
            )
            if proc.returncode != 0:
                raise RuntimeError("apt install failed inside container.\n" + proc.stderr.decode(errors="replace"))
            log("▸ Exporting app to host launcher…")
            export_cmd = f"distrobox-export --app \'{app_name}\' 2>/dev/null || true"
            cancellable_run(["distrobox", "enter", CONTAINER, "--", "bash", "-c", export_cmd], token=cancel_token)
            log("✅ .deb installed via distrobox.")

        def _install_deb_alien():
            log("▸ Installing .deb via alien…")
            if subprocess.run(["which", "alien"], capture_output=True).returncode != 0:
                log("▸ alien not found — installing…")
                proc = sudo_run(["dnf", "install", "-y", "alien"], stream=True)
                if proc.returncode != 0:
                    raise RuntimeError("Failed to install alien.\n" + proc.stderr.decode(errors="replace"))
            with tempfile.TemporaryDirectory() as tmpdir:
                conv = cancellable_run(["alien", "--to-rpm", "--scripts", path], token=cancel_token, cwd=tmpdir, log=log)
                if conv.returncode != 0:
                    raise RuntimeError("alien failed.\n" + conv.stderr.decode(errors="replace"))
                rpm_files = [f for f in os.listdir(tmpdir) if f.endswith(".rpm")]
                if not rpm_files:
                    raise RuntimeError("alien did not produce an .rpm file.")
                rpm_path = os.path.join(tmpdir, rpm_files[0])
                log(f"▸ Installing converted RPM: {rpm_path}")
                proc = sudo_run(["dnf", "install", "-y", rpm_path], stream=True)
                if proc.returncode != 0:
                    raise RuntimeError(proc.stderr.decode(errors="replace"))
            log("✅ .deb converted and installed via alien.")

        # Use chosen method, with fallback to distrobox if alien fails
        if deb_method == "alien":
            try:
                _install_deb_alien()
            except RuntimeError as alien_err:
                log(f"⚠️  alien failed: {alien_err}")
                log("▸ Retrying with distrobox…")
                _install_deb_distrobox()
        else:
            _install_deb_distrobox()

        write_receipt(app_name, "deb", {}, app_name)

    # ── FLATPAK ───────────────────────────────────────────────────────────────
    elif ftype == "flatpak":
        log("Installing Flatpak bundle…")
        proc = cancellable_run(
            ["flatpak", "install", "--user", "--noninteractive", path],
            token=cancel_token, log=log
        )
        stdout_str = proc.stdout.decode(errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        if cancel_token:
            cancel_token.check()

        package_name = None
        try:
            candidates = re.findall(r'\b[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)+\b', stdout_str)
            filtered = [
                c for c in candidates 
                if not any(r in c for r in (".Platform", ".Sdk", ".Locale", ".BaseApp", ".BaseExtension"))
            ]
            if filtered:
                package_name = filtered[-1]
                log(f"Detected Flatpak application ID: {package_name}")
            else:
                log("⚠️  Could not detect Flatpak application ID from installation output.")
        except Exception as e:
            log(f"⚠️  Error parsing Flatpak ID: {e}")

        log("✅ Flatpak installed.")
        write_receipt(app_name, "flatpak", {}, package_name)

    # ── APPIMAGE ──────────────────────────────────────────────────────────────
    elif ftype == "appimage":
        dest_dir = os.path.expanduser("~/.local/bin")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"{app_name}.AppImage")
        shutil.copy2(path, dest)
        os.chmod(dest, 0o755)
        log(f"Copied to {dest}")

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
                    log(f"Icon extracted: {icon_dest}")
            else:
                log("⚠️  Could not extract icon from AppImage (non-fatal)")

        desktop_path = create_desktop_entry(app_name, dest, icon_path, log)
        log("✅ AppImage installed.")
        write_receipt(app_name, "appimage", {
            "install_dir": dest,
            "desktop_entry": desktop_path,
            "icon": icon_path
        })

    # ── TARBALL / ZIP ─────────────────────────────────────────────────────────
    elif ftype in ("tarball", "zip"):
        log("Extracting archive…")

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
            log(f"Installing to {install_dir}…")
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
        symlink_path = None
        desktop_path = None
        icon_path = None

        if os.path.exists(install_sh):
            log("Found install.sh — running it…")
            proc_sh = cancellable_run(
                ["bash", install_sh], token=cancel_token,
                cwd=install_dir, log=log
            )
            if proc_sh.returncode != 0:
                log(f"⚠️  install.sh exited with code {proc_sh.returncode}")
                # Stderr is already logged by streaming; if there are other errors raise them
            else:
                log("install.sh completed.")
        else:
            exe = find_executable(install_dir)
            if exe:
                link = f"/usr/local/bin/{app_name}"
                ln_proc = sudo_run(["ln", "-sf", exe, link])
                if ln_proc.returncode != 0:
                    log(f"⚠️  Symlink failed: {ln_proc.stderr.decode(errors='replace')}")
                else:
                    log(f"Symlinked executable → {link}")
                    symlink_path = link
            else:
                exe = install_dir

            icon_path = find_icon(install_dir)
            desktop_path = create_desktop_entry(app_name, exe or install_dir, icon_path, log)

        log("✅ Archive installed.")
        write_receipt(app_name, ftype, {
            "install_dir": install_dir,
            "symlink": symlink_path,
            "desktop_entry": desktop_path,
            "icon": icon_path
        })

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
    log("Desktop database updated. On Wayland, log out and back in "
        "for the launcher icon to appear.")


# ─────────────────────────── GTK4 UI ─────────────────────────────────────────

def read_receipts() -> list[dict]:
    """Read all receipt files from ~/.local/share/fedora-installer/receipts/"""
    receipts_dir = os.path.expanduser("~/.local/share/fedora-installer/receipts")
    if not os.path.isdir(receipts_dir):
        return []
    receipts = []
    for f in os.listdir(receipts_dir):
        if f.endswith(".json"):
            try:
                with open(os.path.join(receipts_dir, f)) as file:
                    data = json.load(file)
                    data["receipt_file"] = os.path.join(receipts_dir, f)
                    receipts.append(data)
            except Exception:
                pass
    return sorted(receipts, key=lambda x: x.get("installed_at", ""), reverse=True)


def uninstall_app(app_name: str, install_type: str, paths: dict, package_name: str | None, log, sudo_password: str | None = None, cancel_token: CancelToken | None = None):
    """
    Uninstalls an application based on its type and paths.
    """
    log(f"Starting uninstallation of {app_name} ({install_type})")
    
    def sudo_run(cmd: list[str], stream=False):
        """Run a command under sudo.  When stream=True, pipe output
        through _stream_output so it never buffers fully in RAM."""
        if cancel_token:
            cancel_token.check()
        if sudo_password:
            full_cmd = ["sudo", "-S"] + cmd
            proc = subprocess.Popen(
                full_cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            if cancel_token:
                cancel_token.register(proc)
            try:
                proc.stdin.write((sudo_password + "\n").encode())
                proc.stdin.flush()
                proc.stdin.close()
                if stream:
                    stderr_str = _stream_output(proc, log, cancel_token=cancel_token)
                    proc.wait()
                    return subprocess.CompletedProcess(full_cmd, proc.returncode, b"", stderr_str.encode(errors="replace"))
                else:
                    stdout, stderr = _capture_capped_output(proc)
                    return subprocess.CompletedProcess(full_cmd, proc.returncode, stdout, stderr)
            finally:
                if cancel_token:
                    cancel_token.unregister()
        else:
            if stream:
                return cancellable_run(["sudo"] + cmd, token=cancel_token, log=log)
            return cancellable_run(["sudo"] + cmd, token=cancel_token)

    # 1. Package Manager Uninstalls (RPM / DEB / Flatpak)
    if install_type in ("rpm", "deb"):
        if not package_name:
            package_name = app_name
        log(f"Removing package {package_name} via dnf…")
        proc = sudo_run(["dnf", "remove", "-y", package_name], stream=True)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        log(f"✅ {app_name} package removed.")

    elif install_type == "flatpak":
        if not package_name:
            raise RuntimeError("Flatpak application ID not found.")
        log(f"Uninstalling Flatpak {package_name}…")
        proc = cancellable_run(["flatpak", "uninstall", "--user", "-y", package_name], token=cancel_token, log=log)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode(errors="replace"))
        log(f"✅ {app_name} Flatpak uninstalled.")

    # 2. File-based Uninstalls (AppImage / Tarball / ZIP)
    else:
        # Remove desktop entry
        desktop_path = paths.get("desktop_entry")
        if desktop_path and os.path.exists(desktop_path):
            try:
                os.remove(desktop_path)
                log(f"Removed desktop entry: {desktop_path}")
            except Exception as e:
                log(f"⚠️  Could not remove desktop entry: {e}")
        elif not desktop_path:
            guess_desktop = os.path.expanduser(f"~/.local/share/applications/{app_name.lower().replace(' ', '-')}.desktop")
            if os.path.exists(guess_desktop):
                try:
                    os.remove(guess_desktop)
                    log(f"Removed guessed desktop entry: {guess_desktop}")
                except Exception as e:
                    log(f"⚠️  Could not remove desktop entry: {e}")

        # Remove icon
        icon_path = paths.get("icon")
        if icon_path and os.path.exists(icon_path):
            try:
                os.remove(icon_path)
                log(f"Removed icon: {icon_path}")
            except Exception as e:
                log(f"⚠️  Could not remove icon: {e}")

        # Remove install_dir or binary
        install_dir = paths.get("install_dir")
        if install_dir and os.path.exists(install_dir):
            if os.path.isdir(install_dir):
                log(f"Removing directory {install_dir}…")
                if install_dir.startswith("/opt"):
                    proc = sudo_run(["rm", "-rf", install_dir])
                    if proc.returncode != 0:
                        raise RuntimeError(f"Could not remove {install_dir}: {proc.stderr.decode()}")
                else:
                    shutil.rmtree(install_dir)
                log(f"Removed directory: {install_dir}")
            else:
                log(f"Removing binary {install_dir}…")
                try:
                    os.remove(install_dir)
                    log(f"Removed binary: {install_dir}")
                except Exception as e:
                    log(f"⚠️  Could not remove binary: {e}")
        
        # Remove symlink
        symlink = paths.get("symlink")
        if symlink and os.path.exists(symlink):
            log(f"Removing symlink {symlink}…")
            if symlink.startswith(("/usr/local/bin", "/usr/bin")):
                proc = sudo_run(["rm", "-f", symlink])
                if proc.returncode != 0:
                    raise RuntimeError(f"Could not remove symlink: {proc.stderr.decode()}")
            else:
                try:
                    os.remove(symlink)
                except Exception as e:
                    log(f"⚠️  Could not remove symlink: {e}")
            log(f"Removed symlink: {symlink}")

    # Update desktop database
    subprocess.run(
        ["update-desktop-database", os.path.expanduser("~/.local/share/applications")],
        capture_output=True,
    )
    log("Desktop database updated.")


class UninstallDialog(Adw.AlertDialog):
    def __init__(self, parent, app_name, install_type, paths, package_name, receipt_file=None, sudo_password=None):
        super().__init__(transient_for=parent, heading=f"Uninstalling {app_name}")
        
        self.app_name = app_name
        self.install_type = install_type
        self.paths = paths
        self.package_name = package_name
        self.receipt_file = receipt_file
        self.cancel_token = CancelToken()
        self.uninstalling = True
        self._closed = False
        
        # Setup UI
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        
        self.progress = Gtk.ProgressBar()
        self.progress.set_pulse_step(0.05)
        box.append(self.progress)
        
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_size_request(450, 200)
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buffer = self.log_view.get_buffer()
        log_scroll.set_child(self.log_view)
        self._log_adj = log_scroll.get_vadjustment()  # Fix: was never assigned — caused AttributeError in _log
        box.append(log_scroll)

        # Use BoundedLogSink to prevent unbounded RAM growth from log output
        self._log_sink = BoundedLogSink(self.log_buffer, self._log_adj)

        self.set_extra_child(box)
        
        # Responses
        self.add_response("cancel", "Cancel")
        self.add_response("close", "Close")
        self.set_response_appearance("close", Adw.ResponseAppearance.SUGGESTED)
        self.set_response_enabled("close", False)
        
        self.connect("response", self._on_response)
        
        # Start pulse timer — stored so we can cancel it in _done
        self._pulse_id = GLib.timeout_add(100, self._pulse)

        # Start thread
        threading.Thread(target=self._run_uninstall, args=(sudo_password,), daemon=True).start()

    def _log(self, text: str):
        self._log_sink.push(text)

    def _pulse(self):
        if self.uninstalling:
            self.progress.pulse()
            return True
        self._pulse_id = None
        return False

    def _run_uninstall(self, sudo_password):
        try:
            uninstall_app(
                self.app_name, self.install_type, self.paths, self.package_name,
                self._log, sudo_password=sudo_password, cancel_token=self.cancel_token
            )
            # Delete receipt file if present and uninstallation was successful
            if self.receipt_file and os.path.exists(self.receipt_file):
                try:
                    os.remove(self.receipt_file)
                    self._log(f"Receipt deleted: {self.receipt_file}")
                except Exception as e:
                    self._log(f"⚠️  Could not delete receipt file: {e}")
            
            GLib.idle_add(self._done, True, None)
        except CancelledError:
            GLib.idle_add(self._done, False, "Cancelled by user.")
        except Exception as e:
            GLib.idle_add(self._done, False, str(e))
        finally:
            sudo_password = None

    def _done(self, success, error):
        if self._closed:
            return False
        self.uninstalling = False
        # Stop pulse timer (in case GLib hasn't fired the False return yet)
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        # Clear the log buffer so repeated uninstalls don't grow it forever
        self.log_buffer.set_text("")
        self.progress.set_visible(False)
        self.set_response_enabled("close", True)
        self.set_response_enabled("cancel", False)
        
        if success:
            self._log("\n✅ Uninstallation complete!")
            self.set_body("Application has been successfully uninstalled.")
        else:
            self._log(f"\n❌ Error: {error}")
            self.set_body(f"Uninstallation failed: {error}")
            
        parent = self.get_transient_for()
        if parent and hasattr(parent, "refresh_installed_tab") and not getattr(parent, "_closed", False):
            GLib.idle_add(parent.refresh_installed_tab)
        gc.collect()

    def _on_response(self, dialog, response_id):
        if response_id == "cancel":
            self._log("Cancelling...")
            self.cancel_token.cancel()
        else:
            self._closed = True
            self._log_sink.close()
            self.destroy()


class UpdateDialog(Adw.AlertDialog):
    def __init__(self, parent, sudo_password=None):
        super().__init__(transient_for=parent, heading="Updating Fedora Installer")
        
        self.updating = True
        self._closed = False
        self._pulse_id = None
        
        # Setup UI
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        
        self.progress = Gtk.ProgressBar()
        self.progress.set_pulse_step(0.05)
        box.append(self.progress)
        
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_size_request(450, 200)
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buffer = self.log_view.get_buffer()
        log_scroll.set_child(self.log_view)
        self._log_adj = log_scroll.get_vadjustment()  # Fix: was never assigned — caused AttributeError in _log
        box.append(log_scroll)

        # Use BoundedLogSink to prevent unbounded RAM growth from log output
        self._log_sink = BoundedLogSink(self.log_buffer, self._log_adj)

        self.set_extra_child(box)
        
        # Responses
        self.add_response("close", "Close")
        self.add_response("restart", "Restart App")
        self.set_response_appearance("restart", Adw.ResponseAppearance.SUGGESTED)
        self.set_response_enabled("close", False)
        self.set_response_enabled("restart", False)
        
        self.connect("response", self._on_response)
        
        # Start pulse timer
        self._pulse_id = GLib.timeout_add(100, self._pulse)
        
        # Start thread
        threading.Thread(target=self._run_update, args=(sudo_password,), daemon=True).start()

    def _log(self, text: str):
        self._log_sink.push(text)

    def _pulse(self):
        if self.updating:
            self.progress.pulse()
            return True
        self._pulse_id = None
        return False

    def _run_update(self, sudo_password):
        try:
            self._log("Starting Fedora Installer update process…")
            cmd = ["sudo", "-S", "fedora-installer", "--update"]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Fix: stdin MUST be closed after writing the password.
            # sudo -S reads credentials from stdin and then keeps reading,
            # blocking indefinitely if the pipe stays open — the subprocess
            # never actually runs and the readline() loop hangs forever.
            if sudo_password:
                proc.stdin.write((sudo_password + "\n").encode())
            proc.stdin.flush()
            proc.stdin.close()  # signals EOF to sudo

            _stream_output(proc, self._log)
            proc.wait()

            if proc.returncode == 0:
                GLib.idle_add(self._done, True, None)
            else:
                GLib.idle_add(self._done, False, f"Update exited with code {proc.returncode}")
        except Exception as e:
            GLib.idle_add(self._done, False, str(e))
        finally:
            sudo_password = None

    def _done(self, success, error):
        if self._closed:
            return False
        self.updating = False
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
            
        self.progress.set_visible(False)
        self.set_response_enabled("close", True)
        
        if success:
            self._log("\n✅ Update complete! Please restart the application to apply the update.")
            self.set_body("Fedora Installer has been successfully updated.")
            self.set_response_enabled("restart", True)
        else:
            self._log(f"\n❌ Update failed: {error}")
            self.set_body(f"Update failed: {error}")
        gc.collect()

    def _on_response(self, dialog, response_id):
        if response_id == "restart":
            self._log_sink.close()
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                sys.exit(0)
        else:
            self._closed = True
            self._log_sink.close()
            self.destroy()


# ── CSS provider (singleton — loaded once, never duplicated) ─────────────────
_APP_CSS_PROVIDER: "Gtk.CssProvider | None" = None

def _ensure_css_loaded() -> None:
    global _APP_CSS_PROVIDER
    if _APP_CSS_PROVIDER is not None:
        return  # already loaded
    _APP_CSS_PROVIDER = Gtk.CssProvider()
    _APP_CSS_PROVIDER.load_from_data(b"""
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
.badge {
    font-weight: bold;
    font-size: 0.75rem;
    padding: 2px 8px;
    border-radius: 9999px;
    color: white;
}
.badge-rpm { background-color: #34495e; }
.badge-deb { background-color: #c0392b; }
.badge-flatpak { background-color: #2980b9; }
.badge-appimage { background-color: #27ae60; }
.badge-tarball { background-color: #d35400; }
.badge-zip { background-color: #8e44ad; }
.badge-unknown { background-color: #7f8c8d; }
""")
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        _APP_CSS_PROVIDER,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


class InstallerWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Fedora Installer")
        self.set_default_size(560, 600)
        self.set_resizable(True)

        self._file_path: str | None = None
        self._closed = False
        self._installing = False
        self._cancel_token: CancelToken | None = None

        self._search_lock = threading.Lock()
        self._search_thread = None
        self._search_cancel_event = threading.Event()  # replaces bare bool — Event is thread-safe
        self._search_debounce_id: int | None = None    # GLib timer id for keystroke debounce
        self._pulse_id: int | None = None              # GLib timer id for install progress pulse

        # ── root box ──────────────────────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(root)

        # ── header bar ────────────────────────────────────────────────────────
        header = Adw.HeaderBar()
        header.add_css_class("flat")
        root.append(header)

        # Preferences button (gear icon)
        prefs_btn = Gtk.Button(icon_name="preferences-system-symbolic")
        prefs_btn.set_tooltip_text("Preferences")
        prefs_btn.connect("clicked", lambda _: PreferencesWindow(self))
        header.pack_end(prefs_btn)

        # ── update banner (hidden until a newer version is detected) ──────────
        self.update_banner = Adw.Banner()
        self.update_banner.set_button_label("Update Now")
        self.update_banner.set_revealed(False)
        self.update_banner.connect("button-clicked", self._on_update_banner_clicked)
        root.append(self.update_banner)

        # ── content ───────────────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # ── TabView and TabBar ───────────────────────────────────────────────
        self.tab_view = Adw.TabView()
        self.tab_bar = Adw.TabBar()
        self.tab_bar.set_view(self.tab_view)
        
        root.append(self.tab_bar)
        root.append(self.tab_view)

        page_install = self.tab_view.append(scroll)
        page_install.set_title("Install")

        installed_scroll = Gtk.ScrolledWindow(vexpand=True)
        installed_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._build_installed_tab(installed_scroll)

        page_installed = self.tab_view.append(installed_scroll)
        page_installed.set_title("Installed")

        # Restore last-used tab
        cfg = load_config()
        last_tab = cfg.get("last_tab", 0)
        if last_tab == 1:
            self.tab_view.set_selected_page(page_installed)

        # Persist tab choice whenever the user switches
        self.tab_view.connect("notify::selected-page", self._on_tab_changed)

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
        self._log_scroll_adj = log_scroll.get_vadjustment()  # cached — avoids get_parent() in _log

        # Use BoundedLogSink to prevent unbounded RAM growth from log output
        self._log_sink = BoundedLogSink(self.log_buffer, self._log_scroll_adj)

        # ── CSS ───────────────────────────────────────────────────────────────
        # CSS is loaded once at module level — see _APP_CSS_PROVIDER below
        _ensure_css_loaded()

        # ── window close: cancel any active install / search ─────────────────
        self.connect("close-request", self._on_close_request)

        # NOTE: background update check is now started by FedoraInstallerApp.do_activate,
        # not here, so it fires exactly once per process lifetime instead of once per window.

    # ── close-request ─────────────────────────────────────────────────────────

    def _on_close_request(self, window):
        """Cancel any running install or search before the window is destroyed,
        then quit the application so the process actually exits."""
        self._closed = True
        # Stop the log sink timer before anything else so no more GTK widget
        # access can happen from the drain callback after the window tears down
        self._log_sink.close()
        # Stop the search debounce timer so no new search threads are spawned
        self._search_cancel_event.set()
        if self._search_debounce_id is not None:
            GLib.source_remove(self._search_debounce_id)
            self._search_debounce_id = None
        # Stop the install subprocess
        if self._installing and self._cancel_token:
            self._cancel_token.cancel()
        # Stop pulse timer so it doesn't fire against a destroyed widget
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        # Quit the GLib main loop so the process exits cleanly.
        # Without this, daemon threads and GLib sources can keep the process
        # alive indefinitely after the window closes.
        app = self.get_application()
        if app is not None:
            app.quit()
        return False  # allow the window to close

    # ── update check ──────────────────────────────────────────────────────────

    def _check_for_update(self):
        """Non-blocking background check — silently ignored on any failure."""
        import weakref
        win_ref = weakref.ref(self)
        try:
            req = urllib.request.Request(VERSION_URL, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                latest = resp.read().decode().strip()
            win = win_ref()
            if win is not None and not win._closed and latest and latest != VERSION:
                GLib.idle_add(win._show_update_banner, latest)
        except Exception:
            pass  # network down, timeout, 404 — all fine, just skip

    def _show_update_banner(self, latest: str):
        if self._closed:
            return False
        self._latest_version = latest
        self.update_banner.set_title(
            f"A new version is available: v{latest}  (you have v{VERSION})"
        )
        self.update_banner.set_revealed(True)
        return False

    def _on_update_banner_clicked(self, banner):
        latest = getattr(self, "_latest_version", "latest")
        dialog = Adw.AlertDialog(
                        heading="Update Available",
            body=f"Would you like to automatically update Fedora Installer to v{latest}?"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update Now")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        
        def on_response(dlg, response_id):
            if response_id == "update":
                self._trigger_update_flow(latest)
                
        dialog.connect("response", on_response)
        dialog.present(self)

    def _trigger_update_flow(self, latest):
        pwd = self.pwd_entry.get_text().strip() or None
        if pwd:
            self._start_update_dialog(pwd)
        else:
            self._prompt_sudo_password(
                f"Updating Fedora Installer to v{latest} requires administrator privileges.",
                self._start_update_dialog
            )

    def _start_update_dialog(self, pwd):
        dlg = UpdateDialog(self, pwd)
        dlg.present()

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
        self._log_sink.push(text)

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
        self.pwd_entry.set_text("")

        # Clear log buffer so output from previous installs doesn't accumulate.
        # Also drain any leftover items in the sink queue from the previous run.
        self.log_buffer.set_text("")
        self._log_sink.close()
        self._log_sink = BoundedLogSink(self.log_buffer, self._log_scroll_adj)

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
            self._log("Cancelling installation…")
            self.cancel_btn.set_sensitive(False)
            self._cancel_token.cancel()

    def _pulse(self):
        # Fix Bug 8: guard against firing after window close; _closed is set
        # in _on_close_request before the pulse timer is removed, so there is a
        # small window where GLib fires this callback after _closed is True.
        if self._closed:
            return False
        self.progress.pulse()
        return self._installing

    def _install_thread(self, path, app_name_override, sudo_password=None):
        import weakref
        win_ref = weakref.ref(self)
        try:
            install_file(path, app_name_override, self._log,
                         sudo_password=sudo_password,
                         cancel_token=self._cancel_token)
            win = win_ref()
            if win is not None:
                GLib.idle_add(win._install_done, True, None)
        except CancelledError:
            self._cleanup_partial(path, app_name_override, sudo_password)
            win = win_ref()
            if win is not None:
                GLib.idle_add(win._install_cancelled)
        except Exception as e:
            win = win_ref()
            if win is not None:
                GLib.idle_add(win._install_done, False, str(e))
        finally:
            sudo_password = None

    def _cleanup_partial(self, path, app_name_override, sudo_password):
        ftype = detect_type(path)
        app_name = app_name_override or app_name_from_path(path)
        app_name = re.sub(r'\s+', '-', app_name)
        
        self._log("Cleaning up partial installation...")
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
        if self._closed:
            return False
        self._installing = False
        self._cancel_token = None
        # Stop pulse timer now that the install is done
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.progress.set_visible(False)
        self.cancel_btn.set_visible(False)
        self.install_btn.set_sensitive(True)

        if success:
            self._log("Installation complete!")
            # Let the sink drain the final message before closing it
            GLib.timeout_add(200, self._log_sink.close)
            self.refresh_installed_tab()  # already on main thread via idle_add
            # Desktop notification — useful when the user switched windows
            app_name = (
                self.name_entry.get_text().strip()
                or (os.path.basename(self._file_path) if self._file_path else "App")
            )
            send_notification(
                f"{app_name} installed",
                "The app is now available in your GNOME launcher.",
            )
            dialog = Adw.AlertDialog(
                                heading="Installed!",
                body="The app is now available in your GNOME launcher.",
            )
            dialog.add_response("ok", "Great!")
            dialog.present(self)
        else:
            self._log(f"❌ Error: {error}")
            GLib.timeout_add(200, self._log_sink.close)
            send_notification(
                "Installation failed",
                str(error or "Unknown error"),
                urgency="critical",
            )
            dialog = Adw.AlertDialog(
                                heading="Installation failed",
                body=error or "Unknown error",
            )
            dialog.add_response("ok", "OK")
            dialog.present(self)
        gc.collect()
        return False

    def _install_cancelled(self):
        """Called on the main thread when the install thread exits via cancel."""
        if self._closed:
            return False
        self._installing = False
        self._cancel_token = None
        # Stop pulse timer
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.progress.set_visible(False)
        self.cancel_btn.set_visible(False)
        self.install_btn.set_sensitive(True)
        self._log("Installation cancelled.")
        GLib.timeout_add(200, self._log_sink.close)
        send_notification(
            "Installation cancelled",
            "The installation was cancelled. Partial files may remain.",
            urgency="low",
        )
        dialog = Adw.AlertDialog(
                        heading="Cancelled",
            body="The installation was cancelled. Partial files may remain.",
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)
        gc.collect()
        return False

    def _build_installed_tab(self, scroll):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(32)
        content.set_margin_end(32)
        scroll.set_child(content)

        # ── Section A: Installed by Fedora Installer ──────────────────────────
        self.receipts_group = Adw.PreferencesGroup(title="Installed by Fedora Installer")
        
        self.receipts_list = Gtk.ListBox()
        self.receipts_list.add_css_class("boxed-list")
        self.receipts_group.add(self.receipts_list)
        
        self.empty_status = Adw.StatusPage()
        self.empty_status.set_title("No applications installed yet")
        self.empty_status.set_description("Applications you install via Fedora Installer will appear here.")
        self.empty_status.set_icon_name("system-software-install-symbolic")
        self.empty_status.set_margin_top(16)
        self.empty_status.set_margin_bottom(16)
        
        content.append(self.receipts_group)
        content.append(self.empty_status)

        # ── Section B: Search All Installed Apps ──────────────────────────────
        search_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.append(search_section)
        
        search_title = Gtk.Label(label="Search All Installed Apps")
        search_title.add_css_class("title-2")
        search_title.set_halign(Gtk.Align.START)
        search_section.append(search_title)
        
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search live across DNF, Flatpak, AppImages, /opt...")
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_section.append(self.search_entry)
        
        self.search_warning_label = Gtk.Label(label="* Not installed via Fedora Installer — removal is best-effort for AppImage/Manual, clean for DNF/Flatpak.")
        self.search_warning_label.add_css_class("dim-label")
        self.search_warning_label.add_css_class("caption")
        self.search_warning_label.set_halign(Gtk.Align.START)
        self.search_warning_label.set_wrap(True)
        search_section.append(self.search_warning_label)
        
        # Results Groups
        self.search_flatpak_list = Gtk.ListBox()
        self.search_flatpak_list.add_css_class("boxed-list")
        self.search_flatpak_group = Adw.PreferencesGroup(title="Flatpak Applications")
        self.search_flatpak_group.add(self.search_flatpak_list)
        self.search_flatpak_group.set_visible(False)
        content.append(self.search_flatpak_group)

        self.search_appimage_list = Gtk.ListBox()
        self.search_appimage_list.add_css_class("boxed-list")
        self.search_appimage_group = Adw.PreferencesGroup(title="AppImages (in ~/.local/bin)")
        self.search_appimage_group.add(self.search_appimage_list)
        self.search_appimage_group.set_visible(False)
        content.append(self.search_appimage_group)

        self.search_manual_list = Gtk.ListBox()
        self.search_manual_list.add_css_class("boxed-list")
        self.search_manual_group = Adw.PreferencesGroup(title="Manual (/opt directories)")
        self.search_manual_group.add(self.search_manual_list)
        self.search_manual_group.set_visible(False)
        content.append(self.search_manual_group)

        self.search_dnf_list = Gtk.ListBox()
        self.search_dnf_list.add_css_class("boxed-list")
        self.search_dnf_group = Adw.PreferencesGroup(title="DNF Packages")
        self.search_dnf_group.add(self.search_dnf_list)
        self.search_dnf_group.set_visible(False)
        content.append(self.search_dnf_group)

        self.refresh_installed_tab()

    def refresh_installed_tab(self):
        while (child := self.receipts_list.get_first_child()):
            self.receipts_list.remove(child)
            
        receipts = read_receipts()
        if receipts:
            self.receipts_group.set_visible(True)
            self.empty_status.set_visible(False)
            
            for r in receipts:
                row = Adw.ActionRow()
                row.set_title(r.get("app_name", "Unknown App"))
                
                installed_at = r.get("installed_at", "")
                try:
                    date_str = installed_at.split("T")[0]
                except Exception:
                    date_str = installed_at
                    
                paths = r.get("paths", {})
                path_val = paths.get("install_dir") or paths.get("desktop_entry") or r.get("package_name") or ""
                row.set_subtitle(f"Installed: {date_str}  ·  {path_val}")
                
                itype = r.get("install_type", "unknown")
                badge = Gtk.Label(label=itype.upper())
                badge.add_css_class("badge")
                badge.add_css_class(f"badge-{itype.lower()}")
                badge.set_valign(Gtk.Align.CENTER)
                row.add_prefix(badge)
                
                remove_btn = Gtk.Button()
                remove_btn.set_icon_name("user-trash-symbolic")
                remove_btn.add_css_class("flat")
                remove_btn.add_css_class("destructive-action")
                remove_btn.set_valign(Gtk.Align.CENTER)
                remove_btn.connect("clicked", self._on_remove_receipt_clicked, r)
                row.add_suffix(remove_btn)
                
                self.receipts_list.append(row)
        else:
            self.receipts_group.set_visible(False)
            self.empty_status.set_visible(True)

    def _on_search_changed(self, entry):
        query = entry.get_text().strip()
        if not query:
            self._clear_search_results()
            return

        # Cancel any in-flight search
        self._search_cancel_event.set()

        # Cancel pending debounce timer and start a fresh one (300 ms)
        if self._search_debounce_id is not None:
            GLib.source_remove(self._search_debounce_id)

        def _fire():
            self._search_debounce_id = None
            # Fix Bug 9: _search_lock and _search_thread were declared in __init__
            # but never used, leaving a race: the old thread could see the event
            # cleared and post stale results to the UI right after _fire() cleared
            # it. Now we hold the lock while clearing and replacing the thread ref
            # so _run_search checks the lock before posting results.
            with self._search_lock:
                self._search_cancel_event.clear()
                t = threading.Thread(target=self._run_search, args=(query,), daemon=True)
                self._search_thread = t
            t.start()
            return False  # one-shot timer

        self._search_debounce_id = GLib.timeout_add(300, _fire)

    def _run_search(self, query):
        results = {"DNF": [], "Flatpak": [], "AppImage": [], "Manual": []}
        
        cancelled = self._search_cancel_event

        # 1. Search AppImages
        if cancelled.is_set(): return
        bin_dir = os.path.expanduser("~/.local/bin")
        if os.path.isdir(bin_dir):
            try:
                for f in os.listdir(bin_dir):
                    if cancelled.is_set(): return
                    if f.lower().endswith(".appimage") and query.lower() in f.lower():
                        if len(results["AppImage"]) < 50:
                            results["AppImage"].append({
                                "name": f.replace(".AppImage", "").replace(".appimage", ""),
                                "path": os.path.join(bin_dir, f)
                            })
                        else:
                            break
            except Exception:
                pass

        # 2. Search Manual (/opt)
        if cancelled.is_set(): return
        opt_dir = "/opt"
        if os.path.isdir(opt_dir):
            try:
                for d in os.listdir(opt_dir):
                    if cancelled.is_set(): return
                    full_path = os.path.join(opt_dir, d)
                    if os.path.isdir(full_path) and query.lower() in d.lower():
                        if len(results["Manual"]) < 50:
                            results["Manual"].append({
                                "name": d,
                                "path": full_path
                            })
                        else:
                            break
            except Exception:
                pass

        # 3. Search Flatpak
        if cancelled.is_set(): return
        try:
            p_user = subprocess.Popen(["flatpak", "list", "--app", "--user", "--json"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                user_items = json.load(p_user.stdout)
            except Exception:
                user_items = []
            p_user.wait()

            for item in user_items:
                if cancelled.is_set(): return
                name = item.get("name", "")
                app_id = item.get("application_id", "")
                if query.lower() in name.lower() or query.lower() in app_id.lower():
                    if len(results["Flatpak"]) < 50:
                        results["Flatpak"].append({
                            "name": name,
                            "package_name": app_id,
                            "install_type": "flatpak"
                        })

            # Fix Bug 7: p_sys was spawned before the cancel check, so if
            # cancelled mid-iteration above it would return and leave p_sys
            # running as a zombie. Now we check before spawning p_sys.
            if cancelled.is_set(): return
            p_sys = subprocess.Popen(["flatpak", "list", "--app", "--system", "--json"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                sys_items = json.load(p_sys.stdout)
            except Exception:
                sys_items = []
            p_sys.wait()

            for item in sys_items:
                if cancelled.is_set(): return
                name = item.get("name", "")
                app_id = item.get("application_id", "")
                if query.lower() in name.lower() or query.lower() in app_id.lower():
                    if not any(x["package_name"] == app_id for x in results["Flatpak"]):
                        if len(results["Flatpak"]) < 50:
                            results["Flatpak"].append({
                                "name": name,
                                "package_name": app_id,
                                "install_type": "flatpak"
                            })
        except Exception:
            pass

        # 4. Search DNF (rpm -qa) — capped at 15 s to avoid blocking forever
        if cancelled.is_set(): return
        try:
            proc = subprocess.Popen(
                ["rpm", "-qa", "--qf", "%{NAME}|%{SUMMARY}\n"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            timer = threading.Timer(15.0, proc.terminate)
            timer.start()
            try:
                for raw_line in proc.stdout:
                    if cancelled.is_set():
                        proc.terminate()
                        break
                    line = raw_line.decode(errors="replace")
                    if "|" in line:
                        name, summary = line.split("|", 1)
                        if query.lower() in name.lower() or query.lower() in summary.lower():
                            if len(results["DNF"]) < 50:
                                results["DNF"].append({
                                    "name": name,
                                    "summary": summary,
                                    "package_name": name,
                                    "install_type": "rpm"
                                })
                            else:
                                proc.terminate()
                                break
            finally:
                timer.cancel()
                if proc.poll() is None:
                    proc.terminate()
                proc.wait()
        except Exception:
            pass

        # Fix Bug 9: hold the lock to check if we are still the current search
        # thread before posting results, closing the race between _fire() clearing
        # the event and this thread finishing.
        with self._search_lock:
            if not cancelled.is_set() and self._search_thread is threading.current_thread():
                GLib.idle_add(self._update_search_results_ui, results)

    def _clear_search_results(self):
        for lst in (self.search_flatpak_list, self.search_appimage_list, self.search_manual_list, self.search_dnf_list):
            while (child := lst.get_first_child()):
                lst.remove(child)
        self.search_flatpak_group.set_visible(False)
        self.search_appimage_group.set_visible(False)
        self.search_manual_group.set_visible(False)
        self.search_dnf_group.set_visible(False)

    def _update_search_results_ui(self, results):
        if self._closed:
            return False
        self._clear_search_results()
        
        flatpaks = results.get("Flatpak", [])
        if flatpaks:
            self.search_flatpak_group.set_visible(True)
            for item in flatpaks[:50]:
                row = Adw.ActionRow()
                row.set_title(item["name"])
                row.set_subtitle(item["package_name"])
                
                btn = Gtk.Button()
                btn.set_icon_name("user-trash-symbolic")
                btn.add_css_class("flat")
                btn.add_css_class("destructive-action")
                btn.set_valign(Gtk.Align.CENTER)
                btn.connect("clicked", self._on_remove_search_clicked, "flatpak", item["name"], item["package_name"])
                row.add_suffix(btn)
                
                self.search_flatpak_list.append(row)
                
        appimages = results.get("AppImage", [])
        if appimages:
            self.search_appimage_group.set_visible(True)
            for item in appimages[:50]:
                row = Adw.ActionRow()
                row.set_title(item["name"])
                row.set_subtitle(item["path"])
                
                btn = Gtk.Button()
                btn.set_icon_name("user-trash-symbolic")
                btn.add_css_class("flat")
                btn.add_css_class("destructive-action")
                btn.set_valign(Gtk.Align.CENTER)
                btn.connect("clicked", self._on_remove_search_clicked, "appimage", item["name"], item["path"])
                row.add_suffix(btn)
                
                self.search_appimage_list.append(row)

        manual = results.get("Manual", [])
        if manual:
            self.search_manual_group.set_visible(True)
            for item in manual[:50]:
                row = Adw.ActionRow()
                row.set_title(item["name"])
                row.set_subtitle(item["path"])
                
                btn = Gtk.Button()
                btn.set_icon_name("user-trash-symbolic")
                btn.add_css_class("flat")
                btn.add_css_class("destructive-action")
                btn.set_valign(Gtk.Align.CENTER)
                btn.connect("clicked", self._on_remove_search_clicked, "manual", item["name"], item["path"])
                row.add_suffix(btn)
                
                self.search_manual_list.append(row)

        dnf = results.get("DNF", [])
        if dnf:
            self.search_dnf_group.set_visible(True)
            for item in dnf[:50]:
                row = Adw.ActionRow()
                row.set_title(item["name"])
                row.set_subtitle(item.get("summary", ""))
                
                btn = Gtk.Button()
                btn.set_icon_name("user-trash-symbolic")
                btn.add_css_class("flat")
                btn.add_css_class("destructive-action")
                btn.set_valign(Gtk.Align.CENTER)
                btn.connect("clicked", self._on_remove_search_clicked, "dnf", item["name"], item["package_name"])
                row.add_suffix(btn)
                
                self.search_dnf_list.append(row)
        gc.collect()

    def _on_remove_receipt_clicked(self, btn, receipt):
        app_name = receipt.get("app_name")
        install_type = receipt.get("install_type")
        paths = receipt.get("paths", {})
        package_name = receipt.get("package_name")
        receipt_file = receipt.get("receipt_file")
        
        dialog = Adw.AlertDialog(
                        heading="Uninstall Application?",
            body=f"Are you sure you want to uninstall {app_name}?"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
        
        def on_confirm_response(dialog, response_id):
            if response_id == "uninstall":
                needs_sudo = (install_type in ("rpm", "deb")) or (paths.get("install_dir") and paths.get("install_dir").startswith("/opt")) or (paths.get("symlink") and paths.get("symlink").startswith(("/usr/local/bin", "/usr/bin")))
                
                if needs_sudo:
                    self._prompt_sudo_password(
                        f"Uninstalling {app_name} requires administrator privileges.",
                        lambda pwd: self._start_uninstall(app_name, install_type, paths, package_name, receipt_file, pwd)
                    )
                else:
                    self._start_uninstall(app_name, install_type, paths, package_name, receipt_file, None)
                    
        dialog.connect("response", on_confirm_response)
        dialog.present(self)

    def _on_remove_search_clicked(self, btn, item_type, name, path_or_pkg):
        if item_type == 'manual':
            dialog = Adw.AlertDialog(
                                heading="Destructive Action Warning",
                body=f"Warning: This will delete the entire directory {path_or_pkg} and all of its contents. This action cannot be undone.\n\nAre you sure you want to proceed?"
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("proceed", "Delete Permanently")
            dialog.set_response_appearance("proceed", Adw.ResponseAppearance.DESTRUCTIVE)
            confirm_response_id = "proceed"
        else:
            dialog = Adw.AlertDialog(
                                heading="Uninstall Application?",
                body=f"Are you sure you want to uninstall {name}?"
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("uninstall", "Uninstall")
            dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)
            confirm_response_id = "uninstall"
            
        def on_confirm_response(dialog, response_id):
            if response_id == confirm_response_id:
                paths = {}
                package_name = None
                install_type = None
                
                if item_type == 'dnf':
                    install_type = "rpm"
                    package_name = path_or_pkg
                elif item_type == 'flatpak':
                    install_type = "flatpak"
                    package_name = path_or_pkg
                elif item_type == 'appimage':
                    install_type = "appimage"
                    paths = {
                        "install_dir": path_or_pkg,
                        "desktop_entry": os.path.expanduser(f"~/.local/share/applications/{name.lower().replace(' ', '-')}.desktop")
                    }
                elif item_type == 'manual':
                    install_type = "tarball"
                    paths = {
                        "install_dir": path_or_pkg,
                        "symlink": f"/usr/local/bin/{name}",
                        "desktop_entry": os.path.expanduser(f"~/.local/share/applications/{name.lower().replace(' ', '-')}.desktop")
                    }
                
                needs_sudo = (item_type in ('dnf', 'manual'))
                
                if needs_sudo:
                    self._prompt_sudo_password(
                        f"Uninstalling {name} requires administrator privileges.",
                        lambda pwd: self._start_uninstall(name, install_type, paths, package_name, None, pwd)
                    )
                else:
                    self._start_uninstall(name, install_type, paths, package_name, None, None)
                    
        dialog.connect("response", on_confirm_response)
        dialog.present(self)

    def _prompt_sudo_password(self, message, callback):
        dialog = Adw.AlertDialog(
                        heading="Authentication Required",
            body=message
        )
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(8)
        
        pwd_entry = Gtk.PasswordEntry()
        pwd_entry.set_placeholder_text("Password")
        pwd_entry.set_activates_default(True)
        box.append(pwd_entry)
        
        dialog.set_extra_child(box)
        
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("authenticate", "Authenticate")
        dialog.set_default_response("authenticate")
        dialog.set_response_appearance("authenticate", Adw.ResponseAppearance.SUGGESTED)
        
        def on_response(dlg, response_id):
            if response_id == "authenticate":
                password = pwd_entry.get_text().strip()
                pwd_entry.set_text("")
                callback(password)
                password = None
                
        dialog.connect("response", on_response)
        dialog.present(self)

    def _start_uninstall(self, app_name, install_type, paths, package_name, receipt_file, sudo_password):
        dlg = UninstallDialog(self, app_name, install_type, paths, package_name, receipt_file, sudo_password)
        dlg.present()

    def _on_tab_changed(self, tab_view, _param):
        """Persist the currently active tab index to config.json.
        
        notify::selected-page fires on every internal GTK property update,
        not just user interaction. We debounce with a cached index to avoid
        hammering load_config/save_config in a tight loop.
        """
        pages = tab_view.get_pages()
        selected = tab_view.get_selected_page()
        for i in range(pages.get_n_items()):
            if pages.get_item(i) == selected:
                # Only write if the tab actually changed
                if getattr(self, "_last_saved_tab", None) == i:
                    return
                self._last_saved_tab = i
                # Defer the write off the GTK main loop
                GLib.idle_add(self._save_tab_config, i)
                break

    def _save_tab_config(self, index: int):
        cfg = load_config()
        cfg["last_tab"] = index
        save_config(cfg)
        return False



# ── First-launch dialog ───────────────────────────────────────────────────────
class FirstLaunchDialog(Adw.Window):
    """Shown once to let the user pick their .deb install method."""

    def __init__(self, parent, on_done):
        super().__init__(transient_for=parent, modal=True)
        self.set_title("Welcome to Fedora Installer")
        self.set_default_size(420, -1)
        self.set_resizable(False)
        self._on_done = on_done

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_content(box)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        box.append(header)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        inner.set_margin_top(24)
        inner.set_margin_bottom(24)
        inner.set_margin_start(28)
        inner.set_margin_end(28)
        box.append(inner)

        # Icon + title
        icon = Gtk.Image.new_from_icon_name("system-software-install")
        icon.set_pixel_size(56)
        inner.append(icon)

        title = Gtk.Label(label="Choose your .deb install method")
        title.add_css_class("title-2")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        inner.append(title)

        sub = Gtk.Label(
            label="This only applies to .deb files. You can change it later in Preferences."
        )
        sub.set_wrap(True)
        sub.set_justify(Gtk.Justification.CENTER)
        sub.add_css_class("dim-label")
        inner.append(sub)

        # Choice group
        group = Adw.PreferencesGroup()
        inner.append(group)

        # distrobox row
        db_row = Adw.ActionRow()
        db_row.set_title("distrobox  <span weight=\'bold\' foreground=\'#3584e4\'>Recommended</span>")
        db_row.set_use_markup(True)
        db_row.set_subtitle("Installs inside a Debian container — reliable for any .deb")
        self._db_check = Gtk.CheckButton()
        self._db_check.set_active(True)
        db_row.add_prefix(self._db_check)
        db_row.set_activatable_widget(self._db_check)
        group.add(db_row)

        # alien row
        alien_row = Adw.ActionRow()
        alien_row.set_title("alien")
        alien_row.set_subtitle("Converts .deb → .rpm — lighter but may fail on complex packages")
        self._alien_check = Gtk.CheckButton()
        self._alien_check.set_group(self._db_check)
        alien_row.add_prefix(self._alien_check)
        alien_row.set_activatable_widget(self._alien_check)
        group.add(alien_row)

        # Confirm button
        confirm_btn = Gtk.Button(label="Confirm & Continue")
        confirm_btn.add_css_class("suggested-action")
        confirm_btn.add_css_class("pill")
        confirm_btn.set_halign(Gtk.Align.CENTER)
        confirm_btn.connect("clicked", self._on_confirm)
        inner.append(confirm_btn)

    def _on_confirm(self, _btn):
        method = "alien" if self._alien_check.get_active() else "distrobox"
        cfg = load_config()
        cfg["deb_method"] = method
        cfg["first_launch_done"] = True
        save_config(cfg)
        self.close()
        self._on_done()


# ── Preferences window ────────────────────────────────────────────────────────
class PreferencesWindow(Adw.PreferencesDialog):
    """Settings panel accessible from the header bar gear icon."""

    def __init__(self, parent):
        super().__init__()
        self.set_title("Preferences")
        self.set_search_enabled(False)

        page = Adw.PreferencesPage()
        page.set_title("General")
        page.set_icon_name("preferences-system-symbolic")
        self.add(page)

        # .deb method group
        deb_group = Adw.PreferencesGroup()
        deb_group.set_title(".deb Install Method")
        deb_group.set_description(
            "How Fedora Installer handles .deb packages. "
            "distrobox is more reliable; alien is faster but may fail."
        )
        page.add(deb_group)

        cfg = load_config()
        current = cfg.get("deb_method") or "distrobox"

        db_row = Adw.ActionRow()
        db_row.set_title("distrobox")
        db_row.set_subtitle("Recommended — installs inside a Debian container")
        self._db_check = Gtk.CheckButton()
        self._db_check.set_active(current == "distrobox")
        db_row.add_prefix(self._db_check)
        db_row.set_activatable_widget(self._db_check)
        deb_group.add(db_row)

        alien_row = Adw.ActionRow()
        alien_row.set_title("alien")
        alien_row.set_subtitle("Converts .deb → .rpm — lighter but may fail on complex packages")
        self._alien_check = Gtk.CheckButton()
        self._alien_check.set_group(self._db_check)
        self._alien_check.set_active(current == "alien")
        alien_row.add_prefix(self._alien_check)
        alien_row.set_activatable_widget(self._alien_check)
        deb_group.add(alien_row)

        self._db_check.connect("toggled", self._on_toggle)
        self._alien_check.connect("toggled", self._on_toggle)

        # Update group
        update_group = Adw.PreferencesGroup()
        update_group.set_title("Updates")
        update_group.set_description("Manage updates for Fedora Installer itself.")
        page.add(update_group)

        update_row = Adw.ActionRow()
        update_row.set_title("Check for Updates Now")
        update_row.set_subtitle(f"Current version: v{VERSION}")
        
        self.update_btn = Gtk.Button(label="Check")
        self.update_btn.add_css_class("pill")
        self.update_btn.set_valign(Gtk.Align.CENTER)
        self.update_btn.connect("clicked", self._on_check_updates_clicked, parent)
        update_row.add_suffix(self.update_btn)
        update_group.add(update_row)

        self.present(parent)

    def _on_toggle(self, _btn):
        method = "alien" if self._alien_check.get_active() else "distrobox"
        cfg = load_config()
        cfg["deb_method"] = method
        save_config(cfg)

    def _on_check_updates_clicked(self, btn, parent):
        btn.set_sensitive(False)
        btn.set_label("Checking…")
        
        def _check():
            try:
                req = urllib.request.Request(VERSION_URL, method="GET")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    latest = resp.read().decode().strip()
                if latest and latest != VERSION:
                    GLib.idle_add(self._show_update_available, parent, latest)
                else:
                    GLib.idle_add(self._show_no_update)
            except Exception as e:
                GLib.idle_add(self._show_error, str(e))
                
        threading.Thread(target=_check, daemon=True).start()

    def _show_update_available(self, parent, latest):
        self.update_btn.set_sensitive(True)
        self.update_btn.set_label("Update Available")
        
        dialog = Adw.AlertDialog(
                        heading="Update Available",
            body=f"A new version (v{latest}) is available. Would you like to install it now?"
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("update", "Update Now")
        dialog.set_response_appearance("update", Adw.ResponseAppearance.SUGGESTED)
        
        def on_response(dlg, response_id):
            if response_id == "update":
                self.close()
                parent._trigger_update_flow(latest)
                
        dialog.connect("response", on_response)
        dialog.present(self)

    def _show_no_update(self):
        self.update_btn.set_sensitive(True)
        self.update_btn.set_label("Check")
        
        dialog = Adw.AlertDialog(
                        heading="Up to Date",
            body="You are already running the latest version of Fedora Installer."
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)

    def _show_error(self, err_msg):
        self.update_btn.set_sensitive(True)
        self.update_btn.set_label("Check")
        
        dialog = Adw.AlertDialog(
                        heading="Check Failed",
            body=f"Could not check for updates:\n{err_msg}"
        )
        dialog.add_response("ok", "OK")
        dialog.present(self)


class FedoraInstallerApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.path_arg = None
        self._first_launch_shown = False
        self._update_check_done = False  # Fix: prevent spawning a new update-check thread on every activation

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            win = InstallerWindow(application=self)
            if self.path_arg:
                win._set_file(self.path_arg)
            # Fix: only start the background update check once, when the window is first created.
            # Previously this was called inside InstallerWindow.__init__ via threading.Thread, which
            # meant every do_activate (e.g. opening a file from Nautilus) spawned a new thread.
            if not self._update_check_done:
                self._update_check_done = True
                threading.Thread(target=win._check_for_update, daemon=True).start()
        win.present()
        # Show first-launch dialog once per process lifetime (not on every
        # window raise / do_activate call).
        if not self._first_launch_shown:
            cfg = load_config()
            if not cfg.get("first_launch_done"):
                self._first_launch_shown = True
                GLib.idle_add(lambda: FirstLaunchDialog(win, lambda: None) or False)


def main():
    app = FedoraInstallerApp()
    # allow a file path as CLI argument
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        app.path_arg = sys.argv[1]
        sys.exit(app.run([sys.argv[0]]))
    sys.exit(app.run(sys.argv))


if __name__ == "__main__":
    main()
