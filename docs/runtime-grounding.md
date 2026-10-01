# Prime runtime grounding and hybrid execution

Implemented in 0.10.0, with probe v2.0.0 identity/health/checkpoint consumption added in controller 0.11.0. This describes controller behavior and its evidence boundaries, not a claim of autonomous TV reliability. Public operations remain **Play, status by token, and Cancel**.

## Evidence reviewed

The original exploration supplies keyboard/suggestion/search observations and the native collector's timing/channel/window policy. The later supplied `prime-path-capture-fixed (1).zip`, `search-to-play-05-analysis(1).zip` and runtime evidence report add one successful **manual** search-results → action-menu → live-player path on `PVFTV-321.0096-L (321009610)`. Search text was already entered. This is neither an autonomous run nor coverage of every lifecycle state.

Capture 05 contains 103 recovered accessibility events, including six records with a literal newline in `Watch Live\nEnglish Broadcast`. All content descriptions in that capture are null. Earlier exploration records include useful keyboard and suggestion descriptions. Original screenshots 19, 21, 39 and 40 were inspected for the result card, action sheet, player overlay and controls-hidden player respectively. The user confirmed that **one short Select** on the focused result opens the action sheet; the screen's “Hold select for more options” hint does not change that transition.

Replay fixtures retain original record values and source hashes in `tests/fixtures/prime-capture05-accessibility.json` and `prime-capture05-media.json`. They do not include the complete archives, device addresses, account parameters or screenshots. The latter is a selected callback/poll sample, not the entire media journal.

## What each source can establish

| Stage or fact | Useful runtime evidence | Controller use | Boundary |
| --- | --- | --- | --- |
| Keyboard focus | Current input-channel key label; earlier descriptions identify Search Keyboard | Reject a model's claim that Select would activate a visible sports result | A character label is not a query, result identity or complete keyboard map |
| Suggestions | Earlier `[Search Suggestions]` descriptions | Distinguish query suggestions from matching-looking event cards | Matching team text does not make a suggestion an event |
| Result navigation | Repeated `Top Sports` row labels and fresh input transitions | Supply context to the actor; reject stale input; do not declare a loop from repeated row text | Which event occupies the focused card requires pixels; predictable movement does not imply a fixed grid |
| Action sheet | Exact labels such as Watch Live, Resume, Rapid Recap and More details, including variant subtitles | Combine visually observed item order with label-confirmed, one-step controller navigation | Labels name the focused action, not all menu items, the sheet's event, its size or its ordering |
| Player identity | Visible event title, competitors/scoreboard, language and provider when exposed | Match the requested Teamarr event/route and associate it with the runtime media session | In capture 05, native player focus is unlabeled; MediaSession DISPLAY_TITLE is empty and its description title is generic PrimeVideo |
| Runtime continuity | Boot ID, service UUID, connection epoch, session-instance ID, agreeing runtime media identifiers, callbacks and checkpoints | Maintain the current visual association and expose playing/buffering/paused/etc. cheaply | V2 token hashes are diagnostic only; runtime IDs are not Teamarr content IDs; no ID-to-catalog resolver is assumed |
| Playback progress | Application-reported transport state; optional visible elapsed player timer | Report source-qualified PLAYING; use actual visible timer progression for the visual-only fallback | Media position may be extrapolated and has a different timebase from the programme; it is not rendered-frame or live-lag proof |
| Live presentation | Confirmed Watch Live dispatch; explicit playhead-at-live evidence for revalidation | Establish live-mode history and withdraw it on contradictory evidence | A LIVE availability badge alone does not locate the playhead; live-edge latency remains unmeasured |
| Completion | Explicit, scoped visual FINAL/end-of-coverage evidence, independently read twice | Complete only the requested game/broadcast/session | Paused/stopped/NONE, buffering, duration, position, recorder termination and estimated end times never prove completion |

## Collection safeguards

`event_records.py` frames **complete records**, not newline-delimited labels. stdout and stderr have independent UTF-8 decoders and record buffers. A `recordCount` terminator closes an event; a new start, oversized record or stream exit invalidates an incomplete record. Each record/physical line is bounded at 1 MiB. Pending fragments and complete-record transitions participate in screenshot validity, so a screenshot cannot silently inherit a label whose record was still arriving.

