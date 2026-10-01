# Version 2 observation contract

## Identity and ordering

Every journal record and normal dump includes `schemaVersion=2`, `probeBuild=2.0.0`, `serviceInstanceId`, `connectionEpoch`, service creation time and connection start time. A new service object gets a fresh random UUID; it is not restored from disk. An OS listener reconnection increments `connectionEpoch`, ends the prior watches and creates new session observation lifetimes even if the Android token is equal.

Use `(serviceInstanceId, sessionInstanceId)` for a session observation lifetime. Also invalidate any controller continuity binding across a changed `connectionEpoch`. `sessionToken` remains the original token hash for diagnostics and legacy readers. It is neither a unique session ID nor a content ID. Actual `MediaSession.Token` equality is used as the lookup key, so equal hashes do not alias different tokens. A token removed and later re-added gets a new session ID. Multiple Prime sessions remain separate; do not assume the first is the intended player.

Callbacks, polling, watch changes and fresh dump acquisition run on one collector handler thread. `sequence` is allocated for each constructed observation before it is submitted to the writer. It resets only with a new service instance. Unchanged polls that are suppressed consume no sequence. Every delivered media callback that is serialized has its own record, even if its snapshot looks unchanged. Duplicate destruction after a watch already ended is counted in `lateCallbacks` and creates no second final snapshot. Other late callbacks are marked `late_callback` with historical payload identity.

Sequence checkpoints detect loss AFTER an observation has been accepted by this probe, including a final record that never reached disk. They cannot prove Android delivered every callback or detect an app change that reached neither callbacks nor polling. An abrupt process death can lose queued records before another dump reports them; the next service UUID invalidates continuity rather than claiming complete history.

## Checkpoints and writer health

`latestProducedSequence` is the highest allocated observation. `latestWrittenSequence` is the highest observation whose append and `FileDescriptor.sync()` completed. It is a high-water mark, not proof that all earlier sequences were written. Always inspect `droppedRecords`, `writeFailures`, `rotationFailures`, `oversizedRecords`, loss ranges and export coverage. Journal records include the writer-health view taken BEFORE that record is enqueued; a record cannot report its own completed write.

Fresh dumps report `checkpointBeforeSnapshot` and `checkpointAfterSnapshot` under the collector's ordering. Reconciliation during a dump may emit removal records between those checkpoints. Writer progress continues independently while a snapshot is acquired; each checkpoint includes its capture time. The returned `journalHealth` can be slightly later than the after-checkpoint. A dump is not a new journal event and does not promise all queued observations are already written.

The writer queue holds at most 64 immutable UTF-8 records, each no more than 256 KiB including its JSONL newline. An in-flight record is additional. Queue saturation drops the new record, consumes its sequence and reports the exact sequence/range with reason `queue_full`. A failed append or rotation consumes its sequence and reports `write_failure`. There is no retry of that observation because a partial write makes duplication ambiguous; subsequent records can proceed. A closed writer rejects new records with `writer_closed`.

Loss ranges are bounded to 64 recent ranges; `lossRangeDetailsEvicted` reports when older details were discarded. Cumulative loss/failure counters do not reset within the service lifetime. `lastWriteSucceeded` describes the latest attempt, while the historical counters preserve earlier losses. `oldestPendingAgeMs`, `inFlightSequence`, `writerAlive`, `initialized` and `writerClosing` distinguish slow/stalled writing from a quiet collector. All of these are available out of band in a dump without reading or locking the files.

`journalHealth.retention` describes `events.jsonl` and `events.previous.jsonl`. Each file retains up to 64 service-scoped contiguous sequence ranges; `rangesComplete=false` and `evictedRanges` expose incomplete range detail. Startup scanning attributes v2 records by UUID; legacy records count as `unattributedRecords` and invalid or torn records count as `invalidRecords`. The writer separates a trailing partial line before appending a new JSON line. Scan/write/rotation happen on the writer thread. Retention is a published metadata view and is not an atomic export cursor. File rotation normally occurs before an append would exceed 8 MiB; one older file is retained. Pre-existing oversized legacy files may exceed that limit until rotated away.

## Collection health

`listenerConnected` is the notification-listener lifecycle fact. `activeSessionsListenerRegistered` and each session's `registrationCallSucceeded` mean the relevant Android registration call returned without an exception. They do not prove future callback delivery; Android or a vendor may internally swallow a remote failure. `callbacksRegistered` requires the active-session listener and every current watch's callback registration to have succeeded. Failed registrations are retried on subsequent polls while connected.

`activeRegistration`, `callbackRegistration`, `sessionReads`, `poll` and `cleanup` have independent attempt/success/failure counts, original times and bounded last errors. `inProgress=true` distinguishes an unfinished operation from a completed failure. A successful on-demand dump does not clear a callback-registration failure. Historical errors remain available after recovery; use the latest operation state and timestamps for current readiness.

Polling is scheduled one second after the prior attempt finishes. Dumps expose attempt/success time, expected next attempt and overdue time using elapsed realtime. A healthy, unchanged snapshot produces a heartbeat approximately every 15 seconds instead of a full record every second. Ordinary callback records also reset that heartbeat interval. Poll attempts/successes continue advancing when snapshots are unchanged. Poll health in a journal record produced during an attempt can legitimately be `inProgress=true`; the completed result appears in the next dump/observation.

A successfully read empty session array is an observation of no matching active sessions. A failed enumeration has `sessions=null` and an explicit error; it does not remove existing watches or synthesize an empty array. Per-session getter/decoding failures set `readSucceeded=false`, with failed fields in `readErrors` or nested errors/counts. Serialization limits make `dataComplete=false`. `snapshot.complete` requires all returned sessions to have complete data. Incomplete acquisition does not become a successful poll.

