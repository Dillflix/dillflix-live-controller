# Public video overlay

The capture process can show a small lower-left card when no live playback is
confirmed or underway. Each feed uses its own stable controller device ID and
public name. This is burned into the video before the six quality tiers split;
it therefore appears in HLS, Plex and any other consumer of those RTMP outputs.

## What viewers see

| Retained state | Overlay |
| --- | --- |
| Fresh, accepted real live playback | Hidden, including while a replacement is prepared or automation is paused |
| Current Play has been invoked and verification is pending | Hidden until that attempt's existing deadline |
| Play dispatch may have succeeded but acknowledgement was lost | Hidden until that attempt's existing deadline |
| Active request preparing a new event, before Play | Switching live coverage + event title |
| Explicit player/network recovery or executor outage | Reconnecting live coverage; target title only if the current request is still valid |
| Idle, no eligible event, unavailable/upcoming feeds, retry exhausted | Waiting for live sports |
| Manual takeover/input handoff, paused without accepted playback, incompatible player runtime | Live coverage on standby |
| Controller unreachable, wrong device, malformed or expired response | Hidden |

Names and titles are plain text. Runtime phases, entitlement details, routes,
VPN cities, error messages, retries and countdowns never become viewer copy.
The controller's `navigating`/`verifying` labels alone do not establish a Play.
Old, cancelled, superseded, expired and simulated evidence cannot suppress the
overlay. A manually controlled screen cannot be verified by the controller, so
it receives neutral standby copy.

There is no video-pixel detection here. A frozen physical picture can outlive
its evidence; a successful physical Play can precede acknowledgement. The
projection follows retained evidence and bounded dispatch ambiguity. It does
not treat missing evidence as proof the event ended.

## Read contract and rendering

`GET /api/v1/devices/{id}/broadcast-overlay` returns schema version 1 with
`device_id`, `device_name`, `generated_at`, `valid_until`, `visible`, `state`,
`headline` and `event_title`. It uses one SQLite read snapshot and scoped event,
job and executor reads. It neither builds the catalogue nor polls a device.
`Cache-Control: no-store` is set. Existing controller administrator/trusted-LAN
authentication applies; this endpoint does not create a new access bypass.

The capture-side poller reads once per second on a separate thread. Responses
have at most five seconds of validity. Expiry is checked independently every
250ms, so blocked HTTP or DNS cannot freeze a card over the video. A failed read
does not extend the previous lease. Redirects are refused, responses are bounded,
and remote operational messages are ignored. Keep both hosts' clocks synchronized.

`gdkpixbufoverlay` sits on YUY2 before VA upload, or on NV12 before the MPP split.
The hardware encoding ladders, audio and output URLs retain the supplied v2
script's settings. Alpha zero hides the card; state changes do not restart the
pipeline. The Python runner handles bus errors and graceful EOS on SIGINT/SIGTERM.
Blending while visible uses CPU and needs acceptance on the actual capture host.

At 1920×1080 the canvas is 780×144, inset 48 pixels from the left and 42 from the
bottom. A translucent charcoal panel, crimson edge, stream name and two-line
maximum event title keep it quiet. There is no animation or scrolling. The card
scales with the feed: **240p text is very small and is not reliably readable**.
The preview generates the actual whole-frame downscale. A separate low-resolution
composition pass is not included; it would need a measured performance budget.

## Prepare without interrupting capture

Deploy a controller revision containing this endpoint first, using the normal
controller update procedure. Keep the existing capture services running during
preparation. On `dillflix-live-streamer`, install Python bindings; the existing
GStreamer 1.24.13 overlay plugin has already been confirmed on this host:

```bash
sudo apt-get update
sudo apt-get install python3-gi python3-gst-1.0 python3-pil \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gdkpixbuf-2.0 \
  gstreamer1.0-plugins-good fonts-dejavu-core
```

Copy this repository's entire `tools/broadcast_overlay` folder to
`/opt/dillflix/broadcast_overlay` (using your normal user account and sudo for
installation). Keep `/opt/dillflix/dillflix_gstreamer_v2.sh` intact. Then:

```bash
python3 /opt/dillflix/broadcast_overlay/self_test.py
python3 /opt/dillflix/broadcast_overlay/preview.py --output "$HOME/dillflix-overlay-preview"
```

