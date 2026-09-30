# Live device screen

Version 0.7 added a real, optional screen feed above the events and watch-plan pages. It works in demo or Teamarr mode. **Screen viewing is independent of the selected playback adapter.** Viewing it alone does not verify the selected event. Version 0.9 adds opt-in autonomous Prime navigation through [executor setup](executor-setup.md).

The implementation follows the approach evaluated in [NetrisTV/ws-scrcpy](https://github.com/NetrisTV/ws-scrcpy): on-device scrcpy encoding, ADB transport, and H.264 playback in a browser. It uses a separate pinned **Genymobile scrcpy 3.3.4** server and **JMuxer 2.1.4**, rather than requiring the ws-scrcpy application. The server version is intentionally pinned because scrcpy's internal wire protocol changes between versions. An existing ws-scrcpy installation may remain available separately; capture sessions have unique socket names and temporary files.

## Docker setup

Enable ADB debugging on the Fire TV and assign it a stable network address. Put the address and ADB port in the controller's existing `.env`:

```dotenv
SCREEN_ADB_SERIAL=192.168.1.50:5555
SCREEN_MAX_SIZE=1280
SCREEN_MAX_FPS=30
SCREEN_BIT_RATE=2000000
```

Replace the example address with your target. Then update and rebuild:

```bash
git pull --ff-only
docker compose up -d --build
```

The image includes ADB and downloads the pinned capture server with SHA-256 verification during the build. No Android SDK, desktop display, ffmpeg, browser plugin, or separate ws-scrcpy service is required at runtime. The first build needs access to the official GitHub release asset.

Open the controller and accept the ADB authorization prompt on the TV. Docker has its own ADB identity; a connection previously approved for your Ubuntu account does not automatically approve the container. Its user home is `/data/adb`; the ADB keys under that home persist in `controller-data` across rebuilds. The entrypoint creates the home for existing volumes. Database-only backups do not include these keys.

A configured screen opens automatically the first time. **Hide screen** closes this browser's viewer and is remembered locally. **Show screen** returns to the current live image. Reconnect and fullscreen controls sit below the image. Background tabs suspend their viewer and reconnect when visible again. When the last viewer disconnects, the controller removes its ADB forward and stops its own capture process. Hiding the screen does not pause TV playback or controller automation.

## Local Ubuntu setup

From the repository root with your controller virtual environment activated:

```bash
sudo apt-get install adb
python -m pip install -e '.[test]'
python -m controller.screen_install
adb connect 192.168.1.50:5555
adb devices
export SCREEN_ADB_SERIAL=192.168.1.50:5555
cd frontend
npm ci
npm run build
cd ..
python -m uvicorn controller.api:create_app --factory --host 127.0.0.1 --port 8790 --workers 1
```

Use the exact serial shown by `adb devices`. A network serial ending in `:port` is reconnected automatically when capture starts. An already-connected USB device can also be addressed by its serial when running locally; USB passthrough is not configured in the supplied Compose file. Keep your existing Teamarr environment variables. The Python server does not load `.env` itself.

`SCREEN_SERVER_PATH` can point to a different local location of the **same pinned binary**, and `SCREEN_ADB_PATH` can select a local ADB executable. For example, `python -m controller.screen_install /opt/dillflix/scrcpy-server-v3.3.4` installs at a chosen path. Checksums are verified at capture startup too; arbitrary versions are rejected. Run ADB and the controller on the same host/container namespace: remote ADB-server environment overrides are not supported because forward ports belong to the ADB server.

For temporary real device input, use [Take control](manual-control.md). The remote uses a separate control-only scrcpy session; shared screen viewers remain read-only.

The capture server is pushed to a uniquely named temporary file on the device. Audio, input control, and power-on behavior are disabled. It does not install a persistent Android application, launch a streaming app, send remote keys, or alter the watch plan.

## nginx and browser support

Apply the screen and manual-input WebSocket location in [nginx.conf.example](nginx.conf.example) under your existing authenticated server. Both the status URL and WebSocket URL need the same authentication. If your authentication is currently configured only inside a `location` block, include it in the new location too. Preserve the original `Host`, including a nonstandard public port, using `$http_host`.

The browser uses the same origin and existing proxy session; no ADB address or unauthenticated device port is exposed to it. HTTPS pages use `wss://`. Vite's development proxy also forwards the WebSocket. Avoid an nginx `^~ /api/` block that bypasses the sample regex location, or put the WebSocket upgrade directives in that block.

Playback requires H.264 Media Source support. JMuxer also supports Managed Media Source for compatible iOS browsers. Video is muted and `playsinline`; fullscreen uses the browser's available API. An unsupported browser gets an explicit explanation. No device audio, seek bar, replay, or saved recording is provided. Automated checks use desktop and phone-sized Chromium, including actual H.264 decode; physical iPhone/Safari behavior still needs target-device validation.

## Capture behavior and troubleshooting

Defaults are a maximum 1280-pixel long edge, 30 fps, and 2 Mbps. `SCREEN_MAX_SIZE` is clamped to 320–1920, `SCREEN_MAX_FPS` to 5–60, and `SCREEN_BIT_RATE` to 250,000–12,000,000. The device may produce fewer frames on a static menu. Dimensions preserve aspect ratio. Source timestamps normally determine video timing; equal timestamps receive a positive sample duration and gaps longer than one second are compressed for live viewing. Backward timestamps still restart decoding with an explicit timing diagnostic. The browser trims old media and returns toward the live edge; startup and latency depend on the encoder, browser, and network.

Up to eight simultaneous viewers share one capture session. A joining viewer waits for a keyframe; slow viewers are disconnected rather than accumulating unbounded video. Each viewer has a 90-message/6 MiB queue limit. Capture reads, sends, and ADB commands have timeouts, with a 45-second overall startup deadline. The browser retries after 1, 2, 4, 8, then at most 15 seconds and clears the old picture while reconnecting. Backoff resets after ten seconds of successful playback, so a brief picture followed by an error does not create a rapid retry loop. A stalled decoder or excessive retained video buffer also reconnects. Normal cleanup retains approximately 30 seconds and runs every ten seconds; the emergency ceiling is 60 seconds. Keyframe cleanup positions use cumulative sample durations so variable frame rates do not corrupt the cleanup index. The feature is idle when disabled or unused; ADB is not contacted by the status endpoint or ordinary controller startup.

| Symptom | Check |
| --- | --- |
| Connect a device to enable live mirroring | Set `SCREEN_ADB_SERIAL`, recreate/restart the controller, and refresh the page |
| ADB authorization error | Approve this host/container on the TV; check `adb devices` in the same runtime |
| Missing capture server | Run the installer locally, or rebuild the Docker image |
| Works directly, fails behind nginx | Verify Upgrade/Connection headers, original Host, and authentication on the stream location |
| Capture cannot start | Check `docker compose logs controller` for encoder/Fire OS errors and try a lower max size/bit rate |
| TV menus mirror but the playback area is black | The app may protect that surface from capture; ADB cannot guarantee mirroring of protected video |

Protected surfaces are a platform restriction, not evidence that the sports event ended. See Android's [FLAG_SECURE documentation](https://developer.android.com/reference/android/view/WindowManager.LayoutParams#FLAG_SECURE). The controller does not bypass capture restrictions. Actual Fire TV compatibility and protected-app behavior have not been tested in this development environment.

See [contracts.md](contracts.md#screen-viewing-api) for the stream contract and [third-party notices](../third_party/README.md) for sources and licenses.

### Diagnosing a recurring screen error

Version 0.7.1 distinguishes packet/timestamp errors, media-buffer errors, and browser decoder errors in the reconnect overlay. The browser developer console records the detail under `Device screen`. If playback still cycles after updating and refreshing, capture that exact message (including the browser decoder code/message) and the controller logs. These diagnostics contain no video frames. Automated regression checks reproduce equal timestamps, long timestamp gaps, and continued variable-rate decoding through normal buffer cleanup; they do not establish the cause of a particular Fire TV encoder failure without its diagnostics.
