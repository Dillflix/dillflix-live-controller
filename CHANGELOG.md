# Changelog

## 0.9.0 — Integrated Prime Video executor

- Implement Play, status by token, and Cancel in the controller, with an opt-in `prime-video` adapter and service bearer authentication. Preserve the complete Teamarr snapshot and every permitted viewing option. Separate recovery, lifecycle, authority and Stop endpoints remain deferred.
- Persist tokens, request hashes, intent fences, cancellation and input journals in schema 6. Identical retries reuse tokens; uncertain keys are never replayed. Successful playback is re-observed on restart. Restore retains newer target-database executor history before fencing and cleanup.
- Add bounded asynchronous ADB/model transports, package-scoped Prime launch/search, structured team-pair queries, configurable JSON/TVTheseus actors, and independent goal-blind visual observation. Native prompt provenance and Apache license are retained.
- Require matching live content and advancing sampled playback position for verification. Confirm scoped visual completion with two readings; preserve Teamarr feed receipt timestamps and unknown status when evidence is absent. No schedule-based completion inference.
- Serialize physical input with manual control. Cancel drains input, stops owned active Prime playback and confirms inactivity before granting manual input. Old cancellation cannot stop a newer token. Pause leaves verified playback running.
- Add configuration/live-screen/saved-image diagnostics, implemented OpenAPI schemas/examples, deployment guide and updated architecture/contracts. UI labels reflect the selected adapter.
- Bind Compose to configurable `0.0.0.0:8790` by default for access from another PC; host-local nginx deployments can explicitly select loopback.

Validation: **207 backend tests and 27 Chromium browser tests pass**; frontend build and changed Python lint pass. OpenAPI and all nine examples validate against implemented models. All 100 supplied archive PNGs were processed: 97 usable and three rejected as blank/protected. These are controlled boundary and image-processing checks, not model-accuracy measurements. Real Fire TV/account/inference behavior, Docker/nginx, protected video, physical mobile browsers and unattended endurance still require target-host validation.

## Executor handoff preparation — before 0.9 integration

- Simplify the external API proposal to Play, token status including lifecycle, and Cancel including active stop. Defer separate token recovery, device observation, content-ID lookup, and authority endpoints.
- Run synchronous playback work in a serialized worker thread; drain in-flight work before shutdown relinquishes database ownership. Real adapters must impose finite HTTP timeouts.
- Require acknowledged device cancellation before manual input, persist failed handoffs through restart/release, and drain manual transport before automation resumes. Simulator device fences reject late requests and preserve newer playback.
- Update the engineer guide, three-operation OpenAPI draft, adapter mappings, implementation boundaries, and acceptance scenarios. Real executor HTTP and physical cancellation remain integration work.

Validation: 183 backend tests and 27 Chromium browser tests pass, including six new cancellation, concurrency, restart, and cleanup cases. The three-operation OpenAPI draft and all nine embedded examples validate.

## 0.8.0 — Temporary manual device control

- Take control with 5/15/30 minute and 1/2/4/8/12/24 hour choices, a visible deadline, reset/extend, resume, and stay-paused actions.
- Durable exclusive browser ownership, explicit takeover, retained watch plans, cancellation/intent fences, playback-claim invalidation, and restart/expiry recovery.
- Separate pinned scrcpy control channel for D-pad, Select, Back, Home, Menu, play/pause, delete, and printable ASCII text. Bounded taps, sequence/rate checks and no uncertain-input replay.
- Mobile remote layout and focused keyboard shortcuts; shared video remains view-only and independent.
- Schema 5 prevents older releases from ignoring ownership. Offline restore clears sessions. nginx WebSocket routing now includes `/control/input`.
- Automated backend/Chromium checks cover lifecycle, transport, ownership, four-hour selection, text, reconnect and phone layout. Real Fire TV, Docker/nginx and physical Safari remain host-validation work. Autonomous event playback is still simulated.


Validation: 177 backend tests and 27 Chromium browser tests pass, including real HTTP/WebSocket input transport and subprocess/TCP scrcpy framing doubles.

## 0.7.1

- Fixed screen reconnect loops caused by equal encoder timestamps and long forward timestamp gaps. Preserve every encoded frame, assign positive sample durations, and compress long gaps for live viewing; backward timestamps still trigger an explicit stream reset.
- Corrected JMuxer 2.1.4's keyframe cleanup index for variable-rate video using cumulative sample durations. Raised the emergency retention ceiling from 30 to 60 seconds so it no longer conflicts with the muxer's normal 30-second retention and ten-second cleanup cycle.
- Preserve reconnect backoff through short-lived playback, defer buffer teardown until the muxer's error callback returns, and display/log distinct packet, buffer, and decoder diagnostics.
- Added browser regressions for timestamp anomalies and uninterrupted variable-rate H.264 playback through buffer cleanup. No database, ADB capture, or deployment binding changes.

Validation: 153 backend tests and 23 Chromium browser tests pass, including 45 seconds of uninterrupted variable-rate H.264 playback with buffer cleanup.

