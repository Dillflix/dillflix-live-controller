# Changelog

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
