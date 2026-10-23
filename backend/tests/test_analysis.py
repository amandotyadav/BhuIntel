"""Change detection, ML training protocol, building extraction, utility topology, version-aware
synchronisation and acquisition connectors (HTTP mocked - these test our code, not data availability)."""

import gzip
import json

import httpx
import pytest
from shapely.geometry import box, shape

from tests.conftest import FIXTURES, run_jobs, wait_job

TRUTH = json.loads((FIXTURES / "ground_truth.json").read_text())
P = "/api/v1/projects"


def _upload_process(api, pid, did, name):
    with open(FIXTURES / name, "rb") as fh:
        up = api.ok(api.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": (name, fh)}))
    vid = up["version"]["id"]
    for m in api.ok(api.get(f"{P}/{pid}/datasets/{did}/mappings")):
        if m["source_field"] in ("bldg_id", "osm_id", "plot_no", "parcel_id"):
            api.ok(api.patch(f"{P}/{pid}/mappings/{m['id']}", json={"status": "approved", "target_field":
                                                                     "building_id" if "bldg" in m["source_field"] or
                                                                     "osm" in m["source_field"] else "parcel_id"}))
    j = wait_job(api, pid, api.ok(api.post(f"{P}/{pid}/versions/{vid}/process", json={})))
    assert j["status"] == "succeeded", j
    return vid


@pytest.fixture(scope="module")
def proj(analyst, users):
    pid = analyst.ok(analyst.post(P, json={"name": "analysis", "working_crs": 32643,
                                           "default_tolerance_m": 2.0}))["id"]
    analyst.ok(analyst.put(f"{P}/{pid}/members", json={"user_id": users["reviewer"], "role": "reviewer"}))
    analyst.ok(analyst.put(f"{P}/{pid}/members", json={"user_id": users["admin"], "role": "admin"}))
    return pid


def test_vector_change_detection_against_truth(analyst, proj):
    did = analyst.ok(analyst.post(f"{P}/{proj}/datasets", json={"name": "bA", "kind": "buildings",
                                                                "provenance": "synthetic",
                                                                "positional_accuracy_m": 0.7}))["id"]
    v1 = _upload_process(analyst, proj, did, "buildings_source_a_v1.geojson")
    v2 = _upload_process(analyst, proj, did, "buildings_source_a_v2.geojson")
    j = wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/changes", json={"old_version_id": v1,
                                                                                      "new_version_id": v2})))
    assert j["status"] == "succeeded", j
    fc = analyst.ok(analyst.get(f"{P}/{proj}/changes", params={"old_version_id": v1, "new_version_id": v2}))

    def ids(ctype, side):
        out = set()
        for f in fc["features"]:
            p = f["properties"]
            if p["change_type"] == ctype and p[f"{side}_feature_id"]:
                out.add(analyst.ok(analyst.get(f"{P}/{proj}/features/{p[f'{side}_feature_id']}"))["properties"]
                        ["source_id_raw"])
        return out

    ch = TRUTH["changes"]
    assert ids("added", "new") == set(ch["added"])
    assert ids("removed", "old") == set(ch["removed"])
    assert ids("geometry_modified", "new") == set(ch["geometry_modified"])
    assert ids("attribute_changed", "new") >= set(ch["attribute_changed"])
    # a 0.8 m shift with ~1 m combined positional uncertainty is NOT reported as a real geometry change
    assert not (ids("geometry_modified", "new") & set(ch["within_uncertainty"]))
    assert fc["summary"].get("geometry_modified") == 1


