# DAZN tennis broadcast coverage

Tennis is selected as a continuous tournament/day/session/court broadcast,
such as **Beijing Open: Day 4**. Teamarr owns its schedule and supplies
`kind: broadcast`, `source: dazn_tennis`, `competition: tennis`, with a
`prime_video` viewing option carrying `channel: DAZN` and the original title.
Players and individual ATP/WTA matches are not added to the schedule for this
mode. There is no need to enable ATP/WTA game discovery.

## Update

Update and rebuild **Dillflix/teamarr first**, then this controller, using each
existing deployment directory:

```bash
# Teamarr directory
git pull --ff-only
docker compose up -d --build teamarr

# Controller directory
git pull --ff-only
docker compose up -d --build
```

Keep the existing database volumes and `.env` files. The controller remains on
database schema 8 and retains current plans and preferences. Teamarr's new
`dazn_tennis` source is enabled by default. Existing broadcast-config JSON files
inherit that default; `"dazn_tennis": {"enabled": false}` disables it. Explicit
source filters that omit it will not fetch tennis.

After the next successful feed refresh, use **Tennis** in the Events filter.
Live coverage supports **Play now**; upcoming coverage supports **Add to plan**.
Under **Priorities**, choose league **Tennis**, coverage source
**Tennis coverage · DAZN**, and optionally content type **Broadcast**. Existing
manual overlap order and switching rules apply. Tennis coverage is independent
of the Settings league checkboxes, just like golf coverage.

## Continuity and playback

The DAZN EventId provides session identity; names, scheduled dates, and asset
IDs are metadata. The original title, tournament metadata, source IDs, and
every permitted route pass through the controller's durable playback request.
DAZN AssetId/EventId must never be passed to Prime as an Amazon content ID.
Prime result selection must retain the day/session/court distinction and then
use the actual Prime identifier returned by search.

Unknown end times remain unknown. A provider end is a planning estimate.
One match's Final signal cannot complete the broadcast. Missing listings,
stopped playback, intermissions, and expired status preserve commitments.
Fresh broadcast-level completion evidence or the existing manual **Mark event
finished** action can complete it.

Teamarr's `status_received_at` records acquisition, not provider freshness.
Repeated feed reads preserve the earlier acquisition time instead of renewing
cached live evidence. Source outages retain the last complete catalog and
degrade feed health.

## Verification

The public Canadian schedule was checked on 2026-10-03: it returned live
**Beijing Open: Day 4** and four subsequent sessions within the discovery
window. The importer preserves their individual IDs, artwork and UTC times;
the interface displays them in the configured timezone.

The demo's **Tennis coverage · DAZN** scenario exercises a broadcast with no
players and no estimated end. Automated tests cover source timestamps,
unknown ends, saved intent, broadcast completion scope and phone selection.

This change does not implement the separate Prime Player integration redesign
or claim actual TV playback validation. Prime availability, court variants,
the running deployments and end-to-end playback remain to be checked on the
target host. DAZN schedule labels are useful source evidence, not proof of
Prime entitlement.