`accessibility.py` preserves the original independent input/accessibility channels, device-time cutoffs, action IDs, evidence generation/revision, 120 ms burst coalescing, 900 ms post-key wait and 60-second evidence age. Clear, unlabeled, contradictory, expired and missing events remain explicit unknowns. The recognized Prime virtual-node burst remains version-scoped; a hard or incomplete window transition requires renewed visual context. Matching channel labels are not element-instance proof.

The listener becomes `receiving` only after actual Prime events arrive. An unexpected exit **latches failure** instead of repeatedly launching UIAutomator into an existing registration. Normal shutdown can restart cleanly; an unexpected failure requires operator diagnosis and a controller restart. Cleanup uses the owned remote PID, Linux process start ticks and UIAutomator command line, then confirms that identity has exited. A missing ownership marker or failed cleanup latches failure. No global UIAutomator kill or accessibility-permission toggling is performed.

Captures bracket native state and media identity before and after acquisition. Native validity is checked again after observation and under the same input gate as manual control. Collector failure does not invent usable focus: bounded screenshot navigation remains available. Manual takeover and Cancel retain the existing input-drain and owned-playback-stop barrier.

## Search-to-play policy

1. The controller launches/focuses Prime and submits a package-scoped search using structured opponent names and bounded alternate queries.
2. The screenshot-only observer transcribes current focus, identity, live availability and visible layout without receiving the target or native label. Python matches these facts. The actor receives the target and separately qualified native evidence when navigation is unresolved.
3. A confirmed live result is opened with one short Select. Missing variant details can be learned in its menu; a known conflicting route is not accepted. Opening a card establishes **no live-playback proof**.
4. The observer reads the sheet's event identity and the visible items in order. The controller chooses an unambiguous permitted Watch Live variant. It computes the next direction from this observed order, sends one key, and requires the expected fresh input-focus label before continuing.
5. Unknown, off-screen, duplicated or ambiguous targets fall back to fresh visual navigation. An unexpected neighbor, missing input-channel evidence, expired context or hard window boundary stops the sequence and triggers another capture. There is no assumed first item, menu length, wrapping, or “Up until the top” rule. The action/deadline limits are budgets, not menu-size assumptions.
6. Before Select on Watch Live, two fresh visual readings must agree on the focused action, requested event, live availability and permitted variant. Available native focus must agree. Resume, Rapid Recap, Multiview and Watch from beginning are not substitutes. Only an acknowledged Watch Live dispatch establishes the activation history used below.

Deterministic menu arrows still use the shared physical input lock, durable ownership check and action journal. Fast traversal does not bypass cancellation or manual ownership. An actor's FINISH is only a proposal; it cannot verify playback.

## Runtime association and monitoring

The preferred path reads the v2 `dev.tvprobe.mediasession/.ProbeService` dump, bounded tails of both private journals (512 KiB each), and another dump. Service UUID/connection epoch must agree across export. Strict validation rejects cached, failed, incomplete, stale or malformed observations. Current callback registration and recent successful poll/read health are checked independently from writer and export health. V2 does not require PID access. Unversioned v1 keeps a separate legacy adapter; an invalid advertised v2 record never falls back to hash identity.

A missing newest record that is already reported written permits one bounded re-export and final dump. The final produced checkpoint defines the required export interval. Every needed sequence must be present and valid for this service and represented in retention metadata. Pending writes, gaps, conflicting duplicates, truncated records and increased failure/loss counters withdraw continuity. A PLAYING snapshot and a caught-up written high-water mark cannot repair missing callbacks. Old v1 and previous-service records cannot fill a v2 interval. The first checked checkpoint establishes a baseline, not a complete lifetime-history claim.

After Watch Live, the observer must see the requested player identity and permitted route. Prime must remain foreground with a fresh, active, reported PLAYING session and healthy continuity evidence. Identity must agree across capture and the post-inference read. Only then does the executor associate Teamarr content with `(device boot, service UUID, connection epoch, session-instance ID, runtime media ID, local revision)`. The legacy token hash is diagnostic only. This association is specific to the request; runtime IDs are not universal catalog identifiers.

