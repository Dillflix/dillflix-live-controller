"""Trust only identity supplied by the authenticated reverse proxy.

The secret authenticates nginx, not the browser. nginx must overwrite every
X-Dillflix-* header, authorize the admin service, and isolate the port.
Explicit guest mode bypasses authentication only on the public HTTP surface.
Explicit trusted-lan admin mode permits direct administration without login.
"""

import hmac
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

LEGACY_ADMIN = {"type": "admin", "id": "legacy", "name": "Admin"}
GUEST = {"type": "user", "id": "guest:anonymous", "name": "Guest"}


class ProxyAccess:
    def __init__(self, app, secret, public_auth_mode="proxy", admin_auth_mode="proxy"):
        self.app = app
        self.secret = secret
        self.public_auth_mode = public_auth_mode
        self.admin_auth_mode = admin_auth_mode

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        # Container liveness must work without revealing the administrative health report.
        if scope["type"] == "http" and scope["path"] == "/healthz" and scope["method"] == "GET":
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        path = scope["path"]
        public = path.startswith("/api/public/")
        viewer_path = public or path in {"/public", "/public/"} or path.startswith("/assets/")
        guest = self.public_auth_mode == "guest" and viewer_path and scope["type"] == "http"
        key = headers.get("x-dillflix-proxy-key", "")
        trusted_proxy = bool(self.secret and hmac.compare_digest(key.encode(), self.secret.encode()))
        status, detail = 0, ""
        actor = LEGACY_ADMIN.copy()
        if guest:
            # Never accept a visitor-supplied username or role in anonymous mode.
            actor = GUEST.copy()
        elif self.admin_auth_mode == "trusted-lan" and not public:
            # An explicit deployment choice, never inferred from guest mode or
            # caller-supplied identity headers. Public APIs retain their policy.
            actor = LEGACY_ADMIN.copy()
        elif not self.secret:
            if self.public_auth_mode == "guest":
                status, detail = 403, "Administrator access requires a configured authentication proxy."
            elif public:
                status, detail = 503, "Public access requires a configured authentication proxy."
        else:
            role = headers.get("x-dillflix-role", "")
            user = headers.get("x-dillflix-user", "").strip()
            if not trusted_proxy or not user or len(user) > 200:
                status, detail = 401, "Sign in through PlexSSO to continue."
            elif role not in {"admin", "user"}:
                status, detail = 403, "The authentication proxy must supply an authorized role."
            else:
                actor = {"type": role, "id": "plexsso:" + user, "name": user}
                if role != "admin" and (not viewer_path or scope["type"] == "websocket"):
                    status, detail = 403, "Administrator access required."
        if (
            not status
            and (guest or self.secret or self.admin_auth_mode == "trusted-lan")
            and scope["type"] == "http"
            and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
        ):
            try:
                origin = urlsplit(headers.get("origin", ""))
            except ValueError:
                origin = urlsplit("")
            scheme = scope.get("scheme", "http")
            if trusted_proxy:
                scheme = headers.get("x-forwarded-proto", scheme)
            # Guest requests retain the same cross-origin protection as signed-in users.
            if (
                origin.scheme != scheme
                or origin.netloc != headers.get("host")
                or origin.path not in {"", "/"}
                or origin.query
                or origin.fragment
                or headers.get("sec-fetch-site") not in {None, "same-origin"}
            ):
                status, detail = 403, "Actions require the same origin."
        if status:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
            return
        scope.setdefault("state", {})["actor"] = actor
        await self.app(scope, receive, send)
