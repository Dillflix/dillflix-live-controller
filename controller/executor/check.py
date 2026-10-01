"""Read-only executor diagnostics: python -m controller.executor.check --help."""

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ..config import Settings
from .accessibility import observation_validity
from .adb import PACKAGE, AdbDevice, Frame, prepare_image
from .models import ExecutorError, PlaybackRequest
from .verification import activation_allowed, completed, playback_sample, prime_option
from .vision import VisionClient


async def check(settings, *, observe=False, image=None, request=None):
    """No database, navigation, input, launch, search, or stop, including on failure."""
    config = settings.executor
    config.validate()
    result = {
        "adapter": config.mode,
        "adb_executable_available": bool(shutil.which(settings.screen_adb_path)),
        "external_api_enabled": bool(config.api_token),
        "input_actions_sent": 0,
    }
    if not (observe or image):
        return result
    if config.mode != "prime-video":
        raise ValueError("Set PLAYBACK_ADAPTER=prime-video for vision diagnostics")
    started = time.monotonic()
    device = vision = None
    try:
        if image:
            # A file has no live acquisition time, native focus, or media authority.
            raw = Path(image).read_bytes()
            if len(raw) > 12 * 1024 * 1024:
                raise ValueError("Diagnostic image must be a PNG under 12 MiB")
            jpeg = await asyncio.to_thread(prepare_image, raw)
            frame = Frame(jpeg, hashlib.sha256(jpeg).hexdigest(), datetime.now(UTC), PACKAGE, [])
            result["evidence_origin"] = "file_replay_not_live"
        else:
            device = AdbDevice(settings)
            await device.ready()
            frame = await device.capture()
            result["evidence_origin"] = "live_capture"
        vision = VisionClient(config)
        scene = await vision.observe(frame)
        result.update(
            scene=scene.model_dump(),
            image_sha256=frame.sha256,
            foreground=frame.foreground,
            media_sessions=frame.sessions,
            native_focus=frame.native_focus,
            native_focus_validity_after_observer=(
                observation_validity(frame.native_focus, device.accessibility.snapshot()) if device else None
            ),
            observer_seconds=round(time.monotonic() - started, 3),
        )
        if request:
            body = PlaybackRequest.model_validate_json(Path(request).read_text()).model_dump(mode="json")
            option = next(
                (option for option in body["allowed_viewing_options"] if prime_option(option)), None
            )
            if option is None:
                raise ValueError("Diagnostic request has no Prime Video option")
            # Diagnostics do not load the controller database or its timezone.
            result["comparisons_timezone"] = "UTC"
            result["comparison"] = {
                "activation_allowed": activation_allowed(scene, body, option, frame, "UTC"),
                "single_playback_sample_matches": bool(playback_sample(scene, frame, body, "UTC")),
                "completion_candidate": completed(scene, frame, body, "UTC"),
            }
            result["proposed_action_not_executed"] = await vision.decide(frame, body, option, [])
        result["total_seconds"] = round(time.monotonic() - started, 3)
        return result
    finally:
        try:
            if vision:
                await vision.close()
        finally:
            if device:
                await device.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--observe", action="store_true", help="Read the current Prime screen and call the observer"
    )
    source.add_argument("--image", help="Send an existing PNG to the observer, without contacting the TV")
    parser.add_argument(
        "--request", help="Also test matching and actor output against a saved Play request; never send input"
    )
    parser.add_argument("--output", help="Save diagnostic JSON to this local path instead of stdout")
    args = parser.parse_args()
    if args.request and not (args.observe or args.image):
        parser.error("--request needs --observe or --image")
    try:
        report = asyncio.run(
            check(Settings.from_env(), observe=args.observe, image=args.image, request=args.request)
        )
        output = json.dumps(report, indent=2) + "\n"
        if args.output:
            Path(args.output).write_text(output)
        else:
            print(output, end="")
    except ExecutorError as error:
        print(json.dumps({"error": error.detail(), "input_actions_sent": 0}), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError) as error:
        parser.exit(1, f"Diagnostic setup error: {error}\n")


if __name__ == "__main__":
    main()