def test_ml_refuses_without_labels_then_trains_on_synthetic_labels(analyst, reviewer, admin, proj):
    from app.db import SessionLocal
    from app.models import MatchRun
    from app.services import ml

    j = wait_job(reviewer, proj, reviewer.ok(reviewer.post(f"{P}/{proj}/ml/train",
                                                           json={"match_type": "building_building"})))
    assert j["result"]["status"] == "insufficient_data"
    assert "deterministic matching remains in use" in j["result"]["training_info"]["message"]

    da = analyst.ok(analyst.post(f"{P}/{proj}/datasets", json={"name": "A", "kind": "buildings",
                                                               "provenance": "synthetic", "reliability": 0.6}))["id"]
    db_ = analyst.ok(analyst.post(f"{P}/{proj}/datasets", json={"name": "B", "kind": "buildings",
                                                                "provenance": "synthetic", "reliability": 0.5}))["id"]
    va = _upload_process(analyst, proj, da, "buildings_source_a_v1.geojson")
    vb = _upload_process(analyst, proj, db_, "buildings_source_b.gpkg")
    # wide tolerance so that many genuine non-matches become candidates (needed for negative labels)
    run = analyst.ok(analyst.post(f"{P}/{proj}/match-runs", json={
        "left_version_id": va, "right_version_id": vb, "match_type": "building_building", "tolerance_m": 40,
        "thresholds": {"store_min": 0.0, "match": 0.75, "possible": 0.5}}))
    run_jobs()
    truth = {tuple(p) for p in TRUTH["buildings"]["pairs_a_b"]}
    with SessionLocal() as db:
        n = ml.label_from_ground_truth(db, db.get(MatchRun, run["id"]), truth)
        db.commit()
    assert n >= ml.MIN_LABELS
    j = wait_job(reviewer, proj, reviewer.ok(reviewer.post(f"{P}/{proj}/ml/train",
                                                           json={"match_type": "building_building"})))
    res = j["result"]
    assert res["status"] == "trained", res
    m, base = res["metrics"]["model"], res["metrics"]["deterministic_baseline"]
    assert m["support"] == base["support"] > 0  # both evaluated on the same held-out rows
    assert "GroupShuffleSplit" in res["training_info"]["split"]
    assert res["metrics"]["evaluated_on"].endswith("synthetic_fixture")
    assert res["metrics"]["probability_calibration"].startswith("none")
    print("\n[synthetic-fixture ML evaluation] model:", m, "\nbaseline:", base)  # recorded in docs/TEST_RESULTS.md
    assert reviewer.post(f"{P}/{proj}/ml/models/{res['model_version_id']}/activate").status_code == 403
    admin.ok(admin.post(f"{P}/{proj}/ml/models/{res['model_version_id']}/activate"))
    rf = analyst.ok(analyst.post(f"{P}/{proj}/match-runs", json={"left_version_id": va, "right_version_id": vb,
                                                                 "match_type": "building_building",
                                                                 "method": "random_forest"}))
    run_jobs()
    rf_run = analyst.ok(analyst.get(f"{P}/{proj}/match-runs/{rf['id']}"))
    assert rf_run["status"] == "succeeded" and rf_run["model_version_id"] == res["model_version_id"]
    c = analyst.ok(analyst.get(f"{P}/{proj}/match-runs/{rf['id']}/candidates", params={"limit": 1}))["items"][0]
    assert c["score_kind"] == "model_probability_uncalibrated" and "baseline_score" in c["features"]
    csv = analyst.get(f"{P}/{proj}/ml/labels.csv", params={"match_type": "building_building"})
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("candidate_id,group,label")


def test_model_artifact_tamper_detected(proj, analyst):
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ModelVersion
    from app.services import ml
    from app.storage import get_storage

    with SessionLocal() as db:
        mv = db.execute(select(ModelVersion).where(ModelVersion.project_id == proj, ModelVersion.status == "trained")
                        ).scalars().first()
        path = get_storage().local_path(mv.artifact_key)
        original = path.read_bytes()
        path.write_bytes(original + b"x")
        with pytest.raises(ValueError, match="checksum"):
            ml.load_model(db, mv)
        path.write_bytes(original)
        assert ml.load_model(db, mv).version == mv.version


