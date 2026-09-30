# Playback executor API implementation handoff

**Implement three business APIs: Play, token status, and Cancel.** Play durably accepts a live playback request and returns a token. The getter uses that token to report navigation, current playback, and event completion. Cancel ends pending navigation and stops active playback belonging to its scope. The controller chooses what to watch; the executor chooses how to reach it through the supplied viewing options.

**Contract status: proposed external HTTP API, revised 30 September 2026.** The [OpenAPI draft](executor-api.openapi.yaml) defines the three operations and validated synthetic examples. These are not the controller's existing `/api/v1/...` routes. Autonomous playback remains simulated; no real HTTP executor or authoritative completion integration has been connected. Controller preparation included with this revision is described below.

## Scope decisions

| Earlier proposal | v1 decision | Reason |
| --- | --- | --- |
| Recover a token through a separate `request_id` endpoint | **Deferred optimization** | Retry the same Play request with the same request ID and receive the same token. No extra lookup is needed. |
| Observe the device independently | **Deferred** | The token getter includes current playback evidence. After restart, inspect the saved token. After a switch, inspect the new token. Manual navigation has no verified autonomous playback identity. An inventory of arbitrary TV activity is outside this release. |
| Look up lifecycle by `content_id` | **Deferred** | Once Play returns a token, that token identifies the content for lifecycle checks. Teamarr supplies pre-play scheduling/eligibility evidence. Independent tracking of content that has never been played, particularly outside the feed window, is a separate future capability. |
| Transfer/renew input authority | **No separate API in v1** | One controller owns autonomous scheduling and the browser input gateway. A durable, acknowledged Cancel barrier plus monotonic intent versions coordinates manual takeover. |
| Stop the device | **Fold into Cancel** | Cancel stops its active playback as well as pending navigation. An unrestricted stop of unrelated/manual playback is outside scope. |
| Webhooks, SSE, bulk status, discovery, capability negotiation | **Deferred** | Polling the token getter covers v1. Deployment health checks can be added operationally without expanding the playback workflow. |

The earlier proposal exposed internal adapter concerns as separate service endpoints. They are useful distinctions in the implementation, but do not each require an API.

The simplified scope has one deliberate limit: an event with no playback token and no fresh Teamarr evidence remains unknown. Retain its watch-plan entry. Do not infer that it ended or launch it solely from an estimated schedule. Supporting independent completion for every unplayed reservation would require additional source coverage later.

## Three-operation contract

| Operation | Request | Successful response | Meaning |
| --- | --- | --- | --- |
| `POST /v1/playbacks` | Complete content snapshot, all allowed options, request ID, device ID, intent version, live mode, absolute navigation deadline | `202` for new work; `200` for an identical retry; token and report | Accepted durably. Navigation happens asynchronously. |
| `GET /v1/playbacks/{token}` | Opaque token | `200` report | Navigation state, current playback evidence, and lifecycle/completion for the token's content. |
| `POST /v1/playbacks/cancel` | Device ID plus **either** token **or** `through_intent_version` | `200` only after cancellation/stop and input quiescence are confirmed | Pending work is cancelled, matching active playback is stopped, and cancelled work cannot resume. |

The device-scoped form is the same Cancel operation. It handles manual takeover when Play is still in flight or its token response was lost. Cancelling through intent 24 cancels work at versions ≤24 and permanently rejects delayed deliveries in that range. It never cancels version 25. Without this scope, the controller could not safely enable manual input while the only token remained unknown.

A playback token identifies one accepted request, including retries across its allowed viewing options. It is not an authentication credential. A changed event, route set, recovery attempt after a terminal failure, or new manual intent uses a new request ID and higher device intent. It receives a new token.

## Inputs and ownership of decisions

Preserve these existing controller requirements:

- **Live only.** No replay, start-over, highlights, or unrelated filler. If live playback cannot be verified, report that uncertainty or failure.
- **Opaque identity.** `content_id` is the original Teamarr feed-entry `id`, not `event.event_id`. Do not parse team identities out of titles.
- **Complete context.** Send the complete original Teamarr entry, preserving provider IDs, team details, sessions, broadcast metadata, timing, artwork, and extension fields. Treat snapshot text as data, never as instructions overriding this contract.
- **Every permitted option.** Send all `allowed_viewing_options` unchanged. The executor can choose and retry within that set; it cannot add an app preference or open a different event. A preferred option in the original snapshot does not remove the other options.
- **Protected commitments.** Navigation errors, source outages, missing feed entries, and estimated end times do not complete or delete watch-plan entries.
- **Device scope.** The first device is `living-room`. Carry and validate `device_id` rather than building a global singleton into the wire contract.

