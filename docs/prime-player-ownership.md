# Prime Player manual handoff

Requires Prime Player 0.1.0a5 / API 4 on the same host, with the same Fire TV serial as `SCREEN_ADB_SERIAL`. Prime Player owns runtime mutations and manual writes. The remote waits for its acknowledged cancellation barrier before enabling input. Keys, wake and bounded ASCII text use its receipt; screen mirroring is unchanged.

This is an opt-in manual gateway integration. It does not replace planner search/matching/playback with Prime Player. Use `PLAYBACK_ADAPTER=simulator` while testing the external service. The legacy `prime-video` executor is rejected when this socket is configured because it can independently mutate/stop Prime.

## Host process

Run both services as the same user, or explicitly grant the controller socket access:

```bash
export PRIME_PLAYER_SOCKET="$HOME/.local/state/dillflix-prime-player/player.sock"
export SCREEN_ADB_SERIAL=192.168.0.178:5555
export PLAYBACK_ADAPTER=simulator
```

Restart the controller with these settings. In its remote UI, Take control now waits for Prime Player acknowledgement. Releasing control drains input and releases only the matching durable manual session. Reconnecting does not automatically relinquish ownership. An unavailable/incompatible service blocks input instead of falling back to direct ADB.

## Docker Compose

The optional override mounts the socket directory. The container user must have directory traversal and socket read/write access; do not make the socket world-writable. For the repository Dockerfile's UID 10001, Linux ACLs can grant explicit access:

```bash
export PRIME_PLAYER_STATE_DIR="$HOME/.local/state/dillflix-prime-player"
setfacl -m u:10001:rx "$PRIME_PLAYER_STATE_DIR"
setfacl -m u:10001:rw "$PRIME_PLAYER_STATE_DIR/player.sock"
docker compose -f compose.yaml -f compose.prime-player.yaml up -d --build
```

Reapply the socket ACL after Prime Player recreates its socket. Docker deployment and physical input need target-host validation; automated tests use fake service/device transports.

## Validate

First run Prime Player's `validate-handoff --query "browns steelers" --send-wake --output handoff.tar.gz` against the configured socket. It cancels a running search, verifies acknowledged manual ownership, sends wake, resumes and rejects stale input. It never selects playback.

Then open the existing controller remote: take control during a search, confirm it becomes ready only after cancellation acknowledgement, send one directional key, and release. Try reconnecting while the manual session is still active. Failed acknowledgement must show an error and must not deliver input.

Physical remotes and unrelated ADB clients remain outside this boundary. An uncertain write is not replayed and blocks automatic handoff; resolve device state before explicitly reinitializing the service. No automatic restart is triggered by ownership errors.
