"""Bootstrap users and (optionally) load the SYNTHETIC demo project.

    python scripts/seed.py                      # create the bootstrap admin if no users exist
    python scripts/seed.py --demo-users         # also create demo analyst/reviewer/viewer accounts
    python scripts/seed.py --demo               # load fixtures/synthetic_pune through the real API pipeline

Passwords come from BHU_BOOTSTRAP_ADMIN_PASSWORD / --password; nothing is hard-coded for production.
The demo pipeline uses the same HTTP endpoints and job handlers as the UI (FastAPI TestClient in-process).
"""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402

FIXTURES = Path(os.environ.get("BHU_FIXTURES_DIR", Path(__file__).resolve().parents[2] / "fixtures" / "synthetic_pune"))
P = "/api/v1/projects"


def ensure_user(email: str, password: str, role: str, name: str) -> bool:
    with SessionLocal() as db:
        if db.execute(select(User).where(func.lower(User.email) == email.lower())).scalar_one_or_none():
            return False
        db.add(User(email=email.lower(), full_name=name, role=role, password_hash=hash_password(password)))
        db.commit()
        return True


def bootstrap_admin(password: str | None) -> None:
    s = get_settings()
    with SessionLocal() as db:
        if db.execute(select(func.count(User.id))).scalar():
            print("users already exist; bootstrap admin not created")
            return
    pw = password or s.bootstrap_admin_password
    generated = pw is None
    pw = pw or secrets.token_urlsafe(16)
    ensure_user(s.bootstrap_admin_email, pw, "admin", "Administrator")
    print(f"created admin {s.bootstrap_admin_email}" + (f" with generated password: {pw}" if generated else ""))


def demo_users(password: str) -> None:
    for role in ("analyst", "reviewer", "viewer"):
        if ensure_user(f"{role}@example.org", password, role, f"Demo {role}"):
            print(f"created demo user {role}@example.org")


