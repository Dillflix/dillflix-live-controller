# Changelog

## 0.14.14

- Stop automatically retrying events after search finds no matching feed. Preserve watch-plan entries; Play now or changed viewing options permit a new attempt.
- Let an in-progress automatic fallback attempt finish when a failed higher-priority candidate exits backoff. Manual selections, configuration changes, route changes and deadlines still take precedence.
- Preserve search coverage/warnings in no-match diagnostics.

## 0.14.13

- Match upcoming/unavailable Prime event tiles and use API 9 tile lock evidence to distinguish waiting feeds from locked alternatives.
- Persist 60-second search retries without playback failure counts; retain selection, deadlines, cancellation and restart protections.
- Skip locked matching feeds until Play now or changed viewing options, retaining watch-plan entries.
- Require Prime Player 0.1.0a20 / API 9 for search-to-play workflows. Add Linux host CI.

## 0.14.11

- Honor the five-minute Prime evidence lifetime in the coordinator's refresh path as well as result acceptance. A remaining 15-second expiry check could still start recovery early.
- Keep paused, buffering and temporarily unknown playback unverified while holding the original attempt through the remaining evidence window. Repeated observations do not extend that window. Resumed verified playback preserves the attempt and viewing timers.
- Require a recently completed status check after recovery becomes due; queue it on the existing worker and wait for an in-flight check without cancelling it. Explicit Play now, manual takeover and route changes retain their existing precedence.
- Report the actual monitoring failure or player state in recovery activity instead of labelling every loss as expired evidence.

## 0.14.10

- Allow one minute for Prime Player's playback-status check instead of truncating its multi-query native progression check at 10 seconds.
- Expire previously verified Prime playback evidence five minutes after its observation. Newly accepted native evidence must still be fresh; nonplaying, failed, mismatched and unavailable checks immediately withdraw verification. Simulator timing is unchanged.

## 0.14.9

- Persist correlated Prime Player RPC request/response bodies, transport failures and controller runtime logs with bounded retention and redaction.
- Extend the web diagnostic export with player runtime/resolver evidence and read-only device logs, including shared-directory fallback when the player is offline. Export reports missing sources, writer failures, dropped records and truncation. Requires Prime Player 0.1.0a14 for new player/device capture.

## 0.14.8

- Add College Football (`college-football`) to schedule discovery and readable event, priority and team-preference labels. New installations discover it by default; existing discovery choices are preserved and the league can be enabled in Settings.
- Use Teamarr's existing ESPN college schedule and team identities with its new default Prime Video route. Live matching, startup verification and event lifecycle rules are unchanged.

## 0.14.7

- Persist model request bodies (including prompts, options and transmitted schema), response bodies, HTTP status, timestamps and outcomes with each playback token. Diagnostic exports include this evidence on success, HTTP rejection, malformed response, timeout and cancellation.
- Retain up to three calls per token under existing job retention, with explicit body truncation flags and credential redaction. Requests are recorded before the call; interrupted process records remain pending. No model or player requests are made by export.

## 0.14.6

- Omit `maxLength` from the transmitted strict JSON schema to avoid the reproduced model-backend grammar initialization failure. Keep `json_schema`, `strict: true`, all other schema constraints, and existing local length/selection validation.

## 0.14.5

- An expired nonterminal copy of Teamarr feed status no longer overrides the current catalog when an event becomes selected/tracked. This removes a selection-dependent eligibility flip that can repeatedly stage and supersede queued jobs. Catalog timestamps remain unchanged; expired catalog evidence stays unknown and terminal observations remain retained.
- This does not repair missing Prime search content IDs or model-provider request rejection.

## 0.14.4

- Retain bounded model-provider JSON rejection details (message, parameter, code and type) in playback errors and diagnostic exports, redacting the configured API key and bearer/key patterns. Do not retain arbitrary error pages or extra response fields.
- No model request, selection, playback or retry-policy changes. The provider rejection must be diagnosed from its actual response.

## 0.14.3

- Accept Prime Player's verified launch directly; current playback inspection remains monitoring, not an extra startup gate.
- Allow cancellation barriers through failed runtime health and blocked ownership, while still requiring service acknowledgement. Uncertain stop is never replayed.
- Search for scheduled Prime events once their start time arrives, retaining the provider's scheduled status and requiring live-only result matching and verified live playback.
- Include stored feed status and independent lifecycle evidence in diagnostic exports.
- Validation: 297 controller tests, including the real Prime Player Unix-socket service against a simulated runtime. Physical-device acceptance remains pending.


## 0.14.0 — Prime Player execution replaces the native screenshot executor

