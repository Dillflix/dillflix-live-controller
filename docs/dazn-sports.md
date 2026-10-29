# National Squash League and Beatdown MMA

Update Teamarr first, then the controller. Teamarr imports the DAZN Canada
schedule as broadcast coverage using source `dazn_sports` and competitions
`nsl` (National Squash League) and `beatdown` (Beatdown MMA).

Discovery is enabled by default independently of the Settings game-league
checkboxes, as with tennis and golf coverage. No controller database migration
or saved preference changes are required; both streams use the existing shared
catalogue. In Teamarr's broadcast configuration, `dazn_sports.enabled: false`
disables the source; `dazn_sports.competitions` accepts `nsl` and/or `beatdown`.

Events filters display the competition names when listings are present.
Under Priorities, both competition choices are always available; choose
**Squash & MMA · DAZN** as the coverage source and optionally **Broadcast** as
the content type. Team rankings are unavailable without provider team IDs.
Beatdown listings represent whole cards, not individual bouts.

Original titles and opaque Teamarr IDs pass through to search and playback.
Prime entitlement and live availability are still required. DAZN and Prime may
have different broadcast start times. Missing end times remain unknown, and
source disappearance or an elapsed estimate does not complete a saved plan entry.

Use the existing deployment checkouts, `.env` and systemd service. Build the
controller image before `sudo systemctl restart dillflix-controller.service`;
the unit retains its configured Compose files. Verify the running container
and web endpoint after restart; `active (exited)` alone is not an application
health check. Prime Player does not need an update for this source.
