# LED display API

`GET /api/v1/devices/living-room/display` returns schema version 1, a compact
read model for piTicker and other displays. It reads one SQLite WAL snapshot,
does not reserve the writer lock, and makes no player, Teamarr, artwork-server
or device requests. Responses have `Cache-Control: no-store`; unknown devices
return 404. A display can poll every 5–10 seconds and cache artwork by URL.

The route retains the existing **admin API authentication boundary**. It does
not enable guest access or add an API token. Existing trusted-LAN installations
can read it directly; proxy deployments must use their authenticated admin
route. Do not expose the controller port or distribute proxy administrator
credentials merely to connect a display. A proxy can restrict the display's
machine identity to this GET route. Public user/guest access does not authorize
this endpoint, even though its response contains only presentation data.

## Response

| Field | Meaning |
| --- | --- |
| `schema_version` | `1` |
| `device_id` | Requested controller device; stable internal ID |
| `device_name` | Public display name, e.g. `Dillflix 1` |
| `streams` | All configured streams, each with `device_id`, `name`, and independent `playback` |
| `generated_at` | Real UTC response time, not renewed source evidence |
| `timezone` | Configured IANA timezone for displaying schedules |
| `playback` | Accepted playback, display status, and separately identified retained/desired events |
| `events` | At most 100 entitled active live/upcoming events |
| `schedule` | `state: ok/stale/unavailable`, `observed_at`, `valid_until` |

The fleet summaries reuse the same catalogue projection and database snapshot.
The existing top-level `playback` remains the requested device for older clients;
`events` remains one shared schedule. Names do not change device IDs or bindings.

`playback` contains:

- `state`: `playing`, `idle`, or `unverified`, using the same acceptance checks
  as the now-playing API. Only `playing` establishes accepted live playback.
- `status`: `manual`, `paused`, `starting`, `playing`, `unavailable`,
  `rechecking`, or `waiting`. This is presentation context, independent of the
  evidence state. For example, automation may be paused while playback remains
  verified, or a new target may be starting while the previous event still plays.
- `detail`: short, fixed presentation text. No exception text, device commands,
  request IDs, private network configuration, or raw diagnostic data is returned.
- `observed_at`, `valid_until`, `simulated`: original playback evidence and its
  effective expiry. An unverified response has no `valid_until`. With no
  observation, `simulated` identifies whether the configured adapter is the
  simulator, including while waiting.
- `event`: accepted current `DisplayEvent`, or null.
- `last_event`: retained observed identity when evidence is unverified, or null.
  This is not a claim that playback continues and may be a completed event.
- `target_event`: current desired identity, or null. It is not verified playback
  or a promise of the next selection; it may match the current event.

Display status gives manual control and paused automation precedence, then a
current navigating request, accepted playback, offline executor, unverified
observation, and network recovery. Otherwise it reports waiting. Startup detail
comes only from the current intent's pending request and allowlisted phase labels.

Each `DisplayEvent` contains exactly:

```text
content_id, title, league, kind, start_time, expected_end_time,
end_time_estimated, teams, scores, status_detail, lifecycle,
planned, priority, artwork
```

- `content_id` is opaque and must not be reconstructed from titles or teams.
- `teams` is empty for broadcasts without competitors. Otherwise it contains
  away then home, each with nullable `abbreviation`, `full_name`, `logo_url`.
  A missing side remains a null-valued entry to preserve positional identity.
- `scores` is `[away_score, home_score]`. Zero stays zero; unknown remains null.
  `status_detail` is optional provider display text, not a clock to extrapolate.
- `lifecycle` contains `state`, `stale`, `observed_at`,
  `effective_valid_until`. Provider observation time may be null even when
  controller receipt evidence is fresh. Do not infer completion from an estimate.
- `planned` indicates watch-plan membership; `priority` is the configured
  zero-based rule rank (1,000,000 when no rule matches), not a playback guarantee.
- `artwork` contains nullable `league_logo_url`, `away_team_logo_url`,
  `home_team_logo_url`. Team logos also appear in `teams`. Only absolute HTTP(S)
  URLs without embedded credentials are returned. Sources are not downloaded or
  verified by this endpoint; clients should tolerate unavailable artwork.

Schedule entries must have confirmed `entitled` status and be active. They include
`live`, `scheduled`, and future `unknown` broadcast/session entries; confirmed
ended/cancelled entries never appear. The cap reserves 50 places for live entries
and 50 for upcoming entries; either group can use the other's unused capacity.
Within those bounds, plan order (admin before user), policy priority, start instant,
and opaque ID determine a stable selection and final order.
The list is a schedule, not an exact future playback sequence. Current, retained,
and desired events are available separately even if they fail these schedule
filters. Simulator/non-Prime entries without entitlement records are omitted
from the schedule; simulated current playback remains explicitly identified.

## Freshness and connection failure

`schedule.observed_at` is the last complete controller feed receipt, not the
provider's score update time. Its `valid_until` is receipt plus two configured
refresh intervals (at least 60 seconds). An overdue or degraded feed is `stale`;
no successful receipt is `unavailable`. Repeated display reads never renew it.
Fresh feed receipt does not prove every provider's scores were recently updated.

Playback uses its original evidence age and expiry, capped at five minutes for
Prime Player. Missing/invalid/non-live evidence, manual ownership, and confirmed
completion cannot establish current playback. On HTTP failure, a display may
retain last-known information with an unavailable/stale indicator, but must
withdraw its verified-playing claim when the saved `valid_until` passes. Do not
turn a failed read into zero scores, a fabricated completion, or another launch.

No database migration, Prime Player update, or new credentials are required.
Automated tests cover contract, authentication, snapshot reads, entitlement and
lifecycle filtering, missing metadata, evidence expiry and display-state labels.
Physical LED hardware and the deployment's authentication proxy require on-device
acceptance; these tests do not validate either external integration.