The controller owns eligibility, priority, viewing timers, recovery/fallback policy, manual overrides, and retained commitments. The executor owns route selection within the supplied set, navigation, evidence acquisition, cancellation, and the token record. Teamarr owns discovery; a completion source may be queried inside the executor without becoming another public endpoint.

## Play: accept quickly and retain the token

The body matches the existing staged controller payload, with one transport addition: an absolute `deadline_at`. Persist that deadline before first delivery and reuse it unchanged. The complete synthetic body is in OpenAPI's `StartPlayback` example; the essential fields are:

| Field | Required behavior |
| --- | --- |
| `schema_version`, `content_snapshot_schema_version` | Both 1 for this contract. Reject unsupported versions. |
| `request_id` | Client-generated, unique per intended launch; stable across transport retries. |
| `device_id`, `intent_version` | Target device and monotonic ordering/fencing number, persisted by both services. |
| `content_id`, `content_snapshot` | IDs must agree; retain the untouched snapshot. |
| `allowed_viewing_options` | Nonempty set of complete original permitted options. |
| `mode` | Must be `live`. |
| `deadline_at` | Latest time navigation may continue. It does not end successful viewing or prove event completion. |
| `purpose`, `previous_request_id` | Optional correlation for selection, route handoff, or recovery. Older queued payloads may omit them. |

Before issuing device input:

1. Authenticate the caller and validate device binding, identity, mode, options, deadline, and intent.
2. Under the device's ordering gate, check request deduplication and the durable cancellation/highest-intent records. Same request ID with different immutable data returns `409`; identical data returns its existing token/report. Check existing records before applying expiry rules to a retry; never restart an old terminal operation.
3. For new work, reject intents at/below the cancellation fence and intents older than accepted device intent. Equal intent with a different request is a conflict. A higher intent revokes/drains older navigation before any new input. Replace older active playback only as part of executing that newer request.
4. Atomically commit the immutable request, token, and recoverable queued work. Only then return `202` with `Location: /v1/playbacks/{token}` and the initial report.
5. Run navigation outside the HTTP handler. Respect the original deadline across service restarts and internal route attempts.

Use meaningful progress phases such as `opening_app`, `finding_content`, `choosing_live`, and `verifying`. Record attempted viewing-option IDs and bounded error reasons. Do not invent percentage progress. Unsupported live routes or sign-in requirements can fail with a specific error such as `needs_user_action`; do not make purchases or change subscriptions.

A cached snapshot can have old lifecycle information. Its `status` field alone must not override newer evidence or establish that playback is live. Verify actual content identity, the selected permitted route, and live presentation.

### Retry behavior

The normal loop is exactly: **call Play; retry transient/uncertain errors; save a returned token; poll that token.**

The important qualification is that an HTTP timeout does not prove Play failed before acceptance. Retry the **same request ID and immutable body**, with bounded backoff, so the executor returns the original token instead of launching twice. This is basic safe retry behavior; a separate recovery-by-request-ID endpoint remains deferred.

Do not blindly retry malformed input, denied authentication, or a stale intent. Correct the cause or reevaluate current controller intent. Once a token exists, an operation failure is visible in the getter; the controller decides whether a new attempt is warranted. Reposting the old failed request only returns its old result.

If manual takeover or a newer selection supersedes an uncertain Play, stop retrying it and cancel its scope. A delayed network delivery must remain fenced even after its sender stops waiting.

## Token getter: three answers in one response

| Report section | Answers | Interpretation |
| --- | --- | --- |
| `operation` | Was this launch accepted, navigating, verified, failed, cancelled, superseded, or timed out? | `playing_verified` records launch success. `finished_at` is the navigation finish time, not the event end. |
| `observation` + `observation_status` | Is this token's requested content currently verified on the TV? | Fresh device evidence can establish playback; old success cannot. |
| `content_status` | Is the underlying requested event/broadcast scheduled, live, ended, cancelled, delayed, suspended, postponed, or unknown? | Only explicit evidence establishes completion. |

The OpenAPI `Verified` example shows successful playback with lifecycle temporarily unavailable. `Ended` shows explicit completion evidence. Both are valid `200` responses: service contact, playback health, and lifecycle-source health are independent.

Return all token-bound identity fields and a monotonically increasing report `revision`. The controller rejects mismatched identity and older revisions. `GET` is read-only and never navigates, resumes, stops, or extends authority. It may read maintained evidence or perform bounded source refreshes; it must not wait indefinitely for perception or a provider. Use `Cache-Control: no-store`.

