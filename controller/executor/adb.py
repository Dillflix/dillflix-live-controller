"""Bounded ADB operations. Remote shell arguments are quoted, never model code."""

import asyncio
import hashlib
import io
import math
import re
import shlex
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

from PIL import Image, ImageStat, UnidentifiedImageError

from .accessibility import FocusCollector, associate_capture, observation_validity
from .models import ExecutorError

PACKAGE = "com.amazon.firebat"
COMPONENT = PACKAGE + "/com.amazon.pyrocore.IgnitionActivity"
KEYS = {"UP": 19, "DOWN": 20, "LEFT": 21, "RIGHT": 22, "SELECT": 23, "BACK": 4, "HOME": 3, "WAKEUP": 224}
MAX_OUTPUT = 12 * 1024 * 1024


@dataclass(frozen=True)
class Frame:
    image: bytes
    sha256: str
    captured_at: datetime
    foreground: str | None
    sessions: list[dict]
    native_focus: dict | None = None
    captured_monotonic: float | None = None


def prepare_image(png):
    try:
        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG" or not (160 <= image.width <= 7680 and 90 <= image.height <= 4320):
                raise ValueError("Invalid dimensions")
            image.load()
            gray = image.convert("L").resize((160, 90))
            stats = ImageStat.Stat(gray)
            if stats.mean[0] < 5 and stats.stddev[0] < 5:
                raise ExecutorError(
                    "protected_or_blank_frame", "Screen is blank or protected; playback cannot be verified"
                )
            image = image.convert("RGB")
            image.thumbnail((1280, 1280))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88)
            return output.getvalue()
    except ExecutorError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ExecutorError("invalid_frame", "ADB did not return a usable screenshot") from error


def foreground(activity):
    match = re.search(
        r"(?:mResumedActivity|mFocusedActivity|topResumedActivity)[^\n]*?\bu\d+\s+([\w.]+)/", activity
    )
    return match[1] if match else None


def media_sessions(text):
    if "MEDIA SESSION SERVICE" not in text:
        raise ExecutorError("media_state_unavailable", "Android media-session state is unavailable")
    result = []
    for block in re.split(r"\n\s+package=", text)[1:]:
        package = re.match(r"([\w.]+)", block)
        active = re.search(r"\bactive=(true|false)", block)
        state = re.search(r"state=PlaybackState\s*\{state=(\d+)", block)
        position = re.search(r"\bposition=(-?\d+)", block)
        updated = re.search(r"\bupdated=(\d+)", block)
        if package:
            result.append(
                {
                    "package": package[1],
                    "active": bool(active and active[1] == "true"),
                    "state": int(state[1]) if state else None,
                    "position_ms": int(position[1]) if position else None,
                    "updated": int(updated[1]) if updated else None,
                }
            )
    return result


