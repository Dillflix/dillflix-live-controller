# Prime MediaSession probe

This directory contains the supplied `dev.tvprobe.mediasession` source. `ProbeService.java`, `StartActivity.java` and `AndroidManifest.xml` are unchanged. The original signed APK is packaged at `controller/executor/assets/prime-media-probe.apk`, including in the controller's Python wheel and image build inputs. The build wrapper retains the original pinned Android/ECJ dependencies, uses streamed downloads, and writes a separate output APK.

The probe observes Prime's published MediaSession metadata, extras, queues and transport state. It does not issue playback commands, read notification content, use an accessibility service or request network access. Native focus labels still come from the separately ported UIAutomator collector. Both sources contribute to [runtime grounding](../../docs/runtime-grounding.md).

## Install or inspect

The controller never installs the probe or grants its notification-listener access automatically. Run this explicit setup once against an authorized TV, using the controller's existing ADB identity. Python commands below run inside an updated controller container; for a local checkout, omit `docker compose exec controller` and use the project's Python environment with `SCREEN_ADB_SERIAL` exported, or pass `--serial YOUR_TV_SERIAL`.

```bash
docker compose exec controller python -m controller.executor.probe verify-apk
docker compose exec controller python -m controller.executor.probe check
docker compose exec controller python -m controller.executor.probe install --grant-listener-access
docker compose exec controller python -m controller.executor.probe check
```

`verify-apk` is offline. `check` reads foreground/media dumps, the service snapshot, process identity and journal tails without launching the probe or sending keys. `install` verifies the pinned APK before device changes, uses `adb install -r`, optionally grants listener access **only with the explicit flag**, and starts the source's no-display activity to request a listener rebind. It waits up to 30 seconds for readiness after installation/rebind; each ADB command also has a finite timeout. Local commands accept `--adb-timeout` up to 60 seconds if device installation needs a longer command budget.

A ready result means a fresh connected service snapshot, a readable process identity and at least one parseable journal record. Zero Prime sessions is valid when Prime is not playing. Readiness does not establish correct event identity, live presentation, uninterrupted callbacks, rendered video or event completion. The original probe has no explicit journal-write/registration health fields; see the [proposed improvements](IMPROVEMENTS.md).

The adapter exports only this debuggable helper app's own journals through `run-as`; no root or access to Prime's private files is required. It reads at most 512 KiB from each of the current and previous journal files. A missing previous file is normal. Incomplete/oversized JSON records are discarded, and detected sequence gaps withdraw the current visual association. The service process must remain the same across acquisition, checked by PID/start ticks plus the device boot ID. If those process reads are unavailable on the deployment, structured binding stays unavailable; confirm with `check`.

No real TV or ADB executable was available for validating installation here. Follow [target-TV validation](../../docs/executor-setup.md#validate-on-the-target-tv), including rebind, permission loss, process restart and suspend/resume.

## Rebuild

Requirements: Linux x64, Python 3, Java 17+ including `keytool`, and access to the pinned Google/Maven downloads. Choose a persistent build cache on a filesystem that permits execution of Android build tools:

```bash
python3 android/prime-media-probe/build.py --cache /path/to/probe-build-cache
```

Output defaults to `android/prime-media-probe/dist/prime-media-probe.apk`; `--output` changes it. The bundled original APK is not overwritten. `.build/`, `dist/` and signing keys are excluded from Git. Preserve the cache's `development.p12` locally for subsequent updates of your own build. The wrapper uses the supplied development signing scheme; it does not contain the original signer's private key.

The integrated source was rebuilt with the pinned toolchain. Its `classes.dex` and compiled `AndroidManifest.xml` match the supplied APK byte-for-byte; hashes are in [validation.json](validation.json). The complete rebuilt APK differs because it is signed with a newly generated key and includes build-dependent archive/signing data. [provenance.json](provenance.json) records the supplied archive, source and original APK hashes.

For an explicitly chosen custom build:

```bash
python -m controller.executor.probe install --serial YOUR_TV_SERIAL \
  --apk /path/to/custom-probe.apk --sha256 ITS_PRINTED_SHA256 \
  --grant-listener-access
```

An existing installation with another signing certificate cannot be updated in place with that build. The installer reports `probe_signature_mismatch` and preserves the installed app and journals. It never uninstalls, clears data or attempts a signing workaround. Use the supplied APK for installations signed with the original certificate. A future modified-probe rollout needs either the original signing key retained by its owner or a deliberate installation migration with journal export first; private signing keys should not be uploaded to this repository.

## Source contract and limits

- Snapshot getters execute separately; `snapshotAtomic=false` is intentional. Callback payloads retain their own semantics and can differ from the accompanying snapshot.
- Journal records contain `sequence`, wall/device times, reason, payload and snapshot. The service dump has a fresh snapshot and timestamps but **no sequence**. Sequence resets on process restart while old files remain.
- `sessionToken` is the hexadecimal hash of the Android token, a process-local correlation marker. It is neither a globally unique session ID nor a content ID. The controller now scopes it to the probe process and boot, but only a probe change can remove hash-collision ambiguity.
- The poll runs every second but suppresses unchanged snapshots. Journal silence is not a one-second heartbeat failure. Each file rotates after roughly 8 MiB; one previous file is retained. Large individual records can exceed that threshold.
- `listenerConnected=true` alone does not prove callback registration, all session reads or disk writes succeeded. Whole-snapshot errors cannot bind; incomplete session identity cannot bind. More explicit health is proposed.
- Metadata is limited to what Prime publishes. In the reviewed capture, the display title was empty and the description title generic. Event/route matching still uses independently observed pixels; runtime identity maintains that association.
- Reported PLAYING/position and session disappearance cannot prove decoded frames, measured live lag or a completed event.

Android reference: [MediaSessionManager](https://developer.android.com/reference/android/media/session/MediaSessionManager). The source uses the enabled notification listener as its MediaSession access path. The manifest requires Android's binding permission for the exported listener service.
