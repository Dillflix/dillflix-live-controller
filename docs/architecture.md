# Architecture and delivery scope

The first milestone implements the web application and persistent controller independently of Fire TV navigation. Selection is deterministic. Device interaction will be delegated to an executor; the planner does not interpret screenshots or choose streaming apps.

## Responsibilities

| Module | Responsibility |
| --- | --- |
| `controller/teamarr.py` | Fetch a complete paginated Teamarr catalog and cached team rosters; validate provider identities |
| `controller/planner.py` | Project catalog cards, qualify viewing options, rank live candidates, and preview estimated manual windows |
| `controller/service.py` | Apply revisioned commands, retain commitments, expose read models, and refresh the catalog/team directory |
| `controller/coordinator.py` | Stage playback intent, deliver/inspect requests, validate results, enforce deadlines, retry cancellation, and reconcile observations |
| `controller/playback.py` | Playback adapter protocol and a separate persistent simulator; no device I/O |
| `controller/content_status.py` | Independent lifecycle lookup contract, evidence validation/persistence, freshness projection, and refresh worker |
| `controller/database.py` | Versioned SQLite migrations, records, edit history, activity, command receipts, and leases |
| `controller/fixtures.py` | Explicit sample lifecycle transitions, independent of estimated end times |
| `controller/api.py` | Same-origin HTTP API, update notifications, and built frontend |
| `frontend/src` | React interface using server state rather than an independent browser watch plan |

## Selection rules

1. Candidates need live status and at least one permitted viewing option. A recent failed attempt temporarily defers that content. Inactive catalog entries remain candidates only when already observed or manually committed.
2. The first eligible watch-plan entry wins. Its rank only matters during overlap; all other entries remain reserved for their remaining live windows.
3. Otherwise, the first enabled matching rule wins. Conditions within a rule are ANDed. Within that tier, the best team rank wins, followed by the current event, scheduled start, and opaque content ID for stable ties.
4. Existing verified playback is retained if its content status becomes unknown, unless a higher manual choice is confirmed live. Editing a future reservation does not accidentally clear this protection.
5. Automatic changes respect minimum viewing time, cooldown, and same-tier switching. Manual choices bypass automatic dwell restrictions.
6. If no eligible live event exists, the controller waits. It never selects a replay as filler.

The phase matcher uses explicit provider season metadata. It does not guess playoffs from dates or title strings. Missing stage metadata is `unknown`; rules requiring a stage will not match it. Broadcast and session entries can have no teams. A team's ranking key is `provider:league:id`, preventing provider IDs shared by two leagues from colliding.

Expected viewing windows are estimates only. They cannot guarantee uninterrupted viewing of two overlapping live events. The interface makes the chosen overlap order explicit. Ended and cancelled entries stop competing but remain in the plan until removed; delays, postponements, unknown status, and failures preserve commitments.

## State and recovery

The device record separates configuration revision, manual plan, desired content, monotonically increasing intent version, and observed playback. A configuration edit increments the revision; ordinary status observations do not. A stale client gets HTTP 409 rather than overwriting another browser's changes.

Command receipts are persisted with a payload hash. Repeating the exact command returns its previous receipt even after the configuration revision changes. Reusing an ID with different content is rejected. A transaction saves desired intent and a pending job together. Before accepting a simulator result, the worker reevaluates selection and checks that the intent and content still match. Superseded requests cannot become observed playback.

The SQLite lease prevents a second coordinator from acting on the same device. Pending requests survive restart; the next owner can resume them after graceful lease release or expiry. The coordinator calls the playback adapter outside database transactions, inspects each request before delivery, and uses the original request ID and payload for uncertain retries. The simulator has its own durable jobs and per-device intent watermark, so a lost acknowledgement or a process exit between executor success and controller persistence can be reconciled. A current device observation is required to adopt an old success report.

