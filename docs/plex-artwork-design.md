# Controller-owned Plex artwork integration

Status: implemented on the unmerged controller feature branch. Live Plex acceptance remains pending.

The implementation uses a before-commit reconciliation hook with a savepoint to cover
all controller write paths, including lifecycle-only changes. This replaces the
per-call-site hooks proposed below; post-commit notifications remain change-driven.
See [setup, delivered behavior and validation limits](plex-artwork.md).

Reviewed controller main at `e1d36dbb046601dd3b3a93cca14174e1107f758c`
(0.14.23), plus Plex's official PMS OpenAPI specification, version 1.2.3,
on October 5, 2026. This replaces the proposed standalone polling service.

## 1. Scope and decisions

The existing controller will maintain the artwork on a configured Plex library
item from its accepted live playback observations. The first binding is device
`living-room` to the existing item `8`, currently titled
`Dillflix Live - Stream 1`. The item ID is configurable and scoped to a verified
Plex server identity; neither the title nor ID alone identifies a global item.

Requirements:

- Configure the Plex connection, target item and default images in the web UI.
- Update event artwork when real live playback is accepted by the controller.
- Apply defaults after five minutes without a fresh accepted confirmation.
- Use the available matchup thumbnail for the poster; use the saved default
  background until a distinct event background source is available.
- Drive work from committed state changes and deadlines, without polling the
  controller's own HTTP API or adding a minute-based image-fetch loop.
- Isolate Plex failures and image downloads from playback, navigation, manual
  input, and scheduling.
- Restrict Plex mutations to artwork operations. Preserve media properties,
  including duration, bitrate, codecs, streams, resolution, and media paths.

The new `dillflix-live-nowplaying` repository is unnecessary for this design.
Do not change or delete that repository. No separate daemon or Compose service
is required.

## 2. Findings from the current code

| Area | Existing behavior | Design consequence |
| --- | --- | --- |
| `controller/coordinator.py:receive_playback_report` | Accepts a guarded `playing_verified` result into `device.observed` | Artwork follows accepted observations, not desired events, navigation, or raw player callbacks |
| `controller/recovery.py:refresh_playback_observation` | Renews, withdraws, and expires evidence; previous content can remain observed during a switch | Startup alone is an insufficient trigger; renewals and withdrawals matter too |
| `controller/now_playing.py:now_playing` | Checks identity, live evidence, age, manual ownership and event completion, then reads the retained source snapshot | Extract a transaction-aware internal projection; preserve its semantics without an HTTP round trip |
| `controller/now_playing.py:matchup_thumbnail` | Derives a game-thumbs thumbnail from structured artwork while preserving matchup identity and style parameters | Reuse this helper; do not infer teams from a title or rewrite arbitrary provider image URLs |
| `controller/manual_control.py` | Taking control clears observations; pausing automation alone can preserve playback | Manual handoff starts/continues the no-confirmation policy; automation pause alone does not clear artwork |
| `controller/content_status.py` and `prime_player/workflow.py:record_completion` | Completion can invalidate playback through content status independently of a device write | Lifecycle changes must participate in artwork reconciliation |
| `controller/service.py:replace_catalog` | Updates retained event snapshots | An artwork source change for the currently observed event is another trigger |
| `controller/service.py:start/stop` | Owns background-task startup and drained shutdown | Add a separate artwork task here; never perform Plex I/O under `playback_lock` |
| `controller/service.py:mutate` | Implements command receipts and optimistic revisions | Reuse those semantics for integration settings, with an integration-specific revision |
| `frontend/src/main.tsx` | Settings edits currently use rules/preferences; overview updates use SSE plus a browser refresh fallback | Add a dedicated Plex component and sanitized status to the existing view transport |
| `service.py`, `models.py`, diagnostics | Device payloads are exposed and configuration is exported; current redactors do not explicitly cover `X-Plex-Token` | Keep the credential out of device JSON, ordinary preferences, command results, and exports |
| `controller/ops.py` | SQLite backup is comprehensive; restore deliberately removes playback claims | Preserve defaults/configuration but invalidate artwork jobs and applied claims on restore |

The now-playing tests explicitly cover observed-versus-desired identity,
expired evidence, wrong requests/devices, completion, manual ownership,
missing feed entries, unknown lifecycle, and automation pause. They also prove
that a valid event such as RedZone can have no matchup thumbnail.

## 3. Internal architecture

Add a small integration package:

| Component | Responsibility |
| --- | --- |
| `controller/current_playback.py` | Transaction-aware read-only projection of currently accepted playback and its source snapshot |
| `controller/artwork.py` | Source resolution, including the existing matchup-thumbnail derivation |
| `controller/plex/client.py` | Narrow typed Plex reads and artwork setters using the existing async HTTP dependency |
| `controller/plex/state.py` | Configuration, asset records, desired artwork, per-slot application state, and deadlines |
| `controller/plex/coordinator.py` | Pure transactional reconciliation, generation changes and work eligibility |
| `controller/plex/worker.py` | Downloads, validation, artwork delivery, read-back verification, retries and shutdown |
| `controller/plex/api.py` | Settings, read-only connection/item checks, defaults and explicit retry actions |
| `frontend/src/PlexSettings.tsx` | Connection, binding, default artwork and integration status UI |

Use the existing SQLite database for durable intent. There is one coalesced
desired-artwork record per binding, not an unbounded history of every player
sample. A monotonically increasing generation identifies each material change
of target, desired assets, or enabled state. New timestamps for the same event
renew the deadline without requesting another upload.

State consists of:

- Connection: base URL, server machine identifier, server version, credential
  reference, connection revision and last read-only check result.
- Binding: device ID, connection ID, rating key, validated target identity,
  enabled flag, default poster/background asset IDs, binding revision.
- Artwork state: desired generation and sources, current content identity,
  last accepted confirmation time, fallback deadline, per-slot last verified
  image digest, in-flight attempt, retry deadline and sanitized error.
- Assets: content-addressed validated image bytes, media type and dimensions.
  Store persistent defaults in a dedicated SQLite BLOB table so existing
  database backups contain them. Bound image size, dimensions and retained
  versions; downloaded live images are a bounded, reproducible cache.

Keep a unique binding for each `(server machine identifier, rating key)` to
prevent two devices from competing over the same Plex item. Support one
connection and one device in the initial UI without erasing device scope from
the data model.

### Transactional changes and wake-ups

Call one idempotent reconciliation function with the caller's database
transaction after relevant domain mutations. Do not open a nested transaction.
It uses the shared projection, updates confirmation/deadline state, and persists
new desired work in the same transaction. It performs no network operations.

Integrate it at:

1. Accepted startup observations and subsequent accepted monitoring updates.
2. Withdrawal, expiry, invalid routes and player-recovery invalidation.
3. Manual handoff/takeover, completion commands, and cancellation paths that
   actually revoke an observation.
4. Accepted terminal content status, native completion and relevant feed changes.
5. Integration configuration/default changes, startup and database restore.

Audit these write paths explicitly. Do not put all behavior into a generic
`Database.save_device()` hook: content lifecycle and source changes do not
always call it. Do not attach to activity log strings: some important updates
do not emit a log, and logs are not a delivery contract.

After a successful commit, signal the worker using an event-loop-safe wake-up
(some mutations run in the playback thread). A wake-up is only a hint: durable
state is authoritative. Rollbacks publish no wake-up. Startup scans pending
state once, so a crash between commit and notification cannot lose work.

The worker sleeps until a state notification, the next fallback deadline, or
a retry deadline. It rechecks persisted state when waking. Existing player
monitoring and Teamarr refresh loops continue; they are not new artwork polls.

## 4. Playback and timing policy

Interpret “no confirmed playback for five minutes” literally: measure from the
most recent **new, accepted real observation's original timestamp**, not from
the last time an old observation was read. Do not add a second five-minute
grace after the controller's existing five-minute evidence retention expires.

Example: a positive sample at 12:00, followed by no positive samples, makes
defaults due at 12:05. A new accepted sample timestamped 12:04 moves that to
12:09. Re-reading the 12:00 sample at 12:04 does not move the deadline. A known
pause at 12:02 does not establish a new positive sample.

This is a proposed precise interpretation of the requirement, and differs
from “five continuous minutes after entering the unverified state.”

| Condition | Desired artwork |
| --- | --- |
| Event A remains verified while B is queued, searched, or navigating | A |
| B obtains accepted real live playback evidence | B immediately; cancel obsolete queued A/default work |
| A receives another accepted confirmation | Keep A; advance the fallback deadline without reuploading |
| Playback is temporarily unverified, paused, buffering, manually controlled, or completed | Hold the last artwork until the no-confirmation deadline, unless a new event is verified |
| Deadline arrives without a fresh confirmation | Saved default poster and background |
| Automation is paused while accepted playback continues | Continue current event artwork |
| Schedule disappears, expected end passes, or lifecycle becomes unknown | Follow playback evidence; these facts alone do not trigger defaults |
| Simulator/demo evidence | No production Plex writes, including fallback writes caused by entering demo mode |
| Integration disabled | No new writes; leave Plex's current artwork in place |