The self-test uses synthetic video, no V4L2/ALSA device, controller, Fire TV or
RTMP connection. It checks actual GStreamer overlay changes and hidden pixels
while the pipeline remains running. The preview exercises waiting, switching,
recovery, standby, long titles and hidden playback without waiting for events.

Verify the new endpoint from the capture host before enabling either service:

```bash
curl --fail --silent --show-error \
  http://192.168.0.22:8790/api/v1/devices/living-room/broadcast-overlay
curl --fail --silent --show-error \
  http://192.168.0.22:8790/api/v1/devices/stream-2/broadcast-overlay
```

Confirm the IDs and public names are respectively `living-room` / `Dillflix 1`
and `stream-2` / `Dillflix 2`. A 404 means the endpoint/device is unavailable; a
401/403 means this route needs the existing authenticated connection. Resolve
that before enabling. Do not disable authentication or copy the nginx
administrator proxy secret into the capture service. Deployments with an
existing machine-authenticated route can pass a restricted JSON headers file
via `DILLFLIX_OVERLAY_HEADERS_FILE`; its contents are never logged.

## Enable one feed at a time

These are the existing mappings supplied for this installation:

| Service | Output | Device ID | Public name |
| --- | --- | --- | --- |
| `dillflix-gstreamer.service` | `sports0` | `living-room` | Dillflix 1 |
| `dillflix-gstreamer-sports1.service` | `sports1` | `stream-2` | Dillflix 2 |

Both use VA mode. The second service retains its existing `sports1.env` device
and ALSA selection. Create **only the second service's** new drop-in first:

```bash
sudo install -d /etc/systemd/system/dillflix-gstreamer-sports1.service.d
sudo tee /etc/systemd/system/dillflix-gstreamer-sports1.service.d/40-broadcast-overlay.conf >/dev/null <<'EOF'
[Service]
Environment=DILLFLIX_OVERLAY_DEVICE=stream-2
Environment="DILLFLIX_OVERLAY_NAME=Dillflix 2"
Environment=DILLFLIX_OVERLAY_URL=http://192.168.0.22:8790/api/v1/devices/stream-2/broadcast-overlay
ExecStart=
ExecStart=/bin/bash /opt/dillflix/broadcast_overlay/dillflix_gstreamer.sh va
EOF
sudo systemctl daemon-reload
```

Restarting this service briefly interrupts Dillflix 2's capture. It does not
restart either Prime Player, change scheduling or alter Plex metadata:

```bash
sudo systemctl restart dillflix-gstreamer-sports1.service
sudo journalctl -u dillflix-gstreamer-sports1.service --since '1 minute ago' --no-pager -n 60
```

Check the actual feed at 1080p and a lower tier: video/audio remain stable; the
card appears during waiting/switching; verified or started playback hides it.
Compare CPU usage and dropped frames with the prior capture before enabling the
other feed. A controller outage should remove the card within about five seconds,
without a capture restart. Do not stop the production controller to test this;
the isolated test covers lease expiry.

After Dillflix 2 acceptance, use this first-service drop-in and restart only it:

```bash
sudo install -d /etc/systemd/system/dillflix-gstreamer.service.d
sudo tee /etc/systemd/system/dillflix-gstreamer.service.d/40-broadcast-overlay.conf >/dev/null <<'EOF'
[Service]
Environment=DILLFLIX_OVERLAY_DEVICE=living-room
Environment="DILLFLIX_OVERLAY_NAME=Dillflix 1"
Environment=DILLFLIX_OVERLAY_URL=http://192.168.0.22:8790/api/v1/devices/living-room/broadcast-overlay
ExecStart=
ExecStart=/bin/bash /opt/dillflix/broadcast_overlay/dillflix_gstreamer.sh va
EOF
sudo systemctl daemon-reload
sudo systemctl restart dillflix-gstreamer.service
```

## Roll back capture

Remove only the new drop-in for the affected service, reload and restart it:

```bash
sudo rm -- /etc/systemd/system/dillflix-gstreamer-sports1.service.d/40-broadcast-overlay.conf
sudo systemctl daemon-reload
sudo systemctl restart dillflix-gstreamer-sports1.service
```

For Dillflix 1, substitute `dillflix-gstreamer.service`. The original unit,
v2 capture script, `sports1.env` and device mappings remain in place. The new
controller read endpoint can remain deployed. Alternatively, unsetting
`DILLFLIX_OVERLAY_URL` in the new script returns to the original `gst-launch-1.0 -e`
execution path without a Python runner or overlay element.
