# Prime Player workflow integration

## Status

Controller 0.14.18 requires Prime Player **API 11 / 0.1.0a26**, including the `broadcasts` capability, for non-navigational catalogue search and explicit launch-refusal evidence. Search, matching, Play, current status, native completion, cancellation, scoped stop and manual ownership are connected. The old screenshot executor and native probe are removed.

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

1. When a planned event becomes eligible (including scheduled start), the controller
   searches through the authenticated Find data path **before changing playback
   intent**. It does not navigate, cancel the current attempt or stop playback.
2. The matcher reads `containers[].items`: content ID/type, full title, synopsis,
   entitlement, native event state and exact feed start/end timestamps. Only EVENT
   entries with a GTI and title qualify. Replay/highlight/recap variants and explicit
   conflicting local dates are excluded; feed start need not equal kickoff.
3. A unique exact event title or matchup using source opponent aliases can be
   selected deterministically. Duplicate GTIs are grouped; conflicting entitlement
   or event states become unknown. Otherwise the bounded text model selects only
   a supplied GTI and permitted route, with field-attributed evidence from the full title, synopsis or other supplied
   text. Evidence is checked against the named source field; entitlement/state
   alone cannot establish event identity.
4. Entitled LIVE matches are launch candidates. Entitled UPCOMING matches wait;
   UNENTITLED alternatives are skipped. Missing/conflicting state or entitlement
   remains unknown. ENDED feeds cannot authorize a new live launch.
5. Before declaring an entitled LIVE match ready, retrieve its `broadcasts` metadata
   through the non-navigational live-details endpoint. Parse the captured Broadcasts
   carousel and LIVE_EVENT_ITEM child IDs. Read ENTITLED_ICON/OFFER_ICON per child;
   missing or conflicting entitlement stays unknown. Prefer explicitly English
   or unlabeled feeds and skip other explicit language qualifiers, including French.
   Among usable live feeds, explicit English precedes unlabeled, then GTI breaks ties.
   An unlabeled title remains language unknown: an English synopsis or artwork
   locale does not establish audio language. Explicit route language requirements
   still need positive evidence. Channel/title constraints are rechecked against
   child title and entitlement messaging. No alternate row leaves the direct event
   ID in use; an invalid/unknown broadcast row never falls back to Prime's default.
6. After a ready result, the coordinator reevaluates the latest plan and ownership.
   Only then does it advance intent and create a playback request. The recent
   result is tied to device revision, prior intent, event snapshot, viewing options,
   service session and expiry. A changed plan or stale response cannot launch.
7. Play receives the selected **broadcast content ID** (or a direct event ID without alternatives), live mode and a persisted attempt UUID. The
   player performs fresh metadata resolution and invokes its live Watch Now result.
   Catalogue eligibility is not proof that launch succeeded; tile actions and
   renderer handles are not used by this controller.

The complete latest catalogue response, timings, matching audit and model calls
and broadcast selection audit are retained in `catalogue_probe` in the diagnostic export. The launch record
retains the consumed catalogue and probe ID alongside attempt/status evidence.
Persistent RPC diagnostics retain subsequent responses. Native state strings and
backend cache behavior will be observed in production; no transition capture is
required to enable this path. Research hooks remain outside production payloads.

Coverage is `find_initial_response`. Pagination is not followed. A complete response with only explicitly disallowed
languages is skipped; absent eligible choices in a paginated response stay unknown.
Prime did not provide a separate audio-language field in the October 5 capture. A no-match decision does not establish catalogue-wide
absence. Full artwork, provider logos, overlays, messaging and native metadata
remain available from the player; the controller uses the fields needed to match.

Event identity can still match deterministically, but final broadcast selection now
requires `PRIME_PLAYER_MATCH_MODEL`, including direct and single-broadcast events.
The event-identity prompt is versioned with the candidate contract: it
explains feed windows, structured source identities and explicit route constraints.
Both catalogue preflight and playback use it for the same identity task; readiness
and launch decisions remain in code. Each model request receives one readiness
group, with entitled LIVE candidates considered first. Explicit no-match is
distinct from uncertain identity: unrelated entitled results do not hide a known
unentitled target, while uncertain alternatives prevent a false denial. The model may choose a stable content-ID tie
between positively identified equivalent feeds; uncertain identities still abstain.
A general catalogue-association API that needs all matching variants would require
a multi-match output, rather than reusing this single-feed selection contract.

## Persistence and monitoring

