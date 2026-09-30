# Playback API implementation guide

Implemented in **dillflix-live-controller 0.9.0**, in `controller/executor/`. The controller, device worker, token store, and manual remote share one application and SQLite database. Real playback is opt-in through `PLAYBACK_ADAPTER=prime-video`; the default remains the simulator. See [setup and diagnostics](executor-setup.md).

The public contract has exactly three operations. Separate request recovery, current-device observation, content-ID lifecycle, input-authority/renewal, and Stop APIs remain unnecessary for this release.

| Operation | Route | Meaning |
| --- | --- | --- |
| Play | `POST /v1/playbacks` | Persist a live-only request and return its token; navigation runs asynchronously. |
| Status | `GET /v1/playbacks/{token}` | Read launch progress, current playback evidence, and lifecycle/completion for that request. |
| Cancel | `POST /v1/playbacks/cancel` | Cancel navigation and stop owned playback, or cancel through a device intent watermark. |

The [OpenAPI contract](executor-api.openapi.yaml) includes schemas and synthetic examples. `/docs` and `/openapi.json` expose the implemented routes and Pydantic request/response schemas. External calls require `Authorization: Bearer <EXECUTOR_API_TOKEN>`. A blank credential disables external executor access; the embedded controller still works. The service credential is separate from playback tokens, manual browser ownership tokens, model credentials, and nginx authentication.

## Play and retry

| Field | Requirement |
| --- | --- |
| `schema_version` | `1` |
| `request_id` | Unique ID for one logical request. Preserve the ID and body across uncertain delivery retries. |
| `device_id` | `living-room`. The ADB destination is configured on the server. |
| `intent_version` | Increasing device intent, above previously accepted executor intents and the durable cancellation fence. The controller allocates it transactionally. |
| `content_id` | Opaque Teamarr **feed entry ID**, equal to `content_snapshot.id`; not an inferred event/provider ID. |
| `mode` | `live` only. |
| `purpose` | `selection`, `route_handoff`, or `recovery`. |
| `previous_request_id` | Prior request for a handoff/recovery, otherwise null. |
| `content_snapshot_schema_version` | `1` |
| `content_snapshot` | Complete original Teamarr entry: teams, event/session/broadcast, timing, identity, artwork, all options, review reasons, and unknown extension fields. |
| `allowed_viewing_options` | Every permitted original option, unchanged. Excluded, replay, highlights, and unsuitable partial/multi-event options cannot authorize a specific game. |
| `deadline_at` | Fixed aware timestamp for navigation, no more than one hour ahead. Does not limit event duration or successful playback. |

Bodies are limited to 2 MiB. Invalid devices, replay requests, malformed identities, changed options, and invalid timestamps are rejected. The controller does not select an app. The current executor supports Prime Video options and records unsupported apps in attempt history; other apps require another device adapter.

`202 Accepted` means the token and request are committed, **not** that the TV is playing. The response is a `PlaybackReport`, with a `Location` header pointing to the getter. Identical ID/body retries return `200`, the same token, and its current report without navigating twice. Reusing an ID with changed data returns `409`.

1. Persist the request, including its ID, intent and fixed deadline.
2. POST Play. On a transport timeout, disconnect, or retryable HTTP problem, retry that same body while its intent remains current. Do not mint a new ID because the response was lost.
3. On `200` or `202`, persist the token and use the getter.
4. An asynchronous `failed` or `timed_out` operation is a finished attempt. A deliberate retry of the navigation itself needs a new ID, newer intent and new deadline, after checking current live eligibility.

The separate `request_id` recovery endpoint remains deferred. Repeating the original POST handles a lost response without another API. Exactly-once ADB execution is not claimed: a key may reach Android before the connection/process fails. The action journal records ambiguity and never blindly replays that key.

## Interpret token status

GET reads durable records and applies freshness rules. It sends no input and makes no model or device call. A background worker acquires observations. Responses use `Cache-Control: no-store`.

