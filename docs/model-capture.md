# Chooser captures for training research

The controller automatically saves model requests and responses for event matching
and broadcast selection. Captures stay local in the controller's existing SQLite
database and persistent Docker volume. No training or external upload occurs.

Schema 11 adds `chooser_captures`, keyed by model call ID. Calls are saved before
dispatch, updated after transport completes, and annotated after the controller's
selection validation. An interrupted process can leave a pending call. These records
survive restarts, routine retention, and ordinary database backup/restore. They have
no reference to an expiring executor report or catalogue probe.

The records retain the bounded, credential-redacted request body, response envelope,
model and sampling settings, output schema, call/context identity, timing, transport
outcome, and validation outcome. Truncation is explicit. JSON is redacted before
truncation; malformed or non-JSON bodies that cannot be safely scrubbed are omitted
with an explicit marker. Validation means the answer
passed the controller's schema and supplied-evidence checks; it does not establish
that the choice was correct or that playback succeeded.

The capture writer uses a separate short transaction with at most 100 ms of SQLite
lock waiting and does not run planner or UI transaction hooks. Write failures log a
sanitized warning and appear in runtime capture health; they do not prevent playback.
A failed write may be recovered by a later update of the same call, but collection
is not guaranteed when storage is unavailable. Runtime error counters reset at
controller restart; saved calls and their timestamps persist.

## Status and export

Run these commands on the controller host. They read the configured
`CONTROLLER_DATABASE`, require no restart, and do not navigate or interrupt playback.

```bash
cd /appdata/dillflix-live-controller
docker compose exec -T controller python -m controller.model_capture status

docker compose exec -T controller python -m controller.model_capture export \
  > "chooser-captures-$(date -u +%Y%m%dT%H%M%SZ).jsonl"

docker compose exec -T controller python -m controller.model_capture export --format messages \
  > "chooser-review-seeds-$(date -u +%Y%m%dT%H%M%SZ).jsonl"
```

The default export contains every retained call, including errors, rejections,
cancellations, and incomplete calls. `--format messages` includes only completed,
locally validated calls with untruncated JSON requests/responses, no changes from
credential redaction, and one bounded
assistant answer. It emits prompt/answer messages plus model, configuration,
validation, and call metadata, marked `review_status: unreviewed`. These are review
seeds, not an automatically approved training set or provider upload format.

The status command reads stored counts and the most recent capture time. Runtime
write failures are available in the controller's diagnostics and service logs;
a separate CLI process does not share the running process's error counters.

Both exports stream a consistent read-only database snapshot and leave stored
captures untouched. An explicit path can precede the command:

```bash
python -m controller.model_capture --database /path/to/controller.sqlite3 export
```

## Retention and review

Capture retention is intentionally indefinite; ordinary history cleanup does not
delete examples. Each stored call is bounded to 4 MiB in addition to the transport's
request/response limits. Total storage and backup size therefore grow with model
usage. Monitor the existing persistent volume and retain normal database backups.
Export does not delete records. Restoring an older backup restores that backup's
capture collection, so export recent evidence before rolling the database back.

Before training, review accepted and rejected decisions, remove duplicates, separate
evaluation examples by event, and choose the desired prompt/output contract. A
teacher's valid response is still a prediction. Transport errors and invalid output
remain available for diagnosis and evaluation rather than being silently discarded.