Target Fire TV confirmation remains required; these locally reproduced failures are not proof of the particular hardware failure's cause.

## 0.7.0

- Added optional live device-screen mirroring through a separate view-only ADB/scrcpy bridge. The pinned server is verified by checksum, launched with unique session ownership, and stopped after the last viewer leaves. It sends no navigation/input commands and does not change playback evidence or watch-plan state.
- Added a responsive screen panel with inline muted H.264 video, fullscreen, automatic reconnect, visible connection states, remembered show/hide preference, and background-tab suspension. Browser media retention and stalled decoding are bounded.
- Share one capture among up to eight viewers; joining clients wait for a keyframe, and slow clients reconnect rather than retaining an unbounded backlog. WebSocket origins are checked, ADB targets remain server-configured, and input messages are rejected.
- Added opt-in capture environment settings, pinned server installer, Docker ADB support and persistent identity home, nginx WebSocket configuration, setup/contract documentation, and dependency licenses. Existing installations remain capture-disabled until configured. Schema remains 4.
- Used ws-scrcpy as an architectural reference; the implementation uses upstream scrcpy 3.3.4 and JMuxer 2.1.4 without requiring a separate ws-scrcpy deployment.

Validation: 153 backend tests and 20 Chromium browser tests pass. Coverage includes real Uvicorn WebSocket transport, ADB subprocess/TCP doubles, shared capture and cleanup, and actual H.264 decoding/reconnection on desktop and phone-sized layouts. The pinned server download and checksum were also verified locally.

Playback/navigation and independent content-status adapters remain simulated. Actual Fire TV capture, Docker deployment, protected-app video, nginx authentication, and physical mobile browsers require target-host testing. Protected video surfaces may appear black; mirroring cannot guarantee their visibility.

Upgrade with `git pull --ff-only` and `docker compose up -d --build`, preserving `.env` and the data volume. See [screen setup](docs/screen-mirroring.md) to enable the optional feed.

## 0.6.0

- Added consistent online SQLite backups, integrity/schema/JSON/reference verification, SHA-256 reporting, and atomic publication without overwriting existing backup files. Committed WAL data is included.
- Added guarded offline restore, including standard-input support for Docker. Restore verifies mode/schema, saves a rollback snapshot, preserves user data/history, clears runtime playback claims, advances revision/intent, and starts automation paused. Running controllers block replacement.
- Added startup/hourly history retention and a maintenance-health endpoint. Recent job/receipt limits and inactive-catalog age are configurable; watch plans, undo references, current playback, pending work, cancellation obligations, and simulator intent fences are preserved.
- Documented the retained command-receipt window: an original command retried after receipt cleanup receives stale-revision 409 without being reapplied. Old playback requests remain fenced after their payloads are removed.
- Added a reusable, isolated accelerated recovery runner and local/Docker operations guide. The 30-day run passed 2,940 ticks, 60 restarts, five restores, 15 handoffs, and 30 playback recoveries while pruning historical data.
- Kept database schema 4 and the existing web interface. The roadmap now separates completed local operations work from outstanding target-host deployment and real-time endurance checks.

Validation: 134 backend tests, 15 Chromium browser tests, and the 30-day accelerated simulator exercise pass. Docker, physical mobile devices, real-time endurance, and real status/playback services remain untested here; both adapters remain simulated.

Upgrade with `git pull --ff-only` and `docker compose up -d --build`. Keep the existing `.env` and persistent volume; no new environment settings are required. See [docs/operations.md](docs/operations.md) for backups, restore, retention, and target-host checks.

## 0.5.0

- Continued milestone 3 with same-event coverage handoff. A withdrawn option or changed playback locator triggers a new request containing the latest complete Teamarr snapshot and every currently permitted option. Late results from obsolete routes are fenced out.
- Preserve manual commitments and original viewing/cooldown timestamps across a successful handoff or same-event recovery. Option reordering, added alternatives, display changes, and estimated ends do not interrupt valid coverage.
- Added durable observation recovery grace and increasing retry delays, with reset after sustained healthy observations. Unknown content status cannot authorize reopening; missing lifecycle and playback evidence eventually allows confirmed live fallback or waiting without completing commitments.
- Added persisted playback-service health and probes with backoff, suspension of navigation while unreachable, restart continuity, and reevaluation of the latest manual intent on reconnect. Existing navigation deadlines remain unchanged.
- Added recovery explanations, service contact, request purpose, and observed coverage to the existing interface. Demo controls now include same-event coverage change, service outage, and explicit disconnect/reconnect.
- Prevented recovery grace for the previous event from reversing navigation already underway to a new selection.
- Database schema remains 4; new device fields initialize automatically. Updated API contracts, operational examples, and the roadmap. Real Fire TV and authoritative status adapters remain deferred.

Validation: 110 backend tests and 15 Chromium browser tests pass, including desktop and 390/320 px phone workflows. Docker and real status/playback services were not tested in this environment; both adapters remain simulated.