| Report field | Interpretation |
| --- | --- |
| `operation.state` | `accepted`, `navigating`, `playing_verified`, `failed`, `timed_out`, `cancelled`, or `superseded`: the launch attempt's history. |
| `operation.phase`, `attempts`, `error` | Progress, attempted option IDs, and sanitized failure reasons. |
| `operation.finished_at` | When the launch attempt finished, **not** when the event finished. |
| `observation` | Playback evidence attributable to this token; null before verification and after acknowledged cancellation. |
| `observation.verified` | True only while acquired evidence remains current and matches the requested content, permitted route and live presentation. |
| `observation_status` | `fresh`, `stale`, or `unavailable`, independently of HTTP success. |
| `content_status.effective_state` | `scheduled`, `live`, `ended`, `cancelled`, `delayed`, `suspended`, `postponed`, or `unknown`: the event/broadcast lifecycle. |
| `content_status.observation` | Lifecycle fact, source, original acquisition/receipt times and optional visual evidence. |
| `cancellation` | `none`, `requested`, or `acknowledged`, plus whether input in scope is quiescent. |
| `retained_until` | Earliest retirement time after cancellation acknowledgment; null while active. |

Historical `playing_verified` can coexist with stale playback evidence and an ended event. After Cancel, historical launch success is retained but current playback is null. **Cancel never marks an event ended.** Treat launch result, current playback and lifecycle separately.

Playback evidence expires 15 seconds after acquisition; polling cannot refresh its timestamps. Expired evidence becomes `verified=false`. Nonterminal lifecycle evidence expires after 120 seconds or its earlier source expiry. Explicit terminal facts remain retained through outages, although their evidence can be marked stale. `revision` counts stored writes, not time passing or catalog projection changes; it is not an ETag.

Polling every 2–5 seconds is sufficient for most callers. The embedded controller reads the same store directly. Use the controller's normal selection/Play now commands for user intent; do not run an independent scheduler against the same TV alongside it. Raw API callers must coordinate the same device intent sequence.

## Verification and completion

The actor proposes one D-pad action at a time. It cannot supply arbitrary commands, coordinates, URLs, or global Home/settings actions. A separate observer receives only the screenshot and observation schema: no target, expected answer, actor reasoning, or prior success claims. Python checks its transcribed facts against the request.

Before SELECT activates content, two captures must agree on the focused control. Both competitors must match structured team aliases. A visible league/date must agree. Explicit route/channel and language constraints must match. The focused content must be identified as live. Replay, upcoming, ended, start-over, purchase, sign-in, unclear focus and unrelated content are rejected. Ordinary navigation controls can be selected; Play/Watch/Resume cannot masquerade as menu navigation.

Launch success requires a previously confirmed live-content activation and two playback samples at least one second apart, with:

- Prime Video still foreground;
- independently observed identity matching the request;
- visible live-edge evidence;
- one active playing Android media session belonging to Prime Video;
- an advancing visible elapsed timer or actually advancing sampled media position. Stationary media position is never extrapolated using wall-clock time.

Actor FINISH, successful ADB, app launch, audio, generic media metadata and elapsed navigation time cannot prove success. Black/protected frames, hidden identity/live controls, paused playback, ads and unsupported telemetry remain unverified. Read-only monitoring does not press keys to reveal controls. This conservative policy needs validation on the actual device and inference service.

| Lifecycle source | Policy |
| --- | --- |
| Teamarr feed | Explicit lifecycle state with original feed receipt time. `timestamp_basis=feed_received`, `observed_at=null`; provider acquisition freshness is unknown. Re-reading cached data never extends expiry. |
| Prime Video visuals | Two matching scoped conclusion readings at least 15 seconds apart within the 120-second evidence window. `timestamp_basis=device_observed`, `source=prime_video_visual`, `evidence.decision=confirmed`. An intervening contrary/failed observation clears the candidate. |

A game's FINAL/full-time can complete that game, not RedZone. Aggregate coverage requires explicit end-of-coverage evidence for the whole named broadcast/session. A round, period, half-time, intermission, scheduled end, missing feed entry, navigation failure or stopped player cannot establish completion. Confirmed visual completion takes precedence over cached feed status still saying live. The worker never navigates elsewhere just to investigate lifecycle.

No independent sports-results provider is implemented. For an unplayed or switched-away event, fresh Teamarr facts and retained terminal facts may be the only evidence. When absent/stale, status stays unknown and the reservation remains. This limitation does not require another public endpoint.

## Cancel, active stop and manual control

Cancel one request, including active playback:

```json
{"device_id":"living-room","token":"pb_example"}
```

Or cancel all requests through an intent watermark, including delayed Play deliveries whose token was never received:

```json
{"device_id":"living-room","through_intent_version":24}
```

Supply exactly one scope. Token cancellation affects only that token. Device cancellation durably raises the fence: Play at/below it is rejected even after restart. Neither form affects newer work outside its scope. A manual session also pauses automation and prevents higher-intent submissions through the ownership check.

