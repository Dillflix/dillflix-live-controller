# Expanded sports discovery (0.19.0)

## Soccer cups (schema 19)

Discovery now also includes EFL Cup / Carabao Cup (`eng.league_cup`) and
DFB-Pokal (`ger.dfb_pokal`), bringing the default selection to 38 competitions.
Update Teamarr and the controller together. Schema 19 adds both cups once when
the saved selection matches the previous 36 defaults, preserving their order.
Custom and empty selections remain unchanged; enable the cups in Settings.
Back up the controller database before upgrading; older versions cannot open
schema 19. Restore the backup to roll back.

If Teamarr has an explicit `controller.league_apps` map, merge these entries:

```json
{
  "eng.league_cup": "prime_video",
  "ger.dfb_pokal": "prime_video"
}
```

Both use Teamarr's existing ESPN soccer providers. Prime Video entitlement and
live availability checks still apply; a DAZN listing alone does not establish
availability through the Prime channel.

## Original expansion

The controller adds the following 25 competitions to the existing 11 defaults.
Teamarr already supplies their providers; no new provider API keys are required.

| Competition | Teamarr league code | Provider |
| --- | --- | --- |
| EFL League One | `eng.3` | espn |
| EFL League Two | `eng.4` | espn |
| UEFA Europa League | `uefa.europa` | espn |
| UEFA Conference League | `uefa.europa.conf` | espn |
| Premier League | `eng.1` | espn |
| Serie A | `ita.1` | espn |
| La Liga | `esp.1` | espn |
| MLS | `usa.1` | espn |
| NWSL | `usa.nwsl` | espn |
| FA Cup | `eng.fa` | espn |
| WNBA | `wnba` | espn |
| NCAA Men's Basketball | `mens-college-basketball` | espn |
| NCAA Women's Basketball | `womens-college-basketball` | espn |
| PWHL | `pwhl` | bellmedia |
| CHL | `chl` | bellmedia |
| OHL | `ohl` | bellmedia |
| WHL | `whl` | bellmedia |
| QMJHL | `qmjhl` | bellmedia |
| NCAA Men's Hockey | `mens-college-hockey` | espn |
| NCAA Women's Hockey | `womens-college-hockey` | espn |
| United Rugby Championship | `urc` | espn |
| Premiership Rugby | `prem` | espn |
| NRL | `nrl` | espn |
| Australian AFL | `afl` | squiggle |
| PLL | `pll` | espn |

Use these canonical league codes, not Teamarr display aliases such as `epl`,
`mls`, `ncaam` or `ncaah`. CHL and its three constituent leagues are distinct
provider competitions. The controller preserves their opaque event IDs and does
not merge events by display names.

## Upgrade and enable

1. Back up the controller database and Teamarr configuration using the existing
   deployment procedures.
2. Deploy the matching Dillflix Teamarr feed update and restart Teamarr.
3. If `TEAMARR_BROADCAST_CONFIG` omits `controller.league_apps`, the new routes
   are automatic. If it contains an explicit map, merge the entries below into
   that map, keeping all other leagues, sources and configuration fields.
   An explicit empty map still disables routes.
4. Deploy controller 0.19.0 and restart it. Schema 14 adds the new competitions
   only when the saved selection matches the previous 11 defaults (order does
   not matter). Older eight-league defaults pass through schema 13 first.
   Custom and empty selections remain unchanged; enable desired leagues under
   **Settings → Leagues in your schedule**. New installations start with all 36.
5. Refresh the browser. Check schedule and team-directory health, then verify
   actual event entitlement. Public and admin apps share the same discovery
   selection; existing public action permissions and admin priority still apply.

Merge these **entries** into `controller.league_apps`; this is not a replacement
for the complete broadcast configuration:

```json
{
  "eng.3": "prime_video",
  "eng.4": "prime_video",
  "uefa.europa": "prime_video",
  "uefa.europa.conf": "prime_video",
  "eng.1": "prime_video",
  "ita.1": "prime_video",
  "esp.1": "prime_video",
  "usa.1": "prime_video",
  "usa.nwsl": "prime_video",
  "eng.fa": "prime_video",
  "wnba": "prime_video",
  "mens-college-basketball": "prime_video",
  "womens-college-basketball": "prime_video",
  "pwhl": "prime_video",
  "chl": "prime_video",
  "ohl": "prime_video",
  "whl": "prime_video",
  "qmjhl": "prime_video",
  "mens-college-hockey": "prime_video",
  "womens-college-hockey": "prime_video",
  "urc": "prime_video",
  "prem": "prime_video",
  "nrl": "prime_video",
  "afl": "prime_video",
  "pll": "prime_video"
}
```

A configured `prime_video` route is permission to search Prime Video, not proof
of subscription access or a live broadcast. Selected television coverage,
TSN+ exclusives, Sportsnet+ digital content, and DAZN/Fubo packages may differ
from the Prime channel catalog. Existing entitlement checks continue to gate
actions in both apps; no broadcaster-based fallback or standalone app launch
has been added.

Up to 100 leagues can be selected per controller device. The controller retains
20-league feed batches, including leagues retained by watch-plan commitments,
and publishes only after every batch succeeds. An unavailable batch preserves
the previous complete schedule. Teamarr's feed accepts up to 100 league filters.

No Prime Player update is needed for this change. Production subscription
availability, upstream schedules and device playback require live acceptance;
the regression suite uses synthetic events and mocked provider responses.

