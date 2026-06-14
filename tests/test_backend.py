import pytest
import os
import tempfile
import threading
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, GLib

from fedora_installer import (
    app_name_from_path,
    detect_type,
    find_executable,
    BoundedLogSink,
    _MAX_QUEUED_LINES
)


def test_app_name_from_path():
    assert app_name_from_path("google-chrome-stable-120.0.x86_64.rpm") == "google-chrome-stable"
    assert app_name_from_path("VLC-3.0.18-arm64.dmg") == "VLC" # dmg is not in known list but fallback works
    assert app_name_from_path("VLC-3.0.18-arm64.AppImage") == "VLC"
    assert app_name_from_path("app.tar.gz") == "app"


def test_app_name_collision():
    # Two different files that would produce the same app_name
    # receipt system would overwrite — is that handled?
    assert app_name_from_path("vlc-3.0.18.x86_64.rpm") == \
           app_name_from_path("vlc-3.0.20.x86_64.rpm")


def test_detect_type():
    assert detect_type("/downloads/pkg.tar.gz") == "tarball"
    assert detect_type("/downloads/pkg.TAR.GZ") == "tarball"
    assert detect_type("/downloads/pkg.tar.gz.bak") == "unknown"


def test_find_executable():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a mock directory structure
        bin_dir = os.path.join(tmpdir, "bin")
        os.makedirs(bin_dir)
        
        # Create a non-executable file
        with open(os.path.join(tmpdir, "readme.txt"), "w") as f:
            f.write("text")
            
        # Create an executable in the root
        root_exec = os.path.join(tmpdir, "app-main")
        with open(root_exec, "w") as f:
            f.write("exe")
        os.chmod(root_exec, 0o755)
        
        # Create an executable in bin with .sh extension
        bin_sh = os.path.join(bin_dir, "app-main.sh")
        with open(bin_sh, "w") as f:
            f.write("exe")
        os.chmod(bin_sh, 0o755)
        
        # Create an executable in bin (best match)
        bin_exec = os.path.join(bin_dir, "app-main")
        with open(bin_exec, "w") as f:
            f.write("exe")
        os.chmod(bin_exec, 0o755)

        # Create an excluded script
        install_sh = os.path.join(tmpdir, "install.sh")
        with open(install_sh, "w") as f:
            f.write("install")
        os.chmod(install_sh, 0o755)

        # The best match should be the one in bin/ without .sh
        result = find_executable(tmpdir)
        assert result == bin_exec


def test_bounded_log_sink_concurrency():
    Gtk.init()
    buf = Gtk.TextBuffer()
    adj = Gtk.Adjustment()
    sink = BoundedLogSink(buf, adj)

    # Stress test: multiple producers
    def producer(num_messages):
        for i in range(num_messages):
            sink.push(f"Message {i}")

    threads = []
    for _ in range(10):
        t = threading.Thread(target=producer, args=(100,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()

    # The internal queue should have dropped messages gracefully if it exceeded _MAX_QUEUED_LINES
    # But it shouldn't crash or exceed maxsize
    assert sink._q.qsize() <= _MAX_QUEUED_LINES

    # Stop the timer so the test can exit
    sink.close()