### Current playback

`verified=true` requires matching device, request, intent, content, and an allowed viewing-option ID, plus healthy playback and live presentation. Return the actual acquisition time and expiry. A screenshot of the right app or successful input delivery is insufficient.

Initial freshness target: no more than 15 seconds, capped by the controller's own policy. Repeated polling of cached evidence must never advance `observed_at`. Expired evidence becomes stale and `verified=false`; unavailable evidence can be null. A newer request's observation never appears as this token's observation.

After restart, load and inspect persisted tokens. Reacquire evidence before claiming current playback. After a switch, an old token may retain historical launch success while reporting no current verified playback. During manual control the controller clears its autonomous playback identity; do not try to identify arbitrary manually opened content for v1.

Physical remotes or other ADB clients can still change the TV outside this application's gate. The getter should detect loss/mismatch when observable and report unknown/stale when it cannot determine the current state. That does not require an independent device-observation endpoint.

### Lifecycle and event completion

The getter resolves lifecycle using the token's stored content snapshot/provider identifiers. A provider status source is preferable where available. Device/video inference requires an explicit evidence policy for the exact content scope; a confidence number alone is not proof.

For example, a hockey game's final result can end that game. One golf player's finished round does not end the requested tournament coverage, and one game ending does not end an aggregate broadcast. Commercials, buffering, a menu, an app exit, a disconnected TV, a navigation deadline, a missing feed entry, or an estimated end time do not establish completion.

Report `source`, real evidence timestamps, expiry, and evidence metadata. `ended`/`cancelled` requires `evidence.decision=confirmed`. Keep cancellation of the **playback request** distinct from cancellation of the **sporting event**. Unknown lifecycle must not terminate a commitment.

Nonterminal evidence expires to unknown. Previously confirmed terminal evidence can remain known through a source outage; a newer explicit correction can supersede it. Continue lifecycle checks through the token while retained when the source supports that, including after a route switch or Cancel. If observation depended on a screen that is no longer showing the content, report unavailable rather than fabricate ongoing observation. Never reopen content merely to satisfy a getter.

Without a token, use fresh Teamarr evidence for pre-play eligibility. A reserved event outside discovery may remain unknown. A content-ID lookup is not a prerequisite for this playback integration.

## Cancel: active stop and manual-input barrier

Examples:

```json
{"device_id":"living-room","token":"pb-example-001"}
```

```json
{"device_id":"living-room","through_intent_version":24}
```

The forms are mutually exclusive. The first targets a known playback. The second cancels all autonomous work through an intent version, including requests that have not arrived or whose token is unknown.

Cancel must:

1. Persist the cancellation request/fence before acknowledging or starting cleanup. Fence values only increase and survive process/device-worker restart and history pruning.
2. Stop retries, queued navigation, recovery attempts, and input already in flight within scope. Serialize cancellation with every input action, including retries and process callbacks; an entry-point check is insufficient.
3. Stop active playback attributable to the cancelled token/scope. Implement a reliable app-specific stop behavior; do not blindly toggle play/pause or send Home after ownership has moved to newer/manual content.
4. Drain all owned input sources and confirm the scope cannot issue further input. Return `200` with `input_quiescent=true`, `active_playback: stopped|already_inactive`, and acknowledgement time only after that guarantee holds.

If the token is already inactive, do not disturb a newer target. Repeated Cancel is safe and returns an acknowledgement without additional stop input. A device-scoped retry with an old fence never cancels newer intents. Cancelling active playback does not mark its event ended.

If stop/quiescence cannot be established within the bounded request budget, return a retryable failure such as `503`; retain the cancellation obligation. The controller leaves manual input disabled and retries. Do not return an early `200` meaning merely “cancellation queued.” No separate Stop, authority-transfer, or renewal endpoint is necessary.

### Manual handoff order

**Take control:** persist automation paused, increment intent, cancel pending controller jobs, clear desired/observed playback, and record a pending device cancellation fence. Wait for any controller-side executor call already running. Send device-scoped Cancel. Enable the manual input channel only after acknowledgement.

**Release or expiry:** revoke the manual connection and drain its input transport while automation is still paused. Then restore the requested automation mode. A new Play uses an intent above the cancellation fence. Failed/pending executor cancellation survives the session ending and still blocks new autonomous delivery until acknowledged.

**Takeover by another browser:** invalidate the old owner, drain its transport, and honor the current cancellation barrier before enabling the new owner's input. Replaying an old successful browser command receipt must not reclaim a newer session.

