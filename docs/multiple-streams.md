# Multiple Fire TV streams

One controller manages a shared watch plan and assigns different eligible events
to its configured streams. Each Fire TV has its own player socket, playback
attempts, recovery, screen and manual-control ownership. The existing primary
stream keeps the `living-room` ID and its history. Adding a stream does not create
a second controller database or a separate watch plan.

The normal upgrade copies the existing stream's plan to the new stream. If an
older database already contains conflicting independent plans, startup rejects
the configuration before changing those plans. Reconcile those records with an
explicit migration first; startup never chooses an arbitrary plan to overwrite
the others. The same check protects an existing shared plan from stale saved
device projections.

## Current device support

Prime Player currently requires available device root, its supported ARM32 native
runtime and matching Frida 17.2.17. ADB connectivity and an ARM32 ABI alone do not
establish player compatibility. The service installer does not root a stick.

The second stick identified during this work is `192.168.0.164:5555`, model
`AFTSS` / device `sheldon`, Android 9, build `PS7717.5741N`, incremental
`0036408028548`. Root, production runtime adaptation and direct live playback
have now succeeded on that stick. Its own Toronto/Montreal profiles and pairing
are saved, and VPN Bridge 8 is installed. Updating that bridge does not require
repeating pairing, profile import or app routing.

Controller deployment can proceed with this setup. Regional VPN playback and
simultaneous playback on the shared Prime account remain unverified on the
physical devices; those observations are deferred until eligible events are
available. The player only offers network targets with the required evidence.
Do not represent deferred checks as completed. The `karat` root tools must not
be run on `sheldon`; its model-specific root setup is separate and must be
available after a device reboot.

## Prepare the additional stream

Update the controller to the reviewed multi-device version. Use a separate player
checkout and virtual environment for the second stick so changes to the primary
service's editable installation cannot affect its running process. For example,
after fetching the reviewed player revision into the existing repository:

```bash
git -C "$HOME/dillflix-prime-player" worktree add --detach \
  "$HOME/dillflix-prime-player-stream-2" REVIEWED_PLAYER_COMMIT
python3 -m venv "$HOME/dillflix-prime-player-stream-2/.venv"
"$HOME/dillflix-prime-player-stream-2/.venv/bin/python" -m pip install \
  -e "$HOME/dillflix-prime-player-stream-2[device]"
```

The commit must include the multi-instance installer and Sheldon support. This
does not switch the primary checkout or restart its service.
Keep the existing deployment account, approved ADB identity, state directory,
Compose project and named database volume. The existing `.env` must name its
literal `PRIME_PLAYER_STATE_DIR`, `SCREEN_ADB_SERIAL`, and include
`compose.prime-player.yaml` in `COMPOSE_FILE`.

This command only validates and displays the proposed device registry. It does
not publish configuration or install, enable or start another player:

```bash
cd /appdata/dillflix-live-controller
sudo python3 tools/add-stream.py \
  --player-repo "$HOME/dillflix-prime-player-stream-2" \
  --id stream-2 --name 'Stream 2' \
  --serial 192.168.0.164:5555
```

The helper preserves existing registry rows. On the first addition it retains
`living-room`, obtains that stick's serial from the existing `.env`, and keeps
its `/run/prime-player/player.sock` endpoint. New streams use stable IDs and
`/run/prime-player/instances/ID/player.sock`. Duplicate IDs, serials, sockets,
existing service names and more than eight streams are rejected.

## Add the second player and activate the controller

Root, runtime and direct playback have been confirmed for this second stick.
Use its existing network configuration and the approved host ADB identity:

```bash
cd /appdata/dillflix-live-controller
sudo python3 tools/add-stream.py \
  --player-repo "$HOME/dillflix-prime-player-stream-2" \
  --id stream-2 --name 'Stream 2' --serial 192.168.0.164:5555 \
  --network-config "$HOME/.config/dillflix-prime-player/devices/stream-2/network.json" \
  --adb-key "$HOME/.android/adbkey" --activate
```

