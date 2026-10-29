"""Capture integration checks that do not open devices or publish RTMP."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.broadcast_overlay.runner import OverlayRunner, geometry, safe_error

SCRIPT = Path(__file__).resolve().parents[1] / "tools/broadcast_overlay/dillflix_gstreamer.sh"


def bash_path():
    # Avoid Windows' unrelated WSL bash shim when Git Bash is available.
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    command = str(git_bash) if os.name == "nt" and git_bash.exists() else shutil.which("bash")
    if not command:
        pytest.skip("Bash is needed for the capture script dry-run")
    return command


def dry_run(tmp_path, mode, *, overlay=False, device="living-room"):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name in ("gst-launch-1.0", "gst-inspect-1.0"):
        stub = stubs / name
        stub.write_text("#!/usr/bin/env bash\nexit 0\n")
        stub.chmod(0o755)
    entry = tmp_path / "dry-run.sh"
    entry.write_text('export PATH="$PWD/stubs:$PATH"\nexec bash "$1" --dry-run "$2"\n')
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith("DILLFLIX_OVERLAY_") or key in {
            "STREAM", "RTMP_HOST", "RTMP_PORT", "RTMP_APP", "VIDEO_W", "VIDEO_H", "VA_ENCODER",
        }:
            env.pop(key)
    if overlay:
        env.update(
            DILLFLIX_OVERLAY_URL="http://controller/api/device/overlay",
            DILLFLIX_OVERLAY_DEVICE=device,
            DILLFLIX_OVERLAY_HEADERS_FILE="/secret/do-not-print.json",
        )
    return subprocess.run(
        [bash_path(), "--noprofile", "--norc", str(entry), str(SCRIPT), mode],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("mode", ["va", "vaapi", "mpp"])
def test_overlay_is_opt_in_and_leaves_the_six_existing_outputs(tmp_path, mode):
    plain = dry_run(tmp_path, mode)
    assert plain.returncode == 0, plain.stderr
    original = shlex.split(plain.stdout)
    assert original[:2] == ["gst-launch-1.0", "-e"]
    assert "gdkpixbufoverlay" not in original

    enabled = dry_run(tmp_path, mode, overlay=True)
    assert enabled.returncode == 0, enabled.stderr
    actual = shlex.split(enabled.stdout)
    assert actual[0] == "python3"
    assert actual[1].endswith("runner.py")
    assert "/secret/" not in enabled.stdout
    pipeline = actual[actual.index("--") + 1:]
    assert pipeline.count("gdkpixbufoverlay") == 1
    index = pipeline.index("gdkpixbufoverlay")
    assert pipeline[index - 1:index + 4] == [
        "!", "gdkpixbufoverlay", "name=dillflixOverlay", "alpha=0", "!",
    ]
    # Removing the one new element must recover the exact baseline fanout argv,
    # including audio, timings, hardware memory, bitrate, profile, and RTMP URLs.
    assert pipeline[:index - 1] + pipeline[index + 3:] == original[2:]
    assert pipeline.count("flvmux") == 6
    assert pipeline.count("rtmpsink") == 6
    assert pipeline.count("alsasrc") == 1
    if mode == "mpp":
        assert pipeline[index - 2] == "video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1"
        assert pipeline[index + 4:index + 6] == ["tee", "name=videoTee"]
    else:
        assert pipeline[index - 2] == "queue"
        assert pipeline[index + 4] == "vapostproc"
        assert index < pipeline.index("name=video60Tee")


def test_enabling_overlay_requires_explicit_device(tmp_path):
    result = dry_run(tmp_path, "va", overlay=True, device="")
    assert result.returncode == 2
    assert "DILLFLIX_OVERLAY_DEVICE is required" in result.stderr
    assert not result.stdout


def test_help_does_not_import_gi_or_require_controller_environment():
    import sys

    result = subprocess.run(
        [sys.executable, str(SCRIPT.with_name("runner.py")), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert "--width" in result.stdout


class FakeOverlay:
    def __init__(self):
        self.properties = {}

    def set_property(self, key, value):
        self.properties[key] = value


class FakeGLib:
    def __init__(self):
        self.pending = []

    def MainLoop(self):
        return SimpleNamespace(quit=lambda: None)

    def idle_add(self, callback):
        self.pending.append(callback)


class FakeLoader:
    def __init__(self):
        self.copy = object()

    def write(self, png):
        assert png == b"png"

    def close(self):
        pass

    def get_pixbuf(self):
        return SimpleNamespace(copy=lambda: self.copy)


def fake_runner(render=None):
    overlay, glib, loader = FakeOverlay(), FakeGLib(), FakeLoader()
    pipeline = SimpleNamespace(get_by_name=lambda name: overlay)
    pixbuf = SimpleNamespace(PixbufLoader=SimpleNamespace(new_with_type=lambda _: loader))
    runner = OverlayRunner(pipeline, None, glib, pixbuf, render or (lambda *_, **__: b"png"), geometry(1920, 1080))
    holder = SimpleNamespace(state=SimpleNamespace(visible=True))
    runner.poller = SimpleNamespace(current_state=lambda: holder.state)
    return runner, holder, overlay, glib, loader


def test_runtime_update_uses_fresh_mutable_pixbuf_and_hides_on_lease_expiry():
    calls = []

    def render(state, **dimensions):
        calls.append(dimensions)
        return b"png"

    runner, holder, overlay, _, loader = fake_runner(render)
    runner.refresh()
    assert overlay.properties["pixbuf"] is loader.copy
    assert overlay.properties["alpha"] == 1
    assert calls == [{"width": 780, "height": 144}]
    runner.refresh()
    assert len(calls) == 1  # No frame-by-frame rendering of unchanged content.
    holder.state = SimpleNamespace(visible=False)
    assert runner.refresh_lease() is True
    assert overlay.properties["alpha"] == 0


def test_delayed_callback_does_not_resurrect_a_visible_state_after_expiry():
    runner, holder, overlay, glib, _ = fake_runner()
    runner.on_update(holder.state)
    holder.state = SimpleNamespace(visible=False)
    glib.pending.pop()()
    assert overlay.properties["alpha"] == 0
    assert "pixbuf" not in overlay.properties


def test_renderer_failure_hides_overlay_without_terminating_video():
    def bad_renderer(*_, **__):
        raise ValueError("malformed bitmap")

    runner, _, overlay, _, _ = fake_runner(bad_renderer)
    runner.refresh()
    assert overlay.properties["alpha"] == 0
    assert runner.stopping is False
    assert runner.exit_code == 0


def test_shutdown_sends_eos_once_and_does_not_wait_for_an_http_callback():
    runner, holder, overlay, glib, _ = fake_runner()
    runner.refresh()
    events, deadlines, quits = [], [], []
    runner.gst = SimpleNamespace(Event=SimpleNamespace(new_eos=lambda: "eos"))
    runner.pipeline.send_event = lambda event: events.append(event) or True
    glib.timeout_add = lambda delay, callback: deadlines.append((delay, callback)) or 7
    runner.loop = SimpleNamespace(quit=lambda: quits.append(True))

    runner.stop()
    runner.stop()
    assert overlay.properties["alpha"] == 0
    assert events == ["eos"]
    assert deadlines[0][0] == 5000
    runner.on_update(holder.state)
    assert glib.pending == []
    # Even if EOS never arrives, the GLib deadline exits without asking the
    # network worker to finish its current request first.
    deadlines[0][1]()
    assert quits == [True]
    assert runner.refresh_lease() is False


def test_geometry_uses_capture_size_and_safe_area():
    assert geometry(1920, 1080).x == 48
    assert geometry(1280, 720).width == 520
    for width, height in [(320, 240), (1920, 1080), (3840, 2160)]:
        card = geometry(width, height)
        assert 0 < card.width <= width - 2 * card.x
        assert 0 < card.height <= height - 2 * card.y
    with pytest.raises(ValueError):
        geometry(0, 1080)


def test_native_pipeline_errors_do_not_print_url_credentials():
    message = safe_error("Could not open rtmp://user:password@host/live/key123 or https://host?token=secret")
    assert message == "Could not open <redacted URL> or <redacted URL>"
