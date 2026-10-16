# Upcoming subscription entitlement

Version 0.18.0 queues one durable entitlement job for each Prime event first imported
from Teamarr. The existing Teamarr import window is the only discovery boundary.
The upgrade also seeds currently imported events once. Feed refreshes, changing
scores, restarts and reimporting the same event ID do not reset completed checks.

Version 0.18.2 also records positive subscription evidence from accepted foreground
catalogue selections and fresh, attempt-bound playback with an explicitly entitled
selected feed. This completes the same durable job without an extra search. Existing
playback is reconciled on its next ordinary monitor. Event/route identity and device
scope must match; a late background error cannot overwrite the accepted positive.
Routine monitoring does not repeatedly refresh completed entitlement records.

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

Unknown checks retry at 5, 10, 20, 40, 80, 160, then 300 minutes (five-hour cap).
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
group, overdue retries precede first checks, and events are ordered by scheduled
start rather than opaque ID. Retries keep their long backoff; they cannot be starved
by the zero-valued initial deadline of hundreds of untouched jobs.
The worker waits behind due playback monitoring, launches, cancellation, recovery, manual
control and foreground catalogue work. It uses only the existing non-navigating
search API, shared query fallbacks and identity matcher; it never calls broadcasts,
resolve, play, stop, suspend or network-switch APIs. Model calls retain the existing
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

Schema 12 stores observations and jobs separately from playback readiness. Diagnostics
include up to 500 current schedule records. Catalogue retention removes old evidence
but retains a compact event marker to prevent rediscovery from creating a new automatic
check. Schema 11 controllers cannot open this database after migration; back up before
upgrading if an older binary must be restored.