- Use Prime Player API 4 / 0.1.0a5 for search, live launch, attempt-bound monitoring, cancellation and scoped stop. Preserve controller scheduling, durable tokens, opaque Teamarr IDs and all permitted source routes.
- Match playable live result labels deterministically or with a bounded text LLM, supporting abstention and retained selection evidence. Structured competitor metadata in Prime is not required.
- Share one asynchronous RPC transport and service ownership contract with manual input. Interrupt remote search out of band, reject late results and stale owners, and never replay an uncertain Play or Stop.
- Report input quiescence separately from native stop confirmation. Player stopped/switched/ended cannot complete a sports event.
- Remove the screenshot navigator, native accessibility and MediaSession collectors, bundled APK/Java sources, vision prompts, obsolete dependencies/settings, fixtures and setup instructions. Screen mirroring and the manual remote remain supported.
- Preserve database history and restore fences. Existing `prime-video` configuration must migrate to `prime-player`; cancel outstanding old work before upgrading. Update socket/model settings using `docs/prime-player.md`.
- Controller and actual API 4 host-service contract tests use controlled runtime boundaries. Physical-device, live model and deployment acceptance remain pending.

## 0.13.1 — Prime Player manual ownership

- Add opt-in API 4 service handoff before manual input, including wake and text.
- Drain in-flight writes and reject stale session release; no direct-ADB fallback when configured.
- Reject coexistence with the legacy mutation executor on the same gateway. Automated planner integration is unchanged.
- Simulated handoff tests pass; target-device acceptance remains pending.

## 0.13.0 — Continuous DAZN tennis coverage

- Consume Teamarr's DAZN Canadian day/session/court broadcast listings through Prime Video. Keep titles, artwork, source identities, and unknown ends intact; individual ATP/WTA match discovery is unchanged.
- Add Tennis filtering and the DAZN tennis priority source, plus a dedicated demo scenario and phone browser test.
- Preserve upstream status acquisition time across feed cache hits. A match's Final signal cannot finish the encompassing broadcast; failures and elapsed estimates preserve reservations.
- No database migration. Update Teamarr first. The real public schedule was checked; installed deployment and Fire TV playback validation remain outstanding.

## 0.12.0 — Configurable leagues and expanded Prime Video coverage

- Add Settings controls for NFL, NHL, MLB, NBA, CFL, UEFA Champions League and Formula 1 discovery. Selection persists across restarts and supports configuration export/import and undo.
- Send selected leagues explicitly to Teamarr; keep saved commitments and current playback discoverable even when their league is disabled. Filter discovery per device, preserve cursor-only pagination, and reject responses for a selection changed in flight.
- Show readable Champions League and Formula 1 names. Consume Teamarr's individual racing sessions without inventing home/away teams or inferring live/completed status from the clock.
- Upgrade existing databases to schema 8 with all seven leagues initially selected. Existing plans, settings, manual completions and playback records are retained. Viewing routes stay in Teamarr; this deployment accesses DAZN, Sportsnet and TSN exclusively through Prime Video.

## 0.11.1 — Manual event completion

- Add **Mark event finished** to current playback and event details, with durable undo, command idempotency, and revision checks.
- Preserve watch plans, configuration, and automation mode; clear only matching controller playback intent and observations, fence late results, and let active automation choose another live event. The command sends no physical TV stop command.
- Keep completion decisions separate from Teamarr snapshots and asynchronous status evidence, including across refreshes, restarts, and retention.
- Schema 7 protects manual completions and upgrades both the 0.8.1 deployment hotfix and the 0.9–0.11 executor schema without losing existing state. Real playback remains opt-in.

## 0.11.0 — Probe v2 integration and strict runtime continuity

- Bundle the supplied probe v2.0.0 APK/source, schema and host tests. Verify its signature matches the previous supplied APK and the source rebuild matches DEX/compiled manifest; keep the supplied signer-compatible binary packaged.
- Migrate runtime identity to boot/service UUID/connection epoch/session-instance/runtime-media IDs. Treat token hashes as diagnostics. Keep the legacy unversioned reader separate; invalid advertised v2 never falls back to it.
- Validate original acquisition intervals, current registration/poll/read health, writer status, produced/written checkpoints, retention and complete exported sequence intervals. Check bounded journal exports between dumps, with one bounded follow-up for a missed newly written checkpoint. Cached/truncated/incomplete data, pending writes, gaps, conflicting duplicates and failure/loss increments withdraw prior continuity.
- Keep callback identity/state separate from later snapshots. Retain historical session-removal evidence with original times; never use it as current playback or event completion. Fresh visual live evidence is required to associate again after a gap; historical losses remain visible.
- Expose optional v2 diagnostics through token status and the setup CLI. Retain cheap stable monitoring, explicit installation/permission changes, signing-conflict preservation and unchanged Play/status/Cancel operations. No database migration.

Validation: **387 controller tests**, **19 supplied JVM fault tests** and **12 supplied checker tests** pass. Changed controller Python lint, APK signature/checksum, equal rebuilt payloads and all nine OpenAPI examples pass. The Python wheel builds and includes the exact supplied v2 APK and migrated adapter. Target-TV upgrade, callbacks, permission/suspend behavior, model accuracy and Docker deployment remain untested here.