Cancellation persists before interruption. Model work is cancelled. A bounded in-flight ADB input is drained while holding the physical input lock; it may already have reached the TV. If the cancelled token still owns the app, the executor issues `am force-stop com.amazon.firebat`, then confirms Prime is neither foreground nor actively playing/paused/buffering. Play/Pause is not used as Stop. An old token cannot issue Stop after a newer token owns the device.

`200` acknowledges that input in scope is quiescent and owned playback is stopped/already inactive. Cancel is idempotent. Timeout/`503` leaves durable cancellation pending and cleanup retrying; **manual input stays disabled**. Repeating Cancel wakes cleanup immediately. `input_quiescent` concerns the cancelled scope, not newer work outside it.

Manual and autonomous input share one `asyncio.Lock`. Take control saves new manual intent, waits for device cancellation acknowledgment, then enables the remote. Release/expiry drains manual transport before automation resumes. Sessions, fences and pending handoffs survive restart. Pause alone leaves successful playback running; Take control uses cancellation and stops it.

Play, getter and Cancel therefore suffice. Physical remotes and unrelated ADB clients remain outside this application's ownership gate.

## Implementation and durability

| Module | Responsibility |
| --- | --- |
| `api.py`, `models.py` | Auth, bounded requests, validated contracts, response schemas, safe problem responses. |
| `store.py` | Token acceptance, hashes, intent/fences, cancellation, journal, freshness and retirement. |
| `runtime.py` | Single device worker, navigation deadline, preemption, stop, monitoring and completion. |
| `adb.py` | Quoted package-scoped commands, bounded subprocesses, images, foreground/media telemetry. |
| `vision.py`, `verification.py` | Model transport, strict protocols, matching and evidence policy. |
| `integration.py` | Controller adapters reading/writing the same durable token records. |
| `check.py` | Configuration/live-screen/saved-PNG diagnostics that send no input. |

Controller coordination runs in a serialized, drained worker thread. Model HTTP uses asynchronous HTTPX, total deadlines, limited connections, bounded responses, no redirects and explicit schema validation. ADB uses bounded asynchronous subprocesses. There is no blocking HTTP on the event loop serving screens, manual input, feed/status work and web requests, and no loopback HTTP hop for the embedded integration.

Schema 6 adds `executor_jobs`, `executor_devices`, and `executor_actions`. Acceptance and intent advancement commit together. Each input is journaled before dispatch and marked acknowledged/uncertain afterward. Durable intent/manual state is rechecked immediately before dispatch. Higher-intent navigation waits for older cleanup.

On restart, successful jobs are re-observed without relaunching; saved playback claims are invalidated immediately. Interrupted navigation that touched the device fails and queues cleanup; uncertain keys are never replayed. Accepted jobs with no input may proceed if still eligible and within deadline. A process lock enforces one executor per database. Run one API worker and one controller database per physical TV.

After acknowledged cancellation, full reports/payloads remain for seven days, then GET returns `410 Gone`. Compact ID/hash/token/intent tombstones remain so old requests cannot relaunch. Unresolved cancellation is never discarded. Backup includes all executor state. Restore preserves newer executor history from the target database, advances fences, pauses automation, and requires stop confirmation before manual input/resumption. State lost with an unavailable newer database cannot be reconstructed.

## Errors and validation

| HTTP outcome | Action |
| --- | --- |
| `401` | Correct service credentials. |
| `404` | Unknown token in this database; do not automatically turn a status miss into new Play. |
| `409` | Changed body, stale intent, expired deadline, paused automation or manual ownership. Refresh intent/state. |
| `410` | Retired token; its request still cannot relaunch. |
| `413` / `422` | Correct size/contract violations. |
| `503`, retryable | Retry the same operation with bounded backoff. Cancellation stays unconfirmed until acknowledged. |
| `503`, not retryable | Configure the real executor/external API. |

Problems use `application/problem+json` with `type`, `title`, `status`, `detail`, `code`, and `retryable`. Retryable problems include `Retry-After: 5`. Failures after acceptance are recorded in the token report. Reports omit credentials, model reasoning and images; evidence contains acquisition time and image hash.

Automated coverage includes auth/contracts, idempotency, active stop, old-token fencing, model cancellation, draining input, manual takeover, token persistence, crash ambiguity, completion scope, stale evidence, unsupported routes, quoted ADB subprocesses, total inference timeout, full controller delivery/status ingestion, and restoring a backup older than current playback. Controlled device/model boundaries do not establish physical compatibility or model accuracy. Follow [target-TV validation](executor-setup.md#validate-on-the-target-tv).
