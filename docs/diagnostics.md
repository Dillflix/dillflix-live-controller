# Playback diagnostics and boot recovery

Activity now shows the controller state, executor availability, current job progress/deadline and last job error. Manual-control cancellation failures are retained on the ownership barrier and explained in the remote. Loss of verified playback produces an Activity entry; it does not prove the sports event ended.

Use **Activity → Export diagnostics** during an incident, before restarting either service. The device-scoped JSON contains up to 1,000 Activity entries, 100 controller/executor jobs, 1,000 executor action records, retained search/matching/launch/status evidence, cancellation and stop receipts, manual handoff, controller worker status and a read-only Prime Player health snapshot. Job tokens, request IDs, service session IDs and attempt IDs correlate the records. Capturing this does not search, query native playback, stop playback or acquire input ownership.

Prime Player 0.1.0a7 additionally supplies its last 1,000 completed RPC records and currently running RPCs with elapsed durations. This history is bounded to the current player process and omits search strings, handles and request bodies. The export works with older/unreachable players and records which collection failed. Database collection precedes service collection; timestamps describe each snapshot rather than claiming an atomic cross-service snapshot.

Credentials/configuration and full catalog/request snapshots are excluded. Content titles, searches in retained controller matching evidence, and device identifiers can be present. This is a support bundle, not an exhaustive export of host journal, Docker stdout or Frida debug output. Those logs remain separate:

```sh
sudo journalctl -u dillflix-prime-player -u dillflix-controller --since '30 minutes ago' --no-pager > dillflix-service.log
docker compose logs --since 30m --no-color controller > dillflix-controller.log
```

The service journal is available after supervised installation. Existing foreground output remains in the operator's `service.log` if launched through `tee`.

## One-time supervised installation

Capture incident evidence first. Stop the existing foreground player once with Ctrl+C; the installer deliberately refuses to replace a live service's socket. Update both checkouts and install the player package:

```sh
cd "$HOME/dillflix-prime-player"
git pull --ff-only
.venv/bin/python -m pip install -e '.[device]'
cd /appdata/dillflix-live-controller
git pull --ff-only
sudo python3 tools/install-prime-stack.py \
  --player-repo "$HOME/dillflix-prime-player" \
  --serial 192.168.0.178:5555
```

Requires Linux systemd, Docker Compose, an existing controller `.env` with Teamarr settings, a provisioned player venv and Frida server. The installer preserves existing credentials, persists the Compose overlay/socket directory/group, builds the controller once, and enables both boot services. Existing database volumes are retained. Installation restarts Prime and the controller; schedule it when interrupting playback is acceptable.

The player restarts after crashes or latched runtime failure, retrying every 30 seconds. Socket creation reapplies shared-group permissions; abandoned sockets are recovered only after an exclusive owner lock and inactive socket check. The controller starts its existing Compose images at boot and Docker handles container restarts. No venv activation, exported environment variables, per-socket ACL commands or foreground terminal are needed after installation.

```sh
sudo systemctl status dillflix-prime-player dillflix-controller --no-pager
docker compose exec controller python -m controller.prime_player.check
```

Physical-device reboot, runtime failure recovery and socket replacement acceptance still require testing on the deployment host.


The export also includes `catalog_status`: the stored Teamarr status/start time,
feed receipt time, permitted routes, and any independent status observation or
lookup error. Together with `device.failures` and controller jobs, this explains
selection eligibility and “Retry pending”. The latter means playback retry backoff
(5 seconds after the first failure, 15 after the second, 300 thereafter), not an
event lifecycle state. Upload a new export while the symptom is visible; an older
overview cannot explain a later failure.
