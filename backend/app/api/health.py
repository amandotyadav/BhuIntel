import uuid

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import db_ready
from app.storage import get_storage

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
def health():
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe: database, PostGIS, migrations and storage")
def ready():
    ok_db, detail = db_ready()
    try:
        st = get_storage()
        probe = st.root / f".ready-{uuid.uuid4().hex}"
        probe.write_text("ok")
        probe.unlink()
        ok_st, st_detail = True, "writable"
    except OSError as exc:
        ok_st, st_detail = False, type(exc).__name__
    body = {"status": "ready" if ok_db and ok_st else "not_ready", "database": detail, "storage": st_detail}
    return JSONResponse(status_code=200 if ok_db and ok_st else 503, content=body)