## 0.10.1 — Supplied probe packaging and continuity safeguards

- Bundle the subsequently supplied original MediaSession APK, Java and manifest with source/artifact/signer hashes. Retain the original signed APK; the adapted pinned build produces matching bytecode and compiled manifest with a separate development signature.
- Add `python -m controller.executor.probe verify-apk|check|install`. Verify the artifact before installation, require an explicit listener-permission flag, request rebind and bound readiness waiting. Preserve existing app data/journals on signing conflicts; startup and Play do not install or grant permissions.
- Scope the source's process-local session hash to probe PID/start ticks and device boot. Reject restarts during acquisition and withdraw continuity on reconnect/disconnect/error/destruction callbacks, including unchanged composite snapshots.
- Read bounded tails from both rotating journals instead of allowing two roughly 8 MiB files to exceed ADB output limits. Detected sequence gaps invalidate the old association. Original records/dumps still lack the instance/checkpoint fields needed to prove complete history.
- Add optional `runtime.probe_instance` and `runtime.history_gap` diagnostics; synchronize the API schema/examples. No new endpoints or migration.
- Document source-backed priorities for explicit service/session IDs, sequence checkpoints, read/write health, final historical session evidence, timing and bounded record/export work. These probe modifications are proposed; the bundled Java/APK remains unchanged.

Validation: **344 backend tests pass**; changed Python lint, APK checksum verification and all nine API examples pass. Original signature and equal rebuilt code/manifest payloads are verified. The Python wheel builds and contains the exact supplied APK. Target-TV installation, process-identity access, permission/restart behavior, model accuracy and Docker deployment were not exercised here.

## 0.10.0 — Runtime grounding and hybrid navigation

- Port capture 05's complete multiline event framing, independent stdout/stderr buffers and pending-record screenshot safeguards. Keep original channel/timing/window conformance. Latch unexpected listener failures and confirm owned remote-process cleanup instead of spawning repeated UIAutomator listeners.
- Navigate action menus from observed visible ordering and current input labels. No fixed Watch Live index, item count or wrap rule. Fresh visual context validates Select; one short result Select opens the menu and does not establish playback proof. Keyboard/suggestion and non-live action labels can reject contradictory model activations.
- Read the exploration's installed MediaSession probe and rotating journals. Associate visual event/route identity with boot/session/runtime IDs; monitor stable playback without per-poll screenshots or inference. Keep callback payloads separate from composite snapshots, runtime IDs separate from Teamarr IDs, and visual timestamps separate from transport timestamps.
- Expose optional runtime diagnostics through the existing token getter. Detect buffering, interrupted continuity, source loss and identity changes without inferring event completion. Remove MediaSession position increments as rendered-video/live-edge proof; live-lag remains unmeasured. The fallback requires visible elapsed-timer progression.
- Document the new evidence, decisions and gaps. The original probe source/APK/installer is absent from the archives; the adapter consumes an already-installed service. No new public endpoint, database migration or default activation of real playback.

- Wake the device when the Take control remote connects or reconnects, using a wake-only key before controls become ready. Wake respects session ownership, expiry and the playback cancellation/input gate; screen viewing alone remains passive.

Validation: **329 backend tests pass**, including original collector conformance, all 103 recovered capture-05 accessibility records, varied menu layouts, media callback/identity semantics, input-gate races and runtime monitoring. Changed Python lint passes; all nine API examples validate against synchronized models. No TV/model endpoint, probe installer, Docker deployment or autonomous reliability trial was exercised here.

## 0.9.1 — Native accessibility collector

- Port the supplied exploration's native focus collector to asynchronous Python: separate input/accessibility channels, device-time action cutoffs, monotonic freshness, delayed/duplicate/unlabeled/cleared event handling, and version-scoped Prime virtual-node window bursts.
- Keep a bounded UTF-8 event reader with reconnect invalidation, per-connection app-version discovery and cancellation-safe owned-process cleanup. Unknown versions do not receive the known-version window exception.
- Bind native evidence around screenshot capture and finalization. Recheck revisions after observation and under the shared input gate; recapture after a change instead of sending the stale action. Manual input, including the remote's initial wake, invalidates native evidence. Failed/stale Select does not establish activation history.
- Supply source-qualified native metadata to JSON and TVTheseus actors while keeping the goal-blind visual observer separate. Expose native channels, validity, window provenance and post-inference validity in read-only diagnostics. No API or database migration is required.
- Correct the earlier completeness claim and inventory available search/focus evidence. Add a reproducible reference corpus generated from the unchanged JavaScript collector, plus subprocess, capture, inference and input-race checks.

Validation: **290 backend tests pass**, including all **316 original-collector snapshots across 54 scenarios**. Python lint passes. These validate the port and controlled integration; no new target-TV capture, autonomous search-to-play trial, Docker build or inference-accuracy evaluation has been performed. The existing visual activation and playback criteria still need evaluation against the full real path.

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
