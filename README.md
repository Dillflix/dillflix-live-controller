# Dillflix Controller

A self-hosted live-sports planner with a responsive web interface. It reads the unified feed from **Dillflix/teamarr**, maintains an ordered watch plan, chooses live content from configurable priorities, and prepares durable playback requests.

**Version 0.2: playback and content-status verification remain simulated. It does not connect to Fire TV or send ADB commands.** The interface, database, API, policy engine, and Teamarr HTTP adapter are implemented. Initial user testing has confirmed startup and the Teamarr connection; automated integration tests use the fork's contract and mocked HTTP responses.

## What works

- Events page with live/upcoming views, league filters, search, structured team names, provider logos, event details, and manual selection.
- A persistent watch plan. The first eligible manual entry wins an overlap; the others resume while still live. Play now moves an event to the front without deleting other commitments.
- Ordered priority rules for leagues, season stages, teams, content kind, and coverage sources, all editable in the UI. Team rankings break ties within a rule.
- A persistent, searchable team directory. Rank preferred teams even when they have no event in the schedule window; unranked teams tie.
- Durable undo for watch-plan, priority, team-ranking, and settings edits, including after a restart.
- Configuration export and reviewed import, with validation, cross-mode warnings, and protection against overwriting newer edits.
- Minimum viewing time, automatic-switch cooldown, same-priority switching, and timezone settings.
- Separate desired and observed playback, pause/resume, decision history, and side-effect-free selection/conflict previews.
- Versioned SQLite migrations, optimistic concurrency, command idempotency, a durable request queue, and a coordinator lease.
- Demo scenarios for overlaps, overtime, delays, route failures, stale status, and an empty live schedule.
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

Open **http://localhost:8790**. The demo clock starts at an illustrative Sunday afternoon; advance it with the controls at the bottom of the page. Sample events are intentionally fictional, including playoff matchups on the sample date.

The Compose service binds to loopback for use behind your existing nginx authentication. See [docs/nginx.conf.example](docs/nginx.conf.example). For a containerized nginx, attach both services to your chosen shared network and proxy to the controller service instead of loopback.

State is stored in the `controller-data` volume. Rebuilding the container preserves it. Demo and Teamarr modes use separate database files. Pause is also retained across restarts.

The Docker configuration is supplied but has not been build-tested in the development environment, which did not have a Docker engine.

## Update an existing installation

From your existing checkout:

```bash
git pull --ff-only
docker compose up -d --build
```

Keep your existing `.env` and `controller-data` volume. Version 0.2 automatically migrates the version-1 database and retains settings, watch-plan entries, catalog snapshots, pending requests, and command receipts. Refresh the browser after updating. A database created by a newer controller is rejected rather than silently downgraded. See [CHANGELOG.md](CHANGELOG.md) for release details.

Configuration export under **Settings → Configuration backup** saves priorities, preferred teams, and switching/display preferences. Import shows a review before replacing those fields; it preserves the watch plan and automation mode. It is a configuration transfer, not a complete database backup, and contains no Teamarr credentials. Unresolved team IDs are retained with a warning so preferences survive temporary directory gaps.

**Undo last edit** restores the latest saved plan or configuration edit. It is shared across browsers for the device, survives restarts, and retains up to 50 recent edits. It does not rewind live playback or restore an earlier pause/resume state. Repeated undo walks backwards through available edits. Loading a demo scenario clears that sample history.

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

The adapter reads `GET /api/v1/events/feed`, covering the current time through three days ahead, with Teamarr's normal lookback semantics. It uses schema version 1. Its first request specifies the window and page size; subsequent requests use the opaque cursor alone. One expired snapshot is retried from the beginning.

The team directory reads `GET /api/v1/cache/leagues/{league}/teams` on startup and hourly, independently of feed ingestion. It requests NFL, NHL, MLB, and NBA, plus up to 16 additional leagues already known from team data. It also remembers teams encountered in events. These are Teamarr's cached rosters; an unpopulated cache can return no teams. Failed or empty refreshes never delete known teams or preferences. Settings shows directory health separately from schedule health. Provider team IDs are used for matching; Teamarr's local cache row IDs are not.

Playback remains simulated in Teamarr mode. Provider-reported event status can drive the status simulator, but unknown broadcast status remains unknown: a RedZone or golf schedule window alone cannot prove that playback is live. A failed refresh retains the last catalog; the UI shows degraded feed health. Teamarr upstream fetch errors that appear as a successful empty result remain an upstream limitation.

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
python -m uvicorn controller.api:create_app --factory --host 127.0.0.1 --port 8790
```

For hot reload, run the API with `--reload`, and run `npm run dev` from `frontend` in a second terminal. Vite proxies API calls to port 8790. The Python server does not automatically load `.env`; export variables in your shell when running outside Compose.

Use **one API worker**. The current release has one logical device, `living-room`. The endpoints and records are device-scoped, but device creation, multiple playback workers, and multi-device coordination are future work.

## API

Interactive request schemas are available at `/docs`; the generated OpenAPI document is at `/openapi.json`. Response shapes and the playback request contract are documented in [docs/contracts.md](docs/contracts.md).

| Endpoint | Purpose |
| --- | --- |
| `GET /api/health` | Process health and explicit simulation mode |
| `GET /api/v1/overview` | One consistent snapshot for the web interface |
| `GET /api/v1/events` | Catalog cards, lifecycle observations, options, and metadata |
| `GET /api/v1/teams?league=nhl` | Persistent team directory and independent refresh health |
| `GET /api/v1/devices/{id}/state` | Plan, configuration revision, desired target, and observed playback |
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
| `POST /api/v1/devices/{id}/simulate` | Explain a selection without changing playback |
| `GET /api/v1/devices/{id}/activity` | Recent decisions and actions |
| `GET /api/v1/devices/{id}/jobs` | Recent simulator requests, including original Teamarr payloads |
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

Coverage includes database upgrades, persistent undo, configuration round trips, stale previews across browsers, team-directory failures and provider IDs, API persistence, idempotent delivery, manual overlaps, overtime, failed playback fallback, unknown status, pending-request recovery, original payload preservation, and pagination failures. Desktop, 390 px phone, and 320 px phone layouts are checked for horizontal overflow. These browser tests use Chromium viewport emulation, not physical phone or Safari testing.

## Next integrations

See [docs/architecture.md](docs/architecture.md) for boundaries and remaining production work. Continue reviewing real event data and selection behavior against your Teamarr deployment. An authoritative content-status adapter and a playback executor can then replace the simulations without changing the user's watch-plan commands.

Source repository: [Dillflix/dillflix-live-controller](https://github.com/Dillflix/dillflix-live-controller). This application is versioned and deployed independently of Teamarr.
