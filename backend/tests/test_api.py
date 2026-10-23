"""API tests: authentication/authorization failures, validation errors, CRS workflow, mapping
confirmation, dependency-safe deletion, OGC API Features, health/readiness, jobs."""

import io

from tests.conftest import FIXTURES, run_jobs, study_area_geojson, wait_job

P = "/api/v1/projects"


def _project(api, name="api-test"):
    return api.ok(api.post(P, json={"name": name, "working_crs": 32643}))["id"]


def _dataset(api, pid, kind="parcels", name="d"):
    return api.ok(api.post(f"{P}/{pid}/datasets", json={"name": name, "kind": kind, "provenance": "synthetic"}))["id"]


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready" and "postgis" in r.json()["database"]


def test_request_id_propagates(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"
    r = client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
    assert r.headers["X-Request-ID"] != "bad id with spaces"


def test_unauthenticated_requests_rejected(client):
    r = client.get(P)
    assert r.status_code == 401 and r.json()["error"]["code"] == "not_authenticated"
    r = client.get(P, headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


def test_wrong_password(client, users):
    r = client.post("/api/v1/auth/token", data={"username": "admin@test.local", "password": "nope-nope-nope"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials"


def test_role_enforcement(viewer, analyst, admin):
    r = viewer.post(P, json={"name": "x", "working_crs": 32643})
    assert r.status_code == 403
    assert viewer.get("/api/v1/users").status_code == 403
    assert admin.get("/api/v1/users").status_code == 200
    r = admin.post("/api/v1/users", json={"email": "weak@test.local", "password": "short", "role": "viewer"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


def test_project_isolation(analyst, viewer):
    pid = _project(analyst, "private")
    assert viewer.get(f"{P}/{pid}").status_code == 404  # existence not revealed
    assert viewer.get(f"{P}/{pid}/datasets").status_code == 404


def test_project_membership_caps_role(analyst, reviewer, users):
    pid = _project(analyst, "membership")
    analyst.ok(analyst.put(f"{P}/{pid}/members", json={"user_id": users["reviewer"], "role": "viewer"}))
    # reviewer globally, but only viewer in this project -> cannot create datasets
    r = reviewer.post(f"{P}/{pid}/datasets", json={"name": "x", "kind": "parcels"})
    assert r.status_code == 403


def test_validation_errors_structured(analyst):
    r = analyst.post(P, json={"name": "", "working_crs": 32643})
    body = r.json()
    assert r.status_code == 422 and body["error"]["code"] == "validation_error" and body["error"]["request_id"]
    r = analyst.post(P, json={"name": "geo", "working_crs": 4326})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_working_crs"
    r = analyst.post(P, json={"name": "nocrs"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "working_crs_required"
    bad_area = {"type": "Polygon", "coordinates": [[[0, 0], [10, 10], [10, 0], [0, 10], [0, 0]]]}
    r = analyst.post(P, json={"name": "badarea", "study_area_geojson": bad_area})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_geometry"


def test_upload_rejections(analyst):
    pid = _project(analyst, "uploads")
    did = _dataset(analyst, pid)
    r = analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("x.dwg", b"abc")})
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_format"
    r = analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("x.geojson", b"")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "empty_file"
    r = analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("x.geojson", b"{not json")})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unreadable"
    with open(FIXTURES / "parcels_missing_shx.zip", "rb") as fh:
        r = analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("p.zip", fh)})
    assert r.status_code == 422 and r.json()["error"]["code"] == "shapefile_incomplete"
    img = _dataset(analyst, pid, "imagery", "img")
    with open(FIXTURES / "revenue_parcels.geojson", "rb") as fh:
        r = analyst.post(f"{P}/{pid}/datasets/{img}/versions", files={"file": ("r.geojson", fh)})
    assert r.status_code == 422 and r.json()["error"]["code"] == "kind_mismatch"
    # nothing half-registered after rejections
    assert analyst.ok(analyst.get(f"{P}/{pid}/datasets/{did}/versions")) == []


def test_upload_size_limit(analyst, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_mb", 0)
    pid = _project(analyst, "size")
    did = _dataset(analyst, pid)
    r = analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("x.geojson", b"{}" * 10)})
    assert r.status_code == 413


def test_missing_crs_workflow(analyst):
    pid = _project(analyst, "crs")
    did = _dataset(analyst, pid)
    with open(FIXTURES / "parcels_no_prj.zip", "rb") as fh:
        up = analyst.ok(analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("p.zip", fh)}))
    vid = up["version"]["id"]
    assert up["version"]["status"] == "needs_crs" and up["version"]["source_crs"] is None
    r = analyst.post(f"{P}/{pid}/versions/{vid}/process", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "crs_required"
    r = analyst.post(f"{P}/{pid}/versions/{vid}/crs", json={"crs": "EPSG:32643"})
    assert r.status_code == 422  # justification is mandatory
    r = analyst.post(f"{P}/{pid}/versions/{vid}/crs", json={"crs": "NOT-A-CRS", "justification": "x" * 5})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_crs"
    v = analyst.ok(analyst.post(f"{P}/{pid}/versions/{vid}/crs",
                                json={"crs": "EPSG:32643", "justification": "Supplier metadata sheet, UTM 43N"}))
    assert v["crs_confirmed"] and v["source_crs"] == "EPSG:32643"
    assert any(e["step"] == "crs_confirmed" for e in v["processing_log"])
    job = analyst.ok(analyst.post(f"{P}/{pid}/versions/{vid}/process", json={}))
    assert wait_job(analyst, pid, job)["status"] == "succeeded"
    fc = analyst.ok(analyst.get(f"{P}/{pid}/versions/{vid}/features"))
    xs = [c[0] for c in fc["features"][0]["geometry"]["coordinates"][0]]
    assert all(73 < x < 74 for x in xs)  # reprojected to lon/lat


def test_ambiguous_mapping_requires_confirmation(analyst):
    pid = _project(analyst, "mapping")
    did = _dataset(analyst, pid)
    geo = ('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"area":1,"plot_area":2,'
           '"plot_no":"A1"},"geometry":{"type":"Polygon","coordinates":[[[73.8,18.5],[73.801,18.5],[73.801,18.501],'
           '[73.8,18.5]]]}}]}')
    analyst.ok(analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("m.geojson", geo.encode())}))
    maps = {m["source_field"]: m for m in analyst.ok(analyst.get(f"{P}/{pid}/datasets/{did}/mappings"))}
    assert maps["plot_no"]["status"] == "approved" and maps["plot_no"]["method"] == "alias"
    assert maps["plot_area"]["ambiguous"] and maps["plot_area"]["status"] == "suggested"
    r = analyst.patch(f"{P}/{pid}/mappings/{maps['plot_area']['id']}", json={"status": "approved"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ambiguous_mapping"
    analyst.ok(analyst.patch(f"{P}/{pid}/mappings/{maps['plot_area']['id']}",
                             json={"status": "approved", "confirm_ambiguous": True}))
    r = analyst.patch(f"{P}/{pid}/mappings/{maps['area']['id']}", json={"status": "approved",
                                                                       "confirm_ambiguous": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "target_taken"


def test_delete_respects_dependencies(analyst, reviewer, users):
    pid = _project(analyst, "deletion")
    analyst.ok(analyst.put(f"{P}/{pid}/members", json={"user_id": users["reviewer"], "role": "reviewer"}))
    a, b = _dataset(analyst, pid, name="a"), _dataset(analyst, pid, name="b")
    for did, name in ((a, "municipal_parcels.gpkg"), (b, "revenue_parcels.geojson")):
        with open(FIXTURES / name, "rb") as fh:
            vid = analyst.ok(analyst.post(f"{P}/{pid}/datasets/{did}/versions",
                                          files={"file": (name, fh)}))["version"]["id"]
        wait_job(analyst, pid, analyst.ok(analyst.post(f"{P}/{pid}/versions/{vid}/process", json={})))
        if did == a:
            va = vid
        else:
            vb = vid
    analyst.ok(analyst.post(f"{P}/{pid}/match-runs", json={"left_version_id": va, "right_version_id": vb,
                                                           "match_type": "parcel_parcel"}))
    run_jobs()
    assert analyst.delete(f"{P}/{pid}/datasets/{a}?mode=delete").status_code == 403  # analyst cannot delete
    r = reviewer.delete(f"{P}/{pid}/datasets/{a}?mode=delete")
    assert r.status_code == 409 and r.json()["error"]["details"]["match_runs"] >= 1
    r = reviewer.ok(reviewer.delete(f"{P}/{pid}/datasets/{a}"))
    assert r["archived"] is True
    names = [d["name"] for d in analyst.ok(analyst.get(f"{P}/{pid}/datasets"))]
    assert "a" not in names
    c = _dataset(analyst, pid, name="c")
    assert reviewer.ok(reviewer.delete(f"{P}/{pid}/datasets/{c}?mode=delete"))["deleted"] is True


def test_match_run_validation(analyst):
    pid = _project(analyst, "matchval")
    a, b = _dataset(analyst, pid, "parcels", "a"), _dataset(analyst, pid, "buildings", "b")
    vids = []
    for did, name in ((a, "municipal_parcels.gpkg"), (b, "buildings_source_b.gpkg")):
        with open(FIXTURES / name, "rb") as fh:
            vids.append(analyst.ok(analyst.post(f"{P}/{pid}/datasets/{did}/versions",
                                                files={"file": (name, fh)}))["version"]["id"])
    r = analyst.post(f"{P}/{pid}/match-runs", json={"left_version_id": vids[0], "right_version_id": vids[1],
                                                    "match_type": "parcel_parcel"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "kind_mismatch"
    r = analyst.post(f"{P}/{pid}/match-runs", json={"left_version_id": vids[1], "right_version_id": vids[0],
                                                    "match_type": "building_parcel"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_processed"


def test_ogc_api_features(admin):
    pid = admin.ok(admin.get(P))[-1]["id"]  # earliest project (the end-to-end scenario, if it ran)
    landing = admin.ok(admin.get(f"{P}/{pid}/ogc"))
    assert any(link["rel"] == "conformance" for link in landing["links"])
    conf = admin.ok(admin.get(f"{P}/{pid}/ogc/conformance"))
    assert "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core" in conf["conformsTo"]
    cols = admin.ok(admin.get(f"{P}/{pid}/ogc/collections"))
    assert {c["id"] for c in cols["collections"]} == {"parcels", "buildings"}
    items = admin.ok(admin.get(f"{P}/{pid}/ogc/collections/parcels/items", params={"limit": 2}))
    assert items["type"] == "FeatureCollection" and items["numberReturned"] <= 2
    assert admin.get(f"{P}/{pid}/ogc/collections/nope/items").status_code == 404
    assert admin.get(f"{P}/{pid}/ogc/collections/parcels/items", params={"bbox": "1,2,3"}).status_code == 422


def test_metrics_come_from_database(analyst):
    pid = _project(analyst, "metrics")
    m = analyst.ok(analyst.get(f"{P}/{pid}/metrics"))
    assert m["datasets"] == [] and m["conflicts"] == []
    _dataset(analyst, pid)
    m = analyst.ok(analyst.get(f"{P}/{pid}/metrics"))
    assert m["datasets"][0]["datasets"] == 1 and m["datasets"][0]["features"] == 0


def test_job_failure_is_recorded_and_retryable(analyst):
    pid = _project(analyst, "jobs")
    did = _dataset(analyst, pid)
    with open(FIXTURES / "municipal_parcels.gpkg", "rb") as fh:
        up = analyst.ok(analyst.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": ("m.gpkg", fh)}))
    vid = up["version"]["id"]
    from app.db import SessionLocal
    from app.models import DatasetVersion
    from app.storage import get_storage

    with SessionLocal() as db:
        key = db.get(DatasetVersion, vid).storage_key
    path = get_storage().local_path(key)
    original = path.read_bytes()
    path.write_bytes(b"corrupted after upload")
    job = analyst.ok(analyst.post(f"{P}/{pid}/versions/{vid}/process", json={}))
    j = wait_job(analyst, pid, job)
    assert j["status"] == "failed" and j["error"]
    v = analyst.ok(analyst.get(f"{P}/{pid}/versions/{vid}"))
    assert v["status"] == "failed"
    path.write_bytes(original)
    retry = analyst.ok(analyst.post(f"{P}/{pid}/jobs/{job['id']}/retry"))
    assert wait_job(analyst, pid, retry)["status"] == "succeeded"
    assert analyst.post(f"{P}/{pid}/jobs/{retry['id']}/retry").status_code == 409


def test_study_area_and_suggested_crs(analyst):
    pid = analyst.ok(analyst.post(P, json={"name": "sa", "study_area_geojson": study_area_geojson()}))["id"]
    assert analyst.ok(analyst.get(f"{P}/{pid}/suggest-crs"))["epsg"] == 32643
    fc = analyst.ok(analyst.get(f"{P}/{pid}/study-areas"))
    assert fc["features"][0]["properties"]["area_m2"] > 10000


def test_personal_data_redacted_for_viewers(analyst, viewer, users):
    pid = _project(analyst, "pii")
    analyst.ok(analyst.put(f"{P}/{pid}/members", json={"user_id": users["viewer"], "role": "viewer"}))
    did = _dataset(analyst, pid, "property_records")
    csv = b"property_number,plot_no,owner_name\nPROP-1,P1,Synthetic Owner A\n"
    vid = analyst.ok(analyst.post(f"{P}/{pid}/datasets/{did}/versions",
                                  files={"file": ("r.csv", io.BytesIO(csv))}))["version"]["id"]
    wait_job(analyst, pid, analyst.ok(analyst.post(f"{P}/{pid}/versions/{vid}/process", json={})))
    f = viewer.ok(viewer.get(f"{P}/{pid}/versions/{vid}/features"))["features"][0]["properties"]
    assert f["attributes"]["owner_name"] == "[redacted]"
    assert f["attributes"]["property_number"] == "PROP-1"