Request acceptance and navigation are progress, not playback verification. Results must match request/device/content/intent identity; verified observations must also identify an allowed viewing option, healthy live presentation, and valid observation/expiry timestamps. The controller requires evidence younger than 15 seconds and honors an earlier reported expiry. It never updates observation timestamps itself. An observation outage retains the last identity as unverified and does not imply event completion. Fresh matching evidence can recover verification without opening a new request. Pausing does not turn expired evidence into verification.

Navigation has a persisted wall-clock deadline, normally 120 seconds from staging. Transport/inspection failures retry with the same request ID and bounded backoff without resetting that deadline. Expiry records a timed-out attempt, queues cancellation, and enables live fallback. The explicit demo timeout scenario uses three seconds. Pending schema-2 jobs receive a deadline on their first delivery check after upgrade.

Cancellation is a durable obligation until the adapter acknowledges it. The simulator remembers cancellation even if it precedes submission; old intent cannot replace newer intent. Cancelling old navigation does not stop already-playing or newer content. An approved switch is not reversed by the previous event's dwell timer while awaiting verification, but current eligibility and manual order are still reevaluated before accepting results.

These guarantees are tested against the local simulator. Exactly-once external device effects are **not** established. A real executor still needs transport timeouts, idempotent request IDs, cancellation/intent fencing, and device evidence. Current adapter calls are synchronous local operations; the coordinator deadline does not interrupt a blocked external call.

Failure simulation has a short retry burst at 5 and 15 seconds, followed by 5-minute probes, while allowing another live event to fill the gap. A failed manual target remains reserved. Pause cancels pending simulated jobs and retains the plan and current observation; it pauses automation, not the TV itself.

Schema version 2 adds a team directory and edit history; schema 3 adds job progress/deadlines/cancellation state and separate simulator tables; schema 4 adds content-status evidence, request IDs, health, and retry scheduling. Migrations run in a transaction, preserve earlier records, and reject a newer unsupported schema version. Existing simulated observations and their requests are adopted on upgrade; migrations never fabricate fresh content-status evidence. Undo stores scoped before/after snapshots with the originating command in the same transaction, retaining the latest 50 edits per device. It restores only the plan or configuration, never observed playback, automation mode, time, or job state. Current live eligibility is reevaluated normally. The request names the latest available edit and the current revision; a different edit or stale revision is rejected. Undo itself remains idempotent. A demo scenario reset clears that device's edit history.

Configuration transfer has its own version-1 document format, separate from database and feed schema versions. Import validates the complete document, previews changes and unresolved team IDs, then replaces rules, team rankings, and preferences in one undoable command. Plan and automation records are outside its scope. Export excludes connection credentials and playback state. Unknown team identities are retained to support imports before a directory has populated.

## Catalog and freshness

Raw Teamarr entries are preserved without reconstructing their nested event, session, broadcast, team, artwork, or extension fields. A new catalog becomes active only after every page succeeds. HTTP errors preserve the previous complete snapshot. Feed absence never creates a completion observation.

The content-status adapter is separate from discovery. A worker collects the union of desired, observed, and manually committed content across device records, regardless of each catalog entry's active flag. It refreshes due IDs on a separate cadence, including during pause, with a separate lease. Each pass limits work to 50 IDs and eight concurrent lookups, with a five-second timeout per lookup. Calls occur outside database transactions. Records persist the current request ID, accepted observation, last attempt/success, sanitized error, failure count, and next check time. Late results cannot overwrite newer requests or survive loss of the lease.

Results identify the content and request, source, timestamp basis, observation time, and expiry. A fresh unknown result is distinct from a failed or missing lookup. Failure preserves accepted evidence until expiry; it never manufactures completion. Unknown also cannot erase previously confirmed terminal evidence. New explicit evidence can correct a terminal state. Comparable observations from the same source/basis cannot go backwards in time or disagree at the same timestamp. Evidence from a new source preserves that source's own timestamp rather than comparing it to an unrelated feed receipt time.

