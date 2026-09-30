"""Authenticated three-operation executor API; shared with the built-in adapters."""

import hmac

from fastapi import APIRouter, Depends, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .models import CancelRequest, CancelResult, ExecutorError, PlaybackReport, PlaybackRequest, Problem


def problem_response(error):
    headers = {"Cache-Control": "no-store"}
    if error.retryable:
        headers["Retry-After"] = "5"
    if error.status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        status_code=error.status,
        media_type="application/problem+json",
        headers=headers,
        content={
            "type": "urn:dillflix:problem:" + error.code,
            "title": error.code.replace("_", " "),
            "status": error.status,
            "detail": error.message,
            "code": error.code,
            "retryable": error.retryable,
        },
    )


class ExecutorBodyLimit:
    def __init__(self, app, limit=2 * 1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/playbacks"):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                response = problem_response(
                    ExecutorError(
                        "body_too_large", "Playback request exceeds 2 MiB", status=413, retryable=False
                    )
                )
                return await response(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def install_executor_api(app, service):
    bearer = HTTPBearer(auto_error=False, scheme_name="ServiceBearer")

    async def authorize(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        configured = service.settings.executor.api_token
        if not service.executor or not configured:
            raise ExecutorError(
                "executor_disabled", "Real executor API is not configured", status=503, retryable=False
            )
        supplied = credentials.credentials if credentials else ""
        if not hmac.compare_digest(supplied.encode(), configured.encode()):
            raise ExecutorError(
                "unauthorized", "A valid executor service credential is required", status=401, retryable=False
            )
        return service.executor

    errors = {
        code: {
            "description": "Playback problem; retry only when retryable is true.",
            "content": {"application/problem+json": {"schema": Problem.model_json_schema()}},
        }
        for code in (401, 404, 409, 410, 413, 422, 503)
    }
    router = APIRouter(
        prefix="/v1/playbacks",
        tags=["Playback executor"],
        dependencies=[Depends(authorize)],
        responses=errors,
    )

    @router.post(
        "",
        response_model=PlaybackReport,
        status_code=202,
        responses={200: {"model": PlaybackReport, "description": "Identical retry, same token"}},
        operation_id="initiatePlayback",
        summary="Accept live playback and return a durable token",
    )
    def play(command: PlaybackRequest):
        report, created = service.executor.submit(command.model_dump(mode="json"))
        return JSONResponse(
            report,
            status_code=202 if created else 200,
            headers={"Location": "/v1/playbacks/" + report["token"], "Cache-Control": "no-store"},
        )

    @router.post(
        "/cancel",
        response_model=CancelResult,
        operation_id="cancelPlayback",
        summary="Cancel pending navigation and stop owned playback",
    )
    async def cancel(command: CancelRequest):
        try:
            result = await service.executor.cancel(command)
        except TimeoutError as error:
            raise ExecutorError(
                "cancel_unconfirmed", "Cancellation is retained but stop is not yet confirmed"
            ) from error
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get(
        "/{token}",
        response_model=PlaybackReport,
        operation_id="getPlaybackStatus",
        summary="Read progress, playback evidence, and event completion by token",
    )
    def status(token: str):
        return JSONResponse(service.executor.store.report(token), headers={"Cache-Control": "no-store"})

    @app.exception_handler(ExecutorError)
    async def failure(request, error):
        return problem_response(error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error):
        if not request.url.path.startswith("/v1/playbacks"):
            return await request_validation_exception_handler(request, error)
        return problem_response(
            ExecutorError(
                "invalid_request",
                "Request does not match the playback contract; check field types, identities, original options, and timezone-aware deadline",
                status=422,
                retryable=False,
            )
        )

    app.add_middleware(ExecutorBodyLimit)
    app.include_router(router)
