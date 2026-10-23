"""Worker process: ``python -m app.worker``. Polls the processing_jobs table."""

import logging
import signal
import time

from app.config import get_settings
from app.db import SessionLocal
from app.jobs import claim_next, recover_stale, run_job
from app.logging_setup import configure_logging

_running = True


def _stop(*_):
    global _running
    _running = False


def main() -> None:
    s = get_settings()
    configure_logging(s.log_level, s.log_json)
    log = logging.getLogger("bhuintel.worker")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    with SessionLocal() as db:
        n = recover_stale(db)
    log.info("worker started", extra={"ctx": {"recovered_stale_jobs": n}})
    while _running:
        with SessionLocal() as db:
            job = claim_next(db)
        if job is None:
            time.sleep(s.worker_poll_seconds)
            continue
        log.info("job started", extra={"ctx": {"job_id": job.id, "type": job.job_type}})
        done = run_job(job.id)
        log.info("job finished", extra={"ctx": {"job_id": done.id, "status": done.status}})
    log.info("worker stopped")


if __name__ == "__main__":
    main()
