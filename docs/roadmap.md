# Agreed delivery roadmap

Build and validate the controller and web interface before connecting Fire TV navigation. Playback remains live-only. Teamarr supplies opaque content identities and original event/team data; the executor receives every allowed viewing option. Manual commitments survive delays, missing catalog entries, estimated ends, and temporary failures. Authentication stays behind nginx.

## 1. Interface, contracts, and planner — delivered foundation

Responsive events, ordered manual watch plan, overlap preview, priorities and team rankings, settings, activity, and separate desired/observed playback. Manual order breaks overlaps while preserving each entry's remaining live windows. The server owns selection and conflict previews.

## 2. Persistent application and Teamarr — delivered foundation

Independent repository and deployment, real API and SQLite persistence, revisioned/idempotent commands, paginated Teamarr feed ingestion, retained team directory, durable undo, configuration export/import, and simulated playback/status. Initial startup and feed connectivity have been confirmed by user testing. Broader household-data validation continues.

## 3. Controller recovery before devices — in progress

Version 0.3 completes the playback recovery increment:

- Separate persistent playback simulator and coordinator, with durable intent/jobs, lease checks, request inspection, and idempotent submission.
- Lost-acknowledgement and restart recovery, intent fencing, stale-result rejection, durable cancellation, and navigation deadlines.
- Strict simulated live/content/option/freshness verification, observation expiry/recovery, retry, and live fallback without deleting commitments.
- Request progress and failure explanations in the existing interface; timeout/replay scenarios and desktop/mobile regression coverage.

Version 0.4 completes the independent content-status increment:

- Separate asynchronous lookup contract and worker for desired, observed, and reserved content, including inactive discovery entries and paused automation.
- Durable observation timestamps/expiry, bounded lookup timeouts/retries, lease checks, and rejection of late, older, malformed, or expired results.
- Separate content-status health, event-level evidence details, and outage/outside-feed scenarios, with restart and mobile regression coverage.
- Explicit distinction between source observation time and feed receipt time. The Teamarr-mode simulator cannot fetch new out-of-window facts; real authoritative lookup remains milestone 4.

Version 0.5 completes the coverage and prolonged-outage increment:

- Same-event handoff on source withdrawal or playback-locator change, updated full payloads/all permitted options, stale-route fencing, and preserved viewing timers/manual commitments.
- Durable observation grace and increasing recovery delays; fresh playback can survive unknown lifecycle status, but unknown status never permits a new playback request. Confirmed fallback or waiting follows loss of both kinds of evidence.
- Persisted service-outage probes, navigation suspension while unreachable, restart continuity, and reconciliation of the latest manual choice on reconnect.
- Recovery explanations, request purpose/current coverage, coverage-change and disconnect/reconnect demo controls, and desktop/mobile regression coverage.

Version 0.6 completes the local operations increment:

- Consistent online SQLite snapshots, integrity/schema/reference checks, and offline restore with a rollback snapshot, process exclusion, advanced revision/intent, and automation paused.
- Automatic bounded historical job/receipt retention and aged inactive-catalog cleanup, preserving watch-plan/undo references, current playback, pending work, and cancellation/intent fences.
- A maintenance-health endpoint and documented Docker/local operations. No database schema change or required environment edit.
- A reusable accelerated recovery runner. The 30-day exercise passed 2,940 ticks, 60 restarts, five restores, 15 coverage handoffs, and 30 recovery requests.

Remaining milestone-3 work is target-host validation: Docker build and disposable restore trial, longer real-time observation with household data, and nginx/physical-mobile checks. See [operations.md](operations.md). The local environment cannot perform those host checks.

Do not treat a successful local simulator run as real playback verification or as completion of all milestone-3 work.

## Live screen addition — implemented in 0.7

The requested screen view is available before autonomous navigation: an independent ADB/scrcpy capture bridge, native browser video panel, phone layout, fullscreen, shared viewers, and automatic reconnect/cleanup. It is opt-in and makes no event-selection or playback-verification claims. Local tests use a subprocess/TCP ADB double and actual H.264 browser decoding. Validate capture on the target Fire TV, protected streaming apps, authenticated nginx, and physical mobile browsers using [the setup guide](screen-mirroring.md). This addition does not complete or replace the remaining host checks or deferred executor work.

## Manual device control — implemented in 0.8

Take control suspends automation for a chosen duration, including four-hour events and up to 24 hours. It provides a mobile remote, focused keyboard shortcuts, text entry, explicit browser takeover, extension, and resume/stay-paused actions. Ownership and deadlines survive restarts; expiry and input are fenced on the server. Input uses a separate control-only scrcpy connection; capture stays shared. Actual Fire TV input compatibility, nginx, and physical mobile behavior need host validation. Version 0.14 routes manual input through Prime Player when its socket is configured.

## 4. Prime Player execution — implemented; target acceptance pending

Version 0.14 replaces the old screenshot/ADB executor and native probe with the external Prime Player service. The controller searches only after selecting a playback intent, matches eligible live result labels deterministically or with a text LLM, records an attempt before launch, and monitors that attempt without repeating playback on uncertain responses. Public operations remain Play, status by token and Cancel.

Ownership envelopes, out-of-band cancellation and attempt-scoped stop connect automation and manual input to one runtime authority. Native stop confirmation remains distinct from input quiescence. Controller 0.14.15 recognizes fresh, bound native Ended evidence from the owning live attempt as event completion; other nonplaying states remain recovery conditions. See [workflow and acceptance](prime-player.md).

Remaining work: target-host rollout, live matching accuracy, physical search/play/cancel/handoff, socket permissions, restart/outage checks, and endurance. Other streaming applications and independent unplayed-event results providers remain future integrations.

## Later iterations

Multiple devices, broader sports programming beyond live events, richer tournament/session filters, and natural-language rule/plan authoring through the same previewable commands. Keep the existing team/event/session/broadcast model suitable for golf, combat sports, international tournaments, and multi-sport competitions.
