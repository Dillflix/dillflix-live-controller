# Now-playing diagnostics

`GET /api/v1/devices/{device_id}/now-playing` returns the controller's accepted
live playback, not a queued or desired event. It is read-only, uses retained
controller state, and makes no player, Teamarr, image-server, or Plex requests.
Responses include `Cache-Control: no-store`. An unknown device returns 404.

Example on the controller host:

```bash
curl -fsS http://127.0.0.1:8790/api/v1/devices/living-room/now-playing
```

The response contains `device_id`, `state` (`playing`, `idle`, or `unverified`),
`playback_state`, `simulated`, `observed_at`, `valid_until`, and `event`.
When playing, `event` includes the opaque `content_id`, title, kind, league,
start/end estimates, and `thumbnail_url`. Otherwise `event` is null. Expired,
mismatched, non-live, completed, or manually withdrawn evidence does not establish
current playback. Repeated reads do not renew the observation's lifetime.

Artwork selection:

1. Use a game-thumbs matchup thumbnail derived from the supplied matchup URL,
   preserving the matching cover's style/query when available.
2. Otherwise use Teamarr's supplied `artwork.cover_url` verbatim. DAZN tennis
   day/court coverage supplies its landscape event image in this field.
3. Return null if neither source is a valid absolute HTTP(S) image URL.

The endpoint reports the selected source URL; it does not download it or prove
image availability. Plex delivery separately validates the image and reads it
back after upload. Its default background remains in use until a distinct
background source is supported.

This endpoint was removed in 0.15.0 and restored in 0.15.1 for diagnostics and
external consumers. Controller-owned Plex delivery calls the shared internal
projection directly; it has no dependency on this HTTP endpoint.
