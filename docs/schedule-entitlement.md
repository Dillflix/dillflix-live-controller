# Upcoming subscription entitlement

Version 0.18.0 queues one durable entitlement job for each Prime event first imported
from Teamarr. The existing Teamarr import window is the only discovery boundary.
The upgrade also seeds currently imported events once. Feed refreshes, changing
scores, restarts and reimporting the same event ID do not reset completed checks.

Entitlement is separate from event lifecycle, regional availability and live-launch
readiness. An ENTITLED/UPCOMING or ENTITLED/UNAVAILABLE result is entitled. No match,
missing data, ambiguous alternatives, incomplete negative coverage and errors are
unknown. An entitled matching feed wins over another provider's subscription denial;
negative evidence must cover each permitted matching Prime route. Non-Prime routes
are never disabled by a Prime-only observation. Full subscription coverage beyond
the inspected catalogue response cannot be inferred.

Unknown checks retry at 5, 10, 20, 40, 80, 160, then 300 minutes (five-hour cap).
The retry count and deadline are durable. Completed entitlement results have no
periodic refresh. Confirmed completion/cancellation stops retries; events outside
the imported active catalogue are not scanned. Admin Check/Recheck entitlement on
cards and details queues a new check immediately, bypassing the backoff. Repeated
clicks coalesce with active work. The endpoint uses normal admin authentication,
revision checks and command receipts. A manual check never launches playback.

Both apps explain entitlement and disable Play now/Add to plan for confirmed denial;
the command API enforces the same rule. Unknown stays usable with an explicit warning.
Existing commitments remain visible and removable, but denied events are not selected
automatically or shown as winners in schedule previews. Current playback is not stopped
by this background observation. Changed event identity/routes invalidate the observation
for gating without automatically resweeping; an admin can recheck. Existing live
acquisition still performs its own fresh readiness checks.

## Work scheduling and playback

Automatic dispatch is limited to one event check per minute across the queue. The
worker waits behind due playback monitoring, launches, cancellation, recovery, manual
control and foreground catalogue work. It uses only the existing non-navigating
search API, shared query fallbacks and identity matcher; it never calls broadcasts,
resolve, play, stop, suspend or network-switch APIs. Model calls retain the existing
training capture with source `schedule_entitlement`.

Each search has a five-second service budget, with the existing five-second transport
allowance. The controller releases its shared operation lane between searches and
does model work outside it. A check has a two-minute total budget. An in-flight search
cannot be preempted safely merely by dropping its HTTP connection; urgent playback
work can wait for that bounded read to settle, but no further background request may
pass it. No player update is required. Host tests establish scheduling and ownership
behavior, not Fire TV performance under sustained load; device validation must check
continued position advancement and monitor latency while a populated queue drains.

Schema 12 stores observations and jobs separately from playback readiness. Diagnostics
include up to 500 current schedule records. Catalogue retention removes old evidence
but retains a compact event marker to prevent rediscovery from creating a new automatic
check. Schema 11 controllers cannot open this database after migration; back up before
upgrading if an older binary must be restored.
