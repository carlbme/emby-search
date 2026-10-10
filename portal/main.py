import asyncio
import hmac
import ipaddress
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlsplit

from portal.settings import Settings
from portal.store import Store
from portal.services import Services, UpstreamError, public_request

ASSETS = Path(__file__).parent / "static"
COOKIE = "media_search_session"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(max_length=1024)


class Submission(Input):
    kind: Literal["movie", "tv", "anime", "cartoons"]
    provider_id: int = Field(gt=0, le=2147483647)


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope["headers"]).get(b"content-length")
        if length and (len(length) > 10 or not length.isdigit() or int(length) > 8192):
            return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
        # Bound streamed bodies before FastAPI parses them; never accumulate an unbounded upload.
        chunks, count = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            count += len(chunk)
            if count > 8192:
                return await JSONResponse({"detail": "Request too large"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        sent = False
        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


def create_app(settings=None, transport=None, run_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or Settings.from_env()
        app.state.store = Store(app.state.settings)
        app.state.services = Services(app.state.settings, app.state.store, transport)
        task = asyncio.create_task(app.state.services.worker()) if run_worker else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await app.state.services.http.aclose()

    app = FastAPI(title="Media Search", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(BodyLimit)
    limits = defaultdict(deque)

    def ip(request):
        peer = request.client.host if request.client else "unknown"
        candidate = request.headers.get("cf-connecting-ip") if peer in request.app.state.settings.trusted_proxy_ips else peer
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return peer

    def rate_limit(request, bucket, max_count):
        now = time.monotonic()
        key = (bucket, ip(request))
        if len(limits) >= 10000 and key not in limits:
            for old in list(limits):
                if not limits[old] or limits[old][-1] < now - 60:
                    del limits[old]
            if len(limits) >= 10000:
                raise HTTPException(429, "Too many requests; try again in a minute")
        queue = limits[key]
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= max_count:
            raise HTTPException(429, "Too many requests; try again in a minute")
        queue.append(now)
        if len(limits) > 10000:
            for old in list(limits):
                if not limits[old] or limits[old][-1] < now - 60:
                    del limits[old]

    def origin(request):
        if request.headers.get("origin") not in request.app.state.settings.origins:
            raise HTTPException(403, "Request origin rejected")

    async def session(request, mutation=False):
        current = request.app.state.store.session(request.cookies.get(COOKIE))
        if current is None:
            raise HTTPException(401, "Sign in to continue")
        if mutation:
            origin(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), current["csrf"]):
                raise HTTPException(403, "Request verification failed")
        try:
            await request.app.state.services.validate_session(current)
        except UpstreamError as exc:
            if exc.status in (401, 403, 404):
                request.app.state.store.logout(current["id"])
                raise HTTPException(401, "Sign in again to continue") from None
            raise HTTPException(503, "Emby is temporarily unavailable") from None
        return current

    @app.middleware("http")
    async def headers(request, call_next):
        allowed = {urlsplit(x).hostname for x in request.app.state.settings.origins} | {"localhost", "127.0.0.1", "::1"}
        if request.url.hostname not in allowed:
            return JSONResponse({"detail": "Request host rejected"}, status_code=400)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.exception_handler(UpstreamError)
    async def upstream_error(request, exc):
        return JSONResponse({"detail": "Media service temporarily unavailable"}, status_code=503)

    @app.get("/")
    async def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/assets/{name}")
    async def asset(name: Literal["portal.css", "portal.js"]):
        return FileResponse(ASSETS / name)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.post("/api/login")
    async def login(payload: Login, request: Request):
        origin(request)
        rate_limit(request, "login", 10)
        try:
            user, token = await request.app.state.services.authenticate(payload.username, payload.password)
        except UpstreamError as exc:
            if exc.status in (400, 401, 403):
                raise HTTPException(401, "Username or password not accepted") from None
            raise HTTPException(503, "Emby is temporarily unavailable") from None
        existing = request.cookies.get(COOKIE)
        if existing:
            request.app.state.store.logout(existing)
        s = request.app.state.settings
        sid, csrf = request.app.state.store.new_session(user["Id"], user["Name"], token, s.session_seconds)
        response = JSONResponse({"name": user["Name"], "csrf": csrf})
        response.set_cookie(COOKIE, sid, max_age=s.session_seconds, secure=s.secure_cookie, httponly=True, samesite="strict")
        return response

    @app.get("/api/me")
    async def me(request: Request):
        current = await session(request)
        return {"name": current["name"], "csrf": current["csrf"]}

    @app.post("/api/logout")
    async def logout(request: Request):
        current = await session(request, mutation=True)
        request.app.state.store.logout(current["id"])
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/search")
    async def search(request: Request, kind: Literal["movie", "tv", "anime", "cartoons"], q: str):
        await session(request)
        rate_limit(request, "search", 20)
        query = q.strip()
        if not query or len(query) > 200 or any(ord(c) < 32 for c in query):
            raise HTTPException(422, "Enter a title of up to 200 characters")
        return {"results": await request.app.state.services.search(kind, query)}

    @app.post("/api/requests", status_code=202)
    async def submit(payload: Submission, request: Request):
        current = await session(request, mutation=True)
        rate_limit(request, "submit", 10)
        row = await request.app.state.services.submit(payload.kind, payload.provider_id, current["user_id"])
        return public_request(row, request.app.state.settings.emby_public_url)

    @app.get("/api/requests")
    async def requests(request: Request):
        current = await session(request)
        return {"requests": [public_request(row, request.app.state.settings.emby_public_url) for row in request.app.state.store.requests(current["user_id"])]}

    @app.get("/api/requests/{rid}")
    async def status(rid: str, request: Request):
        current = await session(request)
        row = request.app.state.store.get(rid, current["user_id"])
        if row is None:
            raise HTTPException(404, "Request not found")
        return public_request(row, request.app.state.settings.emby_public_url)

    return app


app = create_app()
