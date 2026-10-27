import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .access import ProxyAccess
from .config import Settings
from .current_playback import NowPlaying, current_playback
from .device_input import DeviceInput, same_origin
from .diagnostic_log import LogHandler, Recorder, read_logs
from .display import DisplayView, display_view
from .executor.api import install_executor_api
from .fleet import select_for_stream
from .live_streams import LiveStreams, install_live_streams_api
from .loop_watchdog import LoopWatchdog
from .models import (
    AutomationUpdate,
    Command,
    CompletionCommand,
    ConfigurationImport,
    EntitlementCommand,
    ManualControlCommand,
    PublicAccessUpdate,
    PublicCommand,
    RulesUpdate,
    SimulationCommand,
    UndoCommand,
)
from .planner import priority, team_priority
from .plex.api import install_plex_api
from .public import public_overview
from .screen import ScreenStream, serve_screen
from .service import Controller


def create_app(settings=None, *, start_workers=True):
    settings = settings or Settings.from_env()
    service = Controller(settings)
    screens = {key: ScreenStream(runtime.settings, device_id=key) for key, runtime in service.runtimes.items()}
    controls = {key: DeviceInput(runtime) for key, runtime in service.runtimes.items()}
    screen, control = screens[service.device_id], controls[service.device_id]
    live_streams = LiveStreams(settings)

    @asynccontextmanager
    async def lifespan(app):
        recorder = Recorder(Path(str(settings.database) + ".diagnostics"))
        app.state.diagnostic_recorder = recorder
        watchdog = LoopWatchdog(recorder)
        watchdog.start()
        handler = LogHandler(recorder)
        logger = logging.getLogger("controller")
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        server_logger = logging.getLogger("uvicorn.error")
        server_logger.addHandler(handler)
        try:
            runtimes = list(service.runtimes.values())
            starts = await asyncio.gather(
                *(runtime.executor.start() for runtime in runtimes if runtime.executor),
                return_exceptions=True,
            )
            for runtime, result in zip((r for r in runtimes if r.executor), starts):
                if isinstance(result, BaseException):
                    logger.error("Executor startup failed for %s: %s", runtime.device_id, type(result).__name__)
            for device_control in controls.values():
                device_control.start()
            if start_workers:
                service.start()
            yield
        finally:
            try:
                # Always drain every stream, even if another stream's cleanup fails.
                try:
                    await asyncio.gather(*(item.stop() for item in controls.values()), return_exceptions=True)
                    await asyncio.gather(*(item.stop() for item in screens.values()), return_exceptions=True)
                finally:
                    await service.stop()
            finally:
                await live_streams.close()
                logger.removeHandler(handler)
                server_logger.removeHandler(handler)
                logger.setLevel(previous_level)
                await watchdog.close()
                await asyncio.to_thread(recorder.close)

    app = FastAPI(title="Dillflix Controller", version="0.16.1", lifespan=lifespan)
    app.add_middleware(
        ProxyAccess,
        secret=settings.proxy_secret,
        public_auth_mode=settings.public_auth_mode,
        public_origin=settings.public_origin,
        admin_auth_mode=settings.admin_auth_mode,
    )
    app.state.controller = service
    app.state.screen = screen
    app.state.control = control
    app.state.screens = screens
    app.state.controls = controls
    app.state.live_streams = live_streams
    install_live_streams_api(app, live_streams, service)
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
        service.runtime(device_id)
        return await controls[device_id].command(device_id, command)

    @app.websocket("/api/v1/devices/{device_id}/control/input")
    async def manual_input(websocket: WebSocket, device_id: str):
        if device_id not in controls:
            await websocket.close(code=1008)
            return
        await controls[device_id].serve(websocket, device_id)

    @app.get("/api/v1/devices/{device_id}/screen")
    def screen_status(device_id: str):
        service.runtime(device_id)
        return screens[device_id].status()

    @app.websocket("/api/v1/devices/{device_id}/screen/stream")
    async def screen_stream(websocket: WebSocket, device_id: str):
        # Only server-configured bindings can choose a physical ADB target.
        if device_id not in screens:
            await websocket.close(code=1008)
            return
        await serve_screen(websocket, screens[device_id])

    @app.exception_handler(KeyError)
    async def missing(_request, _error):
        return JSONResponse(status_code=404, content={"detail": "Device or content not found"})

    @app.get("/api/health")
    def health():
        workers = service.background_status()
        with service.db.snapshot() as db:
            feed = service.feed_health(db)
        executor_running = bool(service.executor and service.executor.worker and not service.executor.worker.done())
        devices = {}
        for device_id, runtime in service.runtimes.items():
            running = bool(runtime.executor and runtime.executor.worker and not runtime.executor.worker.done())
            devices[device_id] = {
                "background_workers": runtime.background_status(),
                "executor_running": running,
                "executor_database": getattr(runtime.executor, "database_status", None),
                "playback": runtime.settings.executor.mode,
            }
        return {
            "ok": all(worker["state"] == "running" for worker in workers.values())
            and feed["state"] != "degraded"
            and all(worker["state"] == "running" for value in devices.values()
                    for worker in value["background_workers"].values())
            and all(not service.runtime(key).executor or value["executor_running"] for key, value in devices.items()),
            "background_workers": workers,
            "feed": feed,
            "mode": settings.mode,
            "playback": settings.executor.mode,
            "executor_running": executor_running,
            "executor_database": getattr(service.executor, "database_status", None),
            "devices": devices,
        }

    @app.get("/healthz", include_in_schema=False)
    def liveness():
        return {"ok": True}

    @app.get("/api/v1/overview")
    def overview(device_id: str | None = None):
        # This synchronous endpoint runs in FastAPI's thread pool. Render here
        # too, rather than returning a large object for event-loop serialization.
        return JSONResponse(service.runtime(device_id).overview())

    @app.get("/api/v1/devices")
    def devices():
        with service.db.snapshot() as db:
            return {"items": service.stream_summaries(db)}

    @app.get("/api/public/v1/overview")
    def viewer_overview(request: Request, device_id: str | None = None):
        return JSONResponse(
            public_overview(service.runtime(device_id), request.state.actor), headers={"Cache-Control": "no-store"}
        )

    @app.post("/api/public/v1/watch-plan")
    def viewer_command(command: PublicCommand, request: Request, device_id: str | None = None):
        # Source is the public interface even when the signed-in viewer is an admin.
        actor = {**request.state.actor, "type": "user"}
        runtime = service.runtime(device_id)
        return runtime.plan_command(
            runtime.device_id,
            Command(
                command_id=command.command_id,
                expected_revision=command.expected_revision,
                action={"type": command.action, "content_id": command.content_id},
            ),
            actor=actor,
        )

    @app.put("/api/v1/devices/{device_id}/public-access")
    def public_access(device_id: str, command: PublicAccessUpdate):
        return service.runtime(device_id).public_access_command(device_id, command)

    @app.get("/api/v1/devices/{device_id}/diagnostics")
    async def diagnostics(device_id: str):
        from .prime_player.diagnostics import collect, redact

        runtime = service.runtime(device_id)
        bundle = await asyncio.to_thread(collect, settings.database, device_id)
        bundle["controller_health"] = await asyncio.to_thread(health)
        bundle["catalog_health"]["feed_health"] = bundle["controller_health"]["feed"]
        bundle["prime_player"] = {"state": "not_configured"}
        recorder = getattr(app.state, "diagnostic_recorder", None)
        bundle["controller_logs"] = await asyncio.to_thread(
            recorder.snapshot if recorder else lambda: read_logs(str(settings.database) + ".diagnostics")
        )
        if runtime.executor:
            bundle["model_capture"].update(await asyncio.to_thread(runtime.executor.model_captures.status))
            player = runtime.executor.player
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
                    read_logs, Path(runtime.settings.executor.prime_socket).parent / "diagnostics"
                )
        return await asyncio.to_thread(
            JSONResponse, await asyncio.to_thread(redact, bundle),
            headers={
                "Content-Disposition": 'attachment; filename="dillflix-diagnostics.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/v1/maintenance")
    def maintenance():
        return service.maintenance_status()

    @app.get("/api/v1/events")
    def events(device_id: str | None = None):
        data = service.runtime(device_id).overview()
        return JSONResponse({
            "items": data["events"],
            "meta": data["meta"],
            "health": data["health"],
            "status_health": data["status_health"],
        })

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
        return service.runtime(device_id).undo_command(device_id, command)

    @app.get("/api/v1/devices/{device_id}/configuration")
    def configuration(device_id: str):
        return service.runtime(device_id).export_configuration(device_id)

    @app.post("/api/v1/devices/{device_id}/configuration/import/preview")
    def import_preview(device_id: str, command: ConfigurationImport):
        return service.runtime(device_id).import_preview(device_id, command)

    @app.post("/api/v1/devices/{device_id}/configuration/import")
    def import_configuration(device_id: str, command: ConfigurationImport):
        return service.runtime(device_id).import_configuration(device_id, command)

    @app.get("/api/v1/devices/{device_id}/state")
    def state(device_id: str):
        return service.runtime(device_id).overview()["device"]

    @app.get("/api/v1/devices/{device_id}/now-playing", response_model=NowPlaying)
    def now_playing(device_id: str):
        """Expose accepted playback for diagnostics without polling the player."""
        runtime = service.runtime(device_id)
        with service.db.snapshot() as db:
            view = current_playback(runtime, db, device_id)
        return JSONResponse(view.model_dump(), headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/devices/{device_id}/display", response_model=DisplayView)
    def display(device_id: str):
        """Compact read-only LED display state; never contacts a playback device."""
        return JSONResponse(display_view(service.runtime(device_id), device_id).model_dump(), headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/devices/{device_id}/watch-plan")
    def plan(device_id: str):
        data = service.runtime(device_id).overview()
        return {
            "revision": data["device"]["revision"],
            "entries": data["device"]["plan"],
            **data["plan_preview"],
        }

    @app.post("/api/v1/devices/{device_id}/watch-plan/preview")
    def plan_preview(device_id: str, command: Command):
        return service.runtime(device_id).preview(device_id, command)

    @app.post("/api/v1/devices/{device_id}/watch-plan")
    @app.patch("/api/v1/devices/{device_id}/watch-plan")
    def plan_command(device_id: str, command: Command, request: Request):
        return service.runtime(device_id).plan_command(
            device_id, command, actor=request.state.actor if settings.proxy_secret else None
        )

    @app.delete("/api/v1/devices/{device_id}/watch-plan/{entry_id}")
    def remove(device_id: str, entry_id: str, command_id: str, expected_revision: int):
        return service.runtime(device_id).plan_command(
            device_id,
            Command(
                command_id=command_id,
                expected_revision=expected_revision,
                action={"type": "remove", "entry_id": entry_id},
            ),
        )

    @app.post("/api/v1/devices/{device_id}/entitlement-checks")
    def entitlement_check(device_id: str, command: EntitlementCommand):
        return service.runtime(device_id).entitlement_command(device_id, command)

    @app.get("/api/v1/devices/{device_id}/vpn-locations")
    async def vpn_locations(device_id: str):
        runtime = service.runtime(device_id)
        if not runtime.executor or not hasattr(runtime.executor, "player"):
            return {"enabled": False, "locations": []}
        try:
            return await runtime.executor.player.vpn_locations()
        except Exception as exc:
            raise HTTPException(503, "Prime Player VPN locations are unavailable") from exc

    @app.get("/api/v1/devices/{device_id}/rules")
    def rules(device_id: str):
        d = service.runtime(device_id).overview()["device"]
        return {k: d[k] for k in ("rules", "team_ranks", "preferences", "revision")}

    @app.put("/api/v1/devices/{device_id}/rules")
    def save_rules(device_id: str, update: RulesUpdate):
        return service.runtime(device_id).rules_command(device_id, update)

    @app.post("/api/v1/devices/{device_id}/automation")
    def automation(device_id: str, update: AutomationUpdate):
        return service.runtime(device_id).automation_command(device_id, update)

    @app.post("/api/v1/devices/{device_id}/completions")
    def complete_event(device_id: str, command: CompletionCommand):
        return service.runtime(device_id).completion_command(device_id, command)

    @app.post("/api/v1/devices/{device_id}/simulate")
    def simulate(device_id: str):
        runtime = service.runtime(device_id)
        with service.db.snapshot() as db:
            d, items = service.db.device(db, device_id), runtime.items(db, device_id)
            d["force_switch"] = True
            decision = select_for_stream(runtime, db, d, items, service.now(db), datetime.now(UTC), reserve=False)
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
        result = await service.playback_work(service.simulation, request)
        await service.refresh_status(force=True)
        return result

    @app.get("/api/v1/devices/{device_id}/activity")
    def activity(device_id: str):
        return {"items": service.runtime(device_id).overview()["activity"]}

    @app.get("/api/v1/devices/{device_id}/jobs")
    def jobs(device_id: str):
        service.runtime(device_id)
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
        def read_sequence():
            with service.db.snapshot() as db:
                return str(db.execute("SELECT COALESCE(MAX(sequence),0) FROM activity").fetchone()[0])

        async def stream():
            previous = request.headers.get("last-event-id")
            while not await request.is_disconnected():
                sequence = await asyncio.to_thread(read_sequence)
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

    @app.get("/public")
    @app.get("/public/")
    def public_index():
        entry = settings.frontend / "public.html"
        if not entry.is_file():
            raise HTTPException(503, "Build the web interface to include the public application")
        return FileResponse(entry, headers={"Cache-Control": "no-cache"})

    return app