This is one controller's shared input gate, implemented with ordering and cancellation. The executor needs its own per-device gate around actual side effects. An in-process controller lock alone cannot stop an independently running navigation process. All autonomous input, including worker retries and subprocesses, must obey the durable intent fence. Multiple independent controller owners and lease-based leader election are outside v1.

## Controller preparation included with this revision

| Gap | Implemented preparation | Remaining executor-integration work |
| --- | --- | --- |
| Blocking synchronous playback calls on the event loop | `Controller.run_worker()` now runs each tick in a worker thread and awaits it. Ticks do not overlap. Cancellation drains in-flight work before controller shutdown releases leases/database ownership. | Implement finite connection/read/write/pool and total request budgets in the real adapter. Cancelling a coroutine cannot terminate a synchronous HTTP call. |
| Manual input racing autonomous input | A playback lock serializes tick/handoff calls. A durable `input_handoff` blocks new playback and manual authorization until `cancel_device()` acknowledges. Failed handoffs survive restart and session release. Legacy retained manual sessions acquire a barrier before input. | Map `cancel_device()` to the device-scoped Cancel body and enforce the barrier at the executor's physical input boundary. |
| Automation resuming before manual cleanup finishes | Release/expiry drains the input transport before restoring automation. | Exercise actual scrcpy/executor process overlap and cleanup on the target device. |
| Cancel only ending navigation | Simulator Cancel now clears matching active playback. Its device fence rejects delayed requests and preserves higher intents. | Implement and verify real stop behavior for supported apps. |

Network calls remain outside SQLite transactions. The worker-thread boundary preserves existing synchronous adapter compatibility and keeps screen/input sockets and other event-loop tasks responsive. It does not make network operations instantaneous; configure finite timeouts and ensure coordinator lease checks remain valid through the maximum per-tick work budget. One API worker and one playback worker remain the deployment model.

No real executor URL, credentials, HTTP adapter, token persistence/translation, or real completion evidence is configured by these changes. The following are explicit integration tasks, not completed behavior.

## Engineer implementation sequence

1. **Executor persistence and ordering:** store token, immutable request/hash, device intent/fence, operation history, evidence, and cancellation state. Recover queued work on restart without replaying completed or cancelled input. An active token is retained; keep inactive records for at least seven days initially. Retain ordering/fence records beyond payload pruning. Expired/retired IDs must not silently become new launches.
2. **Play + getter:** implement durable acceptance, safe repeated POST, asynchronous navigation, bounded permitted-route attempts, fresh playback evidence, and lifecycle evidence. Add the explicit deadline to persisted controller transport data, without regenerating it on retry.
3. **Cancel:** implement both scopes and verify actual input drainage/stop. Test delayed Play delivery after acknowledgement and old Cancel delivery after a newer Play. Complete this before enabling real autonomous input alongside manual control.
4. **Controller HTTP adapter:** keep `submit`, `inspect`, `observe`, `cancel`, and `cancel_device` as internal methods if convenient. They map to only the three HTTP operations below. Persist tokens immediately on receipt, including replies arriving after their request became obsolete, so cleanup can still locate them.
5. **Completion ingestion:** integrate token lifecycle evidence with the controller's persistent content-status cache and existing freshness/identity rules. Teamarr continues to cover unplayed entries. The existing status coordinator's per-content lease/request validation must be respected; do not bypass it by writing arbitrary status rows.
6. **Hardware/deployment verification:** test app navigation, live-vs-replay verification, stop, manual takeover, proxy authentication, restart, and slow/outage behavior on the target host before removing simulation labels.

| Existing internal method | External mapping |
| --- | --- |
| `submit(request)` | POST Play with the saved immutable body; save returned token using the durable job/token association. |
| `inspect(request_id)` | Resolve the token locally, then GET it. If no token is saved, return no local report so delivery retries the same Play. No external request-ID lookup. |
| `observe(device_id)` | Resolve the controller's current known token locally and project current evidence from its getter. Null when no token exists; never substitute the latest historical success. Cache one fetched envelope per tick if useful. |
| `cancel(request_id)` | Cancel the locally associated token. If delivery was uncertain and no token exists, use the stored request's device/intent scope to cancel safely. |
| `cancel_device(device_id, through_intent_version)` | POST the device-fence form of Cancel; return only on confirmed acknowledgement. |
| Internal content-status lookup | For content with a retained token, consume/project lifecycle from GET token. For unplayed content use Teamarr/cached evidence, preserving its original freshness. No new public lifecycle endpoint. |

The current job table has `executor_job_id`, but the adapter must define and consistently persist its token mapping before relying on it. Do not keep tokens only in process memory. Validate the external report before turning it into existing `receive_playback_report()`/recovery observations. Preserve `request_id`, device, intent, content, and permitted-option checks even when the token appears valid.

