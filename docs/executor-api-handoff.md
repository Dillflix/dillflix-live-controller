# Playback API and implementation guide

Controller 0.14.0 exposes three durable orchestration operations. Prime Player API 4 executes application actions over a local Unix socket. The controller owns scheduling and semantic event selection; it does not run a second device navigator. See [workflow, setup and acceptance](prime-player.md) and [OpenAPI](executor-api.openapi.yaml).

| Operation | Request | Result |
| --- | --- | --- |
| `POST /v1/playbacks` | Original Teamarr snapshot, all allowed options, opaque content ID, request ID, device, intent version, live mode and deadline | 202 and durable token; 200 for an identical retained retry |
| `GET /v1/playbacks/{token}` | Controller token | Operation progress, expiring playback observation, independent event lifecycle, cancellation and Prime selection/attempt/status evidence |
| `POST /v1/playbacks/cancel` | Device plus either token or `through_intent_version` | Acknowledged input quiescence and separately reported active playback state |

External calls require `Authorization: Bearer <EXECUTOR_API_TOKEN>` with a configured 32-character-or-longer token. A blank setting disables these external operations. The built-in planner calls the same durable workflow directly. Keep application authentication behind the existing nginx proxy.

## Initiating playback

The schema in OpenAPI is authoritative. Keep Teamarr's opaque `content_id` and complete original snapshot; do not replace them with a Prime GTI. Send every permitted original viewing option unchanged. Only live mode is accepted. `deadline_at` must include a UTC offset. A 202 response means accepted, not playing.

Reuse the same request ID and identical body when retrying an uncertain POST response. A changed body with the same ID is 409. No request-ID recovery endpoint is necessary. Intent versions fence older work; cancelled or retired request tombstones cannot relaunch content.

The built-in planner performs non-navigational catalogue search before committing a playback switch. It matches EVENT entries by title/date and permitted route, requires ENTITLED/LIVE for a launch, then persists the Prime service session and attempt ID before Play by content ID. External Play requests perform the same catalogue checks inside their already-authorized workflow. The service freshly resolves live Watch Now and verifies launch; monitoring requires current attempt-bound evidence.

## Reading status

`operation` describes the original request. `observation` is current playback evidence with an acquisition timestamp and validity deadline. Its verification is withdrawn on age, service restart, changed content, pauses or unavailable evidence. A historic successful launch does not override current status.

`prime_player` retains the query, eligible/excluded candidate audit, decision and quoted labels, selected GTI/route and consumed catalogue response, service session, attempt, launch outcome, latest playback status, ownership and cancellation/stop evidence. No rendered tile handle is used. Uncertain Play is inspected by saved attempt, never replayed. Polling the controller getter reads stored evidence; it does not refresh timestamps or issue a device query.

`content_status` carries event lifecycle evidence from Teamarr or confirmed native Prime completion; explicit manual completion also applies in the controller. Fresh Prime `ended` completes only the owning, previously verified live attempt with matching service session, attempt, requested/resolved/current content and current intent. It requires `matches_attempt=true`, `is_playing=false` and no error. The operation becomes `completed` with phase `event_ended`; lifecycle source is `prime_player`, with confirmed device evidence naming the session and attempt. Completion persists across expiry, restart and late feed responses. Prime stopped, paused, switched, errors, lost connection and elapsed schedule estimates never prove event completion. Broadcast/session coverage cannot be completed by an unrelated individual match ending.

## Cancellation and manual input

Persist the local fence first. Cancel interrupts remote work through the service's acknowledged runtime barrier, including searches awaiting results. Stop is scoped to the saved service session and attempt, so an old token cannot stop newer playback. An uncertain physical stop is not replayed.

A successful response always has `input_quiescent=true`. `active_playback` is `stopped` only with confirmed native stopping, `already_inactive` for an untouched token, `not_current` when the token no longer owns playback, or `unknown` when inactivity is unproven. A playback page exit alone remains unknown. Retrying an acknowledged Cancel returns its retained outcome without another Stop. An unconfirmed input barrier returns 503 and remains a durable retry obligation.

The web remote acquires its acknowledged service manual receipt after controller cancellation. Wake, keys and text all use that receipt and increasing sequence numbers. Release drains writes, validates the durable manual owner, and acknowledges automatic ownership before planner resumption. Physical remotes remain external actors. No separate public authority, device-observation, stop or lifecycle-by-content endpoint is added.

## Errors and recovery

Errors use `application/problem+json` with stable `code`, message and `retryable`; 401 is authentication, 404 unknown token, 409 conflicting request/stale intent, 410 retired token, 413 body limit, 422 validation and 503 unavailable/unconfirmed execution. An error or HTTP timeout is not proof an action was undelivered. Inspect retained token/attempt evidence before retrying; service mutations are never transport-retried.

The worker and controller calls remain asynchronous or off the event loop. Restart reconciles the saved attempt without replaying Play. Interrupted search and unconfirmed cancellation remain cleanup obligations. Service restart withdraws old verification and cannot transfer an old token to a new service lifetime. Offline restore retains newer execution history, advances intent fences and leaves automation paused.

## Implementation map

| File | Responsibility |
| --- | --- |
| `controller/prime_player/client.py` | Bounded async API 4 RPC, capability/ownership validation |
| `controller/prime_player/matching.py` | Live filtering, deterministic/LLM label selection and audit |
| `controller/prime_player/workflow.py` | Durable search/launch/monitor/cancel lifecycle |
| `controller/prime_ownership.py` | Manual receipts, sequenced wake/keys/text, release |
| `controller/executor/store.py`, `worker.py` | Tokens, intent fences, action journal, cancellation obligations, retention |
| `controller/executor/api.py`, `models.py` | Three authenticated public operations and schemas |
| `controller/executor/integration.py` | Built-in scheduling and lifecycle adapters |

Host tests cover these boundaries. Install the reviewed Prime Player package without device extras and run `tests/test_prime_service_contract.py` for tests against its real HTTP server/ownership engine with a controlled runtime. The normal suite skips that optional contract suite if the package is absent. Actual TV/account/model behavior and Docker permissions require the target acceptance procedure.