Use the [online database backup](operations.md#back-up-a-running-installation) before upgrading the
controller. The configuration backups below do not replace a database backup.

Activation backs up `.env` and any existing registry beside the originals with
a `before-add-stream-TIMESTAMP` suffix. Backups are owner-readable only. It
publishes the registry inside the existing player directory mount, sets
`CONTROLLER_DEVICES_FILE`, validates Compose, and installs/enables/starts only
`dillflix-prime-player@stream-2.service`. The registry is readable through the
existing shared group; credentials and unrelated `.env` settings are preserved.
Configuration is rolled back if validation or player installation fails; any
newly created instance unit is disabled and removed on installation failure.
If stopping that unit fails, the helper reports the failure and leaves its
service files available for inspection; the controller files are still restored.

The helper never restarts the primary player or controller. After successful
activation, apply the controller configuration explicitly:

```bash
cd /appdata/dillflix-live-controller
docker compose up -d --build controller
```

This recreates the controller container while preserving its database volume.
Its watch plan remains shared. Review each stream's state in the interface;
manual control and pause apply to the selected device. Existing consumers such
as piTicker retain the `living-room` device ID until configured otherwise.

## Observe readiness and verify isolation

```bash
sudo journalctl -u dillflix-prime-player@stream-2.service -f
```

The API socket is created before runtime recovery completes. Initialization,
adaptation and recovery backoff can take several minutes; a successful socket
connection is not evidence of ready Prime playback. Inspect current health
using the instance's socket after following the reported stages:

```bash
"$HOME/dillflix-prime-player-stream-2/.venv/bin/prime-player" \
  --socket "$HOME/.local/state/dillflix-prime-player/instances/stream-2/player.sock" \
  health
```

If `PRIME_PLAYER_STATE_DIR` is customized, use that directory in the health
command. If no eligible events are live, waiting is expected and does not require
repeating setup. When events are available, observe two different eligible live events, fresh native playback
evidence on both, independent manual takeover and independent network recovery.
Restarting the second player must leave the first stick playing. The services
share the host ADB server, so do not kill that server to restart one stream.

To exercise automatic recovery on a qualified stream that already has fresh
verified live playback and ready managed networking, target its controller ID
and matching serial explicitly:

```bash
cd /appdata/dillflix-live-controller
sudo -v
python3 tools/production_recovery_acceptance.py \
  --device-id stream-2 --serial 192.168.0.164:5555 \
  --interrupt-playback \
  --output "$HOME/prime-stream-2-recovery-$(date -u +%Y%m%dT%H%M%SZ)"
```

This deliberately stops Prime once on the selected stick, then observes recovery
without further device inputs. Preflight must match the configured stream,
player health serial and native playback session before injecting the fault.
The script captures that stream's database records, player journal and adaptation
directory. Use `--player-state /absolute/host/instance-directory` or
`--player-service custom-player.service` for nonstandard instance paths/units.
Omitting `--device-id` retains the legacy `living-room` target.

Plex publication/capture is a separate output integration. Adding a Fire TV does
not automatically provision a second capture path or Plex stream.

## Roll back configuration

Before the controller has started with the new registry, stop the added instance
and restore the exact `.env` backup and (if it existed) registry backup printed
by the helper. If the registry was newly created, restoring the prior `.env`
stops referencing it; retain the file for inspection.

After the controller has registered the stream in its database, keep its registry
row and pause the stream. The controller intentionally rejects a configuration
that omits a saved device, preserving attempts and history. Removing a registered
stream requires a reviewed database migration or restoration of a matching
pre-registration database backup as well as the configuration; restoring `.env`
alone is insufficient. Do not delete a device row directly.

If taking the second device offline, stop and disable only
`dillflix-prime-player@stream-2.service`. Do not run the original single-player
stack installer to remove an instance, and do not delete the database volume or
shared approved ADB key.