The Activity panel shows the latest playback evidence timestamp separately from
the latest executor contact. Routine successful checks refresh these values
without creating activity entries. Event lifecycle can remain unknown while
Prime playback is freshly verified; the planner distinguishes that condition
from expired event status. Launch deadlines apply only to pending requests.

The mapping is Teamarr content ID → controller token → Prime service session →
Prime attempt → requested/resolved GTI. Persist the attempt ID before dispatch.
Lost Play responses and controller restarts use read-only inspection of that same
attempt; they never resend an uncertain Play. Unfinished catalogue probes are
discarded on restart and the current plan is reevaluated. API 9 tile-access
observations are cleared once during the API 11 controller upgrade.

The launch outcome must establish `live_watch_now` and verified playback. Current
status must independently match the saved service/attempt/IDs and report fresh
playing progression. Native sample age is subtracted from the checkpoint time;
polling never manufactures fresh evidence. Historical Playing is not current
Playing. Service restart or contradictory identity withdraws verification. A
subsequent scheduled retry cannot pass an unresolved cancellation obligation.

`PRIME_PLAYER_STATUS_TIMEOUT_SECONDS` defaults to **60**. A status observation
collects two serialized native identity/position
samples and state checkpoints; the old 10-second controller budget could expire
after the first sample while the event continued playing.

