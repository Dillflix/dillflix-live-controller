# Upcoming subscription entitlement

The controller queues one durable account-level entitlement job for each Prime event first imported
from Teamarr. The existing Teamarr import window is the only discovery boundary.
The upgrade also seeds currently imported events once. Feed refreshes, changing
scores, restarts and reimporting unchanged event details do not reset completed checks.
When event or viewing-option details change enough to invalidate stored evidence,
the next catalog refresh queues a new check automatically. This includes a DAZN
tennis broadcast gaining an end time when it goes live. Completed checks and old
retry deadlines cannot leave such an event permanently Unconfirmed. Earlier
evidence remains available for diagnostics, but does not authorize a new selection
until the changed listing has been checked. Queued/manual, in-flight and stopped
jobs retain their existing handling; playback and manual control still take priority.

Version 0.18.2 also records positive subscription evidence from accepted foreground
catalogue selections and fresh, attempt-bound playback with an explicitly entitled
selected feed. This completes the same durable job without an extra search. Existing
playback is reconciled on its next ordinary monitor. Event/route identity and the originating player
session must match; a late background error cannot overwrite the accepted positive.
Routine monitoring does not repeatedly refresh completed entitlement records.

All configured streams share the same Prime account, subscription receipts and
queue. A single worker picks an available player and yields to playback/recovery
on that player. If a player is unavailable, another can perform the check. Adding
a stream or opening its Activity page never creates a second scan. Check/Recheck
on either stream addresses the same job. Activity identifies the player currently
performing it; diagnostics from any stream include this shared queue and each
player's dependency state.

Schema 17 reuses existing current-identity positive results across devices and
archives the original per-device receipts. A device's regional launch denial
remains local; it does not revoke account subscription evidence. Per-device
network changes, launch and live verification remain necessary for the selected
event. Independent Prime accounts require separate controller instances.

Entitlement is separate from event lifecycle, regional availability and live-launch
readiness. An ENTITLED/UPCOMING or ENTITLED/UNAVAILABLE result is entitled. No match,
missing data, ambiguous alternatives and errors are
unknown. An entitled matching feed wins over another provider's subscription denial;
negative evidence must cover each permitted matching Prime route. Non-Prime routes
are never disabled by a Prime-only observation. Full subscription coverage beyond
the inspected catalogue response cannot be inferred.

Version 0.18.3 searches for an entitled alternative through every distinct team
query: the original paired names, full names, then short names (at most three).
The final event-title fallback for team events is removed; broadcasts/sessions
without paired team labels still use their title. An entitled match finishes
immediately. Finding an unentitled feed does not stop later queries. Only a finished
scan with identified unentitled routes and no unresolved alternatives can settle
as Unentitled. Empty later responses do not erase an identified feed; errors and
timeouts never turn a partial scan into denial. Non-exhaustive search coverage
(`complete: false`) alone does not invalidate a confirmed event identity. These
observations describe the inspected feeds, not the whole Prime catalogue.

Within each background check, unchanged candidate identities and explicit
identity rejections are reused across queries. An identical unresolved pool can
also reuse its uncertain decision without becoming a rejection. New or changed
evidence goes through the existing matcher. Reuse is scoped to the target, route,
timezone, service session, generation and network context. Provider/identity text
changes invalidate reuse; current access flags always come from the new response.
This memory ends with the check, so later retries/manual checks read fresh evidence.

Successful checks exhausting every query with no matching feed retry after 24 hours.
Confirmed subscription denials use the same schedule, because new entitled feeds
can appear later. Older completed denials become retryable from their original
check time; restarts do not postpone the deadline. They remain Unentitled until a
fresh positive result is accepted. Confirmed ended/cancelled events stop retrying.
Existing automatic no-listing retries are rescheduled once from their last check
time when this policy is deployed; deployment does not trigger a new search.
If the event starts sooner, one check is scheduled at its start (at least one hour
since the previous check). No listing is not a subscription denial. Ambiguous
results and technical failures retry at 5, 10, 20, 40, 80, 160, then 300 minutes
(five-hour cap).
The retry count and deadline are durable. Completed entitlement results have no
periodic refresh. Confirmed completion/cancellation stops retries; events outside
the imported active catalogue are not scanned automatically. Admin Check/Recheck entitlement on
cards and details queues a new check immediately, bypassing the backoff. Repeated
clicks coalesce with active work. The endpoint uses normal admin authentication,
revision checks and command receipts. A manual check never launches playback.
It can also check a retained watch-plan event outside the current import window;
that exception does not resume automatic scans outside the window.

