"""Request correlation IDs, access logging and in-memory rate limiting.

The rate limiter is per-process (sliding one-minute window keyed by client IP). For multi-replica
deployments put a shared limiter (e.g. at the reverse proxy) in front; documented in DEPLOYMENT.md.
"""

import logging
import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.errors import ApiError
from app.logging_setup import request_id_var

log = logging.getLogger("bhuintel.http")

_hits: dict[str, deque] = defaultdict(deque)
_login_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _allow(store: dict[str, deque], key: str, limit: int) -> bool:
    now = time.monotonic()
    with _lock:
        q = store[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def rate_limit_login(request: Request) -> None:
    if not _allow(_login_hits, _client(request), get_settings().login_rate_limit_per_minute):
        raise ApiError(429, "rate_limited", "Too many login attempts; try again in a minute")


def reset_rate_limits() -> None:
    with _lock:
        _hits.clear()
        _login_hits.clear()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id")
        if not rid or len(rid) > 64 or not all(c.isalnum() or c in "-_" for c in rid):
            rid = uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            if request.url.path.startswith("/api/") and not _allow(_hits, _client(request),
                                                                   get_settings().rate_limit_per_minute):
                resp = JSONResponse(status_code=429, content={"error": {"code": "rate_limited", "message":
                                                                        "Rate limit exceeded", "request_id": rid}})
            else:
                resp = await call_next(request)
            resp.headers["X-Request-ID"] = rid
            resp.headers["X-Content-Type-Options"] = "nosniff"
            resp.headers["Referrer-Policy"] = "no-referrer"
            log.info("request", extra={"ctx": {"method": request.method, "path": request.url.path,
                                               "status": resp.status_code,
                                               "ms": round((time.perf_counter() - start) * 1000, 1)}})
            return resp
        finally:
            request_id_var.reset(token)