The simulator uses explicit fixture transitions in demo mode and cached Teamarr status in Teamarr mode. The latter has `observed_at: null`, the original feed read time in `received_at`, and `timestamp_basis: feed_received`; provider freshness remains unknown. Polling cached evidence never changes its timestamp or extends expiry. The planner accepts nonterminal evidence for at most 120 seconds, or until an earlier source expiry, and then treats it as unknown. Explicit terminal facts remain retained through expiry/outages. Provider timestamps and declared expiry remain intact in the record; `effective_valid_until` exposes the controller's bounded freshness window. Untracked discovery cards use catalog evidence, with fresh independent evidence or retained terminal facts taking precedence when available.

Demo clock/scenario controls refresh the simulated tracked statuses before returning. A scenario reset clears those demo status records, invalidating in-flight results. Status refresh only changes lifecycle evidence, never the original catalog payload, manual plan, user revision, or observed playback. Broadcasts with unknown status remain unavailable to Play now, even inside scheduled windows. Future unknown broadcasts still appear under Upcoming.

This release does not bypass Teamarr's upstream caching or fix upstream failures that appear as empty successes. The independent lookup machinery is implemented, but the default Teamarr-mode adapter cannot obtain new out-of-window status. That requires a real authoritative status service behind the contract. Playback verification and event completion remain distinct sources of evidence.

Team-directory refresh runs in an independent task with its own lease and health record. It starts with NFL/NHL/MLB/NBA and refreshes hourly, also requesting up to 16 other leagues already seen in team data. A provider team ID, qualified by provider and league, joins directory rows to event teams. Teamarr's cache database row ID is never used for that identity. Teams encountered in feed snapshots are retained, and cache rows with absent city/nickname fields do not overwrite richer feed metadata. Each league refresh validates fully before writing; errors or empty rosters do not delete old teams or rankings. The directory's completeness depends on the Teamarr cache.

## UI transport

The browser fetches `/overview` and listens to SSE invalidation notices. It also polls every 10 seconds to recover from a lost notification. Update sequence numbers represent activity invalidations, not a complete event-sourcing stream; clients fetch a fresh snapshot after reconnecting. GET requests use a generation guard so older responses cannot overwrite newer state.

Mutations include command IDs and the observed configuration revision. The interface refreshes after saving and on a conflict; it does not silently merge another browser's plan. A successful mutation means the command was accepted, not that live playback was verified. Demo status is visible throughout the interface.

Open rule drafts, overlap reviews, and import previews retain the revision they were reviewed against, even if SSE refreshes the underlying overview. An overlap review offers Refresh preview; rule/import conflicts require reopening the review. The team selector uses the retained directory, and ranking controls preserve saved identities absent from it. Unranked teams share a tie-break value rather than acquiring implicit preferences from list order.

## Production work remaining

- Continue validating the actual household catalog, coverage routes, images, timezones, and proxy behavior after successful initial Teamarr testing.
- Connect real content-status and playback services after the remaining simulator recovery work. Both adapter boundaries are implemented; authoritative out-of-window event status and actual device evidence are still future work.
- Define executor option handoff when one broadcast window ends and another valid route for the same event begins. Route changes must not manufacture event completion.
- Define recovery policy for prolonged device/observation outages and verify it with a real heartbeat source. Playback evidence now expires, separately from event lifecycle freshness.
- Add richer tournament/session/major filters.
- Add completed-plan cleanup, job and command retention, complete database backup/restore tooling, and longer soak testing before unattended operation.
- Implement multiple devices and explicit per-device executor ownership later. Natural-language actions can eventually translate into the same previewable API commands.
- Build and exercise the Docker image on the target host; test Safari, physical touch devices, and deployment restart behavior.

Application authentication is intentionally delegated to the existing nginx proxy. The initial deployment uses a single process and local SQLite on persistent storage. No Fire TV credentials, screenshots, or navigation commands are stored in this milestone.
