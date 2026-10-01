# Accessibility grounding: evidence and implementation correction

Audit date: 2026-09-30. Status: collector and capture/input integration implemented in 0.9.1; live critical-path validation not performed.

The initial 0.9.0 Python executor omitted accessibility collection. Its `Scene.focus.label` was only a vision-model reading. No comparative evaluation justified that omission. Version 0.9.1 ports the native collector, action timing, screenshot association and focus-revision safeguards described below. `Scene.focus` remains the independent visual reading; acquired native evidence is carried separately in `Frame.native_focus`. The three API operations remain unchanged. Complete physical search-to-play behavior is still unvalidated.

Accessibility is a foundational runtime evidence source for this work. Its authority must be scoped to what each event actually describes. A native keyboard label can identify the input receiver even while the requested event is visible elsewhere. A sports-row label can establish row context without identifying an individual match. These limitations require combining evidence; they do not justify discarding it.

## What was actually audited

Source: the uploaded exploration archive, under `agent-control/design/sports-search-production-2026-09-25/`. These are **search-and-focus observations**, not search-to-play traces. The report explicitly says no content was activated and no playback was attempted. Creating and validating the missing activation/playback evidence is implementation work, not an assumed user-supplied prerequisite.

The reproducible [inventory](evidence/accessibility-search-audit.json) contains source hashes, observation IDs, native labels/descriptions, original usability verdicts, channel labels, screenshot availability, and checks against the archive's review records. Generate it from the unchanged archive with:

```bash
python tools/audit_prime_accessibility_archive.py /path/to/agent-control \
  --output docs/evidence/accessibility-search-audit.json
```

This audit found 62 directly saved observation JSON files, of which 32 have adjacent native PNGs. At the time of capture, the collector marked 47 usable/fresh and 15 unusable: 11 `no_event`, two `expired`, and two `window_boundary`. These are selected saved snapshots, not a capture-success rate, a complete event stream, or held-out evaluation. All 62 record unchanged focus validity during capture; that does not establish unchanged pixels or continuing validity afterward.

The separate `focus-review.json` records 11 visually focused fixtures: all report `Top Sports`, ten fresh and one invalidated. Only five of those review entries have corresponding directly saved observation/PNG pairs in this archive. All five native readings and native image hashes agree with the review. Do not promote the other six review summaries into newly inspected raw evidence. The report references larger journals that are not present in this extracted archive.

Three paired screenshots were visually reinspected for this audit: `nfl-texans-colts-019` (keyboard a focused), `focus-nfl-003` (keyboard c focused), and `nfl-texans-colts-026` (Texans vs. Colts card outlined in Top Sports). They agree with the specific focus distinctions below. This is manual inspection, not a model benchmark.

The archived `prime-focus-burst.test.js` and `accessibility-unlabeled-diagnostics.test.js` passed when rerun locally. `focus-channels.test.js` could not finish because the archive's `jimp` dependency is not installed. These checks exercise deterministic collector behavior; none observes a live TV or validates the current Python executor's use of native evidence.

## Evidence along the relevant path

| Stage | Actual saved evidence | What it supports | What remains unestablished |
| --- | --- | --- | --- |
| Search entry | `nfl-texans-colts-019.json`: fresh `a Alpha`, description `[Search Keyboard, Search] a Alpha`; both focus channels agree | Current focus is in the keyboard even though results are visible. The screenshot confirms a highlighted a key. | A keyboard label alone does not certify loaded results, current query, or correct content. Some search entries have no usable event. |
| Directional movement through keyboard | `nfl-texans-colts-020` through `024`: fresh `b Bravo`, `c Charlie`, `d Delta`, `e Echo`, `f Foxtrot` after individual RIGHT inputs | Native transitions provide direct evidence of focus progress and which input receiver is active. `focus-nfl-003` also pairs fresh `c Charlie` with a highlighted c key. | No permanent keyboard geometry or fixed input count; a missing event is not proof of a no-op. |
| Query suggestion | `nfl-texans-colts-025.json`: fresh `Texans Colts`, description `[Search Suggestions] Texans Colts`; both channels agree. Comparable saved labels include Jets Lions, Bruins Capitals and Seahawks Commanders. | Scope distinguishes a query suggestion from an event card despite matching competitor names. This is useful negative evidence against a false event activation. | Suggestion text is not proof of the selected match, its date/live state, or current-query identity. No adjacent PNG for these four suggestion observations is present. |
| Enter sports results | `nfl-texans-colts-026.json`: fresh `Top Sports` after DOWN; screenshot outlines Texans vs. Colts. Bruins/Capitals, Seahawks/Commanders and Ravens/Cowboys have corresponding fresh row snapshots. | Native row context complements the visually outlined card and detects leaving the keyboard/suggestion region. | `Top Sports` does not identify the individual event, date, subscription entitlement, or live availability. The inspected Texans card is UPCOMING. |
| Invalidated result focus | `focus-nfl-tile-001.json`: retained `Top Sports`, `usable=false`, `reason=window_boundary`, input channel only | A historical label must not be presented as fresh grounding. Preserve the reason and obtain usable evidence. | Do not relax window handling merely to increase label availability. |
| Move between sports tiles | The exploration describes date-aware selection, including later baseball results, and retains final review summaries. The 62 direct snapshots do not provide a verified sequence of native readings for movement between sports cards. | Existing reports guide the next probe and supply candidate cases. | Whether labels change per card, repeat at row level, or emit no event; behavior across scrolling and duplicate matchups must be captured. |
| Select result, details, live/restart choice, player | The sports probe's summary says `playbackAttempted=false`. Its direct snapshots contain OPEN_SEARCH, RIGHT, DOWN and OBSERVE only. | No native-label conclusions about these stages can be drawn from these probes. | Actual labels, descriptions, focus channels, transitions, button semantics and playback outcomes must be observed on the target app/account. Do not invent Watch live labels or assume a detail page always appears. |

