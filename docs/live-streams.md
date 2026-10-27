# Browser live streams

The public app can watch all feeds configured in Dillflix Live, including
`sports0`, `sports1` and future feeds. It reads the feed catalog from the Live load
balancer and starts an independent browser HLS session for the selected feed and
quality. A browser session does not start Prime playback, change the capture
source, modify a watch plan, update Plex metadata or create a Plex playback session.

The existing **Play now** and **Add to plan** actions still control the shared
physical source. **Watch** selects what this browser receives. Stream IDs are not
Teamarr event IDs. The controller's existing Now playing metadata describes its
configured device. Feed tiles show its verified event title only when an explicit
feed-to-device mapping identifies that capture.

## Configure

Update the Live LB to a version providing `/api/browser/v1`, configure its feed
registry and service key, then set these controller environment variables:

```dotenv
LIVE_BROWSER_API_URL=http://LIVE_LB_HOST:LIVE_LB_PORT
LIVE_BROWSER_API_KEY=the-same-service-key-configured-on-the-lb
```

Use the LB's HTTP origin reachable **from the controller container**, not the
RTMP ingest address or an address reachable only from a viewer's computer.
The URL must have no path, query, fragment or embedded credentials. Use HTTPS
when the controller-to-LB connection crosses an untrusted network. Leave both
values blank to disable this feature; disabled deployments make no Live requests.
Recreate the controller after changing its environment:

```bash
docker compose up -d --build
```

The LB registry determines available feeds and qualities. Adding a configured
feed there makes it appear without rebuilding the controller. Offline feeds
remain visible. Feed availability is transport information, not a claim that the
capture has a valid picture or the expected event.

## Player controls

To show current event titles, map each capture to its controller device in `.env`:

```dotenv
# Example: only if sports0 captures the living-room device.
LIVE_BROWSER_DEVICE_MAP='{"sports0":"living-room"}'
```

Use `sports1` instead for Feed 2, or map both feeds to `living-room` if they mirror
that device. Other independent devices require their own controller device IDs;
never map an unrelated feed merely to give it a title. Blank disables titles.
Recreate the controller after changing the mapping. No Live server change is needed.

Titles come from accepted live playback, not the desired event or watch plan.
Unmapped, offline, unknown, simulated, expired or unverified feeds show only the
feed name. Titles refresh with the catalog and expire in the browser even if a
later refresh fails. Collapsed tiles retain the title, and metadata updates do
not restart viewing sessions.

Feeds appear in a single column with collapsible tiles. `sports0` and `sports1`
are displayed as **Feed 1** and **Feed 2**; other feeds use their catalog labels.
Watching starts with sound on. If the browser blocks playback with sound, press
Play; the controller does not silently mute the feed.

Play/pause, sound, settings and fullscreen are grouped inside the player. Quality
and **Close stream** are in Settings. Pause retains the viewing session; Close
stream releases it. **Jump to live** appears when playback is behind and skips to
the current broadcast. Collapsing a tile or switching tabs keeps the session
running, with compact playback and mute controls on a collapsed tile. Leaving the
page closes its sessions. Browser/OS background suspension can still interrupt
media independently of the app. On iPhone, native fullscreen may expose only the
system video controls; change quality before entering that fullscreen view.

## Access and transport

The browser uses only same-origin `/api/public/v1/streams` endpoints. The
controller keeps the Live service key on the server and proxies the catalog,
sessions, playlists and segments. The existing public authentication policy
applies to **every** request: authenticated proxy identity in proxy mode or
anonymous public access in explicit guest mode. Guest mode therefore also makes
browser viewing available to anyone who can access that public surface.

Existing nginx public examples already forward `/api/public/v1/`; no new public
hostname, exposed receiver port, CORS policy or nginx location is required.

If viewing works over LAN HTTP but the HTTPS public page rejects session creation
with `403 {"detail":"Actions require the same origin."}`, set the browser-facing
origin in the controller's `.env`, then recreate the controller container:

```dotenv
CONTROLLER_PUBLIC_ORIGIN=https://live-controller.example.com
```

Use the actual public hostname, including a nonstandard port if present, without
the `/public/` path. This explicitly identifies the external origin when nginx
terminates HTTPS and forwards HTTP without the controller's proxy credential.
nginx must still preserve `Host` (`proxy_set_header Host $http_host;`). The setting
applies only to public APIs, retains direct LAN access, and does not bypass login,
trust arbitrary forwarding headers, or authorize other origins. Existing PlexSSO
authentication at nginx stays in place. The authenticated proxy examples already
support HTTPS using the matching proxy key and `X-Forwarded-Proto` instead.

The player distinguishes a 401 sign-in failure from a 403 origin/configuration
rejection. Older builds incorrectly described both as expired stream access.

The controller's outgoing connection must reach the LB. Keep receiver and LB
service-key endpoints private to the deployment. The HTTP segment traffic passes
through the controller, so include viewer bandwidth and connection capacity when
sizing that service. Media is streamed with backpressure; playlists and JSON are
buffered with strict size limits. There are at most 32 upstream requests per
controller process, finite connection/read deadlines and a 45-second media
response limit. Media responses use `Cache-Control: no-store`, because the live
segment filenames may wrap and must not be reused from a browser cache.

This first transport supports the existing relative MPEG-TS HLS media playlists,
including audio. DASH, encrypted playlists, nested master playlists and arbitrary
remote URLs are not accepted. The controller rejects redirects and nonlocal
playlist references. Range requests and HEAD are supported for TS segments.
Playlist requests ignore `Range` headers and return the complete playlist with
HTTP 200 (headers only for HEAD), including native browser requests for
`bytes=0-`. Playlists advertise `Accept-Ranges: none` and remain size-limited,
validated and uncached.

Public contract:

| Method | Path below `/api/public/v1/streams` | Purpose |
| --- | --- | --- |
| GET | (root) | `{enabled, streams}` catalog |
| POST | `/sessions` | `{stream_id, tier}` creates a viewing session |
| DELETE | `/sessions/{session_id}` | Releases the viewing session |
| GET / HEAD | `/sessions/{session_id}/hls/index.m3u8` | Current HLS playlist |
| GET / HEAD | `/sessions/{session_id}/hls/{file}.ts` | Media segment |

A successful session response contains `session_id`, `expires_in` and a local
`playlist_url`. Closing or changing the player releases its session; the LB's
expiry also cleans up abandoned sessions. An expired session or a temporary
upstream failure is reported as a viewing failure and does not alter scheduling.

## Acceptance

Run the backend and browser suites required by `AGENTS.md`. On the deployment,
verify two simultaneous feeds, audio, each offered quality, stop/reconnect,
an offline feed, session expiry, proxy authentication for both manifests and
segments, and guest access only when intentionally configured. Check that browser
Watch and Stop leave the physical source and watch plan unchanged, and that Plex
viewers continue working. Automated transport fixtures do not verify hardware,
network reachability, production nginx/PlexSSO, or the deployed Live version.
