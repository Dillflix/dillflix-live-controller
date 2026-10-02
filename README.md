# Dillflix Controller

A self-hosted live-sports planner with a responsive web interface. It reads the unified feed from **Dillflix/teamarr**, maintains an ordered watch plan, chooses live content from configurable priorities, and prepares durable playback requests.

**Version 0.9 adds an integrated Prime Video executor: Play, status by token, and Cancel, with durable jobs, LLM-powered navigation, playback/completion evidence, and the same input gate as manual control.** The default remains simulated; real playback is opt-in with `PLAYBACK_ADAPTER=prime-video`. Start with [executor setup](docs/executor-setup.md) and the [implemented API guide](docs/executor-api-handoff.md). Automated tests use controlled ADB/model boundaries; actual Fire TV, account, inference-service, Docker/nginx and mobile compatibility require target-host validation.

**Version 0.9.1 integrates the exploration's native accessibility collector:** separate focus channels, action timing, Prime window-burst handling, screenshot association and stale-input checks. Native metadata accompanies actor screenshots; the visual observer remains separate. Full search-to-play behavior on the target TV is still unvalidated. See the [evidence audit and implementation status](docs/accessibility-critical-path.md).

**Version 0.10.0 uses runtime evidence in navigation and monitoring:** complete multiline native records from both output streams, observed menu order with label-confirmed movement, separate result/menu and Watch Live activations, and visual identity associated with structured MediaSession telemetry. Stable playback monitoring avoids per-poll screenshots/inference. See the [grounding policy and capture evidence](docs/runtime-grounding.md) and [media-probe dependency](docs/executor-setup.md#structured-media-probe). Target-TV autonomous reliability remains unvalidated.

**Version 0.10.1 packages the supplied MediaSession probe source and original signed APK**, with explicit install/readiness commands, a verified source rebuild, and controller safeguards for probe restarts and journal gaps. See [probe setup](docs/executor-setup.md#structured-media-probe) and the [prioritized probe improvements](android/prime-media-probe/IMPROVEMENTS.md). Installation on the target TV remains untested here.

**Version 0.11.0 integrates the supplied probe v2.0.0 and migrates the controller to its explicit service/session identities, connection epochs, acquisition times, collection/write health and sequence checkpoints.** The supplied APK has the same verified signing certificate as v1. See [upgrade and diagnostics](docs/executor-setup.md#structured-media-probe). Real Fire TV validation remains outstanding.

## What works

- Opt-in Prime Video playback with package-scoped ADB search, configurable JSON/TVTheseus actor, independent vision observer, durable tokens, active cancellation, completion checks and read-only diagnostics.

- **Take control** with a selectable duration (including 4 hours, up to 24 hours), mobile D-pad, focused keyboard shortcuts, and text entry. Extend the session, resume automation, or finish with automation paused. Sessions survive restarts; other browsers must explicitly take over. See [manual control](docs/manual-control.md).
- An optional live device-screen panel with inline phone video, fullscreen, automatic reconnect, shared capture across viewers, and cleanup when the panel or tab is hidden. See [screen setup](docs/screen-mirroring.md).
- Events page with live/upcoming views, league filters, search, structured team names, provider logos, event details, and manual selection.
- A persistent watch plan. The first eligible manual entry wins an overlap; the others resume while still live. Play now moves an event to the front without deleting other commitments.
- Ordered priority rules for leagues, season stages, teams, content kind, and coverage sources, all editable in the UI. Team rankings break ties within a rule.
- A persistent, searchable team directory. Rank preferred teams even when they have no event in the schedule window; unranked teams tie.
- Durable undo for watch-plan, priority, team-ranking, and settings edits, including after a restart.
- Online SQLite backups, integrity verification, and guarded offline restore with a rollback copy and automation paused.
- Automatic history retention that preserves commitments, undo, current playback, and unresolved recovery work.
- Configuration export and reviewed import, with validation, cross-mode warnings, and protection against overwriting newer edits.
- Minimum viewing time, automatic-switch cooldown, same-priority switching, and timezone settings.
- Separate desired and observed playback, pause/resume, decision history, and side-effect-free selection/conflict previews.
- Versioned SQLite migrations, optimistic concurrency, command idempotency, a durable request queue, and a coordinator lease.
- A separate persistent playback simulator with idempotent delivery, restart reconciliation, cancellation retries, and navigation deadlines. Acknowledgement alone does not verify playback.
- Validation of observed content, request, device, intent, permitted option, live presentation, and freshness. Expired evidence becomes unverified without completing the event.
- Independent status checks for desired playback, observed playback, and every reservation, including entries outside the discovery window. Status evidence and retry state survive restarts; status health is separate from schedule health.
- Same-event coverage handoff when a route is withdrawn or its playback locator changes, preserving the watch plan and viewing timers. Every currently permitted option is sent again.
- Durable playback recovery grace, increasing retry delays, and service-outage probes. Reconnection rechecks the latest manual intent before navigation resumes.
- Demo scenarios for overlaps, overtime, delays, route failures, navigation timeout, rejected replay results, coverage handoff, service/status outages, stale status, and an empty live schedule.
- Teamarr pagination, expired-cursor recovery, atomic catalog replacement, and retention of the previous complete catalog when an HTTP refresh fails.

The controller never offers replay or start-over. An expected end time is only a planning estimate. It never completes a manual commitment because an estimate elapsed, an entry disappeared from the feed, or playback temporarily failed.

Rendered screenshots: [desktop](docs/screenshots/desktop.png) and [phone](docs/screenshots/phone.png).

## Run the demo with Docker

Clone the repository and start the demo:

```bash
git clone https://github.com/Dillflix/dillflix-live-controller.git
cd dillflix-live-controller
cp .env.example .env
docker compose up -d --build
```

Open **http://CONTROLLER_HOST_IP:8790** from your PC, or **http://localhost:8790** on the server itself. The demo clock starts at an illustrative Sunday afternoon; sample events are fictional.

Compose binds `0.0.0.0:8790` by default for LAN access. Set `CONTROLLER_BIND_ADDRESS=127.0.0.1` for a host-local authenticated nginx deployment, or another host address/`CONTROLLER_PORT` as needed. The UI uses your existing proxy authentication; direct access is intended for a trusted LAN. See [nginx configuration](docs/nginx.conf.example). For containerized nginx, attach both services to your chosen shared network and proxy to the controller service.

State is stored in the `controller-data` volume. Rebuilding the container preserves it. Demo and Teamarr modes use separate database files. Pause is also retained across restarts.

The Docker configuration is supplied but has not been build-tested in the development environment, which did not have a Docker engine.

## Update an existing installation

From your existing checkout:

```bash
git pull --ff-only
docker compose up -d --build
```

Keep your existing `.env` and `controller-data` volume. Version 0.12.0 upgrades to schema 8, retaining plans, settings, catalog, receipts, history, manual sessions and manual event completions alongside executor records and durable input fences. Both the 0.8.1 hotfix and 0.9–0.11 databases are supported. Real navigation stays off until configured using [executor setup](docs/executor-setup.md). The Compose bind default now permits access from another PC; use `CONTROLLER_BIND_ADDRESS=127.0.0.1` if access should go exclusively through host-local nginx. Refresh the browser after updating. Newer database schemas are rejected by older releases. See [CHANGELOG.md](CHANGELOG.md).

Configuration export under **Settings → Configuration backup** saves priorities, preferred teams, and switching/display preferences. Import shows a review before replacing those fields; it preserves the watch plan and automation mode. It is a configuration transfer, not a complete database backup, and contains no Teamarr credentials. Unresolved team IDs are retained with a warning so preferences survive temporary directory gaps.

**Undo last edit** restores the latest saved plan or configuration edit. It is shared across browsers for the device, survives restarts, and retains up to 50 recent edits. It does not rewind live playback or restore an earlier pause/resume state. Repeated undo walks backwards through available edits. Loading a demo scenario clears that sample history.

## Backup and maintenance

See [docs/operations.md](docs/operations.md) for complete Docker/local backup and restore commands, retention settings, and the accelerated recovery runner. Backups can be taken while running; restore requires the controller to be stopped and returns with automation paused. Retention runs at startup and hourly, preserving manual commitments and recovery obligations.

## Exercise playback recovery

In demo mode, load **Navigation timeout** or **Replay result rejected** from the scenario controls. The controller records the failed attempt, preserves the watch plan, and selects another eligible live event. The timeout scenario uses a three-second deadline so the transition is easy to observe. Normal requests use `NAVIGATION_TIMEOUT_SECONDS`, which defaults to 120 and has a minimum of 5.

The playback details show request purpose, observed coverage, service contact, progress, delivery attempts, the pending deadline, and the latest request error. Activity retains earlier failure reasons after a fallback starts. Request acceptance and navigation are distinct from verified live playback. A playback observation expires after at most 15 seconds without fresh playback evidence; the interface then shows **Last observed**. No new content-status conclusion is inferred from that outage.

Load **Same-event coverage change**, wait for golf to be verified, then use **+15 min**. The fixture explicitly withdraws its TSN option. The controller requests the same golf event with the remaining valid option, preserving its manual protection and original viewing timers. An estimated coverage end alone never triggers a handoff or completes an event; the source must withdraw the option or change its playback locator.

Load **Playback service outage** to disconnect the simulator after initial playback, or use **Disconnect simulator**. The controller stops new navigation and probes at increasing intervals of 5, 10, 20, 40, then 60 seconds. Playback evidence still expires. You can edit your plan during the outage; **Reconnect simulator** prompts a check and the latest live selection is honored. Probe and recovery state survive controller restart. These controls are unavailable in Teamarr mode.

When the service responds but playback evidence is missing, the controller first waits for recovery. The initial grace is `PLAYBACK_RECOVERY_GRACE_SECONDS` (default 60, minimum 5). Repeated losses after recovery attempts increase the wait, up to 300 seconds; 30 seconds of healthy observations reset that budget. After grace, selection can reopen a confirmed live target, choose a confirmed live fallback, or wait. Unknown event status never authorizes a new playback request. Fresh verified playback can continue through a status outage. A higher live manual choice takes precedence, and **Play now** can explicitly retry an unverified live target without waiting for grace.

The simulator stores executor state separately from controller jobs. On restart or an uncertain delivery outcome, the controller inspects the original request before resending the same ID. Cancellation is durable and retried; it ends pending navigation and matching active playback without stopping a newer target. Manual input waits for an acknowledged device cancellation barrier. Playback calls run in a drained worker thread so slow adapter calls do not block the HTTP event loop. In real mode the embedded adapters use durable token records; model HTTP and ADB run asynchronously with finite deadlines, and physical input shares the manual-control gate.

## Exercise independent content status

Load **Reserved event outside feed** in demo mode. The Canadiens game is absent from discovery but stays in the watch plan and remains playable through an independent simulated lookup. Advancing the demo clock eventually returns an explicit ended observation and allows live fallback. **Status lookup unavailable** demonstrates a lookup failure while the schedule is healthy; the reservation remains saved.

Settings shows content-status health independently of schedule and team-directory health. Event details show the status source, observation time, effective expiry, lookup error, and whether the entry is outside the current feed. Missing or failed lookups retain still-valid evidence; expired nonterminal evidence becomes unknown. Already-confirmed terminal evidence survives outages, and a newer explicit observation can correct it.

`STATUS_INTERVAL_SECONDS` defaults to 15 (minimum 5). Lookups have a five-second timeout and bounded retry backoff. They continue while automation is paused. The internal lookup contract carries `content_id` and the complete original Teamarr entry, preserving identifiers for a future real service. Real mode uses acquired Prime Video completion evidence plus unchanged Teamarr feed facts through the same internal boundary.

**Teamarr evidence retains its original freshness.** The default simulator rereads cached feed status; real mode additionally monitors the requested Prime Video content for scoped completion. Neither mode independently fetches sports-results facts for every unplayed/out-of-window reservation. Without fresh evidence those events stay unknown. Teamarr supplies no provider observation timestamp: `observed_at` is null, `received_at` is the feed read, and `timestamp_basis` is `feed_received`. Visual completion uses `device_observed` acquisition times. See [evidence policy](docs/executor-api-handoff.md#verification-and-completion).

## Connect the Teamarr catalog

Set these values in `.env` and recreate the service:

```dotenv
CONTROLLER_MODE=teamarr
TEAMARR_URL=http://your-teamarr-host:9195
FEED_INTERVAL_SECONDS=60
```

```bash
docker compose up -d --build
```

`TEAMARR_URL` is the server root, without `/api/v1`. It must be reachable **from inside the controller container**. A hostname such as `teamarr` works only on a Docker network shared with that service; `localhost` inside a container points to that container. `TEAMARR_TOKEN` optionally supplies a bearer token if your Teamarr proxy requires one.

The adapter reads `GET /api/v1/events/feed`, covering the current time through three days ahead, with Teamarr's normal lookback semantics. It uses schema version 1. Its first request specifies the window, selected leagues and page size; subsequent requests use the opaque cursor alone. One expired snapshot is retried from the beginning.

Choose **Settings → Leagues in your schedule** to enable or disable NFL, NHL,
MLB, NBA, CFL, UEFA Champions League and Formula 1. All seven are enabled when
upgrading from a release without league selection. Changes persist, are included
in configuration export/import, and support **Undo last edit**. A new selection
is fetched on the next scheduled refresh. Disabled leagues stop supplying new
automatic candidates; saved watch-plan entries and current playback remain
protected and their leagues continue to be fetched while needed. RedZone, golf
coverage and special broadcasts retain their separate Teamarr configuration.

Use the matching Teamarr update for NBA/CFL/Champions League/F1 routes. This
installation accesses DAZN, Sportsnet and TSN exclusively through **Prime Video**,
so every new league's configured route is `prime_video`. Formula 1 is delivered
as separate provider-confirmed practice, qualifying, sprint and race sessions.
Only sessions within the three-day discovery window appear; an empty league on
a quiet week is expected. Their scheduled times do not establish live status.

The team directory reads `GET /api/v1/cache/leagues/{league}/teams` on startup and hourly, independently of feed ingestion. It requests the selected leagues and leagues retained for commitments, excluding Formula 1, plus up to 16 additional leagues already known from team data. It also remembers teams encountered in events. These are Teamarr's cached rosters; an unpopulated cache can return no teams. Failed or empty refreshes never delete known teams or preferences. Settings shows directory health separately from schedule health. Provider team IDs are used for matching; Teamarr's local cache row IDs are not.

Teamarr mode defaults to simulated playback. To control the TV, follow [Prime Video executor setup](docs/executor-setup.md). Unknown broadcast status remains unknown: a RedZone or golf schedule window alone cannot prove live eligibility. Failed feed refreshes retain the previous catalog and show degraded health; upstream fetch errors represented by Teamarr as successful empty feeds remain an upstream limitation.

## Develop locally

Use Python 3.12+ and Node.js 24, then:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
cd frontend
npm ci
npm run build
cd ..
python -m uvicorn controller.api:create_app --factory --host 0.0.0.0 --port 8790
```

For hot reload, run the API with `--reload`, and run `npm run dev` from `frontend` in a second terminal. Vite proxies API calls to port 8790. The Python server does not automatically load `.env`; export variables in your shell when running outside Compose.

Use **one API worker**. The current release has one logical device, `living-room`. The endpoints and records are device-scoped, but device creation, multiple playback workers, and multi-device coordination are future work.

## API

Interactive request schemas are available at `/docs`; the generated OpenAPI document is at `/openapi.json`. Response shapes and the playback request contract are documented in [docs/contracts.md](docs/contracts.md).

| Endpoint | Purpose |
| --- | --- |
| `POST /v1/playbacks` | Accept real playback and return a durable token |
| `GET /v1/playbacks/{token}` | Launch progress, playback evidence and event completion |
| `POST /v1/playbacks/cancel` | Cancel navigation/active playback or fence a device intent |
| `GET /api/health` | Process health, adapter mode and executor worker state |
| `GET /api/v1/maintenance` | Retention policy and last maintenance outcome |
| `GET /api/v1/overview` | One consistent snapshot for the web interface |
| `GET /api/v1/events` | Catalog cards, lifecycle observations, options, and metadata |
| `GET /api/v1/teams?league=nhl` | Persistent team directory and independent refresh health |
| `GET /api/v1/devices/{id}/state` | Plan, configuration revision, desired target, and observed playback |
| `GET /api/v1/devices/{id}/screen` | Screen configuration and capture status; no device contact |
| `WS /api/v1/devices/{id}/screen/stream` | On-demand view-only H.264 stream over the authenticated origin |
| `POST /api/v1/devices/{id}/control` | Take, explicitly take over, extend, or end manual control |
| `WS /api/v1/devices/{id}/control/input` | Exclusive session-owned remote key taps and text |
| `GET /api/v1/devices/{id}/watch-plan` | Ordered commitments and estimated overlap timeline |
| `POST /api/v1/devices/{id}/watch-plan/preview` | Preview a proposed command without saving it |
| `POST` or `PATCH /api/v1/devices/{id}/watch-plan` | Add, play now, reorder, or remove |
| `DELETE /api/v1/devices/{id}/watch-plan/{entry_id}` | Remove with command ID and expected revision query parameters |
| `GET` or `PUT /api/v1/devices/{id}/rules` | Rules, per-league team ranks, and preferences |
| `POST /api/v1/devices/{id}/undo` | Undo the latest available plan or configuration edit |
| `GET /api/v1/devices/{id}/configuration` | Export versioned configuration JSON |
| `POST /api/v1/devices/{id}/configuration/import/preview` | Validate and review an import without saving |
| `POST /api/v1/devices/{id}/configuration/import` | Apply a reviewed configuration at its expected revision |
| `POST /api/v1/devices/{id}/automation` | Pause or resume automation |
| `POST /api/v1/devices/{id}/completions` | Manually mark an event finished for this device; undoable |
| `POST /api/v1/devices/{id}/simulate` | Explain a selection without changing playback |
| `GET /api/v1/devices/{id}/activity` | Recent decisions and actions |
| `GET /api/v1/devices/{id}/jobs` | Recent playback requests, including original Teamarr payloads |
| `GET /api/v1/updates` | SSE invalidation notifications; clients fetch current state |
| `POST /api/v1/simulation` | Demo clock/scenario controls; unavailable in Teamarr mode |

## Checks

```bash
. .venv/bin/activate
python -m pytest -q
cd frontend
npm run test:browser
```

The browser runner builds the interface, starts a temporary demo API/database, checks desktop and phone workflows, and stops the API. Set `CONTROLLER_TEST_PYTHON` if Python is not on the active path. Linux x64 uses the npm-packaged Chromium; other platforms require `npx playwright install chromium`. `TEST_BASE_URL` can target a separate **disposable demo instance**: the tests deliberately reset its sample watch plan.

Coverage includes database upgrades, persistent undo, configuration round trips, stale previews across browsers, team-directory failures and provider IDs, API persistence, manual overlaps, overtime, failed playback fallback, unknown status, original payload preservation, and pagination failures. Playback tests cover lost acknowledgements, restart reconciliation, cancellation retry, intent fencing, late results, deadlines, rejected replay/wrong-content observations, and observation expiry/recovery. Status tests cover out-of-window commitments, timestamp preservation, failed/missing/unknown lookups, bounded timeouts, late/invalid results, lease loss, restart persistence, and explicit terminal corrections. Desktop, 390 px phone, and 320 px phone layouts are checked for horizontal overflow. These browser tests use Chromium viewport emulation, not physical phone or Safari testing.

## Real playback and remaining validation

The [API implementation guide](docs/executor-api-handoff.md), [OpenAPI contract](docs/executor-api.openapi.yaml), and [setup/diagnostics guide](docs/executor-setup.md) document the implemented three-operation infrastructure. Play, token status (including lifecycle), and Cancel (including active stop/manual handoff) share the same durable worker and store. Native collection and association are integrated in 0.9.1; critical-path label coverage and physical playback still need validation. See the [implementation status](docs/accessibility-critical-path.md). No separate recovery, device-observation, lifecycle lookup or authority API is required.

See [the roadmap](docs/roadmap.md) and [architecture](docs/architecture.md) for boundaries and remaining host validation. Run the target-TV procedure before relying on unattended navigation. Real model accuracy, Prime account availability, protected video visibility, physical mobile browsers, Docker/nginx and live endurance have not been validated in this development environment. Other streaming app adapters and independent sports-results providers remain future integrations.

Source repository: [Dillflix/dillflix-live-controller](https://github.com/Dillflix/dillflix-live-controller). This application is versioned and deployed independently of Teamarr.
