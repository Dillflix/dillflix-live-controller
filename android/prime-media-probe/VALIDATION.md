# Validation of the 2.0.0 package

This is the supplied probe author's validation report. The controller adapter is now migrated in controller 0.11.0; see [the integration README](README.md). Both sets of host tests were rerun during integration. Device tests below remain outstanding.

## Completed locally

- All five Java source files compiled against the Android API 30 SDK with minimum API 22.
- Signed APK verification passed for v1, v2 and v3 signature schemes.
- Signing certificate SHA-256 matches the original supplied APK: `b718b48891fa342f4b2ae40f6371d47746df25cb8208e4b759519a2e2ec853e2`.
- Compiled package name is `dev.tvprobe.mediasession`; version code is 2 and version name is `2.0.0`.
- Manifest remains a debuggable notification-listener helper, with no network or accessibility permission and no playback-control component.
- 19 host JVM fault tests passed against the production `ProbeSupport` and `AsyncJournal` code.
- 12 Python readiness-checker tests passed.

The test output and final build/signature output are included under `validation/`. `BUILD-MANIFEST.json` records source, APK, DEX and compiled-manifest hashes and the input source-archive hash. The private signing key and tool caches are excluded.

## Host fault coverage

| Area | Cases exercised |
| --- | --- |
| Identity | Distinct tokens with identical hashes; equal-token reuse; remove/re-add creates a new lifetime; retained file ranges distinguish service UUIDs. |
| Historical evidence | Original acquisition time retained; copied snapshot survives later mutation; historical removal explicitly does not infer event completion; removal/destruction deduplication in both orders. |
| Collection health primitives | Registration failure is independent of successful reads; recovered operations retain failure history; in-progress differs from failed. |
| Bounds | Long strings, aggregate text/node limits, surrogate-pair boundary, oversize encoded record diagnostic, capped loss/range details, JSONL newline and Unicode round-trip. |
| Writer and disk | Blocked writer leaves producer/health responsive; queue overflow includes a missing final sequence; append failure followed by recovery; initialization retry; rotation failure; immutable queued bytes; ordered drain; closed-writer rejection. |
| Files | Rotation size/ranges; two-file retention; scan across service restart; legacy attribution; partial-line separation before the next append. |
| Dump checker | Healthy empty sessions; registration failure despite a fresh read; pending/failed/oversized writes; stale polling; success from an old connection epoch; timeout/cached result; incomplete reads; missing v2 fields; colliding diagnostic hashes with distinct IDs. |

These tests do not execute Android's Binder, notification-listener lifecycle, MediaMetadata parcel implementation, or real MediaController callbacks. Full Android serialization adapters and service wiring are compile-checked and reviewed, not device-tested. Host fault coverage must not be described as a successful Fire TV trial.

## Required Fire TV checks

Use a fresh recorder capture directory and keep the resulting raw dump, both journal files, screenshots and context diagnostics. Export existing evidence before an update or deliberate interruption.

1. **Normal path:** repeat search result → Select once → action menu → current `Watch Live` label → player. Confirm callback payloads, session snapshots and screenshots remain distinct evidence. Do not assume the menu order or item count.
2. **Unchanged playback:** take two dumps several seconds apart. Poll attempt/success timestamps should advance while media fields may remain equal; journal growth should be bounded by callbacks and approximately 15-second heartbeats.
3. **Pause/resume or another permitted player action:** verify that callback receive time precedes the accompanying snapshot interval and that emitted sequences progress even when a callback snapshot looks unchanged. Do not use position movement alone as proof of rendered frames or live-edge delay.
4. **Session loss/switch:** navigate out or switch content. Verify a historical `session_removed` record retains the prior acquisition time and that no completion inference is emitted. If metadata was explicitly cleared before removal, that cleared snapshot must stay cleared.
5. **Listener reconnect/service restart:** verify that a reconnect increments `connectionEpoch` and gives watches new session IDs, or a service recreation changes `serviceInstanceId`. Prior continuity bindings must not silently survive.
6. **Permission revoke/restore:** perform this only as an intentional device test. Preserve the command/result. Collection health must distinguish listener/registration/session-read failures and recover after authorization returns. A fresh read alone must not erase registration failure.
7. **Suspend/resume and wall clock:** compare elapsed-time freshness and poll-overdue data before and after suspend. Wall-time adjustments must not freshen a retained historical snapshot.
8. **Multiple sessions and large metadata:** use a controlled Android fixture if available. Verify explicit IDs, serialization markers, no falsely complete truncated sample, and bounded journal records. The host tests cover writer faults; vendor Binder latency and allocation remain device-level risks.
9. **Controller migration:** before enabling continuity decisions with schema v2, update the separate controller adapter to consume service/session IDs, epoch, incomplete/cached flags, registration/poll health, produced/written checkpoints and loss counters. Its current v1 hash-based reader is not upgraded by installing this APK.

Do not force a real TV's storage full to test journal failure. The host harness already injects append/rotation failures and writer saturation without risking device storage. Cursor export remains deferred; ordinary two-file copying can race rotation.