Upgrade with `git pull --ff-only` and `docker compose up -d --build`. Keep the existing `.env` and persistent volume. `PLAYBACK_RECOVERY_GRACE_SECONDS` defaults to 60, so no environment change is required. Refresh the browser after updating.

## 0.4.0

- Continued milestone 3 with an independent asynchronous content-status adapter and refresh worker. Desired playback, observed playback, and reservations remain tracked when entries leave discovery and while automation is paused.
- Added persistent status evidence and retry scheduling, bounded lookup timeouts/concurrency, lease checks, and rejection of mismatched, late, older, invalid, or expired responses.
- Preserve source observation/expiry timestamps. Missing or failed lookups keep usable evidence; expired nonterminal evidence becomes unknown without completing commitments. Confirmed terminal facts survive outages and can be corrected by newer explicit observations.
- Separate status health from schedule health in the API and Settings. Event details expose source/timing/expiry, lookup failures, and retained out-of-window entries. Added status-outage and outside-feed demo scenarios.
- Corrected Teamarr lifecycle timestamp semantics: `observed_at` is null when no provider timestamp exists; `received_at` carries the original feed read time. Rechecking cached evidence never extends its freshness window. A real authoritative out-of-window status source remains deferred.
- Added transactional schema-4 migration preserving earlier records, and updated contracts and the delivery roadmap.
- Fixed a dialog keyboard timing issue found during regression testing: focus and Escape handling are installed before the dialog becomes interactive.

Validation: 88 backend tests and 13 Chromium browser tests pass, including desktop and phone workflows. Docker and real status/playback services were not tested in this environment; both adapters remain simulated.

Upgrade with `git pull --ff-only` and `docker compose up -d --build`. Keep the existing `.env` and persistent volume. `STATUS_INTERVAL_SECONDS` defaults to 15, so no environment change is required. Refresh the browser after updating.

## 0.3.0

- Continued milestone 3 with a separate persistent playback simulator and coordinator. Request acceptance, navigation, and verified live playback are distinct states.
- Added inspection before delivery, recovery after lost acknowledgements or restart, same-request retries, per-device intent fencing, and durable cancellation including cancellation before submission.
- Added persisted navigation deadlines and bounded delivery retry. Timed-out or rejected requests retain manual commitments and permit live fallback.
- Validate playback content/device/request/intent, allowed option, live presentation, health, and observation freshness. Expired observations become unverified; fresh evidence can recover without another playback request.
- Added request progress, deadline, attempt count, and error details to the existing playback view, with navigation-timeout and replay-rejection demo scenarios.
- Fixed switches being reversed by the previous event's dwell timer while navigation was pending, and pause incorrectly marking an expired observation as verified.
- Added transactional schema-3 migration, preserving existing configuration/history/jobs and adopting previous simulated playback. Recorded the agreed delivery sequence and remaining work in `docs/roadmap.md`.

Validation: 63 backend tests and 11 Chromium browser tests pass, including desktop and phone workflows. Docker and real status/playback services were not tested in this environment; playback remains simulated.

Upgrade with `git pull --ff-only` and `docker compose up -d --build`. Keep the existing `.env` and persistent volume; migration runs on startup. `NAVIGATION_TIMEOUT_SECONDS` defaults to 120, so no environment change is required. Refresh the browser after updating.

## 0.2.0

- Added a persistent team directory backed by Teamarr's cached rosters and teams seen in events. NFL/NHL/MLB/NBA rosters refresh independently of the schedule; empty results or failures retain known teams and saved preferences.
- Replaced schedule-limited team ordering with searchable preferred-team controls, including move up/down, move first, and remove. Saved identities missing from the directory remain visible. Rules can use off-schedule teams and expose event/session/broadcast filtering.
- Added durable undo for watch-plan and configuration edits, including imports. Undo survives reloads/restarts and preserves current lifecycle and automation state.
- Added JSON configuration export, validation and import review, cross-mode/unresolved-team warnings, and undoable application. Watch plans and connection credentials are outside the transfer.
- Fixed open overlap reviews and rule drafts to retain their reviewed revision. Import reviews use the same protection; a newer browser edit cannot be silently overwritten after a background refresh.
- Added transactional database migration from schema 1 to 2, preserving existing state and pending jobs. Unsupported newer databases fail explicitly.
- Added API contract documentation and automated coverage for upgrade preservation, undo history, malformed/failed directory refreshes, configuration round trips, off-schedule team selection, and stale reviews.

Validation: 39 backend tests and 9 Chromium browser tests pass, including desktop and 390/320 px phone layouts. Docker builds and a live Teamarr roster fetch were not run in the development environment. Initial controller startup and feed connectivity were confirmed through user testing. Fire TV playback and authoritative status remain simulated.

Upgrade from the existing checkout with `git pull --ff-only` and `docker compose up -d --build`. Keep the existing `.env` and persistent volume; migration runs on startup. Refresh the browser after updating.

## 0.1.0

Initial independent controller: responsive events, watch plan, priorities, settings and activity; Teamarr feed ingestion; persistent SQLite state; live-only selection; and simulated playback/status adapters.
