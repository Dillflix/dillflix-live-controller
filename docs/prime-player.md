# Prime Player workflow integration

## Status

Controller 0.14.0 integrates Prime Player **API 4 / 0.1.0a5**, reviewed at commit `55e296ac1179478733c5efa89389a0743e1b728b`. Search, matching, Play, current status, cancellation, scoped stop and manual ownership are connected. The old screenshot executor and native probe are removed.

Host tests cover durable workflow behavior, RPC delivery and the actual service ownership engine with a controlled runtime. Physical TV, live inference and deployment acceptance remain pending.

## Responsibilities

| Component | Owns |
| --- | --- |
| Teamarr | Event catalog, structured source identities, routes, available event lifecycle facts |
| Controller | Watch plan, scheduling, durable intent/token, recovery policy, manual takeover |
| `prime_player/matching.py` | Semantic association of a Teamarr event with a Prime result |
| Prime Player service | Runtime compatibility, search, resolution, launch verification, current session evidence, application cancellation |

The new workflow does not construct an ADB device, run an accessibility or
MediaSession collector, use screenshot navigation, or instantiate a vision actor.
The optional matcher uses a text completion model. `PLAYBACK_ADAPTER=prime-video` is rejected with a migration message. Supported modes are `simulator` and `prime-player`.

The shared `executor/worker.py` and store contain only durable workflow
lifecycle/ownership infrastructure. Controller Play/status/Cancel remain the
orchestration API, identifying Teamarr events. Prime attempt IDs identify
application launches and are recorded separately. No new public recovery,
authority-renewal, or lifecycle-by-content API is added.

## Matching

1. The planner commits to initiating/switching playback, then the workflow searches
   using supplied opponent names (or the event title when there are no teams).
2. Only live tiles with a valid GTI, sufficiently correlated identity, and a fresh
   handle are eligible. Explicit replay/highlight/recap variants and recognized
   conflicting dates are excluded. Collection IDs and ambiguous candidate IDs
   are never promoted into playable identities.
3. A unique exact event title or matchup using provider-supplied opponent aliases
   can be selected deterministically. Duplicate copies of the same GTI are grouped.
4. Otherwise the LLM receives the original event, permitted Prime routes, and
   eligible titles/labels/date text. Ordinary labels like “Packers vs. Ravens” are
   sufficient inputs; richer Prime team/league metadata is not a prerequisite.
5. The model returns a supplied GTI and route plus a reason and exact supporting
   labels, or abstains. It cannot choose excluded results or invent identifiers.
   A sole eligible result is not automatically treated as the requested event.
6. The service receives the selected **handle**, live mode, and a persisted UUID
   attempt ID. Its fresh resolver retains responsibility for entitlement and
   Watch Now semantics. Model output is selection evidence, not playback proof.

Selection records, candidate exclusions, service session/search generation,
chosen IDs, launch outcome and current status are exposed in the token report's
optional `prime_player` object. No screenshots are captured. Search coverage stays
`loaded_renderer`/incomplete; no match does not establish catalog absence.

Without `PRIME_PLAYER_MATCH_MODEL`, unambiguous deterministic matches still work;
other cases abstain. Identical titles with different IDs can remain genuinely
ambiguous. No arbitrary first-row tie-break is used.

## Persistence and monitoring

The mapping is Teamarr content ID → controller token → Prime service session →
Prime attempt → requested/resolved GTI. Persist the attempt ID before dispatch.
Lost Play responses and controller restarts use read-only inspection of that same
attempt; they never resend Play or repeat a stale search handle. Interrupted
searches are cancelled rather than automatically repeated during recovery.

The launch outcome must establish `live_watch_now` and verified playback. Current
status must independently match the saved service/attempt/IDs and report fresh
playing progression. Native sample age is subtracted from the checkpoint time;
polling never manufactures fresh evidence. Historical Playing is not current
Playing. Service restart or contradictory identity withdraws verification. A
subsequent scheduled retry cannot pass an unresolved cancellation obligation.

Paused, buffering, stopped, switched, ended and unknown player states are recorded
without asserting sporting-event completion. Teamarr and the user's explicit
manual completion remain the lifecycle sources. Live mode means the service
resolved Watch Now; current status is not a fresh measurement of delay behind live
or HDMI/rendered video quality.

## Cancellation, stop and ownership

Every Search and Play includes a current acknowledged **automatic ownership envelope**. The controller never resumes a manual owner on its own. The web gateway obtains a manual receipt using `suspend(true, owner_id, previous)` and forwards wake/keys/text with strictly increasing sequence numbers. Release acknowledges automatic ownership before resuming the planner. There is no additional public authority API.

Cancellation first persists the controller's intent fence, then interrupts in-flight remote work through the service's out-of-band cancellation lane. It does not wait for a long search HTTP response before asking the runtime to cancel. Only an acknowledged input barrier permits the next controller or manual input operation.

