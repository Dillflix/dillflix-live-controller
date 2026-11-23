# World Poker Tour

Teamarr's `sportsnet_wpt` source supplies World Poker Tour TV airings, including
recorded tournament episodes currently airing on Sportsnet. Competition `wpt`
and the Sportsnet WPT coverage source are available in Priorities even before
a program is discovered. Discovery also works with an empty game-league list.

Each airing retains Teamarr's identity and channel. Consecutive episodes are
separate entries. No database migration or fleet configuration change is needed.
Existing priorities continue to control whether an airing is selected.

For `linear_program` coverage, Prime matching requires the same explicit start
and end timestamps plus the requested channel. A series title alone cannot
identify a particular TV slot. Entitlement and live readiness remain independent
checks; missing timing stays uncertain. The scheduled end is not final status.

## Playback validation still required

The current Prime Player carousel collector does not implement the Live TV
guide's program/station schema. The controller still accepts playable `EVENT`
results only. Generic channels, series and on-demand episodes remain excluded.
The new schedule and priority support is therefore not a claim that Prime can
discover or launch every listed WPT airing. Verify an authenticated WPT search
and actual live-TV playback before treating this integration as production-ready.
Do not work around a missing airing by enabling replay playback.
