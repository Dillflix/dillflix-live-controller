# Proposed improvements to the supplied probe

**Status: implemented in the supplied probe v2.0.0; controller migration ships in 0.11.0.** The text below preserves the original proposal and rationale. Current behavior, signing verification, setup, acceptance coverage and remaining device checks are in [README.md](README.md), [SCHEMA.md](SCHEMA.md) and [validation.json](validation.json). Cursor-based export remains deferred. The bundled Java/manifest and APK match the supplied v2 release.

The MediaSession probe complements the native accessibility collector. It cannot supply the event name inside a search card if Prime does not expose that name through this API. Focus timing, input/accessibility channel separation, Prime window bursts and screenshot association remain the existing collector's responsibility.

## Recommended first batch

| Priority | Source finding | Proposed change | Practical benefit |
| --- | --- | --- | --- |
| 1 | `sequence` restarts at zero, old journals survive, and dumps omit sequence | Add `schemaVersion`, probe build identity, a new `serviceInstanceId` on each service creation, and sequence checkpoints to every journal record and dump | Attribute records to the right service lifetime; detect an omitted callback even when the next snapshot looks identical |
| 1 | `sessionToken` is a 32-bit token hash | Assign a `sessionInstanceId` to each watched Android token using token equality; retain the hash only as a diagnostic | Avoid interpreting hash reuse/collisions as continuous playback; scope identity to `(serviceInstanceId, sessionInstanceId)` |
| 1 | `connected` is set before registration succeeds; journal failures only reach logcat | Publish explicit registration, session-read, poll and journal-write health with last success/error times and counters | Distinguish an idle player from a broken observer and a fresh dump from healthy history collection |
| 2 | Removing a watch drops it without a final record; destruction payload is null and its snapshot may already omit the session | Emit session removal/destruction with its last known metadata/state and the time that data was actually read | Preserve evidence around switching and disappearance without inventing current state or event completion |
| 2 | Callback payloads and separately queried snapshots can describe different instants | Add callback-received time, snapshot start/end times, read errors and last-change times; retain the original payload separately | Associate evidence with screenshots more precisely and diagnose transitional disagreements |

### Identity and sequence semantics

Use a random service UUID created in `onCreate`, not restored from disk. A counter scoped to that UUID is sufficient for session IDs. Maintain a map keyed by the actual `MediaSession.Token`, not its hash. A removed and later re-added token gets a new session observation lifetime. Each record also needs the service-instance identity; putting it only in the current dump cannot identify older journal records.

Allocate an event sequence when the observation is accepted, before journal writing. Include `latestProducedSequence`, `latestWrittenSequence`, retained sequence ranges and dropped/write-failure counters in dumps. Keep produced and written checkpoints distinct: a queued or failed write must not look like a successfully exported event. Include instance IDs in those ranges because one retained file may span service lifetimes. Define checkpoint capture under the same ordering discipline as callbacks; a dump must say which observations its snapshot/checkpoints bracket.

With these fields the controller can explicitly classify a sample as current but history incomplete, request fresh visual validation, and explain the reason. It must not blindly accept the next sequence after a gap. The current PID/start-time check and observed-tail sequence comparisons are useful interim safeguards, but cannot fully attribute old files or detect a missing final callback with no subsequent journal record.

### Health and timing semantics

Keep `listenerConnected` as one fact. Add at least `callbacksRegistered`, `lastSuccessfulSessionReadElapsedMs`, `lastPollAttemptElapsedMs`, `lastPollSuccessElapsedMs`, `lastJournalWriteElapsedMs`, write/rotation failure counters and a bounded last-error record. An empty successfully read session list is healthy. A failed read is unknown, not an empty list. A dump's on-demand successful read must not silently clear a failed callback-registration state.

The existing one-second poll emits only changed snapshots. Expose poll-attempt/success times in every dump so unchanged media can still be distinguished from a stalled poll loop. A sparse heartbeat in the journal can help offline analysis later; it is not necessary to write an unchanged full snapshot every second. Measure polling lag using device elapsed time and validate its suspend behavior on the actual Fire TV.

