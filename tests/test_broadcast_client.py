"""Capture-side safety checks; can run without the controller or GI installed."""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "broadcast_overlay"
for module_name in ("overlay_model", "renderer"):
    spec = importlib.util.spec_from_file_location(module_name, TOOLS / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
model = sys.modules["overlay_model"]
renderer = sys.modules["renderer"]


def payload(now=None, **changes):
    now = now or datetime.now(UTC)
    return {
        "schema_version": 1, "device_id": "stream-2", "device_name": "Dillflix 2",
        "generated_at": now.isoformat(), "valid_until": (now + timedelta(seconds=5)).isoformat(),
        "state": "switching", "visible": True,
        "headline": "INTERNAL: Prime network retry in 60s; montreal",  # ignored
        "event_title": "Chicago Blackhawks at Detroit Red Wings", **changes,
    }


def test_only_public_copy_is_rendered():
    state, _, _ = model.decode_payload(payload(), "stream-2", wall_time=model.time.time())
    assert state.headline == "Switching live coverage"
    assert "Prime" not in state.headline


@pytest.mark.parametrize("changes", [
    {"device_id": "living-room"}, {"schema_version": True}, {"visible": "false"},
    {"state": "retrying_network"}, {"state": "hidden"}, {"event_title": {"error": "secret"}},
    {"device_name": "\x00"}, {"valid_until": "2099-01-01T00:00:00Z"},
    {"generated_at": "2099-01-01T00:00:00Z"}, {"generated_at": "2020-01-01"},
])
def test_malformed_wrong_device_or_unbounded_data_is_rejected(changes):
    with pytest.raises(ValueError):
        model.decode_payload(payload(**changes), "stream-2", wall_time=model.time.time())


def test_stale_cached_response_cannot_restore_overlay():
    with pytest.raises(ValueError, match="Expired"):
        model.decode_payload(payload(datetime.now(UTC) - timedelta(seconds=6)), "stream-2", wall_time=model.time.time())


def test_independent_expiry_and_replayed_response(monkeypatch):
    wall, monotonic = 2000000000., 100.
    monkeypatch.setattr(model.time, "time", lambda: wall)
    monkeypatch.setattr(model.time, "monotonic", lambda: monotonic)
    poller = model.OverlayPoller("http://controller/overlay", "stream-2", lambda _: None)
    message = payload(datetime.fromtimestamp(wall, UTC))
    poller.accept(message)
    assert poller.current_state().visible
    # Simulate blocked HTTP, then a wall-clock adjustment with a cached response.
    monotonic += 4
    wall -= .5
    poller.accept(message)
    monotonic += 1.01
    assert not poller.current_state().visible


def test_older_response_cannot_replace_fresh_state(monkeypatch):
    poller = model.OverlayPoller("http://controller/overlay", "stream-2", lambda _: None)
    now = datetime.now(UTC)
    poller.accept(payload(now, state="hidden", visible=False))
    with pytest.raises(ValueError, match="Out-of-order"):
        poller.accept(payload(now - timedelta(seconds=1)))
    assert not poller.current_state().visible


def test_transport_failure_does_not_renew_lease(monkeypatch):
    monotonic = 100.
    monkeypatch.setattr(model.time, "monotonic", lambda: monotonic)
    poller = model.OverlayPoller("http://controller/overlay", "stream-2", lambda _: None)
    poller.accept(payload())
    def offline(*args, **kwargs):
        raise OSError("private credential must not be logged")
    monkeypatch.setattr(poller._opener, "open", offline)
    with pytest.raises(OSError):
        poller.poll_once()
    monotonic += 6
    assert not poller.current_state().visible


def test_no_redirect_auth_leak():
    assert model._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other") is None


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://user:secret@host/overlay", "http://host/#secret"])
def test_invalid_endpoint(url):
    with pytest.raises(ValueError):
        model.OverlayPoller(url, "stream-2", lambda _: None)


def test_plain_text_and_irrelevant_titles():
    state, _, _ = model.decode_payload(payload(event_title="Team\nA\u202e at Team B", device_name="Dillflix\x002"), "stream-2", wall_time=model.time.time())
    assert state.event_title == "Team A at Team B"
    assert state.name == "Dillflix 2"
    state, _, _ = model.decode_payload(payload(state="waiting"), "stream-2", wall_time=model.time.time())
    assert state.event_title is None


def test_hidden_produces_no_image_and_long_titles_fit():
    assert renderer.render_overlay(model.OverlayState()) is None
    state = model.OverlayState("Dillflix 2", "switching", "ignored", "A very long team name versus another very long team name " * 5, True)
    png = renderer.render_overlay(state)
    from PIL import Image
    image = Image.open(BytesIO(png))
    assert image.mode == "RGBA" and image.size == (780, 144)
    assert image.getpixel((779, 0))[3] == 0
    lines = renderer.wrap_title(state.event_title, renderer.font(32), 724)
    assert len(lines) == 2 and lines[-1].endswith("…")
    assert all(renderer.font(32).getlength(line) <= 724 for line in lines)