If the collector does not answer a dump within two seconds, the service returns the last cached dump, marks `snapshotIsCached=true`, `dumpTimedOut=true` and `collectorStalledOrBusy=true`, and reports fresh writer health plus an `outOfBandCheckpoint`. The cached snapshot retains its original times; the request/response times do not freshen it. A completed dump task that failed is marked `dumpFailed=true` with `dumpError`. Do not use cached, failed, truncated or incomplete samples to assert current playback continuity.

## Session disappearance and history

When a watch ends, `reason=session_removed` includes these payload fields:

| Field | Meaning |
| --- | --- |
| `serviceInstanceId`, `sessionInstanceId`, `sessionToken` | Identity of the watch that ended; hash is diagnostic only. |
| `removalReason` | `active_list_removed`, `session_destroyed`, `listener_disconnected`, `listener_reconnected`, or `service_destroyed`. |
| `removalObservedAt` | Time the collector observed removal. |
| `lastKnownSnapshot` | A copy of the last successful per-session acquisition, or null if no acquisition succeeded. Its own completeness/serialization markers remain in force. |
| `historical` | Always true for this retained evidence. |
| `eventCompletionInferred` | Always false. |

The historical snapshot retains its original acquisition times and any empty/cleared metadata. A failed getter does not overwrite the last successful acquisition. A successful acquisition with explicit bounded-value omissions can be retained, but still carries `dataComplete=false`. Removal/destruction signals are deduplicated per watch. After a destruction callback, a still-listed destroyed token is suppressed until it disappears from the active list. Re-adding the same token after absence creates a new observation lifetime.

An ended watch can mean switching, permission/lifecycle changes or interruption. It does not mean the sporting event, video or aggregate broadcast completed. Do not copy historical identifiers into an empty or newly created current session.

## Timing and field provenance

`callbackReceivedAt` is captured when callback code begins on the collector handler, before serialization. It is not Binder arrival time, app mutation time or a video-frame time. It is null for non-callback observations. The removal payload's `removalObservedAt` is the destruction callback receive time when destruction caused the removal.

The top-level `wallTimeMs` and `elapsedRealtimeMs` remain observation-construction timestamps. The snapshot and each per-session row have `acquisitionStart` and `acquisitionEnd`, each containing wall and elapsed time. Getters are separate Binder calls, so `snapshotAtomic=false` remains true as a statement of non-atomicity. A callback payload and a later snapshot can legitimately disagree; retain both.

`lastChangeTimes` maps callback type to the latest callback receive time for that watch; repeated callbacks can report unchanged values. `lastObservedChangeAt` is when a successful acquired data representation last differed from the prior successful one. Neither field dates the original application mutation or frames. `updatedElapsedRealtimeMs` in playback state remains the application's published position-update time.

Elapsed realtime is the freshness clock within the same device boot. Wall-clock adjustments cannot freshen old snapshots. Still pair archive data with the recorder's boot/device identity when comparing separate process lifetimes and validate Fire OS suspend behavior on the TV.

## Serialization bounds

| Scope | Limit / behavior |
| --- | --- |
| String | 4,096 UTF-16 code units, also subject to the shared character budget; no split surrogate pair. A truncated string is an object with type, originalLength and prefix, not a plausible complete identifier. |
| Shared value budget | 2,048 visited values and 32,768 captured characters per per-session acquisition or callback payload. |
| Nesting | Depth 8. |
| Collection / Bundle / queue / custom actions | At most 64 entries, and may stop earlier at the shared budget. |
| Bundle keys | Keys over 256 code units are omitted with a serialization warning. Known identity fields are visited first. |
| Byte array | At most 4,096 bytes, base64 encoded with original size/truncated marker. |
| Bitmap | Type and dimensions; pixels omitted as in v1. |
| Unknown objects | Type and explicit text omission; no arbitrary app-defined `toString()` invocation. |
| Target sessions | At most 16, from an active-list enumeration of at most 128; exceeding the limit is an explicit acquisition error, not inferred removal. |
| Journal record / dump | 256 KiB. An oversized journal observation becomes a small `recordTruncated=true` diagnostic at its original sequence, with payload/snapshot unavailable and a counter increment. An oversized dump omits its snapshot explicitly. |
| Diagnostic detail | Bounded error messages, 32 serialization-reason entries, 64 recent loss ranges and 64 retained ranges per file. |

`serialization.complete`, omission count/reasons, read errors and `dataComplete` accompany values. Absence caused by a limit is not evidence that Prime cleared a field. Small normal values retain v1 shapes, empty strings, nulls and numeric types. Metadata Bundle extraction still follows AOSP's parcel layout; vendor decode errors are explicit. Limits bound the probe's recursive conversion and queued output. They cannot bound the latency or temporary allocation inside a vendor Binder getter or Android's initial parcel operation; dump timeout/stall reporting covers that remaining exposure.

## Consumer migration

1. Require the advertised schema's required identity/health fields. If a v2 field is missing or invalid, classify the sample as unavailable/incomplete; do not fall back to v1 hash identity.
2. Bind identities by service UUID, connection epoch and session instance. Keep content IDs separate from those observation identities.
3. Evaluate callback registration, last successful poll/read age and snapshot completeness separately from writer progress and history/export coverage. Current `PLAYING` does not cure missing history.
4. Compare journal sequences within the same UUID against produced/written checkpoints and the expected requested interval. The written high-water mark alone is insufficient. Retention limits and export races remain possible without cursor export.
5. Treat removal evidence as historical. Continue to require scoped event-completion evidence and visual validation under the controller's existing policy.

API references used while implementing the adapter: https://developer.android.com/reference/android/media/session/MediaController and https://developer.android.com/reference/android/media/session/MediaSession.Token .
