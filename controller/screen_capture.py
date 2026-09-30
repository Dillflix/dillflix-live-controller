"""View-only scrcpy 3.3.4 transport. No planner, input, or playback side effects."""

import asyncio
import logging
import os
import re
import secrets
import shlex
import struct
from contextlib import suppress

from .screen_install import SERVER_VERSION, verified

log = logging.getLogger(__name__)
CONFIG = 1 << 63
KEY_FRAME = 1 << 62
MAX_PACKET = 4 * 1024 * 1024


class CaptureError(Exception):
    """Safe, actionable diagnostics suitable for the browser."""


async def read_packet(reader):
    header = await reader.readexactly(12)
    flags, size = struct.unpack(">QI", header)
    if not 0 < size <= MAX_PACKET or (flags & CONFIG and size > 65536):
        raise CaptureError("The capture server returned an invalid video packet.")
    return header + await reader.readexactly(size)


class AdbCapture:
    def __init__(self, settings):
        self.settings = settings
        self.scid = f"{secrets.randbits(31):08x}"
        self.remote_path = f"/data/local/tmp/dillflix-screen-{self.scid}.jar"
        self.port = None
        self.process = None
        self.pid = None
        self.writer = None
        self.drain_task = None
        self.pushed = False
        self.metadata = None

    async def command(self, *args, timeout=12):
        try:
            process = await asyncio.create_subprocess_exec(
                self.settings.screen_adb_path,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise CaptureError("ADB is not installed. Install adb on the controller host.") from error
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if process.returncode:
            if b"unauthorized" in errors or b"unauthorized" in output:
                raise CaptureError("Authorize this controller's ADB connection on the TV, then reconnect.")
            raise CaptureError(
                "ADB could not reach the device. Check its address, connection and authorization."
            )
        return output.decode(errors="replace").strip()

    async def adb(self, *args, **kwargs):
        return await self.command("-s", self.settings.screen_adb_serial, *args, **kwargs)

    async def __aenter__(self):
        try:
            async with asyncio.timeout(45):
                await self.start()
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()

    async def start(self):
        serial = self.settings.screen_adb_serial
        if not serial or serial.startswith("-") or not re.fullmatch(r"[\w.:[\]%-]+", serial):
            raise CaptureError("Configure SCREEN_ADB_SERIAL with the target shown by adb devices.")
        path = self.settings.screen_server_path
        if not path.is_file():
            raise CaptureError("Capture server is missing. Run python -m controller.screen_install.")
        if not verified(await asyncio.to_thread(path.read_bytes)):
            raise CaptureError("Capture server checksum mismatch. Reinstall the pinned scrcpy server.")
        # TCP forwards are local to the ADB server. A remote daemon needs a separate bridge.
        if os.getenv("ADB_SERVER_SOCKET") or os.getenv("ANDROID_ADB_SERVER_ADDRESS"):
            raise CaptureError("Screen capture requires a local ADB server. Run the controller beside ADB.")
        # TCP devices are reconnected after a reboot; USB serials reuse the existing ADB transport.
        if re.search(r":\d+$", serial):
            await self.command("connect", serial)
        if await self.adb("get-state") != "device":
            raise CaptureError("ADB device is not ready. Check the TV's authorization prompt.")
        self.pushed = True
        await self.adb("push", str(path), self.remote_path)
        forwarded = await self.adb("forward", "tcp:0", f"localabstract:scrcpy_{self.scid}")
        if not forwarded.isdecimal() or not 0 < int(forwarded) < 65536:
            raise CaptureError("ADB did not allocate a capture tunnel.")
        self.port = int(forwarded)
        options = [
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            SERVER_VERSION,
            f"scid={self.scid}",
            "log_level=warn",
            "tunnel_forward=true",
            "audio=false",
            "control=false",
            "power_on=false",
            "cleanup=true",
            "video_codec=h264",
            f"max_size={self.settings.screen_max_size}",
            f"max_fps={self.settings.screen_max_fps}",
            f"video_bit_rate={self.settings.screen_bit_rate}",
            "video_codec_options=profile=1,i-frame-interval=1",
        ]
        remote = f"echo $$; CLASSPATH={self.remote_path} exec {shlex.join(options)}"
        self.process = await asyncio.create_subprocess_exec(
            self.settings.screen_adb_path,
            "-s",
            serial,
            "shell",
            remote,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        pid = (await asyncio.wait_for(self.process.stdout.readline(), 5)).strip()
        if not pid.isdigit():
            raise CaptureError("Could not start capture on the device. Check Fire OS/scrcpy compatibility.")
        self.pid = int(pid)
        self.drain_task = asyncio.create_task(self.drain_logs())
        # An ADB forward accepts connections even before the device socket exists.
        for _ in range(40):
            if self.process.returncode is not None:
                break
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", self.port), 1)
                self.writer = writer
                dummy = await asyncio.wait_for(reader.readexactly(1), 1)
                if dummy != b"\x00":
                    raise CaptureError("Unexpected capture protocol. Reinstall the pinned server.")
                self.reader, self.writer = reader, writer
                break
            except (OSError, asyncio.IncompleteReadError, TimeoutError):
                if writer:
                    writer.close()
                    with suppress(OSError):
                        await writer.wait_closed()
                self.writer = None
                await asyncio.sleep(0.15)
        if not self.writer:
            raise CaptureError("Capture did not start. Check device support and the controller logs.")
        async with asyncio.timeout(10):
            name = (await self.reader.readexactly(64)).split(b"\x00", 1)[0].decode(errors="replace")
            codec, width, height = struct.unpack(">III", await self.reader.readexactly(12))
        if codec != 0x68323634 or not (0 < width <= 8192 and 0 < height <= 8192):
            raise CaptureError("The device did not provide a supported H.264 screen stream.")
        self.metadata = {
            "type": "stream",
            "protocol": 1,
            "codec": "h264",
            "device_name": name,
            "width": width,
            "height": height,
            "max_fps": self.settings.screen_max_fps,
        }

    async def drain_logs(self):
        # Drain continuously without retaining an ever-growing log buffer.
        while chunk := await self.process.stdout.read(4096):
            log.warning("scrcpy: %s", chunk.decode(errors="replace").strip()[:1000])

    async def packet(self):
        # scrcpy repeats static frames, so prolonged silence is a capture failure.
        return await asyncio.wait_for(read_packet(self.reader), 20)

    async def close(self):
        if self.writer:
            self.writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(self.writer.wait_closed(), 1)
        if self.process and self.process.returncode is None:
            with suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 1)
            if self.process.returncode is None and self.pid:
                # Only terminate our exact session; never kill another scrcpy/ADB user.
                check = (
                    f"case \"$(cat /proc/{self.pid}/cmdline 2>/dev/null | tr '\\000' ' ')\" in "
                    f"*com.genymobile.scrcpy.Server*scid={self.scid}*) kill {self.pid};; esac"
                )
                with suppress(Exception):
                    await self.adb("shell", check, timeout=3)
            if self.process.returncode is None:
                self.process.terminate()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.process.wait(), 1)
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
        if self.drain_task:
            self.drain_task.cancel()
            await asyncio.gather(self.drain_task, return_exceptions=True)
        if self.port:
            with suppress(Exception):
                await self.adb("forward", "--remove", f"tcp:{self.port}", timeout=3)
        if self.pushed:
            with suppress(Exception):
                await self.adb("shell", "rm", "-f", self.remote_path, timeout=3)
