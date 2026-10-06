import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .current_playback import NowPlaying, current_playback
from .device_input import DeviceInput, same_origin
from .diagnostic_log import LogHandler, Recorder, read_logs
from .executor.api import install_executor_api
from .models import (
    AutomationUpdate,
    Command,
    CompletionCommand,
    ConfigurationImport,
    ManualControlCommand,
    RulesUpdate,
    SimulationCommand,
    UndoCommand,
)
from .planner import choose, priority, team_priority
from .plex.api import install_plex_api
from .screen import ScreenStream, serve_screen
from .service import Controller


def create_app(settings=None, *, start_workers=True):
    settings = settings or Settings.from_env()
    service = Controller(settings)
    screen = ScreenStream(settings)
    control = DeviceInput(service)

    @asynccontextmanager
    async def lifespan(app):
        recorder = Recorder(Path(str(settings.database) + ".diagnostics"))
        app.state.diagnostic_recorder = recorder
        handler = LogHandler(recorder)
        logger = logging.getLogger("controller")
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        server_logger = logging.getLogger("uvicorn.error")
        server_logger.addHandler(handler)
        try:
            if service.executor:
                await service.executor.start()
            control.start()
            if start_workers:
                service.start()
            yield
        finally:
            try:
                await control.stop()
                await screen.stop()
                await service.stop()
            finally:
                logger.removeHandler(handler)
                server_logger.removeHandler(handler)
                logger.setLevel(previous_level)
                await asyncio.to_thread(recorder.close)

    app = FastAPI(title="Dillflix Controller", version="0.15.1", lifespan=lifespan)
    app.state.controller = service
    app.state.screen = screen
    app.state.control = control
    install_executor_api(app, service)
    install_plex_api(app, service)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if "/plex" in request.url.path:
            return JSONResponse(status_code=422, content={"detail": [
                {"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()
            ]})
        return await request_validation_exception_handler(request, exc)

    @app.middleware("http")
    async def record_unhandled_failure(request, call_next):
        try:
            return await call_next(request)
        except Exception:
            logging.getLogger("controller.api").exception("Unhandled HTTP request failure")
            raise

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

    @app.get("/api/v1/devices/{device_id}/diagnostics")
    async def diagnostics(device_id: str):
        from .prime_player.diagnostics import collect, redact

        bundle = await asyncio.to_thread(collect, settings.database, device_id)
        bundle["controller_health"] = health()
        bundle["prime_player"] = {"state": "not_configured"}
        recorder = getattr(app.state, "diagnostic_recorder", None)
        bundle["controller_logs"] = await asyncio.to_thread(
            recorder.snapshot if recorder else lambda: read_logs(str(settings.database) + ".diagnostics")
        )
        if service.executor:
            player = service.executor.player
            bundle["prime_player"] = {"state": "unavailable"}
            # Collect independently: a failed health call must not hide evidence.
            for method, key in (("health", "health"), ("diagnostics", "service_diagnostics")):
                try:
                    bundle["prime_player"][key] = await player.rpc(method, budget=5)
                    if method == "health":
                        bundle["prime_player"]["state"] = "available"
                except Exception as exc:
                    bundle["prime_player"][key + "_error"] = {
                        "type": type(exc).__name__,
                        "detail": str(exc)[:500],
                    }
            if "service_diagnostics" not in bundle["prime_player"]:
                bundle["prime_player"]["persisted_logs"] = await asyncio.to_thread(
                    read_logs, Path(settings.executor.prime_socket).parent / "diagnostics"
                )
        return JSONResponse(
            redact(bundle),
            headers={
                "Content-Disposition": 'attachment; filename="dillflix-diagnostics.json"',
                "Cache-Control": "no-store",
            },
        )

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

    @app.get("/api/v1/devices/{device_id}/now-playing", response_model=NowPlaying)
    def now_playing(device_id: str):
        """Expose accepted playback for diagnostics without polling the player."""
        with service.db.transaction() as db:
            view = current_playback(service, db, device_id)
        return JSONResponse(view.model_dump(), headers={"Cache-Control": "no-store"})

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

    @app.post("/api/v1/devices/{device_id}/completions")
    def complete_event(device_id: str, command: CompletionCommand):
        return service.completion_command(device_id, command)

    @app.post("/api/v1/devices/{device_id}/simulate")
    def simulate(device_id: str):
        with service.db.transaction() as db:
            d, items = service.db.device(db, device_id), service.items(db, device_id)
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