Use UTC wall-clock timestamps durably and a monotonic wait within the running
process. Do not use the accelerated demo clock. On ordinary restart, preserve
the last confirmation/deadline; startup's deliberate invalidation of old
playback must not restart an additional grace period or reassert stale content.
If enabled without any recorded real confirmation, begin the initial
five-minute interval at enable time. If a persisted deadline is overdue on
restart, reconcile it promptly against any newly accepted evidence.

Restoring a database is different from restarting: suspend the integration,
discard queued runtime generations and applied claims, and require re-enabling
after reviewing the restored connection/binding. Restored historical jobs
must never write old artwork to Plex.

## 5. Image sources and defaults

For the initial release:

| Plex slot | Playing event | No confirmed playback past deadline |
| --- | --- | --- |
| Poster (`thumb`) | Existing matchup-thumbnail helper | Saved default poster |
| Background (`art`) | Saved default background | Saved default background |

The supplied matchup thumbnail is landscape. Use it as requested without
silently cropping it or substituting the portrait `cover_url`. Show its actual
dimensions in the setup preview. A later explicit source option can choose a
portrait cover if desired.

Provide **Upload default poster**, **Upload default background**, and
**Use current Plex artwork as defaults**. Capturing existing artwork performs
authenticated GETs and saves image bytes, not a reference to Plex's changing
current-art URL. Validate both defaults before enabling automatic writes.
Do not recapture them automatically on restart or during normal updates.

Missing event artwork is distinct from missing playback. If a newly verified
event has no supported thumbnail, use its default slot rather than retaining
the previous event's image. If a new event's download fails, use the default
for that slot and retry the event image; report the degraded artwork state.
For a failed refresh of the same event, retain its last good image while retrying.
Neither case changes playback or restarts its confirmation timer.

When a real background source is added to the retained event snapshot, extend
the resolver to return it. Do not infer a background endpoint now. Removing
an event background later selects the default, so an old event's background
cannot persist behind a new poster.

Resolve sources when content changes, relevant catalog artwork changes,
settings/defaults change, or a user requests resynchronization. Compare image
bytes by SHA-256 before upload. Event identity is part of the source-cache key:
a stable URL reused for another event must be fetched again. With no image
polling, a silent change behind an unchanged URL needs a source revision/change
signal or an explicit resynchronization; that limitation should be documented.

## 6. Plex API boundary and media preservation

Plex's official specification documents direct artwork setters:

| Operation | HTTP request |
| --- | --- |
| Read server identity | `GET /identity` |
| Read target metadata and technical properties | `GET /library/metadata/{rating_key}` |
| Set the current poster | `POST /library/metadata/{rating_key}/thumb` with image bytes |
| Set the current background | `POST /library/metadata/{rating_key}/art` with image bytes |
| Verify/capture current images | GET the artwork URLs returned by fresh item metadata |

The official API's operation is `libraryMetadataPostElement`; the `url`
parameter is optional when image bytes are in the body. Python PlexAPI's
`uploadPoster()` and `uploadArt()` instead use the established plural
`/posters` and `/arts` upload routes. Prefer the official direct setters for
this implementation and validate them against the installed PMS version.
Do not quietly switch to metadata refresh if a setter fails.

Use two HTTP clients: an authenticated Plex client and a separate image-source
client. Send the token in `X-Plex-Token`, not an image URL/query. Validate image
bytes before uploading, with bounded byte count, dimensions, redirects and
timeouts. Keep Plex credentials on the configured Plex origin. Reject redirects
for authenticated operations; allow expected LAN image hosts without passing
Plex credentials to them.

The Plex client exposes only specific methods for the reads and two setters.
It does not expose a generic metadata editor or arbitrary write path. No scan,
refresh, analyze, playback, media-part, stream-selection, title, duration or
bitrate update operation is used. No direct Plex database or bundle writes.

Protection has three layers:

1. Contract tests assert the complete allowlist of outbound write methods and
   paths, the single configured item, and image-only bodies.
2. Each actual change reads metadata before and after delivery and compares a
   normalized fingerprint of stable media properties: item duration, Media
   duration/bitrate/codecs/resolution, Part identity/path/size/duration, and
   persisted stream properties. Session decisions, selected-stream flags and
   user playback progress are not stable technical-media fields. Include
   descriptive fields such as title and summary in the non-artwork guard too.