def load_demo(email: str, password: str) -> None:
    import time

    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from app.jobs import run_pending
    from app.main import create_app

    def drain(timeout_s: float = 1800) -> None:
        """Run queued jobs here, and also wait for any a separate worker process has claimed."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            run_pending()
            with SessionLocal() as db:
                busy = db.execute(text("SELECT count(*) FROM processing_jobs WHERE status IN ('queued','running')")).scalar()
            if not busy:
                return
            time.sleep(0.5)
        raise SystemExit("timed out waiting for jobs")

    c = TestClient(create_app())
    tok = c.post("/api/v1/auth/token", data={"username": email, "password": password})
    tok.raise_for_status()
    h = {"Authorization": f"Bearer {tok.json()['access_token']}"}

    def ok(r):
        if r.status_code >= 300:
            raise SystemExit(f"{r.request.method} {r.request.url}: {r.status_code} {r.text[:500]}")
        return r.json()

    sa = json.loads((FIXTURES / "study_area.geojson").read_text())["features"][0]["geometry"]
    pid = ok(c.post(P, headers=h, json={
        "name": "SYNTHETIC demo - Pune grid", "default_tolerance_m": 5.0, "study_area_geojson": sa,
        "description": "Synthetic fixture (not real land records) demonstrating the end-to-end workflow."}))["id"]
    for u in ("reviewer", "analyst", "viewer"):
        with SessionLocal() as db:
            uid = db.execute(select(User.id).where(User.email == f"{u}@example.org")).scalar_one_or_none()
        if uid:
            ok(c.put(f"{P}/{pid}/members", headers=h, json={"user_id": uid, "role": u}))

    def dataset(name, kind, fname, reliability, mapping_overrides, **kw):
        did = ok(c.post(f"{P}/{pid}/datasets", headers=h, json={
            "name": name, "kind": kind, "provenance": "synthetic", "reliability": reliability,
            "license": "Synthetic test data (Apache-2.0 with this repository)", "provider": "BhuIntel fixture generator",
            **kw}))["id"]
        with open(FIXTURES / fname, "rb") as fh:
            vid = ok(c.post(f"{P}/{pid}/datasets/{did}/versions", headers=h, files={"file": (fname, fh)}))["version"]["id"]
        for m in ok(c.get(f"{P}/{pid}/datasets/{did}/mappings", headers=h)):
            target = mapping_overrides.get(m["source_field"], "keep")
            if target is None and m["status"] == "approved":
                ok(c.patch(f"{P}/{pid}/mappings/{m['id']}", headers=h, json={"status": "rejected"}))
            elif target not in (None, "keep"):
                ok(c.patch(f"{P}/{pid}/mappings/{m['id']}", headers=h, json={"status": "approved", "target_field": target}))
        return did, vid

    _, v_muni = dataset("Municipal parcels (SYNTHETIC)", "parcels", "municipal_parcels.gpkg", 0.7,
                        {"data_class": None, "ward_no": "ward"})
    _, v_rev = dataset("Revenue parcels (SYNTHETIC)", "parcels", "revenue_parcels.geojson", 0.6,
                       {"parcel_id": "parcel_id", "data_class": None}, positional_accuracy_m=1.0)
    _, v_props = dataset("Property register (SYNTHETIC)", "property_records", "property_records.csv", 0.5,
                         {"data_class": None})
    ok(c.post(f"{P}/{pid}/versions/{v_props}/crs", headers=h, json={
        "crs": "EPSG:4326", "justification": "Fixture README documents WGS84 latitude/longitude columns"}))
    d_bA, v_bA = dataset("Buildings A - MS-like (SYNTHETIC)", "buildings", "buildings_source_a_v1.geojson", 0.6,
                         {"bldg_id": "building_id", "data_class": None}, source_date="2023-01-01T00:00:00Z",
                         positional_accuracy_m=0.7)
    _, v_bB = dataset("Buildings B - OSM-like (SYNTHETIC)", "buildings", "buildings_source_b.gpkg", 0.5,
                      {"osm_id": "building_id", "data_class": None}, source_date="2024-06-01T00:00:00Z")
    with open(FIXTURES / "buildings_source_a_v2.geojson", "rb") as fh:
        v_bA2 = ok(c.post(f"{P}/{pid}/datasets/{d_bA}/versions", headers=h,
                          files={"file": ("buildings_source_a_v2.geojson", fh)}))["version"]["id"]
    _, v_lines = dataset("Utility lines (SYNTHETIC)", "utility_lines", "utility_lines.gpkg", 0.5, {"data_class": None})
    img = ok(c.post(f"{P}/{pid}/datasets", headers=h, json={"name": "Orthophoto 0.5 m (SYNTHETIC)", "kind": "imagery",
                                                            "provenance": "synthetic"}))["id"]
    with open(FIXTURES / "synthetic_orthophoto.tif", "rb") as fh:
        v_img = ok(c.post(f"{P}/{pid}/datasets/{img}/versions", headers=h,
                          files={"file": ("synthetic_orthophoto.tif", fh)}))["version"]["id"]

    for v in (v_muni, v_rev, v_props, v_bA, v_bB, v_bA2, v_lines, v_img):
        ok(c.post(f"{P}/{pid}/versions/{v}/process", headers=h, json={}))
    drain()
    runs = {}
    for name, (lv, rv, mt) in {"pp": (v_muni, v_rev, "parcel_parcel"), "rec": (v_props, v_muni, "record_feature"),
                               "bb": (v_bA, v_bB, "building_building"), "bp": (v_bA, v_muni, "building_parcel")}.items():
        runs[name] = ok(c.post(f"{P}/{pid}/match-runs", headers=h, json={"left_version_id": lv, "right_version_id": rv,
                                                                         "match_type": mt}))["id"]
    ok(c.post(f"{P}/{pid}/topology", headers=h, json={"version_id": v_muni}))
    ok(c.post(f"{P}/{pid}/topology", headers=h, json={"version_id": v_lines}))
    ok(c.post(f"{P}/{pid}/changes", headers=h, json={"old_version_id": v_bA, "new_version_id": v_bA2}))
    ok(c.post(f"{P}/{pid}/vision/extract", headers=h, json={"raster_version_id": v_img}))
    drain()
    ok(c.post(f"{P}/{pid}/integration/build", headers=h, json={
        "primary_parcel_version_id": v_muni, "parcel_match_run_id": runs["pp"], "record_match_run_id": runs["rec"],
        "building_match_run_id": runs["bb"], "building_parcel_run_id": runs["bp"]}))
    drain()
    jobs = ok(c.get(f"{P}/{pid}/jobs", headers=h, params={"limit": 100}))
    failed = [j for j in jobs if j["status"] not in ("succeeded",)]
    conflicts = ok(c.get(f"{P}/{pid}/conflicts", headers=h, params={"limit": 1}))["numberMatched"]
    print(f"demo project {pid} loaded: {len(jobs)} jobs ({len(failed)} not succeeded), {conflicts} conflicts detected")
    if failed:
        for j in failed:
            print(f"  job {j['id']} {j['job_type']}: {j['status']} {j['error']}")
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--password", help="password for bootstrap admin / demo users (default: env or generated)")
    ap.add_argument("--demo-users", action="store_true")
    ap.add_argument("--demo", action="store_true", help="load the synthetic demo project (implies --demo-users)")
    a = ap.parse_args()
    pw = a.password or get_settings().bootstrap_admin_password
    bootstrap_admin(pw)
    if a.demo or a.demo_users:
        if not pw:
            raise SystemExit("--password (or BHU_BOOTSTRAP_ADMIN_PASSWORD) is required for demo users")
        demo_users(pw)
    if a.demo:
        load_demo(get_settings().bootstrap_admin_email, pw)


if __name__ == "__main__":
    main()