def test_building_extraction_pipeline_and_review(analyst, reviewer, proj):
    img = analyst.ok(analyst.post(f"{P}/{proj}/datasets", json={"name": "ortho", "kind": "imagery",
                                                                "provenance": "synthetic"}))["id"]
    with open(FIXTURES / "synthetic_orthophoto.tif", "rb") as fh:
        up = analyst.ok(analyst.post(f"{P}/{proj}/datasets/{img}/versions", files={"file": ("o.tif", fh)}))
    rv = up["version"]["id"]
    wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/versions/{rv}/process", json={})))
    j = wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/vision/extract",
                                                        json={"raster_version_id": rv, "overlap": 32})))
    assert j["status"] == "succeeded", j
    res = j["result"]
    assert res["model"]["is_ml"] is False and res["model"]["name"] == "threshold_baseline"
    preds = analyst.ok(analyst.get(f"{P}/{proj}/vision/predictions"))
    truth_buildings = [shape(f["geometry"]) for f in json.loads(
        (FIXTURES / "buildings_source_a_v1.geojson").read_text())["features"]]
    # each synthetic roof should be recovered once (no duplicates along tile seams)
    import geopandas as gpd

    tb = gpd.GeoSeries(truth_buildings, crs=4326).to_crs(32643).drop_duplicates()
    pb = gpd.GeoSeries([shape(f["geometry"]) for f in preds["features"]], crs=4326).to_crs(32643)
    hits = sum(1 for t in tb if any(t.intersection(p).area / t.union(p).area > 0.8 for p in pb))
    assert hits / len(tb) >= 0.9
    assert len(pb) <= len(tb) + 1
    assert all(f["properties"]["review_status"] == "pending" for f in preds["features"])
    fid = preds["features"][0]["id"]
    assert analyst.post(f"{P}/{proj}/vision/predictions/{fid}/decision", json={"decision": "approve"}).status_code == 403
    assert reviewer.ok(reviewer.post(f"{P}/{proj}/vision/predictions/{fid}/decision",
                                     json={"decision": "approve"}))["review_status"] == "accepted"


def test_tiled_inference_has_no_seam_duplicates(tmp_path):
    from app.services.vision import ThresholdBaseline, extract_buildings

    small = extract_buildings(FIXTURES / "synthetic_orthophoto.tif", ThresholdBaseline(), tile_size=96, overlap=16)
    big = extract_buildings(FIXTURES / "synthetic_orthophoto.tif", ThresholdBaseline(), tile_size=2048, overlap=0)
    assert small["tiles"] > big["tiles"] >= 1
    assert len(small["polygons"]) == len(big["polygons"])


def test_utility_network_topology(analyst, proj):
    did = analyst.ok(analyst.post(f"{P}/{proj}/datasets", json={"name": "lines", "kind": "utility_lines",
                                                                "provenance": "synthetic"}))["id"]
    vid = _upload_process(analyst, proj, did, "utility_lines.gpkg")
    j = wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/topology", json={"version_id": vid})))
    cats = j["result"]["conflicts_by_category"]
    assert cats.get("disconnected_network") == 1
    assert cats.get("dangling_segment", 0) >= 1


def test_version_conflict_on_stale_correction(proj):
    from app.db import SessionLocal
    from app.models import Correction, IntegratedFeature
    from app.services.integration import VersionConflict, apply_correction

    with SessionLocal() as db:
        f = IntegratedFeature(project_id=proj, feature_class="parcel", integrated_key="parcel:T1",
                              attributes={"land_use": "residential"}, geom="SRID=4326;POLYGON((73.8 18.5,73.801 18.5,"
                              "73.801 18.501,73.8 18.501,73.8 18.5))", version=3)
        db.add(f)
        db.flush()
        c = Correction(project_id=proj, integrated_feature_id=f.id, base_version=2, kind="attribute_update",
                       safety="review_required", proposed_attributes={"land_use": "commercial"}, reason="t", method="t")
        db.add(c)
        db.flush()
        with pytest.raises(VersionConflict):
            apply_correction(db, c, actor_id=None)
        c.base_version = 3
        assert apply_correction(db, c, actor_id=None) == "applied"
        assert f.version == 4 and f.attributes["land_use"] == "commercial"
        db.rollback()


# ---------------- acquisition (HTTP mocked) ----------------

def _ms_transport(index_csv: str, tiles: dict[str, bytes], fail_first: set | None = None):
    calls = {"n": 0}

    def handler(req: httpx.Request):
        url = str(req.url)
        calls["n"] += 1
        if "dataset-links.csv" in url:
            return httpx.Response(200, text=index_csv)
        for qk, body in tiles.items():
            if qk in url:
                return httpx.Response(200, content=body)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_microsoft_connector_filters_tiles_and_clips(tmp_path):
    from app.services.acquisition.fetch import acquire_microsoft, client, quadkeys_for_bbox

    area = box(73.80, 18.50, 73.81, 18.51)
    qk = quadkeys_for_bbox(list(area.bounds))[0]
    inside = {"type": "Feature", "properties": {"confidence": 0.9}, "geometry": box(73.801, 18.501, 73.8012,
                                                                                   18.5012).__geo_interface__}
    outside = {"type": "Feature", "properties": {}, "geometry": box(73.9, 18.6, 73.9002, 18.6002).__geo_interface__}
    body = gzip.compress((json.dumps(inside) + "\n" + json.dumps(outside) + "\n").encode())
    index = f"Location,QuadKey,Url,Size\nIndia,{qk},https://example.test/{qk}.csv.gz,1KB\nIndia,999,https://x/999,1KB\n"
    with client(_ms_transport(index, {qk: body})) as http:
        res = acquire_microsoft(area, "India", tmp_path, http)
    assert res["feature_count"] == 1 and res["files"][0]["quadkey"] == qk and len(res["files"][0]["sha256"]) == 64