class AdbDevice:
    def __init__(self, settings):
        self.settings, self.config = settings, settings.executor
        serial = settings.screen_adb_serial
        if not serial or serial.startswith("-") or not re.fullmatch(r"[\w.:[\]%-]+", serial):
            raise ValueError("Real playback requires SCREEN_ADB_SERIAL for the configured TV")
        self.accessibility = FocusCollector(
            [settings.screen_adb_path, "-s", serial, "shell", "uiautomator", "events"],
            self.app_version,
            timeout=self.config.adb_timeout,
        )

    async def app_version(self):
        package = await self.shell("dumpsys", "package", PACKAGE)
        name = re.search(r"^\s*versionName=([^\r\n]+)", package, re.M)
        code = re.search(r"^\s*versionCode=(\d+)\b", package, re.M)
        return f"{name[1].strip()} ({code[1]})" if name and code else None

    async def close(self):
        await self.accessibility.stop()

    def invalidate_focus(self):
        self.accessibility.invalidate()

    def validate_frame(self, frame):
        validity = observation_validity(frame.native_focus, self.accessibility.snapshot())
        if validity["status"] == "changed":
            raise ExecutorError(
                "stale_navigation_focus", "Focus or capture association changed; observe again"
            )
        return validity

    async def prepare_input(self, action, *, frame=None):
        """Called under the shared input gate, before the durable dispatch record."""
        if action == "STOP_PRIME":
            # Cleanup has no focus goal. Do not start a reader/version/clock
            # request solely to tear it down, especially while ADB is unhealthy.
            self.accessibility.invalidate()
            return None
        await self.accessibility.start()
        if frame is not None:
            self.validate_frame(frame)
        before = self.accessibility.snapshot()
        # Invalidate before the clock read so old labels cannot survive a failed
        # clock request. /proc/uptime is the archive's tested device-clock proxy.
        self.accessibility.invalidate()
        device_time = None
        try:
            uptime = await self.shell("cat", "/proc/uptime")
            seconds = float(uptime.strip().split()[0])
            if math.isfinite(seconds) and seconds >= 0:
                device_time = math.ceil(seconds * 1000) + 10
        except (ExecutorError, ValueError, IndexError):
            pass
        return {**self.accessibility.begin_action(action, device_time), "beforeFocus": before}

    async def after_input(self, action):
        if action and action["action"] in {"UP", "DOWN", "LEFT", "RIGHT", "SELECT", "BACK"}:
            await self.accessibility.wait_for_focus(action)

    async def run(self, *args, device=True):
        command = [self.settings.screen_adb_path]
        if device:
            command += ["-s", self.settings.screen_adb_serial]
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise ExecutorError(
                "adb_unavailable", "Configured ADB executable could not be started"
            ) from error

        async def read(stream):
            chunks, size = [], 0
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > MAX_OUTPUT:
                    raise ExecutorError("adb_output_limit", "ADB output exceeded its limit")
                chunks.append(chunk)
            return b"".join(chunks)

        readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
        try:
            async with asyncio.timeout(self.config.adb_timeout):
                output, errors = await asyncio.gather(*readers)
                await process.wait()
            if process.returncode:
                code = (
                    "adb_unauthorized" if b"unauthorized" in errors.lower() + output.lower() else "adb_failed"
                )
                raise ExecutorError(code, "Check the TV's ADB connection and authorization")
            return output
        except TimeoutError as error:
            raise ExecutorError(
                "adb_timeout", "ADB command timed out; input delivery may be uncertain"
            ) from error
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Never leave a local input subprocess running after cancellation.
            cleanup = asyncio.create_task(process.wait())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            finally:
                for reader in readers:
                    if not reader.done():
                        reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)

    async def shell(self, *args):
        output = (await self.run("shell", shlex.join(args))).decode(errors="replace")
        if re.search(r"Error:|Exception|Permission Denial", output, re.I):
            raise ExecutorError("adb_command_rejected", "The configured device command was rejected")
        return output

    async def ready(self):
        if re.search(r":\d+$", self.settings.screen_adb_serial):
            await self.run("connect", self.settings.screen_adb_serial, device=False)
        if (await self.run("get-state")).strip() != b"device":
            raise ExecutorError("device_unavailable", "ADB device is not ready")

    async def key(self, action):
        if action not in KEYS:
            raise ExecutorError("invalid_action", "Unsupported device action", status=422, retryable=False)
        await self.shell("input", "keyevent", str(KEYS[action]))

    async def launch(self):
        await self.shell(
            "am",
            "start",
            "-W",
            "-a",
            "android.intent.action.MAIN",
            "-c",
            "android.intent.category.LEANBACK_LAUNCHER",
            "-n",
            COMPONENT,
        )

    async def search(self, query):
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 300
            or any(ord(c) < 32 for c in query)
        ):
            raise ExecutorError(
                "invalid_query", "Invalid Prime Video search query", status=422, retryable=False
            )
        uri = "amzn://pvde/search?phrase=" + quote(query.strip(), safe="")
        await self.shell("am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", uri, "-p", PACKAGE)

    async def state(self):
        activity, media = await asyncio.gather(
            self.shell("dumpsys", "activity", "activities"), self.shell("dumpsys", "media_session")
        )
        return foreground(activity), media_sessions(media)

    async def capture(self):
        await self.accessibility.start()
        if self.accessibility.state.action is None:
            await self.prepare_input("OBSERVE")
        try:
            before, _ = await self.state()
            focus_before = self.accessibility.snapshot()
            captured_at, captured_monotonic = datetime.now(UTC), time.monotonic()
            png = await self.run("exec-out", "screencap", "-p")
            focus_after = self.accessibility.snapshot()
            after, sessions = await self.state()
            if before != after or after != PACKAGE:
                raise ExecutorError("foreground_changed", "Prime Video is not the foreground application")
            image = await asyncio.to_thread(prepare_image, png)
            native_focus = associate_capture(focus_before, focus_after, self.accessibility.snapshot())
            return Frame(
                image,
                hashlib.sha256(image).hexdigest(),
                captured_at,
                after,
                sessions,
                native_focus,
                captured_monotonic,
            )
        except BaseException:
            self.invalidate_focus()
            raise

    async def stop(self):
        # A package-scoped stop is deterministic, unlike toggling Play/Pause.
        # Runtime calls this only while the cancelled token still owns the device.
        try:
            await self.shell("am", "force-stop", PACKAGE)
            current, sessions = await self.state()
            if current == PACKAGE or any(
                s["package"] == PACKAGE and s["active"] and s["state"] in {2, 3, 6} for s in sessions
            ):
                raise ExecutorError("stop_unconfirmed", "Prime Video stop has not been confirmed")
        finally:
            await self.close()
