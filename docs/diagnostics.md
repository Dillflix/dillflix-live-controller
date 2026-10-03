# Playback diagnostics and boot recovery

Activity now shows the controller state, executor availability, current job progress/deadline and last job error. Manual-control cancellation failures are retained on the ownership barrier and explained in the remote. Loss of verified playback produces an Activity entry; it does not prove the sports event ended.

Use **Activity → Export diagnostics** during an incident, before restarting either service. The device-scoped JSON contains up to 1,000 Activity entries, 100 controller/executor jobs, 1,000 executor action records, retained search/matching/launch/status evidence, cancellation and stop receipts, manual handoff, controller worker status and a read-only Prime Player health snapshot. Job tokens, request IDs, service session IDs and attempt IDs correlate the records. Capturing this does not search, query native playback, stop playback or acquire input ownership.

Prime Player 0.1.0a7 additionally supplies its last 1,000 completed RPC records and currently running RPCs with elapsed durations. This history is bounded to the current player process and omits search strings, handles and request bodies. The export works with older/unreachable players and records which collection failed. Database collection precedes service collection; timestamps describe each snapshot rather than claiming an atomic cross-service snapshot.

Credentials/configuration and standalone full catalog/request snapshots are excluded; captured model prompts include the source data actually sent to the model. Content titles, searches in retained controller matching evidence, and device identifiers can be present. This is a support bundle, not an exhaustive export of host journal, Docker stdout or Frida debug output. Those logs remain separate:

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

Model HTTP failures in 0.14.4 retain the provider JSON error message, parameter,
code and type in the existing job error and export. Collection is capped at 16 KiB
and individual fields at 1,200 characters; configured API keys and common bearer/key
patterns are redacted. Arbitrary non-JSON bodies and other response fields are omitted.
Provider messages can still contain quoted model names or input excerpts. Existing
failures cannot be enriched retroactively: a subsequent request must capture the response.

Starting with 0.14.6, strict model requests omit `maxLength` from the wire schema
because the deployed backend failed to initialize its grammar with these bounds.
`json_schema` and `strict: true` remain enabled, as do nullable types, required
fields, `additionalProperties: false`, `minLength` and `maxItems`. The original
local model retains every length ceiling and rejects overlength answers.


## Model request and response evidence (0.14.7)

Each exported `playbacks[].workflow.model_calls[]` record belongs to that playback's
`token` and `request_id`. It includes a call ID, start/finish timestamps, duration,
HTTP status, outcome, and the actual model request body: model name, messages with
full prompts, sampling options, token budget, and transmitted response schema.
Responses include the provider envelope and returned assistant content, or error
body. Valid bodies use `request.json` / `response.json`; non-JSON or cut bodies use
`text`. Bodies are captured before local output/selection validation, so rejected
answers remain available. `returned` means transport decoding succeeded, not that
matching or playback succeeded; consult the operation error and selection audit.

The request is persisted before waiting for inference. A process crash can leave
`pending`; this is not proof the provider never received the request. Cancellation
and timeouts retain the request and any acquired response. No response is invented
when no HTTP response arrived. Evidence survives controller restarts under existing
executor-job retention; up to three calls are retained per token, with the export's
existing 100-job limit. Request bodies are capped at 1 MiB, success responses at
512 KiB, and HTTP-error bodies at 16 KiB, with explicit truncation flags.

Authorization headers are never stored. The configured model API key, common bearer
and key patterns, and structured credential fields are redacted before storage.
Prompts can contain event titles and source metadata; responses can quote them.
Only calls made after upgrading are captured. Deterministic selection makes no
model call and therefore produces no model-call record. Standalone CLI test scripts
outside a playback workflow do not write into playback exports.
