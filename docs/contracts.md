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

An accepted mutation returns `{command_id, revision, accepted: true}`. Fetch current state afterwards. HTTP 409 means stale revision or conflicting reuse of a command ID; 422 indicates invalid action data or an ineligible Play now request. For a transport retry with an uncertain outcome, resend the **same** command ID and body. Do not change the revision while retaining that ID. Version 0.6 keeps the newest 10,000 receipts per device by default, plus undo references. Once an older receipt is removed, its original revision is stale and retry returns 409 without reapplying the action. Command IDs are not permanently reserved beyond retention.

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

`playback_job` is null before any request, otherwise the latest request's `{id, content_id, purpose, state, progress, deadline_at, delivery_attempts, error}`. Purpose is `selection`, `route_handoff`, or `recovery`; older jobs default to selection in this read model. Job states are `pending`, `verified`, `failed`, `timed_out`, `rejected`, `cancelled`, or `superseded`. Progress distinguishes `queued`, `accepted`, `navigating`, `retrying`, `playing_verified`, and failure reasons/states; the job state remains authoritative after cancellation or supersession. `delivery_attempts` counts submission attempts, not inspection calls. The jobs endpoint also exposes the original payload, intent, executor job ID, ready time, and `cancel_sent` acknowledgement flag.

Observed playback is separate from desired content. `device.observed` includes content/request/device/intent identity, `viewing_option_id`, `presentation`, `verified`, `simulated`, `health`, `observed_at`, and `valid_until`. Expired evidence makes `verified` false while retaining the last identity. `device.playback_state` can be `waiting`, `navigating`, `verified`, `unverified`, or `failed`; pending navigation/failure may coexist with a last observation of the previous event.

Version 0.5 adds device recovery read models, initialized lazily for existing devices:

- `executor_health`: `state` (`starting`, `ok`, `offline`), `failures`, `since`, `last_contact_at`, `next_probe_at`, and sanitized `error`. The initial starting record can contain only state. A successful observation call establishes contact, even if it returns null; contact never substitutes for verified playback. Offline probes back off from 5 to 60 seconds and survive restart. Navigation delivery and cancellation wait while offline.
- `recovery`: null or a record with `content_id`, `attempts`, `since`, `retry_after`, and `stable_since`. Times are nullable ISO timestamps using wall-clock UTC. A missing `since`/`retry_after` indicates recovered evidence whose retry budget is not yet reset. `attempts` counts same-event recovery reopen requests, not ordinary delivery retries or route handoffs. Thirty seconds of healthy observations clear the record. Grace defaults to 60 seconds and increases up to 300 after reopen attempts. Play now can bypass it for an unverified, confirmed-live target.

