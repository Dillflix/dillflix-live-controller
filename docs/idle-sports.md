# Dynamic idle sports

Enable **Settings → Play sports when schedule is idle** to fill otherwise idle
streams from Prime Video's current Sports catalogue. This requires the Prime
Player Live TV catalogue capability and the controller's configured matching
model. The feature starts disabled and is included in configuration backups.

The chooser considers entitled live events and current TV programmes, including
sports analysis, news and recorded competitions airing on live channels. It uses
programme descriptions and your priorities, without a fixed league or channel
list. Uncertain, stale, locked or conflicting listings cannot authorize playback.

Scheduled content takes priority as soon as it is playable. Manual control and
watch-plan commitments remain protected. Streams avoid selecting the same native
event or station, and a suitable programme is retained during the configured
minimum viewing period. Catalogue refreshes and model choices never directly
control playback: the existing fleet coordinator owns every launch and switch.

Catalogue evidence expires within 60 seconds and at a TV programme's slot boundary.
A missing listing or expired slot means unknown availability, never that a sporting
event finished. Partial catalogue results can supply positively verified choices;
they cannot establish that Prime has no other sports. Idle choices stay out of the
scheduled-events list and cannot be added to the watch plan. No Teamarr entry or
league configuration is needed for these choices.

## Deployment and recovery

Upgrade Prime Player before enabling this setting. Database version 26 prevents
older controllers from misinterpreting idle choices as ordinary scheduled events;
back up the controller data volume before upgrading and restore that backup for a
software rollback. Disabling the setting leaves scheduled playback available.

The primary and secondary players are independent installations: both need the
catalogue capability if both streams will use idle sports. Catalogue failures do
not renew evidence or become subscription denials. Missing capability, invalid
model responses and service outages cannot authorize new playback and retry with
bounded backoff. An already verified native event continues under direct playback
monitoring. Existing manual/device ownership is preserved.

The stream's activity log shows catalogue checks, chooser progress, selections,
unconfirmed results and failures with retry delays. A selection entry is not a
playback confirmation; the normal launch/verification entries follow separately.
Unchanged refreshes are quiet, with repeated checking/failure notices limited to
once per five minutes. Changed outcomes and new playback intent are logged at once.
These entries contain fixed explanations and programme labels, not raw model or
player responses. Full call and validation details remain in diagnostics.

## Internal contract

`linear_catalog(facet="sports", max_pages=3)` starts with a bounded read-only
catalogue traversal. If a partial scan produces no selection, subsequent scans
expand to eight pages; a selection found in that wider scan retains that depth
for renewal. These are sequential native network requests, not model calls.
The 45-second player deadline and 60-second RPC budget are ceilings, not expected
response times. The player reserves delivery time, avoids another page when
observed request latency exceeds the remaining budget, and returns validated
pages with explicit partial coverage if a continuation stalls. A timeout before
any usable page is reported as a catalogue timeout, not an empty selection.

Before launch, rechecking a selected programme uses one page for a linear channel
or three for a native event. An exact current match ends the read; if absent, the
controller can expand to eight pages within the original request budget. Fresh
catalogue admission is required immediately before Play.

After a native EVENT launch, playback uses the same direct status monitoring as
scheduled events: matching session, attempt, requested/resolved identity, network
context and fresh native playback evidence. No catalogue check is required after
dispatch or during monitoring, and catalogue expiry during startup cannot reject
confirmed live launch proof. A fresh, bound native Ended result completes the event.
Independent discovery can propose a replacement, but an omitted current event
cannot erase owned, verified playback or renew its permission for a new launch.
An explicit chooser reassessment or higher-priority intent can still replace it.

A linear station needs continuing guide validation because its programme can
change while the station playback ID stays the same. Its monitor schedules a read
before the remaining guide evidence expires, even for responses old on receipt.
That guide read finishes before the final playback sample, so a slow read cannot
age otherwise fresh playback evidence past the acceptance limit.

If station guide freshness lapses during an active attempt, the controller retains
that attempt and displays it as unverified until matching evidence returns. This
does not authorize another Play or extend any timestamp. Eligible scheduled content,
manual intent, disabled idle mode, withdrawn nominations and the actual TV slot
boundary still take precedence. A refresh gap alone does not cancel playback.
An explicitly failed attempt is not retained by that hold: the coordinator clears
its stale recovery state so idle discovery can resume and nominate a fresh choice.

The controller validates positive rows, uses a separate
`idle_sports_selection` model task, then rechecks intent, ownership, generation,
network and peer claims before publishing a per-device nomination. Full model
calls use the existing capture pipeline; the bounded decision audit is stored in
`idle_sports_audit:<device_id>` metadata.

Rows use source `prime_idle`, with a stable namespaced ID distinct from the native
station or event playback locator. A TV selection binds station ID, airing ID,
title and exact slot; playback monitoring renews that binding. EVENT selections
require explicit entitled LIVE evidence for the exact selected native ID. The
normal durable coordinator performs all playback operations and immediately
prioritizes eligible scheduled content over idle dwell and reservation state.

Ordinary Teamarr feed imports, health and entitlement queues remain independent.
Idle rows have source-specific lifecycle/entitlement projections and never enter
Teamarr status lookups. Slot boundaries expire evidence; only native confirmed
EVENT completion or a manual completion can establish a sporting event's end.