The archive's original query probes were agent-directed, not autonomous actor acceptance. They are useful development evidence. Neither their visual focus results nor their native labels certify live playback.

## Reuse the existing capture implementation

The port in `controller/executor/accessibility.py` uses `tvtheseus/accessibility.js`, `observations.js`, `prime-focus-burst.js`, `focus-metadata.js`, and their tests as its reference. It preserves separate native evidence and asynchronous collection; it does not substitute repeated UI hierarchy dumps or a single last-label string. The following are the port's required behaviors:

- Run the persistent `uiautomator events` stream with bounded parsing, independent input/accessibility focus channels, and disconnect/reconnect invalidation. Treat labels and descriptions as application data, never executable instructions.
- Preserve device-time action cutoffs, action IDs, evidence generation/revision, bounded coalescing, freshness and unlabeled/cleared focus handling. Retain original channel records and reasons; a stale record is diagnostic history, not current grounding.
- Preserve screenshot association checks before/after capture and before action dispatch. Recheck inside the same ownership gate used for manual and autonomous input; an old model decision must not execute after focus/ownership changes.
- Preserve the narrowly version-scoped Prime burst recognizer. New focus can be followed by a focus-node window echo, retired virtual-node window events and subtree closure. A partial sequence or real window boundary must not be accepted as a complete recognized burst. The archived recognizer supports `PVFTV-321.0096-L (321009610)`; unknown versions require explicit treatment, not silent extension of that exemption.
- Do not reinterpret no event, repeated text, or unchanged event revision as proof of a stationary screen, same element instance, or failed movement. Keep a bounded visual fallback when native evidence is unavailable; a known unresolved contradiction requires reobservation.
- Keep collection, capture, inference and waits nonblocking. Tie collector ownership and cleanup to the device worker; invalidate across Cancel, handoff, restart and connection changes. A physical remote or other ADB client remains outside the input gate, so observed changes still matter.

## Ordered implementation and validation plan

### 1. Port collection and add a recorder before extending navigation policy

The collector, acquired frame structure in `adb.py`, and native status in `check.py` are now implemented. The archive's parser, channel, timing, burst and invalidation contracts are reference acceptance cases. Native evidence remains separate from model-produced `Scene.focus`, with its own source, scope, timestamps and validity. The extended multi-step recorder below remains future work; current diagnostics save a single capture and its structured native evidence.

Provide an opt-in bounded recording mode that retains raw event lines, action dispatch/acknowledgment, app version, screenshots and capture windows. Record the collector's verdict as a verdict, not ground truth. Capture should support diagnosis of useful labels rejected by overly conservative handling as well as invalid labels mistakenly admitted. No raw trace is needed in ordinary token responses, and no new public API is required.

Acceptance: archived behavior is preserved under fragmented/delayed output, channel disagreement, event clears, reconnects, incomplete bursts, real window boundaries, changing focus during capture/inference, cancellation and manual handoff. These are deterministic capture/lifecycle checks, not TV accuracy claims.

### 2. Observe the complete live critical path on the target deployment

