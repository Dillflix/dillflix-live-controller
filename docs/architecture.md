# Architecture and delivery scope

The first milestone implements the web application and persistent controller independently of Fire TV navigation. Selection is deterministic. Device interaction will be delegated to an executor; the planner does not interpret screenshots or choose streaming apps.

## Responsibilities

| Module | Responsibility |
| --- | --- |
| `controller/teamarr.py` | Fetch a complete paginated Teamarr catalog and cached team rosters; validate provider identities |
| `controller/planner.py` | Project catalog cards, qualify viewing options, rank live candidates, and preview estimated manual windows |
| `controller/service.py` | Apply revisioned commands, retain commitments, reconcile desired playback, stage requests, and consume simulated observations |
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

The SQLite lease prevents a second coordinator from acting on the same device. Pending requests survive restart; the next owner can resume them after graceful lease release or expiry. This is tested for the simulator. Exactly-once external device effects are **not** established: the future executor must support idempotent request IDs, cancellation/fencing, and observation reconciliation.

Failure simulation has a short retry burst at 5 and 15 seconds, followed by 5-minute probes, while allowing another live event to fill the gap. A failed manual target remains reserved. Pause cancels pending simulated jobs and retains the plan and current observation; it pauses automation, not the TV itself.

Schema version 2 adds a team directory and edit history in a transaction. It preserves every version-1 record and rejects a newer, unsupported schema version. Undo stores scoped before/after snapshots with the originating command in the same transaction, retaining the latest 50 edits per device. It restores only the plan or configuration, never observed playback, automation mode, time, or job state. Current live eligibility is reevaluated normally. The request names the latest available edit and the current revision; a different edit or stale revision is rejected. Undo itself remains idempotent. A demo scenario reset clears that device's edit history.

Configuration transfer has its own version-1 document format, separate from database and feed schema versions. Import validates the complete document, previews changes and unresolved team IDs, then replaces rules, team rankings, and preferences in one undoable command. Plan and automation records are outside its scope. Export excludes connection credentials and playback state. Unknown team identities are retained to support imports before a directory has populated.

## Catalog and freshness

Raw Teamarr entries are preserved without reconstructing their nested event, session, broadcast, team, artwork, or extension fields. A new catalog becomes active only after every page succeeds. HTTP errors preserve the previous complete snapshot. Feed absence never creates a completion observation.

The current status simulator uses explicit fixture transitions in demo mode and copies Teamarr's status field in Teamarr mode. Teamarr data older than 120 seconds becomes unknown unless terminal. The feed read time is not claimed to be provider freshness. Broadcasts with unknown status remain unavailable to Play now, even inside their scheduled windows. Future unknown broadcasts still appear under Upcoming.

This release does not bypass Teamarr's upstream caching or fix upstream failures that appear as empty successes. A real content-status service will need authoritative observations, their timestamps and expiry, and independent lookup of pinned events that have left the discovery window.

Team-directory refresh runs in an independent task with its own lease and health record. It starts with NFL/NHL/MLB/NBA and refreshes hourly, also requesting up to 16 other leagues already seen in team data. A provider team ID, qualified by provider and league, joins directory rows to event teams. Teamarr's cache database row ID is never used for that identity. Teams encountered in feed snapshots are retained, and cache rows with absent city/nickname fields do not overwrite richer feed metadata. Each league refresh validates fully before writing; errors or empty rosters do not delete old teams or rankings. The directory's completeness depends on the Teamarr cache.

## UI transport

The browser fetches `/overview` and listens to SSE invalidation notices. It also polls every 10 seconds to recover from a lost notification. Update sequence numbers represent activity invalidations, not a complete event-sourcing stream; clients fetch a fresh snapshot after reconnecting. GET requests use a generation guard so older responses cannot overwrite newer state.

Mutations include command IDs and the observed configuration revision. The interface refreshes after saving and on a conflict; it does not silently merge another browser's plan. A successful mutation means the command was accepted, not that live playback was verified. Demo status is visible throughout the interface.

Open rule drafts, overlap reviews, and import previews retain the revision they were reviewed against, even if SSE refreshes the underlying overview. An overlap review offers Refresh preview; rule/import conflicts require reopening the review. The team selector uses the retained directory, and ranking controls preserve saved identities absent from it. Unranked teams share a tie-break value rather than acquiring implicit preferences from list order.

## Production work remaining

- Continue validating the actual household catalog, coverage routes, images, timezones, and proxy behavior after successful initial Teamarr testing.
- Extract authoritative content-status and playback executor adapters from the simulator boundary in `service.py`. Add request deadlines, live-edge and content verification, cancellation acknowledgement, periodic observations, and recovery after process/device outages.
- Define executor option handoff when one broadcast window ends and another valid route for the same event begins. Route changes must not manufacture event completion.
- Bound the grace period for stale observed playback once a real heartbeat source exists. The simulator currently keeps an existing unknown-status observation until a better manual choice or fresh lifecycle information arrives.
- Add independent refresh of pinned content and richer tournament/session/major filters.
- Add completed-plan cleanup, job and command retention, complete database backup/restore tooling, and longer soak testing before unattended operation.
- Implement multiple devices and explicit per-device executor ownership later. Natural-language actions can eventually translate into the same previewable API commands.
- Build and exercise the Docker image on the target host; test Safari, physical touch devices, and deployment restart behavior.

Application authentication is intentionally delegated to the existing nginx proxy. The initial deployment uses a single process and local SQLite on persistent storage. No Fire TV credentials, screenshots, or navigation commands are stored in this milestone.
