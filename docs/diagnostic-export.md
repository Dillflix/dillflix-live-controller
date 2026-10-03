# Combined incident export

Use **Export diagnostics** in Playback monitoring. Controller 0.14.9 and Prime
Player 0.1.0a14 add persistent service evidence to the existing export. Both
services must be updated and restarted to begin collecting it. Earlier missing
logs cannot be reconstructed.

The JSON includes:

- Existing decisions, catalog/status observations, durable jobs, cancellation,
  ownership and attempt evidence, plus model prompts, transmitted schema, HTTP
  response bodies and errors retained in each playback workflow.
- `controller_logs.sources.rpc`: outgoing player request and returned response
  bodies, HTTP status, transport errors, cancellation of client waits, durations,
  and call IDs. Navigation/monitoring/cancellation include playback token and,
  when available, request ID. Session and attempt IDs remain in RPC payloads.
- `controller_logs.sources.runtime`: controller Python logs, Uvicorn errors,
  uncaught HTTP request tracebacks, and capture startup records.
- `prime_player.service_diagnostics.persistent.sources.rpc`: received player
  request bodies and returned response bodies, including errors, recorded on
  both sides of execution. `call_id` joins these to controller RPC records.
- The player's `runtime` source: service stdout/stderr, Python logs and exception
  tracebacks, launch/Frida records, decoded runtime observations including
  resolver callbacks. Evidence is recorded as observed; this adds no new
  interpretation of playback success or failure.
- The player's `resolver` source retains resolution events separately from noisy
  runtime observations. Rejected results include a bounded, redacted copy of the
  returned object, with accessor/depth/size omissions identified by the existing
  runtime serializer. This does not relax live-only or ownership checks.
- The player's `device` source: continuous ADB logcat (main/system/crash) and a
  read-only `dumpsys activity activities` snapshot every 30 seconds. Collection
  errors, subprocess exit and restart are retained. No input, log clearing,
  app restart, ownership change or navigation is performed by capture/export.

Health and diagnostic RPC collection are independent. If the diagnostic RPC
fails, `prime_player.persisted_logs` reads retained files through the existing
read-only socket-directory mount. This works while the player is stopped or its
socket is missing. The controller never needs the Docker socket or host journal
privileges. An older player may answer diagnostics without the new `persistent`
section; that section requires the updated service.

## Retention and completeness

Controller files live next to its database in `<database>.diagnostics/`; player
files live in `<socket directory>/diagnostics/`. The existing persistent volume
and socket-directory mount carry these across restarts. Each source retains
three files of at most 4 MiB, and export reads the newest 1 MiB per source.
Records are capped at 256 KiB; larger records contain a redacted prefix and
explicit truncation/size metadata, preserving correlation fields. Retention is
by bytes, not a promised number of minutes. High-volume device logs can age out
quickly. Export soon after failure.

The bounded 256-record writer queue avoids waiting on disk in playback and
Frida callback paths. Each written record is flushed to the OS. A process crash
can lose queued records; power loss can lose OS buffers. Export reports pending
and dropped record counts, writer/storage errors, per-source missing/read
errors and truncation. `capture_status` from disk is the last recorded writer
state, not proof the process is still alive. Rotating-file reads are best effort,
not an atomic cross-service snapshot; record timestamps/process IDs identify
boundaries. The diagnostic RPC itself is excluded from RPC recording to avoid
recursive exports.

Credentials in structured fields, configured secret values, bearer tokens and
common signed-URL query credentials are redacted before persistence. Manual
input text is omitted. Model evidence retains its existing independent size
limits and redaction. Export still contains titles, identifiers and device logs;
it is not an anonymized artifact. Unrelated host journal and Docker logs are not
included; application logs are captured directly.

## Validation

Host tests cover persistent rotation, restart reads, size caps, secret redaction,
queue overflow, disk failure isolation, raw RPC errors and cancelled waits,
offline web export, player request-before-execution capture, resolver evidence,
service tracebacks/output, and read-only device-state collection. Physical ADB
logcat behavior and the deployed shared group/mount require deployment
acceptance. Unix-socket integration tests could not run in the development
environment because socket creation is denied.

After updating, download an export and check both services' `capture_status`,
nonempty RPC/runtime/device sources and missing/error/truncation fields. Capture
one failing attempt, then export promptly. Use its token/attempt, call IDs and
UTC timestamps to follow selection, resolver response, cancellation and device
state without issuing another search solely for diagnosis.