Newly returned samples must still be younger than 15 seconds (and pass the
player's own freshness checks). Previously verified evidence has a separate
bounded lifetime: **five minutes** from the original observation timestamp.
The 5-second monitoring interval is unchanged. An unknown,
failed, nonplaying or mismatched check withdraws verification immediately; expiry
does not renew itself when no successful check arrives.

Existing `.env` files can retain the old explicit value. Change
`PRIME_PLAYER_STATUS_TIMEOUT_SECONDS=10` to `60`, then rebuild/recreate the
controller. Changing only the request timeout leaves the old 15-second evidence
expiry problem in earlier controller versions. For catalogue search, upgrade the
Prime Player service to API 11 before upgrading the controller.

Paused, buffering and temporarily unknown results immediately remove the playing
claim, but automatic recovery waits through the remaining five-minute window from
the last verified observation. Repeated unsuccessful checks do not extend it.
Matching resumed playback restores verification on the original request without
changing viewing timers. Explicit stopped/error/switched results keep the ordinary
recovery grace; none of these results establishes sporting-event completion.

Before automatically reopening the same event, the coordinator requires a recently
completed read-only check after recovery became due. It queues that check on the
existing worker and lets an in-flight monitor finish. A resumed result cancels the
reopen; a continued nonplaying/unverified result allows ordinary recovery if the
event is still live and its route remains permitted. Explicit Play now and route
changes bypass this same-event guard; manual ownership still gates all automation.

Fresh native `ended` completes the selected event only for the owning, previously
verified live attempt: service session, attempt, requested/resolved IDs and native
current content must match, with `matches_attempt=true`, `is_playing=false` and no
error. Superseded/cancelled work, paused automation and manual ownership cannot
publish completion. The operation becomes `completed`, lifecycle source becomes
`prime_player`, and scheduling advances on its next cycle without a playback
failure. The watch-plan entry remains. Completion survives restart, expiry and
late Teamarr live responses. This uses the existing read-only monitoring call;
it requires no extra navigation or player upgrade.

Paused, buffering, stopped, switched, unknown and unconfirmed ended states remain
recovery conditions. Teamarr and explicit manual completion also remain lifecycle
sources. Live mode means the service
resolved Watch Now; current status is not a fresh measurement of delay behind live
or HDMI/rendered video quality.

## Cancellation, stop and ownership

Every Search and Play includes a current acknowledged **automatic ownership envelope**. The controller never resumes a manual owner on its own. The web gateway obtains a manual receipt using `suspend(true, owner_id, previous)` and forwards wake/keys/text with strictly increasing sequence numbers. Release acknowledges automatic ownership before resuming the planner. There is no additional public authority API.

Cancellation first persists the controller's intent fence, then interrupts in-flight remote work through the service's out-of-band cancellation lane. It does not wait for a long search HTTP response before asking the runtime to cancel. Only an acknowledged input barrier permits the next controller or manual input operation.

When a verified attempt is being replaced, cancellation fences its work but does
not stop playback in advance; the new launch replaces it. A confirmed refusal
with `evidence.launch.disposition=not_invoked` waits for a fresh catalogue check
and is not treated as missing subscription. Uncertain delivery remains tied to
the saved attempt and is never relabelled as a pre-navigation refusal.

For an explicit cancellation of an owned launch, the controller calls `stop(session_id, attempt_id, ownership)` after cancellation. It records dispatch **before** sending, so a lost response or restart cannot replay a physical stop. Uncertain delivery is followed by a fresh cancellation barrier and recorded as unknown. Service restart, manual ownership and superseded attempts never authorize stopping unrelated playback.

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
or observer model is required. Catalogue search defaults to 45 seconds
(configurable 5–120); current status defaults to 60 seconds (5–60). Catalogue
checks have a bounded search/matching budget; an authorized launch gets its own
durable deadline. Existing explicit search budgets up to 120 remain supported.

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

## Scheduled feed readiness

The controller checks the eligible event on demand and refreshes identified
UPCOMING or uncertain matches after 60 seconds. Current playback continues while
these checks run; neither playback intent nor the existing executor token changes.
Only one check runs at a time, serialized with native status collection. Waiting
is not a playback failure and preserves the watch plan. Pause, manual takeover,
configuration/intent changes and revised event routes invalidate pending evidence.

`feeds_locked` is retained as a durable/API outcome name for compatibility; it now
means matching catalogue entries were explicitly UNENTITLED, not that artwork
showed a lock. The planner considers another permitted event while retaining the
original plan entry. **Play now** clears the exclusion; changed viewing options
also permit a new check. No provider-wide subscription assumption is stored.

A catalogue ENDED state prevents a new launch for that feed. It does not mark the
sporting event completed or stop an already playing event. Existing native
completion and Teamarr lifecycle handling retain that responsibility.

### No matching feed versus a feed that has not started

A Teamarr live event with a configured Prime league route qualifies for a search,
not proof that Prime carries that event. When that search cannot identify a
matching feed, `no_matching_feed` suppresses automatic retries for the unchanged
viewing options. The watch-plan entry remains. **Play now** clears the result,
and changed viewing options allow another search. Incomplete search coverage
remains recorded; this is a scheduling decision, not a claim of catalog-wide
absence or missing subscription. Identified upcoming/unknown matches and ambiguous matching alternatives enter
the refresh path.

An automatic candidate recovering from a transient failure cannot cancel the
fallback attempt that was started during its backoff merely because the retry
time arrives. This protection lasts only for that bounded navigation attempt
with unchanged intent, configuration and routes. Manual selections and newly
eligible events are still reevaluated.


## Language selection acceptance

The October 5 capture returned eight Lions�Panthers broadcasts in 1.15 seconds.
The first DAZN feed was explicitly French; the second was unlabeled and identified
by the user as English. Both were entitled and LIVE. Six other choices carried
OFFER_ICON. The controller selects `amzn1.dv.gti.a0f3772b-99fe-4b62-a454-a37d04a0b359`
in the captured regression fixture. Earlier resolution captures confirm that
explicit child GTIs resolve to themselves; the parent default resolved to French.
The endpoint was exercised on-device without requesting navigation or playback.
Automatic controller selection and launch still need deployment acceptance.
Existing verified playback is not forcibly switched when this update starts.

## Broadcast selection (0.16.5)

A dedicated `broadcast_selection` prompt uses the existing text model and transport.
It interprets language, provider and presentation semantically; there is no regex
fallback. It prefers English, permits genuinely unlabeled coverage, and rejects
explicit other languages. Missing required-route evidence or conflicting language
labels cause abstention. The language of a synopsis is not audio-language evidence.

The input contains compact event identity/competitor context, matched parent ID/title,
permitted route constraints, completeness, and each child's ID, title, synopsis,
subscription messages, structured entitlement/state and controller-assigned readiness.
Artwork, analytics and transport metadata are omitted. Code supplies selectable IDs
for one readiness group at a time (live entitled first), validates the selected ID
and exact evidence quotes, and preserves all normal ownership/intent/generation fences.
The model cannot override availability. Single broadcasts and direct events are also
checked; model absence/failure never falls back to playing the parent.

The Broadcasts row's carousel type is ignored because it describes presentation.
Item identity, structure and completeness are still checked, and related-event rows
are not treated as alternate broadcasts. Parsing and model failures are retained
through parent scanning and reported separately from unknown entitlement. Model calls
and decisions use the existing bounded diagnostic capture. No extra navigation or
new Prime Player API is required. Live model/device acceptance is separate from the
scripted-response regression suite.