Snapshots remain non-atomic even after adding timestamps: separate getter calls can straddle a transition. Keep callback values and their receive times intact; do not replace a payload with the later snapshot or use current dump time to freshen old fields. A callback receive time is also not a proven video-frame time.

### Session removal semantics

Store the last successful snapshot per watched session. On removal, include `sessionInstanceId`, reason (`active_list_removed` or `session_destroyed`), removal-observed time, `lastKnownSnapshot` and that snapshot's original acquisition times. Mark it historical. Ensure both signals arriving for the same watch have defined ordering/deduplication behavior. If the service dies without a removal callback, its changed instance identity still invalidates continuity.

Removal means the observed session is gone. It does not mean the sporting event or aggregate broadcast ended. The controller must continue using scoped completion evidence and its existing recovery policy.

## Reliability hardening after those fields

1. **Bound record construction and disk work.** Serialization currently limits nesting depth but not collection sizes, string lengths, byte arrays or total record bytes. Disk append/rotation occurs synchronously inside `emit`, normally on the callback handler. Add explicit per-field/record limits with omitted/truncated markers, then an ordered bounded writer queue. Record queue saturation, dropped sequences and write failures in out-of-band health; never silently discard callbacks. Avoid moving mutable Android payload objects to another thread without first capturing an immutable observation. Establish a single order for callbacks, poll snapshots and dump checkpoints.
2. **Make export rotation-aware.** The controller currently reads bounded tails of two files; rotation during export can omit/duplicate records. A bounded dump/export operation that reports instance-scoped retained ranges and accepts an event cursor could remove this ambiguity and reduce transfer/parsing costs. This is an optional transport optimization after sequence/health correctness. Keep it on the existing ADB/service-dump path; no HTTP listener, new network permission or public controller API is needed.
3. **Keep a compact diagnostic mode.** Record schema/build, Android/Fire OS version and observed Prime version when the service starts or the package changes. Preserve raw field provenance and explicit read/decoding failures. Keep expensive queue/artwork/large extras capture bounded and configurable if actual measurements show a cost. Do not drop identifier/state fields merely to make logs smaller.

## Acceptance evidence for a modified probe

| Trial | Required outcome |
| --- | --- |
| Restart/rebind while Prime remains PLAYING | New service identity or explicit reconnect epoch; prior visual binding cannot survive silently |
| Reuse/collision of token hash in a controlled fixture | Distinct session lifetimes; hash is never the lookup key |
| Drop a journal record; fail append/rotation; overflow the writer queue | Dump exposes the exact incomplete range or explicit loss; current PLAYING cannot hide missing history |
| Poll unchanged media for a sustained interval | Fresh health with bounded journal growth; silence does not trigger a fabricated playback failure |
| Revoke/restore listener access; fail session getter/registration | Readiness distinguishes listener, callback, read and export failures; empty sessions do not masquerade as errors or vice versa |
| Remove/destroy a session, including both callback orders | Historical final evidence is retained with original times; no automatic event-ended conclusion |
| Callback during snapshot/export, oversized extras, two Prime sessions | Defined ordering/bounds; conflicts and incomplete observations remain explicit |
| Device suspend/resume, reconnect and wall-clock change | Device-time freshness and epoch behavior are measured; UTC adjustments cannot freshen stale data |
| Real search → menu → Watch Live → player → pause/seek/ad/end | Correlate native labels, screenshots, probe records and controller decisions; document what each source actually contributes |

Controlled tests should cover identity, ordering, bounds and faults before rollout. The target-TV trials are necessary to validate vendor behavior and practical usefulness. The existing APK/source rebuild has been validated locally; these proposed probe changes have not been implemented or tested on a TV.

## Delivery and signing

Implement this as a versioned extension to the bundled source contract, retaining the current adapter for the original schema while migration is underway. For a new schema, require the new identity/health fields before treating it as continuity-capable; do not silently fall back to hash-based identity when advertised fields are missing. Increase APK version metadata and preserve reproducible source/payload hashes.

The original signing key was not included. Keep that key private with its owner if in-place updates are desired. A different signer requires an explicit installation migration; export existing journals before any chosen reinstall. The current installer deliberately preserves the old installation on a certificate mismatch. See [build and installation](README.md).