After reconnect/grace, selection still requires confirmed live status for new requests. Unknown status can retain fresh verified playback on a permitted route; it cannot reopen an event. If both lifecycle and playback evidence are missing beyond grace, the controller selects a confirmed live fallback or waits, preserving reservations.

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
  "purpose": "route_handoff",
  "previous_request_id": "previously-observed-request-id",
  "content_snapshot_schema_version": 1,
  "content_snapshot": {"...": "complete original Teamarr entry"},
  "allowed_viewing_options": [{"...": "original valid option object"}]
}
```

`content_id` is **the feed entry's `id`**, not `event.event_id`. The snapshot includes all original event, team, session, broadcast, identity, timing, artwork, option, and unknown extension fields. Every valid option is passed; there is no controller-selected app. Teamarr's `preferred_option_id` remains in the untouched snapshot but does not authorize the executor to ignore other allowed options.

Excluded options, explicit replay/highlight presentations, and partial/multi-event coverage that cannot satisfy a specific event/session are filtered out. An uncertainty such as an unknown end time is retained with its review reasons. RedZone is eligible as its own broadcast; it cannot be used to claim a full individual NFL game is playing. The future executor must resolve remaining review uncertainty and verify live presentation.

The simulator chooses the first permitted option only to exercise the observation workflow. This is a simulator implementation detail, not a product preference or the future executor's route-selection policy.

`purpose` and `previous_request_id` are additive version-0.5 fields. Purpose is `selection`, `route_handoff`, or `recovery`; previous request is the last observed request ID or JSON null. They explain the request but do not relax live verification or monotonic intent fencing. Older persisted payloads remain valid and are retried unchanged.

Withdrawing the observed route or changing its playback locator stages a new intent for the same `content_id` with the latest snapshot and all currently allowed options. The controller compares `id`, `app`, `channel`, `stream_title`, `listing_url`, `broadcast_id`, `presentation`, and `coverage_type`. Pending requests are superseded if any of their issued options becomes incompatible. Reordering, adding alternatives, display changes, and estimated ends do not interrupt an existing valid route. The controller does not infer withdrawal from elapsed expected end times. Successful same-event handoff/recovery preserves the original viewing and switch timestamps and manual commitment.

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

All identity fields must match the staged request. The option must belong to the original permitted set and remain compatible with current coverage. Missing verification, replay/unknown presentation, unhealthy playback, expired evidence, and timestamps over five seconds in the future are rejected. Evidence is usable for at most 15 seconds from observation, or until its earlier expiry. The controller rechecks its lease, active intent, automation mode, current selection, route compatibility, and deadline before accepting a result. Successful job history alone cannot substitute for current device evidence.

Cancellation targets a request, not a global stop command. The simulator keeps cancellation tombstones and rejects lower device intents. It must not let cancellation of an old request stop newer playback. Failed cancellation delivery remains queued across restarts. The simulator chooses an option only to exercise this contract; its observations are not evidence of real TV playback.

## Maintenance and database operations

`GET /api/v1/maintenance` reports `state` (`starting`, `ok`, `error`), `policy`, and, after a successful pass, `last_run`, `removed`, and `counts`. Policy contains `jobs_per_device`, `receipts_per_device`, `inactive_catalog_days`, and `interval_seconds`. Counts are per table as of the last pass, not a live counter. An error adds sanitized `error` and preserves the previous successful pass. There is no HTTP restore endpoint.

Maintenance preserves pending/cancellation obligations, current observations, intent fences, manual commitments, and undo references even if these exceed history limits. Old inactive catalog entries are removed only when unreferenced; cleanup never asserts event completion. The simulator retains enough intent evidence to reject replayed requests after old payloads are removed. A real executor must define its own compatible receipt-retention policy.

Full database snapshots and offline restore use `python -m controller.ops`; see [operations.md](operations.md). Restore preserves saved user data/history but pauses automation, clears runtime playback claims, cancels pending work, and advances revision/intent beyond the backup and readable target. Clients should reload before issuing new commands. Database schema remains 4; configuration-transfer schema remains 1.

## Future external adapters

The [engineer handoff](executor-api-handoff.md) and [OpenAPI draft](executor-api.openapi.yaml) propose the external transport, token lifecycle, independent lifecycle lookup, and shared input authority. They identify required controller changes and acceptance scenarios. Those routes are not implemented by this controller release.

No outbound playback HTTP endpoint or callback endpoint is connected yet. The future executor should accept the staged payload idempotently by `request_id`, apply monotonic intent fencing per device, and report request progress separately from observations. A successful acknowledgement must not count as live verification.

The real executor must supply device evidence for the playback observation contract above, with transport timeouts and cancellation behavior appropriate to its navigation engine. A real content-status adapter must implement the lookup contract using authoritative event observations rather than discovery receipt times. Exact transport, authentication between services, status tokens, and real device recovery behavior will be finalized when those services are selected.

## Screen viewing API

`GET /api/v1/devices/living-room/screen` is a read-only, side-effect-free status/configuration read. It never starts ADB. Example when configured but unused:

```json
{
  "enabled": true,
  "state": "idle",
  "viewers": 0,
  "error": null,
  "read_only": true,
  "audio": false,
  "max_size": 1280,
  "max_fps": 30,
  "bit_rate": 2000000,
  "stream_path": "/api/v1/devices/living-room/screen/stream"
}
```

Other states are `disabled`, `starting`, `streaming`, and `error`. Disabled configuration returns a null stream path. Errors describe the last capture failure; `idle` may retain that diagnostic until another start. Capture transport state is independent of actual video decoding in each browser and independent of verified sports playback. Target serial, server paths, and credentials are not exposed.

`WS /api/v1/devices/living-room/screen/stream` starts or joins the shared capture. It requires a browser `Origin` with an HTTP(S) scheme and an authority matching the forwarded `Host`. nginx supplies authentication. Other device IDs are rejected; browser messages never become ADB commands, and application-level inbound messages close the socket with 1008. Protocol ping/pong remains transport-managed.

The first text message identifies the stream:

```json
{"type":"stream","protocol":1,"codec":"h264","device_name":"Fire TV","width":1280,"height":720,"max_fps":30}
```

Binary messages preserve the pinned scrcpy 3.3.4 packet format: an 8-byte big-endian unsigned flags/timestamp field, a 4-byte big-endian payload length, then one H.264 Annex B packet. Bit 63 marks codec configuration, bit 62 a keyframe, and the lower 62 bits carry presentation time in microseconds. Configuration packets have no media timestamp. Packet payloads are limited to 4 MiB and configuration payloads to 64 KiB. This is screen-stream protocol 1; it is not compatible with arbitrary upstream scrcpy versions or ws-scrcpy's modified wire format.

New viewers get configuration followed by the next keyframe, not a stored recording. A repeated stream metadata message requests decoder reinitialization after encoder configuration changes; initial dimensions are informational, and the H.264 SPS supplies the actual decoded dimensions. Errors are text messages such as `{"type":"error","message":"Authorize this controller's ADB connection on the TV, then reconnect."}`, followed by connection closure. Consumers reconnect for a fresh session; request IDs or content IDs are intentionally absent because a screen feed alone proves no content identity.

## Manual device control API

`POST /api/v1/devices/living-room/control` uses the standard persisted command receipt and revision check. Example:

```json
{
  "command_id": "unique-command-id",
  "expected_revision": 12,
  "action": "take",
  "session_id": "new-random-session-identifier",
  "owner_token": "at-least-32-random-characters-generated-by-the-client",
  "minutes": 240,
  "takeover": false
}
```

Generate a fresh unpredictable session ID and owner token for each take/takeover. `minutes` defaults to 15 and accepts integers 1–1,440. `takeover: true` explicitly replaces an existing session at the supplied current revision. `action: "extend"` requires the existing session ID/token and resets its deadline to now plus `minutes`. `action: "release"` requires the owner and accepts `release_mode: "active" | "paused"` (default active). Identical command retries return the stored receipt; they do not extend or reclaim ownership twice. A stale revision, missing ownership or expired session returns 409; invalid fields return 422. A provided HTTP Origin must match Host.

State/overview exposes a nullable `device.manual_control`:

```json
{
  "session_id": "new-random-session-identifier",
  "started_at": "2026-09-30T20:00:00+00:00",
  "expires_at": "2026-10-01T00:00:00+00:00",
  "return_mode": "active"
}
```

No token/digest is returned. Automation stays paused for the session. Ownership commands and expiry advance the device revision. Take/end also advance playback intent and invalidate playback claims. Deadline uses real UTC, never the demo clock. `/automation` and watch-plan `play_now` return 409 while manual control exists; other plan/configuration commands remain available.

`WS /api/v1/devices/living-room/control/input` requires same-origin HTTP(S) Origin/Host and existing proxy authentication. Its first text message, within five seconds, is `{"session_id":"…","owner_token":"…"}`. Credentials are not URL parameters. Only one connection can own input. The server starts a separate control-only scrcpy session and sends `{"type":"ready"}` when connected. Then send one of:

```json
{"seq":1,"key":"up"}
{"seq":2,"text":"NFL RedZone"}
```

Allowed keys: `up`, `down`, `left`, `right`, `select`, `back`, `home`, `menu`, `play_pause`, `backspace`. Text is 1–200 printable ASCII characters. Additional fields, keys or combined key/text inputs are rejected. Messages are bounded to 4,096 characters. Each socket uses strictly increasing positive integer sequences; duplicates are rejected before delivery. Inputs are serialized, with eight commands/second replenishment and a ten-command burst allowance. No persisted input queue or reconnect replay exists.

The response `{"type":"sent","seq":1}` acknowledges transport delivery only. It does not verify a visible app response. An error is `{"type":"error","message":"…"}` followed by closure. Failed/uncertain commands must not be blindly retried. Start a new connection with sequence 1 after reviewing the device screen. Browser hidden/disconnect closes the input transport without ending ownership; takeover, release and deadline revoke it. Raw key/text input is not written to activity or command receipts.