def test_microsoft_connector_reports_unavailable_and_no_coverage(tmp_path):
    from app.services.acquisition.fetch import AcquisitionError, acquire_microsoft, client

    def down(req):
        raise httpx.ConnectError("blocked")

    with client(httpx.MockTransport(down)) as http, pytest.raises(AcquisitionError) as e:
        acquire_microsoft(box(73.8, 18.5, 73.81, 18.51), "India", tmp_path, http)
    assert e.value.status == "unavailable"
    with client(_ms_transport("Location,QuadKey,Url,Size\nIndia,000,https://x,1\n", {})) as http, \
            pytest.raises(AcquisitionError) as e:
        acquire_microsoft(box(73.8, 18.5, 73.81, 18.51), "India", tmp_path, http)
    assert e.value.status == "no_coverage"


def test_acquisition_refuses_large_areas(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.services.acquisition.fetch import AcquisitionError, acquire_microsoft, acquire_osm, client

    monkeypatch.setattr(get_settings(), "acquisition_max_tiles", 2)
    with client(httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        with pytest.raises(AcquisitionError) as e:
            acquire_microsoft(box(70, 15, 80, 25), "India", tmp_path, http)
        assert e.value.status == "too_large"
        with pytest.raises(AcquisitionError) as e:
            acquire_osm(box(73.0, 18.0, 74.0, 19.0), tmp_path, http)
        assert e.value.status == "too_large"


def test_download_resumes_partial_file(tmp_path):
    from app.services.acquisition.fetch import client, download

    data = bytes(range(256)) * 100
    part = tmp_path / "f.bin.part"
    part.write_bytes(data[:1000])

    def handler(req):
        rng = req.headers.get("range")
        assert rng == "bytes=1000-"
        return httpx.Response(206, content=data[1000:])

    with client(httpx.MockTransport(handler)) as http:
        info = download("https://example.test/f.bin", tmp_path / "f.bin", http, 10**7)
    assert (tmp_path / "f.bin").read_bytes() == data and info["size"] == len(data)


def test_restricted_sources_are_reported_not_faked(analyst, proj):
    from app.services.acquisition.sources import SOURCES

    for key in ("naksha", "survey_of_india", "bhuvan", "bhoonidhi"):
        assert SOURCES[key]["access"] == "authorization_required" and not SOURCES[key]["automated"]
    j = wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/acquisition", json={"source_key": "naksha"})))
    assert j["result"]["status"] == "unauthorized"
    mans = analyst.ok(analyst.get(f"{P}/{proj}/acquisition/manifests"))
    assert mans[0]["status"] == "unauthorized" and mans[0]["dataset_version_id"] is None
    src = {s["key"]: s for s in analyst.ok(analyst.get(f"{P}/{proj}/acquisition/sources"))}
    assert src["microsoft_buildings"]["license"] == "CDLA-Permissive-2.0"


def test_live_acquisition_reports_unreachable_honestly(analyst, proj):
    """Uses the real network. In environments where the source is blocked this must be reported as such."""
    sa = {"type": "Polygon", "coordinates": [[[73.80, 18.50], [73.81, 18.50], [73.81, 18.51], [73.80, 18.51],
                                              [73.80, 18.50]]]}
    analyst.ok(analyst.post(f"{P}/{proj}/study-areas", json={"name": "tiny", "geojson": sa}))
    j = wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/acquisition",
                                                        json={"source_key": "osm_overpass"})))
    assert j["status"] == "succeeded"
    assert j["result"]["status"] in ("completed", "unavailable", "no_coverage")
    if j["result"]["status"] != "completed":
        assert j["result"].get("dataset_id") is None  # nothing substituted


