import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .device_input import DeviceInput, same_origin
from .executor.api import install_executor_api
from .models import (
    AutomationUpdate,
    Command,
    ConfigurationImport,
    ManualControlCommand,
    RulesUpdate,
    SimulationCommand,
    UndoCommand,
)
from .planner import choose, priority, team_priority
from .screen import ScreenStream, serve_screen
from .service import Controller


def create_app(settings=None, *, start_workers=True):
    settings = settings or Settings.from_env()
    service = Controller(settings)
    screen = ScreenStream(settings)
    control = DeviceInput(service)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if service.executor:
                await service.executor.start()
            control.start()
            if start_workers:
                service.start()
            yield
        finally:
            await control.stop()
            await screen.stop()
            await service.stop()

    app = FastAPI(title="Dillflix Controller", version="0.10.0", lifespan=lifespan)
    app.state.controller = service
    app.state.screen = screen
    app.state.control = control
    install_executor_api(app, service)

    @app.post("/api/v1/devices/{device_id}/control")
    async def manual_control(device_id: str, command: ManualControlCommand, request: Request):
        if "origin" in request.headers and not same_origin(request.headers):
            raise HTTPException(403, "Manual control requires the same origin.")
        return await control.command(device_id, command)

    @app.websocket("/api/v1/devices/{device_id}/control/input")
    async def manual_input(websocket: WebSocket, device_id: str):
        await control.serve(websocket, device_id)

    @app.get("/api/v1/devices/{device_id}/screen")
    def screen_status(device_id: str):
        with service.db.transaction() as db:
            service.db.device(db, device_id)
        return screen.status()

    @app.websocket("/api/v1/devices/{device_id}/screen/stream")
    async def screen_stream(websocket: WebSocket, device_id: str):
        # One configured device today. Never let a browser select an ADB target.
        if device_id != "living-room":
            await websocket.close(code=1008)
            return
        await serve_screen(websocket, screen)

    @app.exception_handler(KeyError)
    async def missing(_request, _error):
        return JSONResponse(status_code=404, content={"detail": "Device or content not found"})

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "mode": settings.mode,
            "playback": settings.executor.mode,
            "executor_running": bool(
                service.executor and service.executor.worker and not service.executor.worker.done()
            ),
        }

    @app.get("/api/v1/overview")
    def overview():
        return service.overview()

    @app.get("/api/v1/maintenance")
    def maintenance():
        return service.maintenance_status()

    @app.get("/api/v1/events")
    def events():
        data = service.overview()
        return {
            "items": data["events"],
            "meta": data["meta"],
            "health": data["health"],
            "status_health": data["status_health"],
        }

    @app.get("/api/v1/teams")
    def teams(league: str | None = None):
        with service.db.transaction() as db:
            items = [t for t in service.db.teams(db) if league is None or t["league"] == league]
            return {
                "items": items,
                "health": service.db.meta(
                    db, "team_directory_health", {"state": "demo" if settings.mode == "demo" else "starting"}
                ),
            }

    @app.post("/api/v1/devices/{device_id}/undo")
    def undo(device_id: str, command: UndoCommand):
        return service.undo_command(device_id, command)

    @app.get("/api/v1/devices/{device_id}/configuration")
    def configuration(device_id: str):
        return service.export_configuration(device_id)

    @app.post("/api/v1/devices/{device_id}/configuration/import/preview")
    def import_preview(device_id: str, command: ConfigurationImport):
        return service.import_preview(device_id, command)

    @app.post("/api/v1/devices/{device_id}/configuration/import")
    def import_configuration(device_id: str, command: ConfigurationImport):
        return service.import_configuration(device_id, command)

    @app.get("/api/v1/devices/{device_id}/state")
    def state(device_id: str):
        return service.overview(device_id)["device"]

    @app.get("/api/v1/devices/{device_id}/watch-plan")
    def plan(device_id: str):
        data = service.overview(device_id)
        return {
            "revision": data["device"]["revision"],
            "entries": data["device"]["plan"],
            **data["plan_preview"],
        }

    @app.post("/api/v1/devices/{device_id}/watch-plan/preview")
    def plan_preview(device_id: str, command: Command):
        return service.preview(device_id, command)

    @app.post("/api/v1/devices/{device_id}/watch-plan")
    @app.patch("/api/v1/devices/{device_id}/watch-plan")
    def plan_command(device_id: str, command: Command):
        return service.plan_command(device_id, command)

    @app.delete("/api/v1/devices/{device_id}/watch-plan/{entry_id}")
    def remove(device_id: str, entry_id: str, command_id: str, expected_revision: int):
        return service.plan_command(
            device_id,
            Command(
                command_id=command_id,
                expected_revision=expected_revision,
                action={"type": "remove", "entry_id": entry_id},
            ),
        )

    @app.get("/api/v1/devices/{device_id}/rules")
    def rules(device_id: str):
        d = service.overview(device_id)["device"]
        return {k: d[k] for k in ("rules", "team_ranks", "preferences", "revision")}

    @app.put("/api/v1/devices/{device_id}/rules")
    def save_rules(device_id: str, update: RulesUpdate):
        return service.rules_command(device_id, update)

    @app.post("/api/v1/devices/{device_id}/automation")
    def automation(device_id: str, update: AutomationUpdate):
        return service.automation_command(device_id, update)

    @app.post("/api/v1/devices/{device_id}/simulate")
    def simulate(device_id: str):
        with service.db.transaction() as db:
            d, items = service.db.device(db, device_id), service.items(db)
            d["force_switch"] = True
            decision = choose(d, items, service.now(db), datetime.now(UTC))
            alternatives = [
                {
                    "content_id": item["content_id"],
                    "title": item["title"],
                    "priority": priority(d, item),
                    "team_priority": team_priority(d, item),
                    "eligible": item["playable"],
                    "reason": item["availability_reason"],
                }
                for item in items
            ]
            return {
                "decision": decision,
                "alternatives": sorted(
                    alternatives,
                    key=lambda item: (not item["eligible"], item["priority"], item["team_priority"]),
                ),
            }

    @app.post("/api/v1/simulation")
    async def simulation(request: SimulationCommand):
        result = service.simulation(request)
        await service.refresh_status(force=True)
        return result

    @app.get("/api/v1/devices/{device_id}/activity")
    def activity(device_id: str):
        return {"items": service.overview(device_id)["activity"]}

    @app.get("/api/v1/devices/{device_id}/jobs")
    def jobs(device_id: str):
        with service.db.transaction() as db:
            service.db.device(db, device_id)
            return {
                "items": [
                    {**dict(r), "payload": json.loads(r["payload"])}
                    for r in db.execute(
                        "SELECT * FROM jobs WHERE device_id=? ORDER BY rowid DESC LIMIT 50", (device_id,)
                    )
                ]
            }

    @app.get("/api/v1/updates")
    async def updates(request: Request):
        async def stream():
            previous = request.headers.get("last-event-id")
            while not await request.is_disconnected():
                with service.db.transaction() as db:
                    sequence = str(db.execute("SELECT COALESCE(MAX(sequence),0) FROM activity").fetchone()[0])
                if sequence != previous:
                    yield f'id: {sequence}\nevent: update\ndata: {{"sequence":{sequence}}}\n\n'
                    previous = sequence
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    assets = settings.frontend / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/screen-licenses.txt")
    def screen_licenses():
        notices = settings.frontend / "screen-licenses.txt"
        if not notices.is_file():
            raise HTTPException(404, "Build the web interface to include its dependency notices")
        return FileResponse(notices, media_type="text/plain")

    @app.get("/")
    def index():
        entry = settings.frontend / "index.html"
        if not entry.is_file():
            raise HTTPException(503, "Build the web interface with npm ci && npm run build in frontend/")
        return FileResponse(entry, headers={"Cache-Control": "no-cache"})

    return app
