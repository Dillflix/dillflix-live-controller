# Now-playing API

`GET /api/v1/devices/{device_id}/now-playing` returns a small JSON view for
programmatic consumers. The default device is `living-room`.

```bash
curl -fsS http://CONTROLLER_HOST_IP:8790/api/v1/devices/living-room/now-playing
```

Example with verified live playback (identifiers and hosts are illustrative):

```json
{
  "device_id": "living-room",
  "state": "playing",
  "playback_state": "verified",
  "simulated": false,
  "observed_at": "2026-10-05T01:11:34+00:00",
  "valid_until": "2026-10-05T01:16:34+00:00",
  "event": {
    "content_id": "event:espn:nfl:401872978",
    "title": "Detroit Lions at Carolina Panthers",
    "thumbnail_url": "http://GAME_THUMBS_HOST:3001/nfl/DetroitLions/CarolinaPanthers/thumb.png?style=6&logo=true&fallback=true",
    "kind": "event",
    "league": "nfl",
    "start_time": "2026-10-05T00:20:00Z",
    "expected_end_time": "2026-10-05T03:50:00Z",
    "end_time_estimated": true
  }
}
```

## Response contract

- HTTP 200 always returns the same object shape. Unknown devices return HTTP 404.
- `state=playing` means the controller has accepted, unexpired, healthy live
  playback evidence. `event` describes the **observed** event, using its original
  opaque Teamarr `content_id`. A desired/queued event is never substituted. During
  a switch, valid evidence for the previous stream can still be reported until
  withdrawn, expired, or replaced.
- `state=idle` means no playback observation is stored. `state=unverified` means
  a stored observation cannot establish current live playback. Both return
  `event=null` and `valid_until=null`; `playback_state` retains the controller's
  operational state, such as `navigating`, `waiting`, or `unverified`.
- Freshness is evaluated on every GET, even if the coordinator has stopped
  ticking. `valid_until` is the earlier of the evidence's expiry and the
  controller's maximum retention window (five minutes for Prime, 15 seconds by
  default for the simulator). `observed_at` is the original observation time;
  reading the endpoint never refreshes it. Known pause/error withdrawals, manual
  control/input handoff, and confirmed event completion cannot be reported as
  active live playback.
- Pausing **automation** alone does not hide continuing verified playback.
  Missing catalogue entries, unknown event lifecycle, and estimated end times
  do not invalidate otherwise verified playback. Metadata comes from the retained
  event snapshot.
- `simulated` explicitly identifies simulator evidence; it is null without an
  observation. Consumers requiring real playback should require `state=playing`
  and `simulated=false`.

`event.thumbnail_url` uses the configured game-thumbs host and exact matchup
identity from Teamarr's `artwork.matchup_logo_url`. It changes the endpoint to
`thumb.png`, retaining the matching cover's style/query parameters when available,
otherwise the matchup logo's parameters. Game-thumbs documents `thumb` as a
**1440×1080 landscape** image; `cover` is portrait. See the
[upstream image endpoint reference](https://game-thumbs-docs.swvn.io/api-reference/).

The thumbnail is null when no supported game-thumbs matchup URL exists, including
teamless broadcasts such as RedZone. Provider cover URLs are not rewritten or
passed off as matchup thumbnails. Configure Game Thumbs in Teamarr and refresh
the feed to populate missing matchup artwork. The consumer must be able to reach
that configured host; the controller neither fetches nor proxies the image.

This GET is read-only, makes no player or Teamarr calls, does not navigate or
interrupt playback, and sends `Cache-Control: no-store`. Polling every 5–15 seconds
is sufficient for ordinary integrations. Existing nginx authentication applies;
this endpoint introduces no separate token scheme. The response schema is also
available in `/openapi.json` and `/docs`.
