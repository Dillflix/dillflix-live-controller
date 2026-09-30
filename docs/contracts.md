# Controller API and executor contract

All examples use the initial device ID `living-room`. Timestamps include a UTC offset. Content IDs and entry IDs are opaque; clients must not parse or substitute them.

## Watch-plan commands

Send the current device revision from `/api/v1/overview` or `/state`:

```json
{
  "command_id": "client-generated-unique-id",
  "expected_revision": 12,
  "action": {
    "type": "add",
    "content_id": "opaque-Teamarr-feed-entry-id",
    "priority": "last"
  }
}
```

POST the same shape to `watch-plan/preview` to inspect estimated conflicts and segments without saving it. POST or PATCH it to `watch-plan` to save. Preview returns the revision it evaluated; saving against a changed revision returns 409.

| Action | Fields | Effect |
| --- | --- | --- |
| `add` | `content_id`, optional `priority: first/last` | Reserve until completion; default append |
| `play_now` | `content_id` | Require live eligibility, promote to first, resume automation, retry failed target |
| `reorder` | `ordered_entry_ids` | Supply each current commitment ID exactly once |
| `remove` | `entry_id` | Remove one manual commitment |

An accepted mutation returns `{command_id, revision, accepted: true}`. Fetch current state afterwards. HTTP 409 means stale revision or conflicting reuse of a command ID; 422 indicates invalid action data or an ineligible Play now request. For a transport retry with an uncertain outcome, resend the **same** command ID and body. Do not change the revision while retaining that ID.

The watch plan contains `{id, content_id, created_at}` entries in priority order. Re-adding existing content moves its existing commitment rather than creating a duplicate. Conflict preview returns `conflicts` (pairs of content IDs), `segments` (`start`, `end`, `content_id`, `estimated`), `unknown_timing`, and a human-readable note. A null segment content ID means automatic selection during that gap; it is not a request to stop.

## Read models

`GET /api/v1/events` returns `{items, meta, health}`. Each card includes:

- `content_id`, `kind`, `title`, `league`, `source`, `sports`, and normalized `phase`.
- `teams`, in away/home order when present. Team fields pass through from Teamarr, with a provider-and-league-qualified `key` added for preferences. `city` is the provider's location, which need not be a literal city.
- `artwork`, scheduled `start_time`, `expected_end_time`, and `end_time_estimated`.
- `lifecycle: {state, stale, observed_at, source, simulated}`. Current lifecycle states are scheduled, live, ended, cancelled, postponed, delayed, suspended, and unknown.
- All allowed `viewing_options`, `playable`, `availability_reason`, `active`, `watch_entry_id`, rule `priority`, scores/status detail when present, and temporary playback-failure information.

The card is a projection. The original Teamarr object is retained privately in the catalog and passed intact in playback jobs. Scores, logos, team names, and dates are not derived from title parsing.

`GET /api/v1/overview` adds the complete device state, plan preview, recent activity, feed health, and metadata. Metadata distinguishes demo/Teamarr catalog mode, simulation status, sample/current time, and wall-clock server time. Feed health distinguishes starting, ok, and degraded, with the last successful fetch and count when known.

## Priorities and automation

PUT `rules` with `{command_id, expected_revision, rules, team_ranks, preferences}`. This replaces that configuration atomically. Each rule has an ID, name, enabled flag, league (`all` is unrestricted), phase (`any` is unrestricted), optional team key, optional coverage source, and optional content kind. Rules are evaluated in array order.

`team_ranks` maps league codes to ordered team keys. Preferences contain `timezone`, `minimum_viewing_seconds`, `switch_cooldown_seconds`, and `same_tier_switching`. The timezone must be an IANA zone such as `America/Toronto`.

POST `automation` with `{command_id, expected_revision, mode: "active" | "paused"}`. POST an empty object to `simulate` for an explained candidate selection without playback changes. The preview bypasses automatic dwell/cooldown so it can show a newly edited priority order; it still respects manual reservations and live eligibility.

## Playback request staged today

The internal durable job contains this payload:

```json
{
  "schema_version": 1,
  "request_id": "unique-request-id",
  "device_id": "living-room",
  "intent_version": 23,
  "content_id": "opaque-Teamarr-feed-entry-id",
  "mode": "live",
  "content_snapshot_schema_version": 1,
  "content_snapshot": {"...": "complete original Teamarr entry"},
  "allowed_viewing_options": [{"...": "original valid option object"}]
}
```

`content_id` is **the feed entry's `id`**, not `event.event_id`. The snapshot includes all original event, team, session, broadcast, identity, timing, artwork, option, and unknown extension fields. Every valid option is passed; there is no controller-selected app. Teamarr's `preferred_option_id` remains in the untouched snapshot but does not authorize the executor to ignore other allowed options.

Excluded options, explicit replay/highlight presentations, and partial/multi-event coverage that cannot satisfy a specific event/session are filtered out. An uncertainty such as an unknown end time is retained with its review reasons. RedZone is eligible as its own broadcast; it cannot be used to claim a full individual NFL game is playing. The future executor must resolve remaining review uncertainty and verify live presentation.

The simulator chooses the first permitted option only to exercise the observation workflow. This is a simulator implementation detail, not a product preference or the future executor's route-selection policy.

## Future external adapters

No outbound playback HTTP endpoint or callback endpoint is connected yet. The future executor should accept the staged payload idempotently by `request_id`, apply monotonic intent fencing per device, and report request progress separately from observations. A successful acknowledgement must not count as live verification.

An observation will need request/device/content identity, applied intent version, observation time, actual viewing option, presentation (`live` versus other/unknown), and evidence confidence/freshness. A separate content-status lookup should accept `content_id` plus the snapshot/provider identifiers and return an explicit lifecycle observation with its source and expiry. Exact transport, authentication between services, status tokens, and cancellation semantics will be finalized when those services are selected.
