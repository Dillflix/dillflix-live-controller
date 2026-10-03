# Prime Player workflow integration

## Status

The controller-side search → label matching → live launch → current-status workflow
is implemented and host-tested against controlled service/model boundaries. The
Prime Player service contract reviewed is API 3, version 0.1.0a4, commit
`bd53c548b9cf1f3fe8cf326fe87d32dc448e2ffe`.

**Activation is intentionally blocked until the independently developed
cancel/stop API is connected.** That service version has no cancel/stop operation.
`PrimePlayerClient.cancel_attempt()` is the single explicit integration seam; it
currently returns an error, and `cancellation_ready=False` prevents Search/Play.
No guessed RPC, force-stop fallback, service restart, or suspend-as-stop is used.
This is development integration code, not a claim of deployed TV acceptance.

## Responsibilities

| Component | Owns |
| --- | --- |
| Teamarr | Event catalog, structured source identities, routes, available event lifecycle facts |
| Controller | Watch plan, scheduling, durable intent/token, recovery policy, manual takeover |
| `prime_player/matching.py` | Semantic association of a Teamarr event with a Prime result |
| Prime Player service | Runtime compatibility, search, resolution, launch verification, current session evidence, application cancellation |

The new workflow does not construct an ADB device, run an accessibility or
MediaSession collector, use screenshot navigation, or instantiate a vision actor.
The optional matcher uses a text completion model. The old `prime-video` mode is
retained explicitly for existing deployments; there is no automatic fallback to
it and the two execution paths never run together.

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

## Cancel/stop integration seam

The other agent owns the service implementation. Do not add a competing stop
implementation to this repository. Once the service's actual wire contract is
available, adapt only `PrimePlayerClient.cancel_attempt(session_id, attempt_id)`
and its contract tests, then set `cancellation_ready=True`.

The method's **internal normalized result** must contain:

```json
{"input_quiescent": true, "active_playback": "stopped"}
```

`already_inactive` is also accepted. This is not a proposed service RPC schema.
The adapter must validate that the real acknowledgement applies to the requested
service session/attempt, prevents late launch, and cannot stop newer playback.
Unknown/refused/stale-session responses must raise `ExecutorError`; a local HTTP
timeout or mere suspension is not acknowledgement. A restart cannot silently
transfer authority from an old attempt to unrelated current playback.

The workflow drains any bounded in-flight mutation under the same lock as manual
input, suspends service automation, and acknowledges cancellation only after the
owned attempt is inactive. Search-only cancellation uses suspension's operation
lock as the quiescence barrier. Device takeover also suspends the service when no
controller-owned attempt exists. A later authorized workflow resumes it. Physical
remotes and independent SDK clients remain external actors; do not run competing
automated clients alongside the controller.

## Deployment after the seam is connected

Keep the existing Prime Player service. Do not launch a second service, stop it,
or restart Prime as part of deploying the controller. Set:

```dotenv
CONTROLLER_MODE=teamarr
PLAYBACK_ADAPTER=prime-player
PRIME_PLAYER_SOCKET=/absolute/path/to/player.sock
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
the optional overlay and set `PRIME_PLAYER_SOCKET_DIRECTORY` to its parent directory:

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

Cancel outstanding real playback before changing from another real adapter;
startup rejects a change that would abandon its cleanup obligations. Simulator
deployments retain their plans/settings and clear old simulated observations.

## Acceptance on the target host

1. Run the read-only `python -m controller.prime_player.check`. Confirm the service
   session, actual capability availability, socket permissions and connected stop
   adapter. This command sends no search, play, suspend or device commands.
2. Initiate one known live event. Inspect its selection audit, live resolution,
   attempt identity and fresh current status. Confirm the TV plays that event.
3. Exercise multiple live results, a replay-only result and unresolved identity.
   Check semantic selection and abstention with the configured real model.
4. Take manual control during search and during playback; verify no late launch,
   correct scoped stop, and input readiness only after acknowledgement.
5. Reconnect/restart the controller with the service left running. Verify the
   original attempt is inspected without another search or launch.
6. Validate service restart, socket loss, paused/buffering/switched playback and
   stale evidence. No condition should fabricate event completion or stop a newer
   externally selected session.

Host tests exercise controlled boundaries. Actual live search/launch, matching
accuracy, cancellation, Docker permissions and sustained device monitoring still
require this acceptance run.