Use the integrated recorder and the existing input gate to capture search entry, keyboard/suggestion departure, vertical and horizontal results movement, target focus, result activation, every intermediate page/dialog, the actual live-play control if present, and the resulting player. Include duplicate matchups/dates, upcoming/replay neighbors, unavailable/subscription-limited results, return navigation, delayed loading and missing native events. Observe the real flow rather than imposing a presumed sequence of screens.

For each step, record what the app emitted, which channels agreed, label/description scope, associated visible control, usability and rejection reason, and what happened after the action. Inspect the app version before applying the archived burst exception. Establish which stages expose exact actionable controls and which expose only containers. Record lateral tile behavior rather than treating a persistent Top Sports label as no movement.

Acceptance: a reviewed matrix for every observed stage, including explicit missing/ambiguous evidence. Successful startup and result focus alone cannot close this step. This development workspace currently has neither an ADB executable nor configured TV/model endpoints; no new live validation has occurred here.

### 3. Integrate evidence according to the measured semantics

Give the actor compact current native focus/context and action-linked transitions alongside screenshots. Use valid keyboard/suggestion context to avoid treating matching visible text as a focused event. Use valid row context to help locate the focused card; use visible identity/date/live evidence for the particular matchup. Do not make exact native/visual text equality mandatory when the sources describe a container and its child.

Keep the goal-blind visual observation separate, then reconcile native and visual claims by subject and freshness in the action guard. This preserves an independent visual reading without excluding native evidence from the final decision. Do not automatically prefer a model inference over contradictory valid application evidence. Conversely, do not let a label substitute for an unsupported claim about selected state, entitlement or playback.

Use native revisions to reject stale actions and native transitions to improve progress tracking. Repeated Top Sports text alone cannot trigger a navigation-loop verdict. Define details/play-control policies from step 2's recordings. Extend token diagnostics with concise evidence/reasons while keeping Play, status by token and Cancel unchanged.

Acceptance: regression cases cover keyboard versus visible target, query suggestion versus event, row versus card, duplicate dates, stale labels, ambiguous channels and focus changes during inference. Native evidence is demonstrably used in actor input and final action checks, not merely logged.

### 4. Validate autonomous playback and measure contribution

Run actual autonomous search-to-live-play episodes on the target device/account/model configuration. Record correct requested-event playback, wrong or blocked activations, input count, wall-clock latency, native availability by stage, capture rejection reasons and recovery behavior. Exercise Cancel and manual takeover during collection, inference, input and active playback.

Compare screenshot-only and combined-evidence decisions on matched recorded observations, preserving cases and failures. Use fresh live episodes for final outcomes; offline replay cannot establish physical playback or event completion. Keep tuning examples separate from later evaluation episodes and report repeated/correlated cases as such.

Acceptance: the requested event actually starts at the live presentation and token status agrees with the independent outcome observation. Upcoming/replay/purchase alternatives are not activated. Label or focus completion never becomes event-completion evidence. Broader unattended reliability and event-end monitoring remain distinct validations.

## Completion accounting

Completed: audit of available search/focus observations and reproducible inventory; the Python native collector; action/device-clock boundaries; dual channels; version-scoped Prime bursts; before/after/finalized screenshot association; native metadata in JSON and TVTheseus actor requests; stale-observation/input rejection; manual-input invalidation; bounded stream reconnect/cleanup; and read-only native diagnostics. The goal-blind visual observer receives no native label or expected answer.

Validation: 290 backend tests pass. The new Python state machine matches 316 snapshots across 54 scenarios generated by the unchanged original JavaScript collector. Additional tests cover fragmented UTF-8/CRLF, whole-record rejection on overflow, process creation/cancellation/reconnect, coalescing/timeouts, clock failure, capture/finalization changes, inference/input races, manual wake/key invalidation, actor/observer separation and diagnostic cleanup. These are port and controlled integration checks, not new device observations or an end-to-end model benchmark.

To regenerate the reference fixture from the original uploaded source (Node is needed only for this development step):

```bash
node tools/generate_accessibility_conformance.cjs /path/to/agent-control/tvtheseus \
  tests/fixtures/prime-accessibility-conformance.json
python -m pytest -q tests/test_accessibility.py tests/test_real_executor.py
```

The fixture includes hashes of its source modules. Expected snapshots come from JavaScript, not the Python implementation under test. The streaming wrapper also discards overlong records whole rather than parsing their tails, and capture finalization downgrades evidence that expired during image preparation.

Not completed: extended raw-event/action/screenshot recording, new target-device observations, a measured per-stage label policy for activation/player screens, or live autonomous search-to-play validation. Existing visual activation criteria still apply, supplemented by native timing/association guards and actor context. They must be assessed against the complete physical path before claiming that path is reliable.