Both apps show a single entitlement/job status in the existing event footer:
Entitled, Unentitled, Unconfirmed, Queued or Checking, with an accessible icon and
tooltip. Play now/Add to plan appear only after entitlement is confirmed. Admins
get a Check button instead while unconfirmed or unentitled; public users can view
details, while manual checks remain admin-only. The command API rejects confirmed
denial. Unknown remains distinct from denial for existing automation and commitments.
Existing commitments remain visible and removable, but denied events are not selected
automatically or shown as winners in schedule previews. Current playback is not stopped
by this background observation. Changed event identity/routes invalidate the observation
for gating without automatically resweeping; an admin can recheck. Existing live
acquisition still performs its own fresh readiness checks.

## Work scheduling and playback

Version 0.18.1 fixes a starvation bug: a routine playback monitor becoming due
during search could previously discard the returned data before matching, sending
the job from Checking back to Queued indefinitely without a result or failure count.
Matching now continues alongside routine monitoring. Before a fallback search, the
worker yields to monitoring within the check's shared two-minute deadline while
preserving its fetched data and matching progress. Launches, recovery and manual
control still defer the job.

Admin **Activity → Entitlement checks** shows the queue, worker phase and waiting
reason, last start/check times, retry counts and deadlines, errors, and last deferral.
Use the filters to see queued, checking, retrying, failed, completed or stopped jobs;
search by event title and check/recheck individual events there. A queued event with
no completed result is labelled **Entitlement not checked yet**. Failed checks remain
visible while awaiting their backoff, and stale worker progress is shown explicitly.
Stable playback does not block the queue for the event's duration.

One event check runs at a time, with at least five seconds between automatic starts;
there is no longer a one-minute dispatch limit. Manual requests come first, then live
and watch-plan events, then other upcoming events. Within each automatic priority
group, version 0.18.4 alternates due retries with first checks when both exist;
events within either group are ordered by scheduled start rather than opaque ID.
Retries keep their long backoff, and neither group can monopolize dispatch.
The worker waits behind due playback monitoring, launches, cancellation, recovery, manual
control and foreground catalogue work. It uses only the existing non-navigating
search API, shared query fallbacks and identity matcher. Before accepting a denied
or unknown matching parent, it also inspects alternate broadcasts through the
non-navigating broadcasts API. An entitled permitted child establishes subscription
access even when the parent is locked. Unresolved or incomplete alternatives remain
unknown. Session, generation, ownership and network context fence these reads;
responses are reused only within the same check. It never calls resolve, play,
stop, suspend or network-switch APIs. Model calls retain the existing
training capture with source `schedule_entitlement`.

Each search has a fifteen-second service budget, with the existing five-second transport
allowance. The controller releases its shared operation lane between searches and
does model work outside it. A check has a two-minute total budget. An in-flight search
cannot be preempted safely merely by dropping its HTTP connection; urgent playback
work can wait for that bounded read to settle, but no further background request may
pass it. No player update is required. Host tests establish scheduling and ownership
behavior, not Fire TV performance under sustained load; device validation must check
continued position advancement and monitor latency while a populated queue drains.
Known catalogue timeouts appear as `prime_search_timeout`, rather than an unexplained
`UnavailableError`. A timeout remains unknown and follows the existing retry backoff.

Version 0.18.4 checks shared player availability before claiming an event. A sleeping
device, disconnected runtime, missing capability or unreachable service pauses the
whole queue. Activity shows the service problem and next availability probe, with
30, 60, 120, 240, then 300 seconds between failed probes. These dependency failures
do not increment individual event failures or consume their unknown-result backoff.
When search recovers, old per-event service-outage retries become queued again;
their historical failure counts remain recorded. Genuine matching retries retain
the five-minute-to-five-hour schedule. Manual checks do not bypass a service outage.
Diagnostics include the worker and shared-service waiting state.

Schema 12 stores observations and jobs separately from playback readiness. Diagnostics
include up to 500 detailed schedule records plus a compact status index of every
retained event, so older checks omitted from the evidence limit remain visible.
Catalogue retention removes old evidence
but retains a compact event marker to prevent rediscovery from creating a new automatic
check. Schema 11 controllers cannot open this database after migration; back up before
upgrading if an older binary must be restored.

Events in both web apps default to the Entitled filter. All events, Unentitled,
Unconfirmed, No Prime listing found, Queued and Checking remain selectable.
Filters do not change watch plans, Now Playing, or automatic selection. Routes
without Prime entitlement checks retain their existing visibility.