3. Before enabling on the real stream item, run a controlled acceptance test
   with retained before/after XML and, if database-column-level evidence is
   required, read-only database snapshots of the item/media/part/stream rows.
   Exercise both event and default artwork transitions.

An unexpected protected-field change suspends further artwork writes and
reports the difference. It must not attempt to repair duration or bitrate by
writing those fields. Independent Plex analysis can also cause a detected
difference; the check detects drift, not its cause.

Plex's API contract supports artwork-only operations, but its server owns
internal timestamps and cache bookkeeping. Do not promise that only two SQL
columns change, nor claim a read-back check prevents server-side changes that
have already occurred. Installed-server validation remains necessary.

Read-back also confirms that the current image is the one intended. Verify
content, not just a changed timestamp. If Plex transforms uploads, retain a
verified source-digest-to-served-image mapping rather than repeatedly assuming
the served bytes must equal the original download.

## 7. Delivery, failures and concurrency

Serialize writes per Plex item, independently of the playback lock. Before
each write and after every network wait, compare the saved generation,
connection/binding revisions, server identity and current desired state.
Discard superseded downloads and queued work. Do not write a second slot for
an old generation after a newer event has become desired.

Poster and background updates are separate operations; there is no atomic
two-image Plex transaction. Record their results independently and retry only
unconfirmed slots. A successful poster with a failed background is shown as a
partial update, not success for the pair. No rollback to a previous event.

Use finite connect/read/write and total operation budgets. Retry transient
network/server/image-source failures with bounded exponential backoff; a new
generation takes priority over old retries. Authentication failures, missing
items, changed server/target identity, or protected-media drift require a
settings correction or explicit retry instead of an endless upload loop.

A timeout after transmitting bytes has an uncertain outcome. Read back
before retrying; the server may have applied the image. Do not mark the digest
applied merely because the request was sent. Keep uncertainty durable across
restart.

Plex exposes no generation/CAS guard for artwork writes. An already transmitted
old write cannot be recalled when playback changes. Drain/resolve that work
before sending newer writes where possible, and reconcile the latest desired
images after uncertain outcomes. The contract is convergence and no knowingly
stale queued writes, not zero stale-image intervals or exactly-once delivery.
Client artwork display/cache refresh timing is also owned by Plex clients.

Disabling stops queued work immediately and reports any already transmitted
request as in flight until resolved. Saving another target does not retarget
an old request. **Restore defaults now** is a separate explicit action; disable
itself does not silently restore images.

Do not automatically remove old Plex artwork variants or call broad bundle
cleanup. Reuse applied content where possible; observe storage growth during
real-server acceptance before designing any narrowly scoped retention policy.

## 8. Settings, credentials and status

Add a **Plex artwork** panel with:

- Enable switch, initially off.
- Plex base URL and a write-only token input.
- Read-only connection test showing server name/identity/version and item access.
- Target item ID or Plex item link; resolve it and show its title/library and
  current artwork before saving. Initially configure `8` for this installation.
- Default-image uploads/capture with previews and validation.
- Fixed five-minute no-confirmation policy with the precise timing text above.
- **Resync now**, **Retry**, and **Restore defaults now** actions.
- Current desired event/default state, applied poster/background previews,
  last success, pending fallback time, partial failure and next retry.

The read-only test proves reachability and read access, not artwork write
permission. The first controlled image update tests write access. Do not make
"Test connection" mutate Plex or trigger analysis.

Proposed device-scoped API surface:

| Endpoint suffix under `/api/v1/devices/{id}/plex` | Purpose |
| --- | --- |
| `GET` / `PUT` | Sanitized settings/status and revisioned settings save |
| `POST /test` | Check draft connection and target without Plex writes |
| `POST /defaults/{poster\|background}` | Validate and store an uploaded default |
| `POST /defaults/capture` | Capture the target's current images as defaults |
| `POST /resync` | Recompute current desired artwork and force source revalidation |
| `POST /restore-defaults` | Explicitly queue defaults; report how automatic updates resume |

All durable mutations use command IDs and expected integration revisions;
runtime confirmations/retries do not change settings revisions. Keep draft
forms stable through overview/SSE refreshes. Show conflicts as reviewable 409s.
For **Restore defaults now**, disable automatic artwork synchronization in the
same configuration transaction, then queue a one-shot defaults operation; the
UI explains that enabling sync again resumes event-driven updates.