def test_image_change_detection_finds_added_and_removed_buildings(analyst, proj, tmp_path):
    """Two co-registered synthetic images rendered from buildings v1 and v2; changed regions must
    coincide with the buildings that were truly added/removed (ground truth)."""
    import importlib.util

    import geopandas as gpd

    spec = importlib.util.spec_from_file_location("gen", FIXTURES.parents[1] / "backend" / "scripts" / "generate_fixtures.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    paths = {}
    for tag in ("v1", "v2"):
        rows = gpd.read_file(FIXTURES / f"buildings_source_a_{tag}.geojson").to_crs(32643).to_dict("records")
        out = tmp_path / tag
        out.mkdir()
        gen.write_raster(out, rows)
        paths[tag] = out / "synthetic_orthophoto.tif"
    did = analyst.ok(analyst.post(f"{P}/{proj}/datasets", json={"name": "dated imagery", "kind": "imagery",
                                                                "provenance": "synthetic"}))["id"]
    vids = []
    for tag in ("v1", "v2"):
        with open(paths[tag], "rb") as fh:
            vid = analyst.ok(analyst.post(f"{P}/{proj}/datasets/{did}/versions",
                                          files={"file": (f"img_{tag}.tif", fh)}))["version"]["id"]
        wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/versions/{vid}/process", json={})))
        vids.append(vid)
    j = wait_job(analyst, proj, analyst.ok(analyst.post(f"{P}/{proj}/changes", json={"old_version_id": vids[0],
                                                                                      "new_version_id": vids[1]})))
    assert j["status"] == "succeeded", j
    assert j["result"]["pixel_size_m"] == 0.5 and j["result"]["image_change_regions"] >= 3
    fc = analyst.ok(analyst.get(f"{P}/{proj}/changes", params={"old_version_id": vids[0], "new_version_id": vids[1]}))
    regions = gpd.GeoSeries([shape(f["geometry"]) for f in fc["features"]], crs=4326).to_crs(32643)
    a = gpd.read_file(FIXTURES / "buildings_source_a_v1.geojson").to_crs(32643)
    b = gpd.read_file(FIXTURES / "buildings_source_a_v2.geojson").to_crs(32643)
    changed = list(a[a.bldg_id.isin(TRUTH["changes"]["removed"])].geometry) + \
        list(b[b.bldg_id.isin(TRUTH["changes"]["added"])].geometry)
    for g in changed:  # every truly added/removed building is covered by a detected change region
        assert any(r.intersection(g).area / g.area > 0.7 for r in regions), g.wkt
    unchanged = a[a.bldg_id == "MSB-001"].geometry.iloc[0]
    assert not any(r.intersection(unchanged).area / unchanged.area > 0.3 for r in regions)


def test_correction_decisions_reject_defer_and_stale_version(analyst, reviewer, proj):
    from app.db import SessionLocal
    from app.models import Correction, IntegratedFeature

    with SessionLocal() as db:
        f = IntegratedFeature(project_id=proj, feature_class="parcel", integrated_key="parcel:T2",
                              attributes={"land_use": "residential"}, geom="SRID=4326;POLYGON((73.8 18.5,73.801 18.5,"
                              "73.801 18.501,73.8 18.501,73.8 18.5))", version=2)
        db.add(f)
        db.flush()
        ids = []
        for _ in range(3):
            c = Correction(project_id=proj, integrated_feature_id=f.id, base_version=2, kind="attribute_update",
                           safety="review_required", proposed_attributes={"land_use": "commercial"}, reason="t",
                           method="t")
            db.add(c)
            db.flush()
            ids.append(c.id)
        db.commit()
    url = f"{P}/{proj}/corrections"
    assert analyst.post(f"{url}/{ids[0]}/decision", json={"decision": "approve"}).status_code == 403
    assert reviewer.ok(reviewer.post(f"{url}/{ids[0]}/decision", json={"decision": "defer"}))["status"] == "deferred"
    assert reviewer.ok(reviewer.post(f"{url}/{ids[1]}/decision", json={"decision": "reject"}))["status"] == "rejected"
    assert reviewer.post(f"{url}/{ids[1]}/decision", json={"decision": "approve"}).status_code == 409
    assert reviewer.ok(reviewer.post(f"{url}/{ids[2]}/decision", json={"decision": "approve"}))["outcome"] == "applied"
    r = reviewer.post(f"{url}/{ids[0]}/decision", json={"decision": "approve"})  # feature is now version 3
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    listed = reviewer.ok(reviewer.get(url, params={"status": "rejected"}))
    assert any(f["properties"]["id"] == ids[1] for f in listed["features"])
