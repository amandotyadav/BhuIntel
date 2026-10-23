"""Test configuration: a real PostGIS database (BHU_TEST_DATABASE_URL), migrated with Alembic per session."""

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

TEST_DB = os.environ.get("BHU_TEST_DATABASE_URL", "postgresql+psycopg://bhuintel:bhuintel@localhost:5432/bhuintel_test")
_storage = tempfile.mkdtemp(prefix="bhu_test_storage_")
os.environ.update({
    "BHU_DATABASE_URL": TEST_DB,
    "BHU_STORAGE_ROOT": _storage,
    "BHU_ENV": "test",
    "BHU_LOG_JSON": "false",
    "BHU_LOG_LEVEL": "WARNING",
    "BHU_RATE_LIMIT_PER_MINUTE": "100000",
    "BHU_LOGIN_RATE_LIMIT_PER_MINUTE": "1000",
    "BHU_SECRET_KEY": "test-secret-key-0123456789-abcdefghijklmnop",
})

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "synthetic_pune"


def _reset_database() -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    eng = create_engine(TEST_DB)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    eng.dispose()
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["database_url"] = TEST_DB
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def database():
    from app.config import get_settings

    get_settings.cache_clear()
    _reset_database()
    from app.db import reset_engine

    reset_engine(TEST_DB)
    yield
    shutil.rmtree(_storage, ignore_errors=True)


@pytest.fixture(scope="session")
def client(database):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def _make_user(email: str, role: str, password: str = "correct-horse-battery"):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import User
    from app.security import hash_password

    with SessionLocal() as db:
        u = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if u is None:
            u = User(email=email, full_name=role.title(), role=role, password_hash=hash_password(password))
            db.add(u)
            db.commit()
        return u.id


class Api:
    def __init__(self, client, token: str):
        self.c = client
        self.h = {"Authorization": f"Bearer {token}"}

    def get(self, url, **kw):
        return self.c.get(url, headers=self.h, **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers=self.h, **kw)

    def patch(self, url, **kw):
        return self.c.patch(url, headers=self.h, **kw)

    def put(self, url, **kw):
        return self.c.put(url, headers=self.h, **kw)

    def delete(self, url, **kw):
        return self.c.delete(url, headers=self.h, **kw)

    def ok(self, resp, status=(200, 201, 202)):
        statuses = status if isinstance(status, tuple) else (status,)
        assert resp.status_code in statuses, f"{resp.status_code}: {resp.text[:2000]}"
        return resp.json()


def login(client, email: str, password: str = "correct-horse-battery") -> str:
    r = client.post("/api/v1/auth/token", data={"username": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def users(client):
    ids = {role: _make_user(f"{role}@test.local", role) for role in ("admin", "reviewer", "analyst", "viewer")}
    return ids


@pytest.fixture(scope="session")
def admin(client, users) -> Api:
    return Api(client, login(client, "admin@test.local"))


@pytest.fixture(scope="session")
def reviewer(client, users) -> Api:
    return Api(client, login(client, "reviewer@test.local"))


@pytest.fixture(scope="session")
def analyst(client, users) -> Api:
    return Api(client, login(client, "analyst@test.local"))


@pytest.fixture(scope="session")
def viewer(client, users) -> Api:
    return Api(client, login(client, "viewer@test.local"))


def run_jobs() -> int:
    from app.jobs import run_pending

    return run_pending()


def wait_job(api: Api, project_id: int, job: dict) -> dict:
    run_jobs()
    for _ in range(50):
        j = api.ok(api.get(f"/api/v1/projects/{project_id}/jobs/{job['id']}"))
        if j["status"] not in ("queued", "running"):
            return j
        time.sleep(0.1)
        run_jobs()
    raise AssertionError(f"job {job['id']} did not finish")


def study_area_geojson() -> dict:
    import json

    fc = json.loads((FIXTURES / "study_area.geojson").read_text())
    return fc["features"][0]["geometry"]
