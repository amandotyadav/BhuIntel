"""FastAPI application factory."""

import logging
import threading
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import analysis, auth, datasets, health, ogc, projects, reviews
from app.config import get_settings
from app.errors import install_error_handlers
from app.logging_setup import configure_logging
from app.middleware import RequestContextMiddleware

DESCRIPTION = """
BhuIntel integrates heterogeneous land-related geospatial datasets: CRS normalisation, attribute mapping,
spatial matching, topology validation, conflict detection with explainable heuristic scores, human review
with an append-only audit trail, and validated exports.

**Scores are heuristic match scores unless explicitly labelled as model probabilities, and are never proof
of ownership or legal validity.**
"""


def _inline_worker() -> None:  # pragma: no cover - dev convenience
    from app.jobs import run_pending

    while True:
        try:
            run_pending(50)
        except Exception:  # keep the dev worker alive; failures are recorded on the jobs themselves
            logging.getLogger("bhuintel.worker").exception("inline worker loop error")
        time.sleep(get_settings().worker_poll_seconds)


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    app = FastAPI(title="BhuIntel API", version="0.1.0", description=DESCRIPTION)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-Request-ID"], expose_headers=["X-Request-ID"])
    install_error_handlers(app)
    for r in (health.router, auth.router, projects.router, datasets.router, analysis.router, reviews.router, ogc.router):
        app.include_router(r)
    if s.inline_worker:
        threading.Thread(target=_inline_worker, daemon=True, name="inline-worker").start()
    return app


app = create_app()