The proposed lifecycle schema supports `device_observed` evidence; the existing controller validator only recognizes its current provider/fixture/feed timestamp bases. Add deliberate validation and source mapping for that extension during integration. Do not mislabel video inference as provider evidence or leave simulated flags on real results.

## Error policy and operational defaults

| Condition | Response / controller action |
| --- | --- |
| Invalid body or unsupported live target | `400`/`422`; correct the request or report a terminal operation error. |
| Missing/denied service credential | `401`/`403`; fix configuration, do not retry forever. |
| Reused request ID with changed data, stale/equal conflicting intent | `409`; do not relaunch. Reevaluate intent. |
| Unknown token | `404`; do not infer event completion. |
| Known retired token | `410`; reconcile local intent, do not replay its launch automatically. |
| Rate limiting / transient outage | `429`/`503`; honor `Retry-After` when supplied, otherwise bounded backoff with jitter. |
| Timeout / reset before response | Outcome unknown. Retry identical Play/Cancel if still relevant; GET can be retried safely. |
| Playback or lifecycle source unavailable inside a valid getter | `200` with the affected evidence marked unavailable/stale and a structured error. |

Return structured errors with stable `code`, safe human-readable detail, and `retryable`. Do not expose credentials, raw screenshots, or manual text. All operations use a server-side service credential scoped to the configured device; tokens do not bypass authentication. Keep that credential separate from browser manual-owner tokens and existing nginx browser authentication.

Starting transport targets, to validate on hardware: Play/status responses within five seconds; Cancel also bounded to five seconds per attempt, returning retryable failure if cleanup has not completed. Poll about every second while navigating and every five seconds while playing. Navigation budget defaults to the existing 120 seconds. Lifecycle freshness defaults to the controller's existing 120-second maximum. Polling cadence never renews evidence by itself.

Configure a controller-reachable executor base URL. Within Docker, `localhost` names that container; use the shared network service name or host address as appropriate. The browser continues using its authenticated controller origin. This handoff does not change website listening/proxy configuration or require the user's PC to contact the executor directly.

Log request ID, token reference, device, intent, route attempts, phase durations, evidence age, cancellation latency, and safe error codes. Monitor expired observations, deadline failures, unacknowledged cancellations, and late results. A connected HTTP service can still have unavailable device/lifecycle evidence.

## Acceptance scenarios

| Scenario | Required result |
| --- | --- |
| Play succeeds, response lost | Identical POST returns the same token; navigation is not duplicated. |
| Play rejects malformed input | No device side effect; actionable error. |
| Acknowledged launch, navigation fails | Token remains queryable with failure; no event-completion claim. |
| Correct event opens as replay | Never return verified live playback. |
| First allowed route fails | Try another supplied route within the same deadline; never another event. |
| Getter is repeatedly polled during evidence outage | Evidence timestamps stay unchanged; verification expires. |
| Playback healthy, lifecycle source down | Verified playback can coexist with lifecycle unknown. |
| Event goes beyond its estimated end | Continue until explicit completion or newer controller intent. |
| Token Cancel during active playback | Owned playback stops; repeat Cancel is safe; event remains independently live/unknown. |
| Old Cancel arrives after newer playback | No stop/input into the newer target. |
| Manual takeover during slow Play with no token | Pause first; wait for device cancellation fence; late Play cannot inject input. |
| Cancel times out or service restarts mid-cancel | Manual input stays disabled; durable cancellation is retried. |
| Cancel succeeds then delayed old worker restarts | Fence prevents every old navigation/input action. |
| Manual release/expiry with slow transport cleanup | Automation cannot resume until manual input drains. |
| Playback HTTP blocks | Feed/status tasks, screen/input sockets, and API event-loop work remain responsive. |
| Shutdown during synchronous executor call | Drain bounded in-flight call before relinquishing database/worker ownership. |
| Unplayed reserved event leaves discovery | Retain commitment; unknown until fresh evidence, without requiring a content-ID API. |
| Controller/executor restart | Retain tokens, intent fences, deadlines, and cancellation obligations; reacquire current evidence. |

`tests/test_executor_handoff.py` exercises controller thread isolation/shutdown, delayed submit/manual takeover, failed-barrier restart, active cancellation, stale device fences, and release/expiry cleanup ordering. Existing playback/manual-control tests cover identity checks, retry, late results, idempotent browser commands, and transport cleanup. These local tests establish controller/simulator behavior; real executor and Fire TV behavior must pass the same contract scenarios.
