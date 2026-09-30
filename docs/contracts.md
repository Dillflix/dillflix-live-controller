# Controller API and executor contract

All examples use the initial device ID `living-room`. Event and observation timestamps include a UTC offset; internal job `ready_at`/`deadline_at` values are Unix seconds. Content IDs and entry IDs are opaque; clients must not parse or substitute them.

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

`GET /api/v1/events` returns `{items, meta, health, status_health}`. Each card includes:

- `content_id`, `kind`, `title`, `league`, `source`, `sports`, and normalized `phase`.
- `teams`, in away/home order when present. Team fields pass through from Teamarr, with `league` and a provider-and-league-qualified `key` added for preferences. `city` is the provider's location, which need not be a literal city.
- `artwork`, scheduled `start_time`, `expected_end_time`, and `end_time_estimated`.
- `lifecycle`: effective `state`, `last_known_state` (the stored observation's state), `stale`, `tracked`, `observed_at`, `received_at`, declared `valid_until`, bounded `effective_valid_until`, `timestamp_basis`, `source`, `simulated`, and `refresh`. Current states are scheduled, live, ended, cancelled, postponed, delayed, suspended, and unknown. Observation/source fields can be null when evidence is unavailable.
- All allowed `viewing_options`, `playable`, `availability_reason`, `active`, `watch_entry_id`, rule `priority`, scores/status detail when present, and temporary playback-failure information.

The card is a projection. The original Teamarr object is retained privately in the catalog and passed intact in playback jobs. Scores, logos, team names, and dates are not derived from title parsing.

`GET /api/v1/overview` adds the complete device state, plan preview, recent activity, feed health, metadata, `teams`, `team_directory_health`, `undo`, and `playback_job`, and includes the same `status_health`. Metadata distinguishes demo/Teamarr catalog mode, simulation status, sample/current time, and wall-clock server time. Feed health distinguishes starting, ok, and degraded, with the last successful fetch and count when known.

`status_health` contains `state` (`idle`, `starting`, `ok`, `degraded`), `pinned_count`, `checked_count`, `error_count`, `stale_count`, `unknown_count`, `last_attempt`, `adapter`, and `simulated`. It describes desired, observed, and reserved content, deduplicated by content ID. Each lifecycle's `refresh` contains `state` (`starting`, `ok`, `error`), `last_attempt`, `last_success`, `next_check_at`, and `error`. A successful check is not a new provider observation: clients must use the evidence timestamps to assess freshness. `tracked` indicates whether the independent worker currently refreshes this content.

In version 0.4, `lifecycle.observed_at` is **nullable**. The Teamarr feed has no provider observation timestamp; its read time is now accurately exposed as `received_at` with `timestamp_basis: feed_received`. Repeated cached lookups cannot move the original evidence expiry. `state` becomes unknown on nonterminal expiry; the stored state remains visible in `last_known_state`.

`playback_job` is null before any request, otherwise the latest request's `{id, content_id, state, progress, deadline_at, delivery_attempts, error}`. Job states are `pending`, `verified`, `failed`, `timed_out`, `rejected`, `cancelled`, or `superseded`. Progress distinguishes `queued`, `accepted`, `navigating`, `retrying`, `playing_verified`, and failure reasons/states; the job state remains authoritative after cancellation or supersession. `delivery_attempts` counts submission attempts, not inspection calls. The jobs endpoint also exposes the original payload, intent, executor job ID, ready time, and `cancel_sent` acknowledgement flag.

Observed playback is separate from desired content. `device.observed` includes content/request/device/intent identity, `viewing_option_id`, `presentation`, `verified`, `simulated`, `health`, `observed_at`, and `valid_until`. Expired evidence makes `verified` false while retaining the last identity. `device.playback_state` can be `waiting`, `navigating`, `verified`, `unverified`, or `failed`; pending navigation/failure may coexist with a last observation of the previous event.

`GET /api/v1/teams` returns `{items, health}`; the optional `league=nhl` query filters items. Each team has a stable `key` (`provider:league:id`), provider `id`, `provider`, `league`, `full_name`, and available `short_name`, `name`, `city`, `abbreviation`, and `logo_url` fields. City/nickname can be null; neither is inferred by splitting a display name. Teamarr cache entries map `provider_team_id` to `id`, not their local cache-row `id`. Event metadata can supply richer names. Teams are retained beyond the discovery window and through empty or failed refreshes.

Directory health starts as `{state: "starting"}` (`demo` in demo mode). After a Teamarr refresh it includes `state` (`ok` or `degraded`), total cached `count`, `last_attempt`, and `leagues`, keyed by league. Each league reports `ok`, `empty`, or `degraded`, a count/last successful read when known, and a sanitized error on failure. Directory failure does not change feed health.

## Priorities and automation

PUT `rules` with `{command_id, expected_revision, rules, team_ranks, preferences}`. This replaces that configuration atomically. Each rule has an ID, name, enabled flag, league (`all` is unrestricted), phase (`any` is unrestricted), optional team key, optional coverage source, and optional content kind. Rules are evaluated in array order.

`team_ranks` maps league codes to ordered preferred team keys. Teams absent from the ranking tie; directory display order does not create a preference. Unresolved saved keys remain intact. Preferences contain `timezone`, `minimum_viewing_seconds`, `switch_cooldown_seconds`, and `same_tier_switching`. The timezone must be an IANA zone such as `America/Toronto`.

POST `automation` with `{command_id, expected_revision, mode: "active" | "paused"}`. POST an empty object to `simulate` for an explained candidate selection without playback changes. The preview bypasses automatic dwell/cooldown so it can show a newly edited priority order; it still respects manual reservations and live eligibility.

## Undo

Overview returns `undo: null` or `{id, description, created_at}` for the latest available edit. POST `/api/v1/devices/{id}/undo` with:

```json
{
  "command_id": "unique-undo-command",
  "expected_revision": 13,
  "history_id": 42
}
```

Both the device revision and the latest undoable history ID must match, otherwise the API returns 409. The command restores only the plan or the rules/team-ranks/preferences from before that edit and increments the revision. It does not restore old lifecycle observations, jobs, automation mode, or clock time. Live eligibility is reevaluated normally. Up to 50 recent edits are retained across restarts, and repeated undo walks backwards. Retrying the same undo command remains idempotent. No redo endpoint is implemented.

## Configuration transfer

GET `/api/v1/devices/{id}/configuration` returns a portable JSON document:

```json
{
  "format": "dillflix-controller-config",
  "schema_version": 1,
  "source_mode": "teamarr",
  "exported_at": "2026-09-30T12:00:00+00:00",
  "configuration": {
    "rules": [],
    "team_ranks": {"nfl": ["espn:nfl:8"]},
    "preferences": {
      "timezone": "America/Toronto",
      "minimum_viewing_seconds": 300,
      "switch_cooldown_seconds": 30,
      "same_tier_switching": false
    }
  }
}
```

POST `{command_id, expected_revision, document}` to `/configuration/import/preview`. It validates the complete document without saving and returns `{revision, configuration, warnings, summary: {current_rules, imported_rules, ranked_teams}}`. Warnings flag a different source mode or team IDs not yet in the directory. It retains those IDs rather than guessing a replacement.

POST the same document to `/configuration/import` with a command ID and **the preview's revision**. This atomically replaces the three configuration fields, records an undoable edit, and returns the normal command receipt. A newer edit causes 409; preview again before retrying. Unsupported document versions, duplicate rule IDs or team rankings, invalid timezones, and unexpected fields return 422 without changes.

This transfer excludes the watch plan, automation mode, playback state, database history, Teamarr URL, and credentials. It is not a full database backup. Configuration documents, API/feed schemas, and SQLite migrations are versioned separately.

## Content-status adapter implemented today

`ContentStatusAdapter.lookup(request)` is an asynchronous internal boundary, implemented by the simulator. It is not an outbound HTTP API configuration. The worker sends:

```json
{
  "schema_version": 1,
  "request_id": "unique-lookup-id",
  "content_id": "opaque-Teamarr-feed-entry-id",
  "content_snapshot_schema_version": 1,
  "content_snapshot": {"...": "complete original Teamarr entry"},
  "catalog_seen_at": "2026-09-30T12:00:00+00:00",
  "as_of": "2026-09-30T12:00:00+00:00"
}
```

`as_of` is the demo clock in demo mode and current UTC time otherwise. It is not an event-completion estimate. Full snapshot/provider identifiers allow a real adapter to resolve the event without parsing opaque IDs. The result is null for no observation, or:

```json
{
  "schema_version": 1,
  "request_id": "unique-lookup-id",
  "content_id": "opaque-Teamarr-feed-entry-id",
  "observation": {
    "content_id": "opaque-Teamarr-feed-entry-id",
    "state": "live",
    "source": "fixture_simulator",
    "simulated": true,
    "timestamp_basis": "fixture",
    "observed_at": "2026-09-30T12:00:00+00:00",
    "received_at": "2026-09-30T12:00:00+00:00",
    "valid_until": "2026-09-30T12:02:00+00:00"
  }
}
```

Timestamp basis is `provider`, `fixture`, or `feed_received`. Provider/fixture observations require an aware `observed_at`; cached feed evidence requires a null `observed_at` and an aware original `received_at`. All observations require source, a boolean simulation flag, a supported state, and expiry after the evidence timestamp. Evidence must be unexpired when accepted, no more than five seconds in the future, and within the controller's 120-second content-status age limit. Source expiry is preserved; `effective_valid_until` is the earlier of that expiry and the controller age limit.

Request/content identity and current lease/request ID must match. Older or conflicting same-time evidence from a comparable source is rejected. Failure/timeout/null retains the previous accepted observation until it expires. An explicit fresh unknown observation makes nonterminal lifecycle unknown immediately. Previously confirmed ended/cancelled evidence survives unknown/error/expiry; a newer explicit state can correct it. None of these operations removes a manual commitment or fabricates a playback observation.

The default cadence is 15 seconds with per-content error backoff of 5, 10, 20, 40, then 60 seconds. Each call has a five-second timeout. Refresh continues during pause and survives restart through persisted evidence/retry state. Only watched/desired/reserved IDs receive independent lookups; ordinary discovery cards use feed/demo evidence. The simulator can resolve retained demo entries outside discovery. In Teamarr mode it only reuses the cached snapshot, so an expired out-of-window event stays unknown until a fresh source is available.

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

## Playback adapter implemented today

`controller/playback.py` defines this internal boundary. It is implemented by the persistent simulator, not HTTP routes:

| Operation | Result and semantics |
| --- | --- |
| `submit(request)` | Report for an idempotent request; the same ID with a different payload is rejected |
| `inspect(request_id)` | Current request report, or null if unknown; used before resubmission and after restart |
| `cancel(request_id)` | Return only after cancellation is acknowledged; raise on uncertainty so it can be retried |
| `observe(device_id)` | Current device observation, or null when unavailable |

A report has `request_id`, `executor_job_id`, `device_id`, `intent_version`, `content_id`, `state`, `observation`, and optional `reason`. The simulator reports `accepted`, `navigating`, `playing_verified`, `failed`, `cancelled`, or `superseded`. Acceptance alone never sets observed playback. A verified report contains an observation such as:

```json
{
  "device_id": "living-room",
  "request_id": "unique-request-id",
  "intent_version": 23,
  "content_id": "opaque-Teamarr-feed-entry-id",
  "viewing_option_id": "original-permitted-option-id",
  "presentation": "live",
  "verified": true,
  "simulated": true,
  "health": "healthy",
  "observed_at": "2026-09-30T12:00:00+00:00",
  "valid_until": "2026-09-30T12:00:15+00:00"
}
```

All identity fields must match the staged request. The option must belong to the original permitted set. Missing verification, replay/unknown presentation, unhealthy playback, expired evidence, and timestamps over five seconds in the future are rejected. Evidence is usable for at most 15 seconds from observation, or until its earlier expiry. The controller rechecks its lease, active intent, automation mode, current selection, and deadline before accepting a result. Successful job history alone cannot substitute for current device evidence.

Cancellation targets a request, not a global stop command. The simulator keeps cancellation tombstones and rejects lower device intents. It must not let cancellation of an old request stop newer playback. Failed cancellation delivery remains queued across restarts. The simulator chooses an option only to exercise this contract; its observations are not evidence of real TV playback.

## Future external adapters

No outbound playback HTTP endpoint or callback endpoint is connected yet. The future executor should accept the staged payload idempotently by `request_id`, apply monotonic intent fencing per device, and report request progress separately from observations. A successful acknowledgement must not count as live verification.

The real executor must supply device evidence for the playback observation contract above, with transport timeouts and cancellation behavior appropriate to its navigation engine. A real content-status adapter must implement the lookup contract using authoritative event observations rather than discovery receipt times. Exact transport, authentication between services, status tokens, and real device recovery behavior will be finalized when those services are selected.
