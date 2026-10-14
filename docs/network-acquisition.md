# Optional Prime network acquisition

Requires the coordinated Prime Player API 12 branch, Android VPN bridge, operator-provisioned OpenVPN for Android profiles/service credentials, and completed device qualification. Disabled by default. This controller never installs VPN software, receives credentials, sends ADB input, or lets a chooser model invent network routes.

Use the player's [versioned signed bundle and provisioning command](https://github.com/Dillflix/dillflix-prime-player/blob/feat/vpn-network-transitions/docs/vpn-provisioning.md)
for new, additional, or wiped sticks. Builds and signing run in CI. Keep a pinned
bundle and reusable profile recipe; pairing, profile UUIDs, and qualification
belong to each device installation. Each controller instance must use its own
player socket/state directory. Reprovisioning does not restore old network
contexts or automatically enable this policy. Pause controller automation during
installation and repeat player qualification after a wipe.

Mount a JSON policy file read-only into the controller container and set `PRIME_NETWORK_POLICY_FILE` to its container path. Use original Teamarr viewing-option IDs observed in diagnostics:

```json
{
  "enabled": true,
  "acquisition_timeout": 600,
  "max_transitions": 2,
  "access_ttl": 300,
  "routes": {
    "route:nhl:prime_video": {
      "targets": ["direct", "calgary"],
      "fallback_on": ["feeds_unavailable"]
    }
  }
}
```

Target IDs must name qualified player profiles, or `direct`. Every permitted Prime viewing option must have a policy; only targets and fallback reasons common to all those routes are allowed. The complete Teamarr snapshot and permitted viewing options remain unchanged. Configure `no_matching_feed` explicitly only if an absent catalogue entry should permit a location change. Subscription, language, model, runtime, unknown-readiness, and upcoming-event results do not authorize network rotation.

The controller first performs the usual same-network catalogue preflight. A configured fallback result allows a new durable playback intent with the acquisition deadline. The workflow searches the current verified network, then tries configured alternatives within its transition limit. Every actual change retires and restarts Prime through the player's managed operation. Catalogue and playback results must belong to the new session and network context. A lost transition response is inspected using the same recorded operation ID and deadline. Controller restart preserves attempted targets and the deadline. A planned replacement session is accepted only with matching completed-operation evidence.

Old network catalogue results and access exclusions expire or become invalid on network/policy changes. Acquisition does not mark a sporting event complete. Failure retains the watch plan. Pause, cancellation, manual input, and policy changes require settling submitted work; they never restore or disconnect a VPN automatically. Manual control remains unavailable while settlement is uncertain.

Enabling fallback authorizes playback interruption. Keep this feature disabled during initial player qualification. Validate direct → configured VPN → direct, Prime playback on each network, per-app routing, continued ADB/scrcpy access, manual handoff during transition, provider reconnect, and service/device restarts before activation. Player documentation contains setup and qualification commands. CI verifies host contracts and builds the APK; Fire TV behavior still requires hardware acceptance.

## Coordinated contract tests

On Linux with both feature branches checked out, install the player without device extras into the controller test environment and run `PRIME_CONTRACT_REQUIRED=1 python -m pytest -q`. That flag fails collection if the coordinated player is absent; the real Unix service, ownership, and HTTP client are exercised with a controlled runtime boundary. Private cross-repository checkout requires a separately configured read credential in CI. During branch development a dedicated `ci/network-integration` branch carries an exact source snapshot solely for that combined run; it is not a production dependency or a merge target.
