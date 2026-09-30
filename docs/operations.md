# Backup, restore, and unattended operation

Version 0.6 adds complete SQLite snapshots and automatic history retention for the Ubuntu/Linux deployment. Playback and authoritative content-status services remain simulated.

## Back up a running installation

From your checkout on the Docker host:

```bash
mkdir -p backups
DILLFLIX_BACKUP="controller-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
docker compose exec -T controller python -m controller.ops backup "/tmp/$DILLFLIX_BACKUP"
docker compose exec -T controller python -m controller.ops verify "/tmp/$DILLFLIX_BACKUP"
docker compose cp "controller:/tmp/$DILLFLIX_BACKUP" "backups/$DILLFLIX_BACKUP"
```

Copy the resulting file to your normal backup storage. These commands are manual; this release does not schedule backups or rotate backup files. Use a new filename each time. Existing destinations are never overwritten.

For a local Python installation:

```bash
python -m controller.ops --database data/controller.sqlite3 backup backups/controller.sqlite3
python -m controller.ops verify backups/controller.sqlite3
```

SQLite's online backup API includes committed data still in WAL and produces one standalone file. The command verifies integrity, schema, JSON records, mode, and watch-plan references, then publishes atomically with owner-only permissions. Snapshot creation has a 60-second limit. Failure does not publish a partial file. Its JSON result includes path, schema, mode, table counts, bytes, and SHA-256.

Snapshots include the complete database: watch plan, configuration, catalog/team snapshots, undo history, command receipts, request history, status evidence, and simulator state. Keep `.env`, nginx configuration, application images, and external-service backups separately. Configuration export in the web interface remains a preference-transfer feature, not a full database backup.

## Restore offline

Choose the backup, stop the controller, and pass the file to a one-off container:

```bash
DILLFLIX_RESTORE="backups/controller-REPLACE-WITH-YOUR-TIMESTAMP.sqlite3"
docker compose stop controller
docker compose run --rm --no-deps -T controller \
  python -m controller.ops restore - --replace < "$DILLFLIX_RESTORE"
docker compose up -d
```

Standard input avoids host/container file ownership differences. The one-off container uses the same mode, environment, and persistent volume. Run the start command after restore succeeds; failures exit nonzero and explain the cause. Keep the existing `.env` and volume.

For a local installation, stop its API process and run:

```bash
python -m controller.ops --database data/controller.sqlite3 \
  restore backups/controller.sqlite3 --replace --mode demo
```

Use `--mode teamarr` for a Teamarr database; mode defaults to `CONTROLLER_MODE` when present. Restore refuses mode mismatches, unsupported schemas, malformed sources, and running controllers. `--replace` is required for an existing target; omit it for a new database filename. An existing target must also be a readable, supported controller database. For an unreadable target, preserve it and its SQLite sidecars for recovery and restore the verified backup to a fresh database path.

Before replacement, restore saves a complete rollback snapshot next to the target as `<database>.before-restore-<timestamp>-<id>.sqlite3`. The result includes that path. Neither restore nor retention deletes rollback files. The backup source remains unchanged.

Saved user data and history are preserved, with these runtime changes:

- Automation starts **paused**. Refresh the browser, review the watch plan, then select **Resume**.
- Pending requests become cancelled. Desired/observed playback and simulator observations are cleared.
- Revision and playback intent advance beyond both the backup and readable target's saved values. Existing command receipts remain historical receipts and are not executed again.
- Old leases are cleared, content-status checks become due, and simulated outages reset. Stored lifecycle facts retain their original timestamps.

A persistent filesystem guard prevents this release's controllers from opening the database during restore. Never delete the `.lock` file to bypass it. WAL is checkpointed before atomic replacement. Stop older controller versions too: their leases are an additional check, not a substitute for stopping their processes. These guarantees cover local filesystems and cooperating controller processes, not network filesystems or independent tools that ignore the guard.

## Automatic retention

Maintenance runs at startup and approximately hourly, independently of pause/resume. A database lease serializes cleanup; shutdown drains its writes before releasing the process guard.

| Setting | Default | Behavior |
| --- | --- | --- |
| `JOB_HISTORY_LIMIT` | 1,000 | Newest completed jobs per device; configurable minimum 50 |
| `COMMAND_HISTORY_LIMIT` | 10,000 | Newest command receipts per device; configurable minimum 100 |
| `CATALOG_RETENTION_DAYS` | 30 | Last-seen age for inactive, unreferenced catalog entries; minimum 1 day |
| Activity | 2,000 | Recent activity rows |
| Undo | 50 | Recent edits per device |

Defaults apply to existing `.env` files. After editing these values, recreate the service with `docker compose up -d`.

Safety references can exceed history limits. Cleanup preserves pending work, unacknowledged cancellation, current controller/simulator observations, the latest request, highest simulator intent evidence, and undo references. Every manual commitment remains, even after completion; removing it is a user action. Active catalog entries and the team directory remain too. Old inactive entries survive while referenced by a watch plan, undo snapshot, current playback, or outstanding work. Cleanup never manufactures event completion.

Simulator request/cancellation history is removed only after a strictly higher persisted intent permanently fences old work. Cancellation records with no provable device intent stay retained. A future external executor needs its own equivalent retention/fencing contract.

Command idempotency is guaranteed for retained receipts. After an older receipt is pruned, retrying its original body returns stale-revision **409** without applying it again. Refresh state and use a new ID for a new action. IDs are not reserved forever after leaving retention. Undo-associated receipts remain with their edit history.

Inspect maintenance:

```bash
curl -fsS http://127.0.0.1:8790/api/v1/maintenance
```

The response gives `state`, policy, last successful run, removed counts, and remaining counts as of that pass. Errors preserve the previous successful summary and add a sanitized error. Counts are not a hard quota: live references and unresolved recovery work take precedence. SQLite reuses freed pages; deletion need not immediately shrink the file. No automatic `VACUUM` or backup deletion runs.

## Repeat the accelerated recovery exercise

```bash
python -m controller.soak --days 30
```

Or, after building the image:

```bash
docker compose run --rm --no-deps -T controller python -m controller.soak --days 30
```

The runner creates and removes its own temporary database, ignores the configured deployment database, and makes no Teamarr/Fire TV calls. It advances schedule and evidence clocks through distinct daily catalogs; injects missing heartbeats, service outages, stale status and feed absence; changes manual selection during an outage; withdraws coverage; pauses/resumes; restarts during outages and pending navigation; and periodically backs up/restores. Assertions cover commitments, fresh live verification, a single pending intent, and historical storage. Reduced retention limits exercise cleanup frequently.

The local 30-day exercise passed with 2,940 ticks, 60 restarts, five restores, 135 requests, 15 handoffs, and 30 recovery requests. It removed 115 old jobs and finished with 20 jobs, 64 receipts (including undo references), and 39 catalog records under the test policy. This is accelerated behavior validation, not weeks of real-time memory/I/O monitoring or actual device verification.

## Target-host checks still required

The development environment has no Docker engine. On your host, build the image, confirm startup/maintenance health, create a backup, and test restore in a disposable installation with a separate database volume.

```bash
docker compose config --quiet
docker compose build
docker compose up -d
curl -fsS http://127.0.0.1:8790/api/health
curl -fsS http://127.0.0.1:8790/api/v1/maintenance
docker compose logs --tail=100 controller
```

Confirm watch-plan/priority persistence across a container restart, nginx/SSE behavior through the real proxy, timezone display, and physical mobile controls. Exercise the simulator with the household feed for an extended period and monitor memory, database size, refresh failures, and decision history. These deployment checks remain separate from connecting authoritative status and Fire TV services.
