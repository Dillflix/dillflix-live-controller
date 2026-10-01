# Prime MediaSession probe v2.0.0 integration

Controller **0.11.0** bundles the supplied v2.0.0 source/APK and consumes schema 2. The five Java files and Android manifest are unchanged from the upload. The supplied signed APK is packaged at `controller/executor/assets/prime-media-probe.apk` and included in the Python wheel. The three public operations remain Play, token status and Cancel.

The probe reads Prime's published MediaSession metadata/state. It sends no playback commands, records no notification contents and requests neither accessibility nor network access. The separately ported accessibility collector continues to ground focus/navigation with its timing, channel, window and screenshot safeguards. These changes do not expand what Prime publishes about the contents of search-result cards.

## Upgrade and check

Update the controller checkout/image first:

```bash
git pull --ff-only
docker compose up -d --build
docker compose exec controller python -m controller.executor.probe verify-apk
docker compose exec controller python -m controller.executor.probe install --grant-listener-access
docker compose exec controller python -m controller.executor.probe check
```

Installation verifies the pinned artifact, uses `adb install -r`, grants listener access only with the explicit flag, and requests rebind through the source's no-display activity. It waits up to 30 seconds for readiness; each ADB command also has a finite timeout. Startup and Play never install or change permissions. `check` sends no keys, starts no UIAutomator reader and contacts no model. `verify-apk` is offline.

The supplied v2 APK verifies under v1/v2/v3 signature schemes. Its certificate matches the previous supplied APK: `b718b48891fa342f4b2ae40f6371d47746df25cb8208e4b759519a2e2ec853e2`. This supports an in-place update preserving app data/journals. Keep existing capture exports. A differently signed installation produces `probe_signature_mismatch`; the installer never uninstalls or clears data. Actual Fire TV installation remains untested here.

For local Python, omit the Docker prefix and use the project's environment with `SCREEN_ADB_SERIAL` exported, or pass `--serial YOUR_TV_SERIAL`. `.env` is not loaded automatically. `--adb-timeout` permits a command budget up to 60 seconds.

## Controller interpretation

`media_v2.py` strictly validates advertised v2 identity, checkpoint, health, completeness and acquisition fields. Invalid v2 cannot fall back to the token hash. Unversioned v1 remains a separate legacy adapter during rollout; setup reports `upgrade_required` instead of calling it v2-ready.

The adapter obtains the device boot clock, a service dump, up to 512 KiB from each rotating journal, then a final dump. Service UUID/connection epoch must agree across export; v2 does not require PID/start-ticks access. The final produced sequence defines the interval to inspect. A last observation still pending on disk, or a callback arriving after export, leaves that acquisition unverified until a later complete sample. If the newest checkpoint is already written but absent from the first export, one bounded follow-up export and final dump may resolve the race. Persistent gaps remain unverified; there is no unbounded retry loop.

| Fact | Rule |
| --- | --- |
| Session identity | Boot + service UUID + connection epoch + session-instance ID + agreeing runtime media ID + local revision. Token hash is diagnostic only. |
| Current collection | Fresh original snapshot interval; uncached, successful and complete acquisition; successful current registration; recent successful read/poll in this connection epoch. |
| Writer | Initialized/live, no closing/stalled/pending work, written checkpoint caught up to produced. Written high-water alone cannot establish continuity. |
| Export | Every needed instance-scoped sequence is present, valid, nontruncated and represented in retention metadata. Identical duplicates are tolerated; conflicting duplicates, foreign records and gaps cannot fill an interval. |
| Interruption | Changed identities, failure/loss counter increases, unavailable history, conflicting callback identity, pause/seek/stop and position discontinuities withdraw the prior binding. |
| Removal | Preserve last-known metadata/state and its original acquisition time as historical diagnostics. Never populate a current session from history or infer event completion. |

Callback payloads remain separate from later composite snapshots; identity and times are checked independently. Freshness uses the **start of snapshot acquisition**, not dump response time or wall-clock agreement. Snapshots remain non-atomic. Neither reported position nor poll/write timing proves decoded frames or measured live lag.

The token getter adds schema/build/service/session/epoch diagnostics, separate collection/journal health, `history_status`, produced/written sequences, loss counters and bounded problem/removal details. `probe_instance` remains a legacy PID diagnostic. No database migration is needed.

### Recovery after incomplete history

`baseline` means a first checked checkpoint for this service/connection and makes no earlier-history claim. `covered` means the interval since the preceding checkpoint was validated. `incomplete` or `unavailable` prevents continuity binding. `history_gap` marks unproven history, including pending export; inspect `problems` and `journal_health` for the reason.

A gap changes the local identity revision and withdraws the old association. Once collection/writing/export recovers, a **new** association requires fresh visual event/route identity and explicit live-playhead evidence. Historical failure counters remain visible; a successful later write cannot retroactively repair the old interval. Historical loss does not make a repaired service permanently unusable. The upstream standalone `check.py` flags any loss in the entire service lifetime; the integrated checker reports readiness at a new explicit checkpoint. Neither proves Android delivered every callback.

Healthy empty sessions mean ready observation infrastructure, not playback. Stable associated playback retains inexpensive runtime polling and periodic visual checks. Completion still requires scoped visual evidence under the existing controller policy.

## Build and validation

Requirements: Linux x64, Python 3 and Java 17+. Pinned dependencies are Android API 30/build-tools 30.0.3 and ECJ 3.37.0. Use a cache filesystem permitting native executable tools:

```bash
python3 android/prime-media-probe/build.py --cache /path/to/probe-build-cache
python3 android/prime-media-probe/test.py --ecj /path/to/probe-build-cache/ecj.jar
python3 -m unittest discover -s android/prime-media-probe/tests -p 'test_*.py' -v
```

Rebuild output is `android/prime-media-probe/dist/prime-media-probe.apk`, or `--output PATH`; the packaged supplied APK is never overwritten. `--keystore /private/path/development.p12` uses a locally held compatible development key. Without it, the cache gets a different development signer. Preserve your key privately. Custom installer artifacts require both `--apk PATH` and `--sha256 EXPECTED_HASH`.

The supplied source/APK hashes were verified. Rebuilding produced identical `classes.dex` and compiled manifest; the complete locally signed APK differs and is not bundled. All **19 JVM fault tests and 12 checker tests** were rerun successfully. Controller tests add schema/checkpoint/export faults, mixed journals, callback disagreements, removals, and Play/monitor/revalidation coverage. See [provenance.json](provenance.json), [validation.json](validation.json), [upstream-build-manifest.json](upstream-build-manifest.json) and [SCHEMA.md](SCHEMA.md).

No TV/ADB endpoint or model endpoint was available here. Run [the supplied Fire TV trials](VALIDATION.md#required-fire-tv-checks) and [controller acceptance](../../docs/executor-setup.md#validate-on-the-target-tv): upgrade/rebind, normal playback, pause/seek, removal, restart, permission loss, suspend and a full search-to-play/end path. Cursor-based export remains deferred; two-file exports are checked, not assumed atomic.
