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

## Prime Live TV matching

TV airings use Prime Player's optional `linear_catalog` capability, including its
`station_summary` response. Ordinary sports events retain the existing Find
search and event matching path. The guide read requires the same current intent,
ownership, session, runtime generation and network checks as event search.

The controller requires an explicit station GTI, airing ID, matching title,
exact UTC start/end and requested channel, subscription evidence, and a direct
station watch action. A deferred watch action behind sign-up is insufficient.
Conflicting repeated station rows or overlapping program identities remain
uncertain. Recorded tournament footage on a live channel is allowed; no
on-demand, replay or start-over action is enabled. The native resolver must
still return `live_watch_now` for that exact station and verify playback.

Teamarr's opaque airing ID remains the controller content identity. The Prime
station ID is only its playback locator. Guide evidence lasts at most 60 seconds
and never beyond the selected slot; monitoring refreshes it before extending
program verification. A station continuing to play after a slot expires cannot
continue reporting that program as verified. Missing guide data, clock expiry,
and even native station `Ended` do not complete the TV airing or remove a manual
commitment. Independent program lifecycle evidence remains required.

The initial guide is partial. Missing stations/programs and unsupported guide
capability stay unknown and retry; they do not establish account-wide absence
or disable ordinary event discovery. Pagination and station-detail schema
support remain pending. Older players continue ordinary event playback but
cannot acquire these TV airings until the guide capability is installed.

## Device validation still required

The November 23 guide diagnostic returned 18 station rows and 157 schedule
entries, with 13 entitled station watch actions. Sportsnet Pacific's explicit
GTI resolved as an entitled `live_watch_now` station without invoking playback.
The captured current program was Real Kyper & Bourne, and the response contained
no WPT airing. The WPT behavior tests therefore use clearly synthetic airings;
they do not substitute for device playback acceptance.

These draft changes have not been permanently deployed. Actual station playback,
WPT guide matching during an airing, and sustained program-bound monitoring on
the target TV remain unverified. The proposed Prime-only idle-content chooser
is separate work and is not enabled here.

The November 23 device inspection confirmed WPT on the Sportsnet channel page
(`RUST_COLLECTIONS`), with `standard_linear_station_card` nodes in Continue
watching. The visible program was `S2026 E19 WPT Montreal (Pt. 4)`, marked
ON NOW, 12:00–1:00 PM, and included with Sportsnet+. Normal Find search returned
only unrelated movies/shows. This means station-card discovery is needed even
on the channel page; simply adding another search query is insufficient.
The filtered inspection contained display labels, not a verified playable
station/program identity. Those labels must not be converted into a guessed GTI.
