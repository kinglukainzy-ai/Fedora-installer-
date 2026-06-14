import pytest
import os
import tempfile
import threading
import time
from unittest.mock import patch, MagicMock

from fedora_installer import (
    app_name_from_path,
    detect_type,
    find_executable,
    BoundedLogSink,
    _MAX_QUEUED_LINES,
    InstallerBackend
)


def test_app_name_from_path():
    assert app_name_from_path("google-chrome-stable-120.0.x86_64.rpm") == "google-chrome-stable"
    assert app_name_from_path("VLC-3.0.18-arm64.dmg") == "VLC" # dmg is not in known list but fallback works
    assert app_name_from_path("VLC-3.0.18-arm64.AppImage") == "VLC"
    assert app_name_from_path("app.tar.gz") == "app"
    # Negative test: pure binary with no extension
    assert app_name_from_path("simple-binary") == "simple-binary"
    assert app_name_from_path("app") == "app"


def test_app_name_collision():
    # Two different files that would produce the same app_name
    # receipt system would overwrite — is that handled?
    assert app_name_from_path("vlc-3.0.18.x86_64.rpm") == \
           app_name_from_path("vlc-3.0.20.x86_64.rpm")


def test_detect_type():
    assert detect_type("/downloads/pkg.tar.gz") == "tarball"
    assert detect_type("/downloads/pkg.TAR.GZ") == "tarball"
    assert detect_type("/downloads/pkg.tar.gz.bak") == "unknown"
    # Negative test: genuinely unknown extension
    assert detect_type("/downloads/pkg.exe") == "unknown"
    assert detect_type("/downloads/pkg") == "unknown"


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
    gi = pytest.importorskip("gi")
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, GLib

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

    # We cannot easily test the asynchronous _drain without spinning a GLib MainLoop,
    # but we can verify the queue enforces bounds.
    assert sink._q.qsize() <= _MAX_QUEUED_LINES

    # Stop the timer so the test can exit
    sink.close()


def test_bounded_log_sink_drains_on_glib_main_loop():
    gi = pytest.importorskip("gi")
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, GLib

    Gtk.init()
    buf = Gtk.TextBuffer()
    adj = Gtk.Adjustment()
    sink = BoundedLogSink(buf, adj)

    try:
        sink.push("first")
        sink.push("second")

        deadline = time.monotonic() + 1.0
        context = GLib.MainContext.default()
        while buf.get_line_count() <= 1 and time.monotonic() < deadline:
            context.iteration(True)

        start = buf.get_start_iter()
        end = buf.get_end_iter()
        assert buf.get_text(start, end, False) == "first\nsecond\n"
    finally:
        sink.close()


def test_installer_backend_init():
    backend = InstallerBackend(deb_method="alien")
    assert backend.deb_method == "alien"


def test_installer_backend_rejects_unknown_deb_method():
    backend = InstallerBackend(deb_method="unknown")
    assert backend.deb_method == "distrobox"


@patch('fedora_installer.InstallerBackend._install_rpm')
def test_installer_backend_dispatch(mock_install_rpm):
    backend = InstallerBackend()
    # Mock log and cancel_token
    log_mock = MagicMock()
    token_mock = MagicMock()
    
    # Trigger dispatch for rpm
    backend.install("test.rpm", "app_override", log_mock, "pass", token_mock)
    
    # Assert dispatch worked
    mock_install_rpm.assert_called_once_with("test.rpm", "app_override", log_mock, "pass", token_mock)

    # Assert raises on unknown
    with pytest.raises(RuntimeError, match="Unrecognised file type"):
        backend.install("test.unknown", None, log_mock)
