# Optional Prime network acquisition

Requires the coordinated Prime Player API 12 branch, Android VPN bridge, operator-provisioned OpenVPN for Android profiles/service credentials, and completed device qualification. Disabled by default. This controller never installs VPN software, receives credentials, sends ADB input, or lets a chooser model invent network routes.

Use the player's [versioned signed bundle and provisioning command](https://github.com/Dillflix/dillflix-prime-player/blob/feat/vpn-network-transitions/docs/vpn-provisioning.md)
for new, additional, or wiped sticks. Builds and signing run in CI. Keep a pinned
bundle and reusable profile recipe; pairing, profile UUIDs, and qualification
belong to each device installation. Each controller instance must use its own
player socket/state directory. Reprovisioning does not restore old network
contexts or automatically enable this policy. Pause controller automation during
installation and repeat player qualification after a wipe.

## Configure in Settings

Open Settings / VPN location policies. Refresh the player inventory, add Toronto
and Montreal, and list permitted team cities for each. Enable fallback and save.
Policy edits use device revisions, idempotent commands, undo, configuration
export/import, and the persistent database. No Compose configuration is required.
A saved UI policy overrides the legacy environment policy file, including when
explicitly disabled. Installations without a UI policy retain legacy behaviour.

Toronto might permit Toronto, Hamilton and Ottawa; Montreal might permit Montreal.
Matching uses either team's structured city, normalizing accents, case and spaces.
It never guesses from the title, venue or team name. Missing cities permit only
current and direct. The current network is always tried first, even without a
matching city policy. On failure, try matching VPN locations in UI order, then
direct. Direct is always allowed. Already checked targets are skipped.

Fallback allows absent, unavailable, unentitled, or confirmed language-rejected
feeds. Upcoming feeds and unknown/parser/model/runtime errors do not trigger
switches. Each location gets fresh search and broadcast selection: providers and
entitlement are never copied from another network context. Acquisition diagnostics
retain matched cities and per-location outcomes.

Defaults allow a shared 30-minute deadline and at most four switches to accommodate
multi-minute Prime startups. These are ceilings; unrelated locations are never
visited. Successful playback stays on its current network. The player still
enforces qualification, which is shown separately from playback success in Settings.

The controller first performs the usual same-network catalogue preflight. A configured fallback result allows a new durable playback intent with the acquisition deadline. The workflow searches the current verified network, then tries configured alternatives within its transition limit. Every actual change retires and restarts Prime through the player's managed operation. Catalogue and playback results must belong to the new session and network context. A lost transition response is inspected using the same recorded operation ID and deadline. Controller restart preserves attempted targets and the deadline. A planned replacement session is accepted only with matching completed-operation evidence.

Old network catalogue results and access exclusions expire or become invalid on network/policy changes. Acquisition does not mark a sporting event complete. Failure retains the watch plan. Pause, cancellation, manual input, and policy changes require settling submitted work; they never restore or disconnect a VPN automatically. Manual control remains unavailable while settlement is uncertain.

Enabling fallback authorizes playback interruption. Keep this feature disabled during initial player qualification. Validate direct → configured VPN → direct, Prime playback on each network, per-app routing, continued ADB/scrcpy access, manual handoff during transition, provider reconnect, and service/device restarts before activation. Player documentation contains setup and qualification commands. CI verifies host contracts and builds the APK; Fire TV behavior still requires hardware acceptance.

## Coordinated contract tests

On Linux with both feature branches checked out, install the player without device extras into the controller test environment and run `PRIME_CONTRACT_REQUIRED=1 python -m pytest -q`. That flag fails collection if the coordinated player is absent; the real Unix service, ownership, and HTTP client are exercised with a controlled runtime boundary. Private cross-repository checkout requires a separately configured read credential in CI. During branch development a dedicated `ci/network-integration` branch carries an exact source snapshot solely for that combined run; it is not a production dependency or a merge target.

## Unavailable but entitled live feeds

A positively matched LIVE + ENTITLED feed marked UNAVAILABLE may be selected for
one fresh playback trial after language and route checks. Equivalent available
feeds are preferred. Catalogue readiness permits an attempt; only the player's
verified live outcome establishes playback. A definitive refusal or settled
failure records the attempt and continues the permitted location plan. Uncertain
outcomes do not authorize another launch or a VPN switch. Failed trial evidence
and visited locations survive controller restart. No fallback marks the event ended.
