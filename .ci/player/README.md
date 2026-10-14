# dillflix-prime-player

An async Python SDK and persistent host service for searching Prime Video on a Fire TV and launching content through the application's own resolver. The initial adapter packages the validated ARM32 proof of concept.

**Status: `0.1.0a31`, alpha.** Search, broadcast selection, live playback and native progress have device evidence on Prime 116634. Supervised operation now adapts to resource updates within the supported ARM32 engine family, retains compatibility knowledge across restarts, and reports recovery through the API. The new discovery and automatic recovery paths have host tests; Fire TV acceptance is still required. See [compatibility and its limits](docs/compatibility.md).

## What is included

- `search(query)` returns rich catalogue items without navigating: full titles, content IDs/types, subscription entitlement/messages, event state, UTC start/end times, named artwork including provider logos, native actions and retained metadata. It makes no per-item resolver calls. API 10 changes the search contract; upgrade SDK/service/consumers together. `discover()` retains page tiles as `BrowseResults`.
- `resolve(tile_or_gti)` returns fresh account-specific action and entitlement metadata without launching playback. Details-page responses are valid results; they do not establish why access is restricted.
- `inspect_page()` and `resolution_evidence(request_id)` provide bounded diagnostic evidence from the existing service. See [access/readiness capture commands and playback impact](docs/access-captures.md).
- `play(tile)` or `play(content_id)` resolves fresh metadata through initialized application clients. Live is the default; replay requires `mode="replay"`.
- Replacing active playback validates the new target, requests one exit from the current player, then waits for confirmed idle before launching. An unconfirmed stop blocks the new launch. See [replacement playback](docs/ownership.md#replacement-playback).
- Playback verification requires a matching destination and native player ID, plus fresh advancing position samples in the same playback session. If Prime omits a new `Playing` event, session-bound native progress can establish playback; explicit pause, buffering, stop and error states take precedence. Startup retains a 20-second observation window.
- `playback_status(attempt_id=...)` inspects playback now, even after the initial attempt has completed. It reports explicit unknowns, retained state-event age, fresh native samples, and whether the playback session still matches.
- A persistent Unix-socket service owns the Frida session. Closing a Python client does not stop playback. Operations are serialized; competing requests receive `BusyError`.
- Startup execution hooks are disabled after initialization. Two fixed native gates remain; transfers and expensive inspections occur on request. There is no automatic tile selection, OCR, or screenshot-based navigation.
- Steady polling sends compatibility inventories only when they change. Passive memory counters and a [memory capture command](docs/memory-capture.md) help investigate long-running memory growth without adding playback inspections.

Explicit broadcast resolution and a [read-only broadcast metadata capture](docs/broadcast-capture.md) support investigation of language variants. Language preference and automatic broadcast selection belong to the controller; unlabeled feeds are not reported as confirmed English.

This release exposes the search → identifier → playback path. General page/focus/input APIs and graph capture are not public SDK methods yet.

## Requirements

- Linux host, Python 3.10+, ADB, and an already connected Fire TV.
- Existing device root usable by ADB or `su -c`. The library does not obtain root or reboot the device.
  Manual Ghostlock launchers, stock payload source, OTA commands, and permanent-root research live under [device maintenance](tools/device/README.md). These tools are separate from service startup.
- Prime signed in normally with access to the requested content. No separate probe APK is required.
- Frida **17.2.17**, host and server matching, **Android ARM32** server binary.
- Validated runtime **`lrc-1.0.116634.0`**, Ignite build **`da7bc38ad2bca142`**, and native test-runtime build **`ba2bf7df0932b61cb217df87e04770d84b4da855`**. See [compatibility](docs/compatibility.md).

The distribution contains instrumentation and fingerprints. It does not contain a Prime APK, Amazon JavaScript bundles, or native runtime binaries.

## Install and start

```bash
git clone https://github.com/Dillflix/dillflix-prime-player.git
cd dillflix-prime-player
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[device]'

adb connect 192.168.0.178:5555
prime-player --socket "$HOME/.local/state/dillflix-prime-player/player.sock" serve \
  --serial 192.168.0.178:5555 \
  --server-binary /absolute/path/to/frida-server-17.2.17-android-arm \
  --restart-prime --supervised
```

Supply the decompressed official Frida server binary already used with the experiments. The service checks its ARM ELF header and version, creates a private loopback device server and ADB forward, and tracks that process's identity for cleanup. Use `--root-prefix 'su -c'` if root command auto-detection needs overriding. Root through a custom helper can also be supplied as a command prefix that accepts one shell-command argument.

Start this in a dedicated terminal. Startup restarts Prime and stops existing playback. Allow up to roughly three minutes for gated activity launch and controller readiness; wait for `Controller ready`. Ctrl-C or SIGTERM shuts down the service, **stops its Prime process**, then detaches instrumentation. Safe detachment while leaving Prime playing has not been validated. Closing a client or its terminal is independent of the service.

An existing dedicated Frida endpoint can be used with `--endpoint HOST:PORT --exclusive-server` instead of `--server-binary`. Do not share that server with another spawn-gating experiment. With this option the caller owns server and forwarding cleanup.

## Python API

```python
import asyncio
from pathlib import Path
from dillflix_prime_player import PrimePlayer

async def main():
    socket = Path.home() / ".local/state/dillflix-prime-player/player.sock"
    async with PrimePlayer(socket) as prime:
        results = await prime.search("browns steelers")
        for item in results:
            print(item.title, item.entitlement_status, item.event_state,
                  item.starts_at, item.ends_at, item.content_id)

        matches = results.matching(title="Steelers vs. Browns", entitlement_status="ENTITLED")
        if len(matches) != 1:
            raise RuntimeError("Choose a unique result using the returned metadata")
        candidate = matches[0]
        if candidate.content_id is None:
            raise RuntimeError("This result has no content ID")

        attempt = await prime.play(candidate, mode="replay")
        print("attempt:", attempt.id)
        outcome = await attempt.wait_for_playing(timeout=150)
        print(outcome.state, outcome.requested_id, outcome.resolved_id, outcome.reason)
        current = await prime.playback_status(attempt_id=attempt.id)
        print(current.state, current.is_playing, current.current_content_id)
        # Client context exit leaves the service and playback running.

asyncio.run(main())
```

`matching()` filters returned metadata locally. Entitlement and timing guide selection and retries; `play()` performs the final fresh resolver authorization check. Catalogue action targets describe UI behavior and are not a playback gate.

`search()` uses the authenticated Find data path without changing the app page or current playback attempt. Default timeout is 45 seconds (range 5–120). It returns one initial result window with explicit `has_more`/`complete` coverage, preserves provider artwork and metadata, and never falls back to navigational search. Device research verified both queries during advancing playback; the production SDK/service integration has host tests and a separate [device acceptance command](docs/search-research.md#public-catalogue-search-acceptance). See [API semantics](docs/api.md).

## CLI

In another terminal with the same virtual environment:

```bash
prime-player --socket "$HOME/.local/state/dillflix-prime-player/player.sock" health
prime-player --socket "$HOME/.local/state/dillflix-prime-player/player.sock" search 'browns steelers'

# Use a content_id returned by your current search.
prime-player --socket "$HOME/.local/state/dillflix-prime-player/player.sock" play \
  amzn1.dv.gti.YOUR_CONTENT_ID --mode replay --wait
```

`attempt ATTEMPT_ID` retrieves or advances verification for an existing attempt. `suspend` pauses automatic operations for manual control and invalidates search handles; `resume` enables them again. A play acknowledgement is not evidence of actual playback. Always inspect the outcome.

## Validation

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
node tests/test_runtime.cjs
node tests/test_native_status.cjs
python -m pip wheel --no-deps --no-build-isolation . -w dist
```

The fixture contains sanitized search collection records from capture `20261003T005207Z-cbcf75`: 31 loaded tiles, eight in Top Sports. It retains unresolved labels and conflicting artwork associations. Runtime tests exercise fresh resolution, asynchronous cancellation, entitlement/expiry checks, and explicit live/replay policy. Service tests use a fake backend; they do not connect to a Fire TV.

For the first installed-device check, follow [acceptance](docs/acceptance.md). No automatic restart or replay is attempted after an uncertain result.

The bundled [`prime-player validate` harness](docs/validation.md) runs bounded search/play/status checks and produces a `.tar.gz` archive, including partial results on failure. Playback requires `--play`. Update and restart an existing service before using the new API.

Startup uses a separate 120-second Frida connection budget (`serve --connection-timeout 120`, configurable 5–120; service override `PRIME_PLAYER_CONNECTION_TIMEOUT`). Only read-only system-parameter queries that hit Frida’s transport timeout are retried within that budget; other failures are immediate. Failed startup prints a `STARTUP CAPTURE ARCHIVE` containing bounded server/ADB diagnostics, even when the API harness cannot connect.

### Runtime compatibility

`await prime.compatibility()` and `prime-player compatibility` report independent engine, JavaScript, native inspection and behavior decisions. `health().capabilities` lists implemented API methods; `health().compatibility.capabilities` reports what this running session can safely use.

```bash
prime-player --socket "$HOME/.local/state/dillflix-prime-player/player.sock" compatibility
prime-player --socket "$HOME/.local/state/dillflix-prime-player/player.sock" compatibility --output runtime-evidence.tar.gz
prime-player analyze-adapter /path/to/decoded/chunk.js > adapter-candidates.json
```

With `serve --supervised` (enabled by the systemd installer), updates revoke stale commands, discover initialized interfaces and derive inspection-library identities automatically. A controlled restart is used when initialization must change; existing playback can be interrupted, and the controller reevaluates its current plan under a new session. Usable loaded capabilities are retained, and manual control suspends recovery. Native engine injection still requires the exact supported architecture and executable fingerprints. See [compatibility](docs/compatibility.md) and [supervision](docs/supervision.md).

### Ownership and cancellation

`cancel()`, attempt-scoped `stop()`, and acknowledged `suspend()`/`resume()` now share a service/runtime ownership fence. The optional web gateway sends manual input through that owner. Run `prime-player validate-handoff --query "browns steelers" --send-wake` for a finite device test with an archive. See [ownership semantics, stop evidence and gateway setup](docs/ownership.md).



Automated device lifecycle testing: [bounded baseline/instrumented runs](docs/device-testing.md) collect continuous device logs, search/handoff evidence and optional playback/stop acceptance. Host CI runs both unittest and pytest tests. Device acceptance must be reported separately from host results.

Background-search research uses a [separate capture tool](docs/search-research.md)
for model/client and native-boundary evidence. Its debug probe is excluded from
the installed runtime; captures own a temporary session and interrupt playback.

### Tile lock indicator (API 9)

`search()` and `discover()` return `is_locked`: `true` for the captured shopping-bag
overlay, `false` for recognized card compositions without it, and `null` for
missing, unrecognized, or conflicting evidence. This describes the tile's visible
access indicator, not the account's subscription inventory. Full artwork URLs and
resolver `action` / `entitlement` metadata are preserved. Discovery keeps lock
state on each occurrence; disagreements yield a null aggregate. An unlocked tile
is not necessarily ready to watch. No additional device navigation is needed.
