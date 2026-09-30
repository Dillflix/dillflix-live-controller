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

Next work, in order:

1. Separate content-status observations from discovery polling. Define and simulate independent refresh of desired, observed, and manually committed content, including entries outside the feed window. Preserve provider timestamps and explicit expiry; an unknown/error response must not become completion.
2. Exercise coverage-option changes and prolonged status/device outages. Specify when to keep observing, retry, select another live event, or wait; avoid repeated switching and preserve commitments.
3. Complete unattended-operation checks: bounded job/receipt retention, full database backup/restore, longer simulation runs, and deployment/mobile checks on the user's host.

Do not treat a successful local simulator run as real playback verification or as completion of all milestone-3 work.

## 4. Connect real status and playback services — deferred

Choose the authoritative content-status service and executor transport. Implement real request timeouts, request inspection, cancellation/fencing, and device observations behind the tested boundaries. Verify the requested event and live presentation on the device, exercise process/device/network recovery, and run unattended trials before relying on continuous coverage.

## Later iterations

Multiple devices, broader sports programming beyond live events, richer tournament/session filters, and natural-language rule/plan authoring through the same previewable commands. Keep the existing team/event/session/broadcast model suitable for golf, combat sports, international tournaments, and multi-sport competitions.
