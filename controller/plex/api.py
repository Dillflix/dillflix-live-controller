"""Authenticated by the controller's existing nginx boundary."""

from typing import Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, SecretStr, field_validator

from ..device_input import same_origin
from ..models import StrictModel
from .client import MAX_IMAGE_BYTES, PlexError, base_url, image_asset, rating_key
from .state import asset_by_id, configuration, runtime


class PlexCommand(StrictModel):
    command_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class PlexConnection(StrictModel):
    base_url: str = Field(max_length=2048)
    rating_key: str = Field(default="8", max_length=2048)
    token: SecretStr | None = Field(default=None, max_length=1024)

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        return base_url(value)

    @field_validator("rating_key")
    @classmethod
    def valid_key(cls, value):
        return rating_key(value)

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        if value and any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in value.get_secret_value()):
            raise ValueError("Use the Plex token without spaces or control characters")
        return value if value and value.get_secret_value() else None


class PlexUpdate(PlexConnection, PlexCommand):
    enabled: bool = False
    remove_token: bool = False


def install_plex_api(app, service):
    integration = service.plex
    prefix = "/api/v1/devices/{device_id}/plex"

    def origin(request):
        if "origin" in request.headers and not same_origin(request.headers):
            raise HTTPException(403, "Plex configuration requires the same origin")

    @app.exception_handler(PlexError)
    async def plex_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get(prefix)
    def get_settings(device_id: str):
        return integration.status(device_id)

    @app.put(prefix)
    async def save_settings(device_id: str, command: PlexUpdate, request: Request):
        origin(request)
        return await integration.save(device_id, command)

    @app.post(prefix + "/test")
    async def test_connection(device_id: str, command: PlexConnection, request: Request):
        origin(request)
        checked, _ = await integration.inspect(device_id, command)
        return {k: v for k, v in checked.items() if k != "target"}

    @app.post(prefix + "/defaults/capture")
    async def capture(device_id: str, command: PlexCommand, request: Request):
        origin(request)
        return await integration.capture(device_id, command)

    @app.post(prefix + "/defaults/{slot}")
    async def upload(
        device_id: str,
        slot: Literal["poster", "background"],
        request: Request,
        command_id: str = Query(min_length=1, max_length=100),
        expected_revision: int = Query(ge=0),
    ):
        origin(request)
        command = PlexCommand(command_id=command_id, expected_revision=expected_revision)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_IMAGE_BYTES:
                raise HTTPException(413, "Default images must be at most 8 MiB")
            chunks.append(chunk)
        asset = image_asset(b"".join(chunks))
        return integration.change(device_id, command, "upload", assets={slot: asset})

    @app.post(prefix + "/{action}")
    def action(
        device_id: str,
        action: Literal["resync", "retry", "restore-defaults"],
        command: PlexCommand,
        request: Request,
    ):
        origin(request)
        return integration.change(device_id, command, action)

    @app.get(prefix + "/images/{which}/{slot}")
    def preview(device_id: str, which: Literal["default", "applied"], slot: Literal["poster", "background"]):
        with service.db.transaction() as db:
            service.db.device(db, device_id)
            digest = (
                configuration(db, device_id)["defaults"].get(slot)
                if which == "default"
                else runtime(db, device_id)["applied"].get(slot, {}).get("digest")
            )
            if not digest:
                raise HTTPException(404, "No saved artwork")
            asset = asset_by_id(db, digest)
        return Response(
            asset.data,
            media_type=asset.mime,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
