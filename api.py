from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import web

from service import AuthError, AuthService, format_code, public_user

log = logging.getLogger("api")


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, limit: int, window: int = 60) -> bool:
        t = time.monotonic()
        start, count = self._hits.get(key, (t, 0))
        if t - start >= window:
            start, count = t, 0
        count += 1
        self._hits[key] = (start, count)
        if len(self._hits) > 50_000:
            self._hits = {k: v for k, v in self._hits.items() if t - v[0] < window}
        return count <= limit


def create_api(service: AuthService, *, cors_origins: list[str] | None = None,
               trust_proxy: bool = False) -> web.Application:
    limiter = RateLimiter()
    cors_origins = cors_origins or []

    def client_ip(request: web.Request) -> str:
        if trust_proxy:
            for header in ("CF-Connecting-IP", "X-Real-IP"):
                if request.headers.get(header):
                    return request.headers[header].strip()
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.remote or "unknown"

    def limit(request: web.Request, name: str, per_minute: int) -> None:
        if not limiter.hit(f"{name}:{client_ip(request)}", per_minute):
            raise AuthError("rate_limited", 429, "Za dużo zapytań. Spróbuj za chwilę.")

    async def body(request: web.Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError:
            raise AuthError("bad_request", 400, "Oczekiwano JSON w treści zapytania.") from None
        if not isinstance(data, dict):
            raise AuthError("bad_request", 400, "Oczekiwano obiektu JSON.")
        return data

    def bearer(request: web.Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return request.headers.get("X-Auth-Token")

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            try:
                response = await handler(request)
            except AuthError as e:
                response = web.json_response({"error": e.code, "message": e.message}, status=e.status)
            except web.HTTPException as e:
                response = web.json_response({"error": "http_error", "message": e.reason}, status=e.status)
            except Exception:
                log.exception("Błąd w %s %s", request.method, request.path)
                response = web.json_response({"error": "internal_error", "message": "Błąd serwera."}, status=500)
        origin = request.headers.get("Origin")
        if origin and ("*" in cors_origins or origin in cors_origins):
            response.headers["Access-Control-Allow-Origin"] = "*" if "*" in cors_origins else origin
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-HWID, X-Auth-Token"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    routes = web.RouteTableDef()

    @routes.get("/")
    @routes.get("/api/health")
    async def health(request: web.Request):
        return web.json_response({"ok": True, "service": "discord-login-bot"})

    @routes.get("/api/apps/{app_id}")
    async def app_info(request: web.Request):
        app = service.require_app(request.match_info["app_id"])
        return web.json_response({"id": app.id, "name": app.name, "hwid_required": app.hwid_lock})

    @routes.post("/api/auth/start")
    async def start(request: web.Request):
        limit(request, "start", 20)
        data = await body(request)
        req = service.start_device_login(data.get("app_id"), data.get("hwid"), client_ip(request))
        return web.json_response({
            "request_id": req.id,
            "code": format_code(req.code),
            "expires_at": req.expires_at,
            "expires_in": req.expires_at - req.created_at,
            "interval": 2,
            "instructions": f"Wpisz na Discordzie: /login {format_code(req.code)}",
        })

    @routes.get("/api/auth/poll/{request_id}")
    async def poll(request: web.Request):
        limit(request, "poll", 120)
        result = await service.poll_device_login(request.match_info["request_id"])
        return web.json_response(result)

    @routes.post("/api/auth/redeem")
    async def redeem(request: web.Request):
        limit(request, "redeem", 10)
        data = await body(request)
        issued = await service.redeem_code(data.get("app_id"), data.get("code"), data.get("hwid"),
                                           client_ip(request))
        return web.json_response(issued.to_json())

    @routes.get("/api/auth/me")
    async def me(request: web.Request):
        limit(request, "me", 120)
        hwid = request.headers.get("X-HWID") or request.query.get("hwid")
        session, app, user = await service.validate_token(bearer(request), hwid)
        return web.json_response({
            "valid": True,
            "session_id": session.id,
            "expires_at": session.expires_at,
            "app": {"id": app.id, "name": app.name},
            "user": public_user(user),
        })

    @routes.post("/api/auth/logout")
    async def logout(request: web.Request):
        limit(request, "logout", 30)
        return web.json_response({"ok": await service.logout(bearer(request))})

    app = web.Application(middlewares=[middleware], client_max_size=64 * 1024)
    app.add_routes(routes)
    return app
