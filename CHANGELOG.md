# Changelog

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