Callback payloads are separate from later non-atomic snapshots. Conflicting callback identity, including a transient change followed by the original ID, invalidates prior continuity. Snapshot freshness is measured from acquisition start. Historical `session_removed` data retains its original acquisition time and is never promoted to a current player or completion claim.

Stable associated playback is sampled every five seconds by default without another screenshot/model call. A visual check is due every 30 seconds. Source timestamps and the last successful visual timestamp stay separate. An ad or hidden identity can temporarily retain a continuous binding, with its original visual time, for at most the larger of 60 seconds or twice the configured visual interval. Positive contradictory identity, a menu instead of a player, an explicit behind-live playhead or a blocking dialog withdraws it immediately when observed.

Service/connection/session/media changes, source loss, long observation gaps, incomplete export, collector/writer failures, pause/seek/stop and position discontinuities withdraw live-mode continuity. A repaired collector can support a new visual association at a later checked checkpoint while retaining historical loss counters. It cannot revive the pre-gap association or retroactively fill the lost interval. The v2 one-second poll has independent health timestamps and approximately 15-second unchanged-state heartbeats; silence is not a one-second heartbeat failure.

A 301 ms BUFFERING→PLAYING transition is present in capture 05: buffering is unverified transport, preserves otherwise continuous identity, and can recover without another Play or Select. Controller recovery grace governs any later retry. No transport or session-removal transition completes an event.

Restart invalidates persisted playback/binding claims. Reassociation after restart, a changed session or a lost binding requires fresh visual identity/route and explicit playhead-at-live evidence. The monitor does not press keys to reveal controls or silently select Watch Live again. If that evidence cannot be acquired, playback stays unverified and ordinary controller recovery policy applies.

Without the structured probe, the fallback needs two matching visual live-player samples, an active Prime PLAYING session and an advancing **visible elapsed player timer**. MediaSession position increments no longer satisfy that check. Hidden timers/live controls may therefore prevent fallback verification.

The existing token getter adds optional `runtime`: schema/build, source/collection/journal health, source timestamp/expiry, service/connection/session identity, produced/written checkpoints, loss counters, history status/problems, binding state, last visual time, live-mode basis, reported position and bounded callback/removal diagnostics. `history_status` distinguishes `baseline`, `covered`, `incomplete` and `unavailable`; `history_gap` marks an unproven interval, including temporarily pending export. `probe_instance` remains a v1 PID diagnostic. `live_edge` stays `unmeasured`. GET projects stored evidence and expiry; API polling does not acquire device evidence.

## Deployment and remaining validation

The collector and navigation are integrated into the controller; no new public API or database migration is needed. All ADB/model I/O is asynchronous and bounded. The standalone recorder is a development reference; continuous raw screenshots and event journals are not saved by ordinary execution.

**Media probe packaging:** controller 0.11.0 includes the supplied v2.0.0 Java/manifest/APK with explicit install/check commands. The APK signature and matching original certificate were independently verified. A source rebuild matches its DEX and compiled manifest. All 31 supplied host tests were rerun. The controller adds fault and integration coverage for the v2 contract; target-TV installation remains untested. See [setup](executor-setup.md) and [probe integration/recovery semantics](../android/prime-media-probe/README.md).

Automated checks cover the original collector conformance corpus, all 103 recovered accessibility records, multiline/channel/framing hazards, media callback semantics, shuffled menu positions/lengths, variant constraints, unexpected-label recovery, input-gate races, cheap stable monitoring, buffering, stale evidence and binding withdrawal. These are deterministic replay/controlled boundary tests. No physical TV, ADB executable or configured model endpoint is available in this development workspace.

Still requiring target evidence: autonomous query-entry-to-play runs; observer accuracy on sheet ordering and scoreboard identities; multiple languages/subscriptions; off-screen or changed menus; probe setup/restart/permission loss; listener contention and suspend clocks; sustained pause/seek/ad behavior; explicit event and aggregate-broadcast endings; and long-running cancellation/recovery. Capture 05 supports useful policies along one real path, not all relevant facets or an unattended-reliability claim.