For an owned launch, the controller calls `stop(session_id, attempt_id, ownership)` after cancellation. It records dispatch **before** sending, so a lost response or restart cannot replay a physical stop. Uncertain delivery is followed by a fresh cancellation barrier and recorded as unknown. Service restart, manual ownership and superseded attempts never authorize stopping unrelated playback.

| Cancel `active_playback` | Meaning |
| --- | --- |
| `stopped` | Service confirmed a matching native stopped/ended event for this attempt |
| `already_inactive` | This controller token never acquired the device |
| `not_current` | The token no longer controls this service lifetime/attempt; unrelated playback was left alone |
| `unknown` | Input is quiescent, but playback inactivity was not proved; page exit alone has this result |

A 200 response confirms `input_quiescent=true`; it does **not** imply `active_playback=stopped`. An unacknowledged input barrier stays pending and returns an error, preserving the cleanup obligation. Read the token's `prime_player.stop_result` / `stop_error` for detail. None of these states proves the sporting event ended.

No competing automated SDK client should control the same device. External physical remotes can change playback, which current status will detect. The controller never kills or restarts the Prime service to resolve ownership errors.

## Deployment

Keep the existing Prime Player service. Do not launch a second service, stop it,
or restart Prime as part of deploying the controller. Set:

```dotenv
CONTROLLER_MODE=teamarr
PLAYBACK_ADAPTER=prime-player
PRIME_PLAYER_SOCKET=/absolute/path/to/player.sock
SCREEN_ADB_SERIAL=YOUR_DEVICE_SERIAL
PRIME_PLAYER_MATCH_MODEL=YOUR_TEXT_MODEL
EXECUTOR_LLM_BASE_URL=http://YOUR_MODEL_SERVER/v1
EXECUTOR_LLM_API_KEY=
NAVIGATION_TIMEOUT_SECONDS=300
```

The model settings reuse the existing structured-completion transport. No actor
or observer model is required. Search defaults to 100 seconds; current-status
inspection defaults to 10 seconds (configurable 5–60). The whole workflow has one
durable deadline, including matching and service launch verification.

For Docker, the service exposes a **mode-0600 Unix socket**, not a TCP port. Use
the optional overlay and set `PRIME_PLAYER_STATE_DIR` to its parent directory:

```bash
docker compose -f compose.yaml -f compose.prime-player.yaml config --quiet
docker compose -f compose.yaml -f compose.prime-player.yaml up -d --build
docker compose -f compose.yaml -f compose.prime-player.yaml exec controller \
  python -m controller.prime_player.check
```

Mount the parent directory, not just the socket inode, so a recreated socket is
visible. The controller UID must have directory traversal and socket access.
The image defaults to UID 10001. Coordinate the host service UID or grant narrowly
scoped ACL access (including on replacement sockets); do not make the socket
world-writable. A read-only bind mount does not itself restrict socket RPCs.
Retain the existing database volume and persistent ADB identity for manual input
and screen mirroring. Changing container UID also requires existing volume access.

Before upgrading an old `prime-video` deployment, pause automation and cancel outstanding real playback on that version. Save a database backup using [operations](operations.md), then stop the old controller. Start only the new `prime-player` controller against that database. Startup rejects an adapter switch with unresolved old cleanup obligations; keep the old version available to resolve those rather than deleting history. Plans/settings are retained. Simulator migration clears simulated observations.

Remove `EXECUTOR_ACTOR_MODEL`, `EXECUTOR_OBSERVER_MODEL`, `EXECUTOR_ACTOR_PROTOCOL`, and the old ADB/probe/visual/settle/action-limit settings from deployment configuration. The remaining model settings are for text matching only. `EXECUTOR_CANCEL_TIMEOUT_SECONDS` now defaults to 80 seconds to allow a bounded native stop inspection. Keep only one `PRIME_PLAYER_SOCKET` entry. The existing probe APK is no longer read or required; this release does not uninstall software from the TV.

For gateway-only testing use `PLAYBACK_ADAPTER=simulator` with the same socket. The supplied Compose overlay deliberately enables real `prime-player` playback; start it with automation paused until the checks below pass.

## Acceptance on the target host

1. Run the read-only `python -m controller.prime_player.check`. Confirm the service
   session, actual capability availability, socket permissions and ownership
   receipt. This command sends no search, play, suspend or device commands.
2. Initiate one known live event. Inspect its selection audit, live resolution,
   attempt identity and fresh current status. Confirm the TV plays that event.
3. Exercise multiple live results, a replay-only result and unresolved identity.
   Check semantic selection and abstention with the configured real model.
4. Take manual control during search and during playback; verify no late launch,
   correct scoped stop result, and input readiness only after acknowledgement.
5. Reconnect/restart the controller with the service left running. Verify the
   original attempt is inspected without another search or launch.
6. Validate service restart, socket loss, paused/buffering/switched playback and
   stale evidence. No condition should fabricate event completion or stop a newer
   externally selected session.

Host tests exercise controlled boundaries. Actual live search/launch, matching
accuracy, cancellation, Docker permissions and sustained device monitoring still
require this acceptance run.
