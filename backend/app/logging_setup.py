"""Structured JSON logging with request correlation IDs.

Log records never include request bodies, passwords, tokens or attribute values of
features (which may contain personal data such as owner names).
"""

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_SENSITIVE_KEYS = {"password", "token", "authorization", "secret", "owner_name", "access_token"}


def _scrub(value):
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _SENSITIVE_KEYS else _scrub(v)) for k, v in value.items()}
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        extra = getattr(record, "ctx", None)
        if extra:
            payload.update(_scrub(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", as_json: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if as_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel("WARNING")


def log(logger: logging.Logger, level: int, msg: str, **ctx) -> None:
    logger.log(level, msg, extra={"ctx": ctx})
