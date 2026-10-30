#!/usr/bin/env python3
"""Exercise runtime overlay pixels using synthetic video, without devices/network.

Run with the capture host's system Python after installing the optional native
dependencies. Both source formats are checked in memory; no audio, encoder,
capture device, RTMP sink, controller request, or output file is opened.
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace


def exercise(format_name, Gst, GLib, GdkPixbuf, OverlayRunner, geometry, render, State):
    pipeline = Gst.parse_launchv_full(
        [
            "videotestsrc", "pattern=black", "is-live=true", "!",
            f"video/x-raw,format={format_name},width=640,height=360,framerate=15/1", "!",
            "gdkpixbufoverlay", "name=dillflixOverlay", "alpha=0", "!",
            "videoconvert", "!", "video/x-raw,format=RGB", "!",
            "appsink", "name=frames", "sync=false", "max-buffers=1", "drop=true",
        ], None, Gst.ParseFlags.FATAL_ERRORS,
    )
    runner = OverlayRunner(pipeline, Gst, GLib, GdkPixbuf, render, geometry(640, 360))
    current = SimpleNamespace(state=State())
    runner.poller = SimpleNamespace(current_state=lambda: current.state)
    sink, bus = pipeline.get_by_name("frames"), pipeline.get_bus()

    def await_pixels(predicate, description):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = bus.pop_filtered(Gst.MessageType.ERROR)
            if message is not None:
                error, _ = message.parse_error()
                raise RuntimeError(f"Synthetic pipeline failed: {error.message}")
            sample = sink.emit("try-pull-sample", Gst.SECOND)
            if sample is None:
                continue
            buffer = sample.get_buffer()
            pixels = buffer.extract_dup(0, buffer.get_size())
            if predicate(pixels):
                return pixels
        raise AssertionError(f"{format_name}: timed out waiting for {description}")

    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Synthetic video could not start")
        baseline = await_pixels(lambda pixels: bool(pixels), "baseline frame")
        input_caps = runner.overlay.get_static_pad("sink").get_current_caps()
        assert input_caps.get_structure(0).get_string("format") == format_name

        waiting = State(name="Dillflix 1", state="waiting", visible=True)
        current.state = waiting
        runner.refresh()
        card_a = await_pixels(lambda pixels: pixels != baseline, "visible waiting card")
        # RGB output must contain the red brand accent, not a channel-swapped
        # blue accent caused by reusing a plugin-mutated pixbuf. Allow chroma
        # subsampling to soften the thin accent in these small test frames.
        assert any(
            card_a[i] > 80 and card_a[i] > card_a[i + 1] + 50 and card_a[i] > card_a[i + 2] + 50
            for i in range(0, len(card_a) - 2, 3)
        ), f"{format_name}: red accent missing"

        current.state = State(
            name="Dillflix 2", state="switching", visible=True,
            event_title="Chicago Blackhawks at Detroit Red Wings",
        )
        runner.refresh()
        await_pixels(lambda pixels: pixels not in (baseline, card_a), "updated event card")

        current.state = waiting
        runner.refresh()
        await_pixels(lambda pixels: pixels == card_a, "identical colors after card replacement")
        runner.refresh()  # Identical state must remain visually stable.
        await_pixels(lambda pixels: pixels == card_a, "unchanged card")

        current.state = State()  # Equivalent to lease expiry or started playback.
        runner.refresh_lease()
        await_pixels(lambda pixels: pixels == baseline, "unmodified video after hiding")
        assert pipeline.get_state(Gst.SECOND)[1] == Gst.State.PLAYING
        print(f"PASS {format_name}: visible, replaced, stable colors, hidden; pipeline stayed PLAYING")
    finally:
        pipeline.set_state(Gst.State.NULL)


def main():
    try:
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib, Gst
        from overlay_model import OverlayState
        from renderer import render_overlay
        from runner import OverlayRunner, geometry

        Gst.init(None)
        for element in ("videotestsrc", "gdkpixbufoverlay", "videoconvert", "appsink"):
            if Gst.ElementFactory.find(element) is None:
                raise RuntimeError(f"Missing GStreamer element: {element}")
        for format_name in ("YUY2", "NV12"):
            exercise(format_name, Gst, GLib, GdkPixbuf, OverlayRunner, geometry, render_overlay, OverlayState)
        print("All synthetic overlay checks passed. Hardware capture/encoding performance is not tested.")
        return 0
    except Exception as exc:
        print(f"Overlay self-test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