Store tokens in controller-owned mode-0600 credential files under persistent
storage, referenced by opaque ID from the integration tables. Stage the file
before committing its reference; clean orphaned/stale credentials only after
in-flight work releases them. A missing credential disables delivery. Do not
store tokens in browser localStorage, ordinary device configuration, edit
history, command receipts, image URLs, SSE, or diagnostics. A blank token on
edit preserves the existing one; removal is explicit. Update both diagnostic
redactors to handle Plex token names while preserving unrelated opaque player
attempt tokens.

Existing SQLite backups include bindings/defaults, but not external credential
files. Document that host-volume backup includes credentials and DB-only
restore on another host requires token re-entry. Ordinary configuration export
continues to contain planner settings only; do not unexpectedly export the
Plex connection or enable external writes on import.

Use existing nginx authentication and same-origin checks for secret-bearing
configuration actions. Serve image previews through bounded authenticated
controller routes so the browser never needs a Plex token.

## 9. Now-playing endpoint

Remove the public `/api/v1/devices/{id}/now-playing` route and its API-specific
documentation if it has no external consumer, as stated for this work. Extract
and retain its useful internal projection and artwork derivation first. Migrate
behavioral tests to that projection; remove only the route/OpenAPI tests.

Keep endpoint removal in a separate commit from the Plex integration so it is
reviewable and reversible. The integration never depends on the HTTP route,
whether that route remains temporarily or is later restored for a real client.

## 10. Implementation and acceptance plan

1. Extract the internal projection without changing its semantics; add a caller
   supplied transaction and time. Preserve observed-versus-desired and retained
   snapshot behavior.
2. Add versioned integration tables, credential handling, validated default
   assets and a narrow mocked Plex client. Disabled migration default.
3. Add transactional desired-state reconciliation, all mutation hooks, durable
   timers, worker delivery and lifecycle shutdown integration.
4. Add the settings component, preview routes and sanitized status/diagnostics.
5. Remove the unused now-playing HTTP adapter separately.
6. Run controller and browser suites, then a controlled Plex acceptance test
   before enabling production automatic writes.

Required integration tests include:

- A stays selected while B is pending; accepted B replaces A promptly.
- Late A downloads/completions and a fallback deadline racing B cannot authorize
  stale queued work.
- The five-minute boundary, repeated old samples, late timer execution, early
  withdrawal, recovery, automation pause, manual handoff and ordinary restart.
- No writes from simulator mode or disabled integration; restore suspends work.
- Missing/failed poster sources, absent background, removed background,
  unchanged bytes, a reused URL for a different event, and resynchronization.
- Plex downtime, invalid credentials, wrong server/item identity, HTTP timeouts
  after a write, partial two-slot success and persisted retries.
- No playback/manual-control blocking while Plex or an image host hangs.
- Write allowlist and preservation of protected metadata with fake and live
  server evidence clearly distinguished.
- Token redaction, settings revision conflicts, idempotent commands, upload
  validation, defaults persistence and missing-secret restore behavior.
- UI at existing desktop/mobile sizes, inaccessible or invalid connection,
  write-only token handling, default capture, partial status and retry controls.

Research validation completed: 68 existing tests passed and one module was
skipped when running now-playing, configuration, Prime monitoring and manual
control selections. The skipped Prime monitoring module requires the external
`dillflix_prime_player` package. No integration code, new integration tests,
Docker build, browser change or live Plex mutation has been performed.

## Sources

- [Reviewed controller revision](https://github.com/Dillflix/dillflix-live-controller/tree/e1d36dbb046601dd3b3a93cca14174e1107f758c)
- [Current playback projection and thumbnail derivation](https://github.com/Dillflix/dillflix-live-controller/blob/e1d36dbb046601dd3b3a93cca14174e1107f758c/controller/now_playing.py)
- [Coordinator acceptance](https://github.com/Dillflix/dillflix-live-controller/blob/e1d36dbb046601dd3b3a93cca14174e1107f758c/controller/coordinator.py)
- [Evidence renewal and withdrawal](https://github.com/Dillflix/dillflix-live-controller/blob/e1d36dbb046601dd3b3a93cca14174e1107f758c/controller/recovery.py)
- [Settings and service lifecycle](https://github.com/Dillflix/dillflix-live-controller/blob/e1d36dbb046601dd3b3a93cca14174e1107f758c/controller/service.py)
- [Plex official PMS API](https://developer.plex.tv/pms/): `libraryMetadataPostElement`, `libraryMetadataPutElement`, `libraryMetadataGetElement`, `getIdentity`
- [Python PlexAPI artwork upload implementations](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/mixins/resources.html)
- [Python PlexAPI artwork selection implementation](https://python-plexapi.readthedocs.io/en/latest/_modules/plexapi/media.html)
