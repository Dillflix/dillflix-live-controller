#!/usr/bin/env python3
"""Run the existing fanout pipeline with a dynamically updated public overlay.

GStreamer/GI imports are deliberately deferred so --help and unit tests work on
machines without the capture host's native plugins. No network or image rendering
runs in a GStreamer streaming callback.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
from dataclasses import dataclass
from typing import Any, Callable


def safe_error(error: object) -> str:
    """Native plugin errors can embed RTMP/HTTP URLs, including secret keys."""
    return re.sub(r"(?:https?|rtmps?)://[^\s]+", "<redacted URL>", str(error), flags=re.I)[:600]


@dataclass(frozen=True)
class Geometry:
    width: int
    height: int
    x: int
    y: int


def geometry(width: int, height: int) -> Geometry:
    """Scale the small card and safe-area margins to the captured picture."""
    if not 16 <= width <= 16384 or not 16 <= height <= 16384:
        raise ValueError("Capture dimensions must be between 16 and 16384 pixels")
    x = max(1, round(width * 48 / 1920))
    y = max(1, round(height * 42 / 1080))
    return Geometry(
        min(width - 2 * x, max(1, round(width * 780 / 1920))),
        min(height - 2 * y, max(1, round(height * 144 / 1080))),
        x,
        y,
    )


class OverlayRunner:
    """Own UI updates and pipeline lifecycle on one GLib main context."""

    def __init__(
        self, pipeline: Any, gst: Any, glib: Any, pixbuf: Any,
        render: Callable[..., bytes | None], dimensions: Geometry,
    ) -> None:
        self.pipeline = pipeline
        self.gst = gst
        self.glib = glib
        self.pixbuf = pixbuf
        self.render = render
        self.dimensions = dimensions
        self.loop = glib.MainLoop()
        self.overlay = pipeline.get_by_name("dillflixOverlay")
        if self.overlay is None:
            raise ValueError("Pipeline must contain gdkpixbufoverlay name=dillflixOverlay")
        self.overlay.set_property("alpha", 0.0)
        self.overlay.set_property("offset-x", dimensions.x)
        # A negative offset anchors the card to the lower edge in the plugin's
        # default pixels-relative-to-edges positioning mode.
        self.overlay.set_property("offset-y", -dimensions.y)
        self.overlay.set_property("overlay-width", dimensions.width)
        self.overlay.set_property("overlay-height", dimensions.height)
        self.poller: Any = None
        self.last_state: Any = None
        self.stopping = False
        self.exit_code = 0
        self.refresh_timer: int | None = None
        self.eos_timer: int | None = None

    def on_update(self, _state: Any) -> None:
        # Called by the poller thread. Re-read its current lease on delivery;
        # a queued payload may already be stale when GLib gets to it.
        if not self.stopping:
            self.glib.idle_add(self.refresh)

    def refresh(self) -> bool:
        if self.stopping or self.poller is None:
            return False
        try:
            state = self.poller.current_state()
            if state == self.last_state:
                return False
            self.last_state = state
            if not state.visible:
                self.overlay.set_property("alpha", 0.0)
                return False
            png = self.render(
                state, width=self.dimensions.width, height=self.dimensions.height,
            )
            if png is None:
                self.overlay.set_property("alpha", 0.0)
                return False
            loader = self.pixbuf.PixbufLoader.new_with_type("png")
            loader.write(png)
            loader.close()
            loaded = loader.get_pixbuf()
            if loaded is None:
                raise ValueError("Renderer did not return a readable PNG")
            # The plugin rewrites channel order in-place. Supply a fresh mutable
            # copy; never reuse the loader's pixbuf or assign an old one again.
            self.overlay.set_property("pixbuf", loaded.copy())
            self.overlay.set_property("alpha", 1.0)
        except Exception as exc:
            self.overlay.set_property("alpha", 0.0)
            # Do not include remote response content or request credentials.
            print(f"Broadcast overlay hidden after {type(exc).__name__}", file=sys.stderr)
        return False

    def refresh_lease(self) -> bool:
        if self.stopping:
            self.refresh_timer = None
            return False
        self.refresh()
        return True

    def on_message(self, _bus: Any, message: Any) -> None:
        if message.type == self.gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            print(f"GStreamer pipeline error: {safe_error(error.message)}", file=sys.stderr)
            self.exit_code = 1
            self.stopping = True
            self.loop.quit()
        elif message.type == self.gst.MessageType.EOS:
            self.stopping = True
            self.loop.quit()

    def stop(self) -> bool:
        if self.stopping:
            return False
        self.stopping = True
        self.overlay.set_property("alpha", 0.0)
        # Match gst-launch -e: allow muxers/filesinks to finalize on SIGINT/TERM.
        self.eos_timer = self.glib.timeout_add(5000, self.force_stop)
        if not self.pipeline.send_event(self.gst.Event.new_eos()):
            self.loop.quit()
        return False

    def force_stop(self) -> bool:
        self.eos_timer = None
        print("GStreamer EOS deadline reached; closing pipeline", file=sys.stderr)
        self.loop.quit()
        return False

    def run(self, poller: Any) -> int:
        self.poller = poller
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)
        old_handlers = {}
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_handlers[signum] = signal.signal(
                    signum, lambda *_args: self.glib.idle_add(self.stop),
                )
            if self.pipeline.set_state(self.gst.State.PLAYING) == self.gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Could not start the GStreamer pipeline")
            self.poller.start()
            # Lease expiry is independent of a blocked HTTP poll or its callback.
            self.refresh_timer = self.glib.timeout_add(250, self.refresh_lease)
            self.loop.run()
            return self.exit_code
        finally:
            self.stopping = True
            self.pipeline.set_state(self.gst.State.NULL)
            self.poller.stop()
            for timer in (self.refresh_timer, self.eos_timer):
                if timer is not None:
                    self.glib.source_remove(timer)
            bus.remove_signal_watch()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("pipeline", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    pipeline_argv = args.pipeline[1:] if args.pipeline[:1] == ["--"] else args.pipeline
    if not pipeline_argv:
        parser.error("Pass the GStreamer pipeline arguments after --")
    url = os.environ.get("DILLFLIX_OVERLAY_URL", "").strip()
    device = os.environ.get("DILLFLIX_OVERLAY_DEVICE", "").strip()
    if not url or not device:
        parser.error("DILLFLIX_OVERLAY_URL and DILLFLIX_OVERLAY_DEVICE are required")
    try:
        dimensions = geometry(args.width, args.height)
        import gi

        gi.require_version("Gst", "1.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, GLib, Gst

        # Local modules ship beside this executable, independent of the
        # controller's Python environment on a different machine.
        from overlay_model import OverlayPoller
        from renderer import render_overlay

        Gst.init(None)
        if Gst.ElementFactory.find("gdkpixbufoverlay") is None:
            raise RuntimeError("Missing GStreamer gdkpixbufoverlay plugin (Good plugins)")
        pipeline = Gst.parse_launchv_full(pipeline_argv, None, Gst.ParseFlags.FATAL_ERRORS)
        runner = OverlayRunner(pipeline, Gst, GLib, GdkPixbuf, render_overlay, dimensions)
        poller = OverlayPoller(
            url, device, runner.on_update,
            headers_file=os.environ.get("DILLFLIX_OVERLAY_HEADERS_FILE") or None,
            name=os.environ.get("DILLFLIX_OVERLAY_NAME", "Dillflix"),
        )
        return runner.run(poller)
    except (ImportError, ValueError) as exc:
        print(f"Cannot start broadcast overlay: {safe_error(exc)}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Cannot start broadcast overlay: {safe_error(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
