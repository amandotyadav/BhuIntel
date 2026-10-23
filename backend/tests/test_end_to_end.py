"""End-to-end acceptance scenario through the HTTP API against PostGIS, scored against the synthetic
fixture's ground truth (fixtures/synthetic_pune/ground_truth.json).

1 create project -> 2 upload two inconsistent parcel layers + property CSV -> 3 inspect schema/CRS ->
4 preprocess -> 5 match -> 6 detect conflicts -> 7 explainable scores -> 8 open a conflict (map payload)
-> 9 review/approve a correction -> 10 audit history -> 11 export -> 12 reload & validate export.
"""

import json
import tempfile
from pathlib import Path

import geopandas as gpd
import pytest

from tests.conftest import FIXTURES, run_jobs, study_area_geojson, wait_job

TRUTH = json.loads((FIXTURES / "ground_truth.json").read_text())
P = "/api/v1/projects"


def upload(api, pid, did, name, layer=None):
    with open(FIXTURES / name, "rb") as fh:
        data = {"layer": layer} if layer else None
        return api.ok(api.post(f"{P}/{pid}/datasets/{did}/versions", files={"file": (name, fh)}, data=data))


def approve_all_unambiguous(api, pid, did, overrides=None):
    for m in api.ok(api.get(f"{P}/{pid}/datasets/{did}/mappings")):
        target = (overrides or {}).get(m["source_field"], "keep")
        if target is None:
            if m["status"] == "approved":
                api.ok(api.patch(f"{P}/{pid}/mappings/{m['id']}", json={"status": "rejected"}))
            continue
        if target != "keep":
            api.ok(api.patch(f"{P}/{pid}/mappings/{m['id']}", json={"status": "approved", "target_field": target}))


def process(api, pid, vid):
    job = api.ok(api.post(f"{P}/{pid}/versions/{vid}/process", json={}))
    j = wait_job(api, pid, job)
    assert j["status"] == "succeeded", j
    return j


def match(api, pid, left, right, mtype, **kw):
    run = api.ok(api.post(f"{P}/{pid}/match-runs", json={"left_version_id": left, "right_version_id": right,
                                                          "match_type": mtype, **kw}))
    run_jobs()
    r = api.ok(api.get(f"{P}/{pid}/match-runs/{run['id']}"))
    assert r["status"] == "succeeded", r
    return r


@pytest.fixture(scope="module")
def scenario(admin, analyst, reviewer, users):
    s: dict = {}
    # 1. project (working CRS derived from the study area -> UTM 43N)
    proj = analyst.ok(analyst.post(P, json={"name": "Synthetic Pune harmonization", "default_tolerance_m": 5.0,
                                            "study_area_geojson": study_area_geojson()}))
    pid = s["pid"] = proj["id"]
    assert proj["working_crs"] == 32643
    analyst.ok(analyst.put(f"{P}/{pid}/members", json={"user_id": users["reviewer"], "role": "reviewer"}))
    analyst_member = {"user_id": users["admin"], "role": "admin"}
    analyst.ok(analyst.put(f"{P}/{pid}/members", json=analyst_member))

    def ds(name, kind, reliability, **kw):
        return analyst.ok(analyst.post(f"{P}/{pid}/datasets", json={"name": name, "kind": kind, "provenance": "synthetic",
                                                                    "reliability": reliability, **kw}))["id"]

    s["muni"] = ds("Municipal parcels (synthetic)", "parcels", 0.7, license="synthetic test data")
    s["rev"] = ds("Revenue parcels (synthetic)", "parcels", 0.6, positional_accuracy_m=1.0)
    s["props"] = ds("Property register (synthetic)", "property_records", 0.5)
    s["bA"] = ds("Buildings source A (synthetic, MS-like)", "buildings", 0.6, source_date="2023-01-01T00:00:00Z")
    s["bB"] = ds("Buildings source B (synthetic, OSM-like)", "buildings", 0.5, source_date="2024-06-01T00:00:00Z")

    # 2-3. uploads + inspection
    up = s["up_muni"] = upload(analyst, pid, s["muni"], "municipal_parcels.gpkg")
    up_rev = s["up_rev"] = upload(analyst, pid, s["rev"], "revenue_parcels.geojson")
    up_props = s["up_props"] = upload(analyst, pid, s["props"], "property_records.csv")
    up_bA = upload(analyst, pid, s["bA"], "buildings_source_a_v1.geojson")
    up_bB = upload(analyst, pid, s["bB"], "buildings_source_b.gpkg")
    s.update(v_muni=up["version"]["id"], v_rev=up_rev["version"]["id"], v_props=up_props["version"]["id"],
             v_bA=up_bA["version"]["id"], v_bB=up_bB["version"]["id"])

    # mappings: confirm the ambiguous/unmapped ones explicitly
    approve_all_unambiguous(analyst, pid, s["rev"], {"parcel_id": "parcel_id", "data_class": None})
    approve_all_unambiguous(analyst, pid, s["muni"], {"data_class": None, "ward_no": "ward"})
    approve_all_unambiguous(analyst, pid, s["props"], {"data_class": None})
    approve_all_unambiguous(analyst, pid, s["bA"], {"bldg_id": "building_id", "data_class": None})
    approve_all_unambiguous(analyst, pid, s["bB"], {"osm_id": "building_id", "data_class": None})

    # CSV has no CRS: processing must be refused until confirmed
    r = analyst.post(f"{P}/{pid}/versions/{s['v_props']}/process", json={})
    s["csv_without_crs_status"] = r.status_code
    analyst.ok(analyst.post(f"{P}/{pid}/versions/{s['v_props']}/crs",
                            json={"crs": "EPSG:4326", "justification": "Register documentation states WGS84 lat/lon"}))

    # 4. preprocessing
    for v in ("v_muni", "v_rev", "v_props", "v_bA", "v_bB"):
        s[f"job_{v}"] = process(analyst, pid, s[v])

    # 5-6. matching + conflicts
    s["run_pp"] = match(analyst, pid, s["v_muni"], s["v_rev"], "parcel_parcel")
    s["run_rec"] = match(analyst, pid, s["v_props"], s["v_muni"], "record_feature")
    s["run_bb"] = match(analyst, pid, s["v_bA"], s["v_bB"], "building_building")
    s["run_bp"] = match(analyst, pid, s["v_bA"], s["v_muni"], "building_parcel")
    topo = analyst.ok(analyst.post(f"{P}/{pid}/topology", json={"version_id": s["v_muni"]}))
    s["topology_job"] = wait_job(analyst, pid, topo)

    # integration build (needed before corrections can be applied to the integrated layer)
    build = reviewer.ok(reviewer.post(f"{P}/{pid}/integration/build", json={
        "primary_parcel_version_id": s["v_muni"], "parcel_match_run_id": s["run_pp"]["id"],
        "record_match_run_id": s["run_rec"]["id"], "building_match_run_id": s["run_bb"]["id"],
        "building_parcel_run_id": s["run_bp"]["id"]}))
    s["build_job"] = wait_job(reviewer, pid, build)
    s["conflicts"] = analyst.ok(analyst.get(f"{P}/{pid}/conflicts", params={"limit": 2000}))
    return s


def raw_ids(api, pid, feature_ids):
    out = []
    for fid in feature_ids:
        f = api.ok(api.get(f"{P}/{pid}/features/{fid}"))
        out.append(f["properties"]["source_id_raw"])
    return out


def test_inspection_reports_schema_and_crs(scenario):
    muni, rev, props = scenario["up_muni"], scenario["up_rev"], scenario["up_props"]
    assert muni["version"]["source_crs"] == "EPSG:32643" and muni["version"]["geometry_type"] == "Polygon"
    assert rev["version"]["source_crs"] == "EPSG:4326"
    assert {"plot_no", "landuse", "area"} <= set(muni["version"]["schema"])
    assert props["version"]["source_crs"] is None and props["version"]["status"] == "needs_crs"
    assert props["inspection"]["crs_suggestion"] == "EPSG:4326"
    assert scenario["csv_without_crs_status"] == 422


def test_preprocessing_persists_normalized_data(scenario, analyst):
    pid = scenario["pid"]
    v = analyst.ok(analyst.get(f"{P}/{pid}/versions/{scenario['v_muni']}"))
    assert v["status"] == "processed"
    assert v["stats"]["features_loaded"] == 31
    assert v["stats"]["invalid_geometries"] == 1  # P-007 bow-tie
    assert v["stats"]["repairs_need_review"] == 1
    assert v["stats"]["exact_duplicates"] == 1  # duplicate P-025
    fc = analyst.ok(analyst.get(f"{P}/{pid}/versions/{scenario['v_rev']}/features", params={"limit": 5}))
    f = fc["features"][0]["properties"]
    assert f["normalized_attributes"]["parcel_id"] and "land_use" in f["normalized_attributes"]
    assert fc["numberMatched"] == 29


def test_parcel_matching_against_ground_truth(scenario, analyst):
    pid, run = scenario["pid"], scenario["run_pp"]
    cands = analyst.ok(analyst.get(f"{P}/{pid}/match-runs/{run['id']}/candidates", params={"match_class": "match",
                                                                                         "limit": 1000}))["items"]
    norm = lambda s: s.replace("P-0", "P").replace("P-", "P")  # noqa: E731
    from app.services.schema_mapping import id_core, normalize_identifier

    predicted = {(c["left_id"], c["right_id"]) for c in cands}
    pred_by_left = {}
    for c in cands:
        pred_by_left.setdefault(c["left_id"], set()).add(id_core(normalize_identifier(c["right_id"])))
    expected = set(TRUTH["parcels"]["matches_by_geometry"])
    tp = sum(1 for pid_ in expected if pid_ in pred_by_left)
    recall = tp / len(expected)
    assert recall >= 0.95, (recall, sorted(expected - set(pred_by_left)))
    assert all(p[0] not in TRUTH["parcels"]["only_in_a"] for p in predicted)
    assert run["stats"]["score_kind"] == "heuristic_match_score"
    c0 = cands[0]
    assert c0["explanation"] and any("Intersection-over-union" in e for e in c0["explanation"])
    assert {"iou", "centroid_distance_m", "id_similarity", "baseline_score"} <= set(c0["features"])
    del norm


def _categories(conflicts):
    out = {}
    for f in conflicts["features"]:
        out.setdefault(f["properties"]["category"], []).append(f)
    return out


def test_detects_all_fixture_conflicts(scenario, analyst):
    pid = scenario["pid"]
    cats = _categories(scenario["conflicts"])
    missing = []
    for exp in TRUTH["conflicts"]:
        cat = exp["category"]
        found = False
        for f in cats.get(cat, []):
            ids = set(raw_ids(analyst, pid, f["properties"]["feature_ids"]))
            want = set()
            for k in ("parcel", "property"):
                if k in exp:
                    want.add(exp[k])
            for k in ("parcels",):
                want |= set(exp.get(k, []))
            if cat == "building_crosses_parcel":
                want = set(exp["parcels"])
            if cat == "parcel_id_mismatch":
                want = {exp["parcel"], exp["other_id"]}
            if want <= ids or any(_same_id(w, i) for w in want for i in ids if len(want) == 1):
                found = True
                break
        if not found:
            missing.append(exp)
    assert not missing, f"undetected fixture conflicts: {missing}"


def _same_id(a, b):
    from app.services.schema_mapping import id_core, normalize_identifier

    return a and b and id_core(normalize_identifier(a)) == id_core(normalize_identifier(b))


def test_conflict_detail_has_evidence_and_map_payload(scenario, analyst):
    pid = scenario["pid"]
    cats = _categories(scenario["conflicts"])
    overlap = cats["parcel_overlap"][0]
    detail = analyst.ok(analyst.get(f"{P}/{pid}/conflicts/{overlap['id']}"))
    assert detail["legal_sensitive"] is True
    assert detail["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert len(detail["features"]["features"]) == 2
    assert detail["evidence"]["overlap_area_m2"] > 50  # 2 m x 40 m strip
    assert "heuristic" in detail["confidence_kind"]
    assert 0 <= detail["priority"] <= 100


def test_review_correction_applies_with_audit(scenario, analyst, reviewer, admin):
    pid = scenario["pid"]
    inv = [f for f in _categories(scenario["conflicts"])["invalid_geometry"]][0]
    detail = analyst.ok(analyst.get(f"{P}/{pid}/conflicts/{inv['id']}"))
    corr = detail["corrections"][0]
    assert corr["safety"] == "legal_no_auto" and corr["proposed_geojson"] and corr["original_geojson"]
    # analyst cannot review; reviewer cannot approve a legal correction; admin must acknowledge
    assert analyst.post(f"{P}/{pid}/conflicts/{inv['id']}/decision", json={"decision": "approve"}).status_code == 403
    r = reviewer.post(f"{P}/{pid}/conflicts/{inv['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "legal_approval_requires_admin"
    r = admin.post(f"{P}/{pid}/conflicts/{inv['id']}/decision", json={"decision": "approve"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "legal_acknowledgement_required"
    res = admin.ok(admin.post(f"{P}/{pid}/conflicts/{inv['id']}/decision",
                              json={"decision": "approve", "comment": "Verified against (synthetic) survey sketch",
                                    "acknowledge_legal": True}))
    assert res["status"] == "approved" and res["correction_outcome"] == "applied"
    # attribute conflict approved by a reviewer -> integrated attribute updated with history
    attr = [f for f in _categories(scenario["conflicts"])["attribute_conflict"]
            if analyst.ok(analyst.get(f"{P}/{pid}/conflicts/{f['id']}"))["evidence"].get("field") == "land_use"][0]
    res = reviewer.ok(reviewer.post(f"{P}/{pid}/conflicts/{attr['id']}/decision",
                                    json={"decision": "approve", "comment": "prefer municipal value"}))
    assert res["correction_outcome"] == "applied"
    # a rejected conflict
    unm = _categories(scenario["conflicts"])["unmatched_feature"][0]
    assert reviewer.ok(reviewer.post(f"{P}/{pid}/conflicts/{unm['id']}/decision",
                                     json={"decision": "reject", "comment": "known gap"}))["status"] == "rejected"
    # decisions + audit preserved and the hash chain verifies
    decisions = reviewer.ok(reviewer.get(f"{P}/{pid}/decisions"))
    assert {d["decision"] for d in decisions} >= {"approve", "reject"}
    events = reviewer.ok(reviewer.get(f"{P}/{pid}/audit", params={"limit": 1000}))
    assert any(e["action"] == "review.conflict.approve" for e in events)
    assert reviewer.ok(reviewer.get(f"{P}/{pid}/audit/verify"))["ok"] is True
    # integrated feature history shows the corrected version
    fc = reviewer.ok(reviewer.get(f"{P}/{pid}/integrated", params={"feature_class": "parcel"}))
    p7 = [f for f in fc["features"] if f["properties"]["attributes"]["parcel_id"] == "P-007"][0]
    assert p7["properties"]["version"] >= 2
    hist = reviewer.ok(reviewer.get(f"{P}/{pid}/integrated/{p7['id']}/history"))
    assert len(hist["features"]) >= 2 and hist["features"][-1]["properties"]["correction_id"]
    scenario["approved"] = True


def test_append_only_tables_reject_mutation(scenario):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.db import SessionLocal

    with SessionLocal() as db:
        with pytest.raises(DBAPIError):
            db.execute(text("UPDATE audit_events SET action='tampered'"))
        db.rollback()
        with pytest.raises(DBAPIError):
            db.execute(text("DELETE FROM review_decisions"))
        db.rollback()


def test_export_and_reload_validates(scenario, analyst, reviewer):
    pid = scenario["pid"]
    assert scenario.get("approved")
    for fmt in ("gpkg", "geojson"):
        job = analyst.ok(analyst.post(f"{P}/{pid}/exports", json={"format": fmt}))
        j = wait_job(analyst, pid, job)
        assert j["status"] == "succeeded", j
        assert j["result"]["validation"]["ok"], j["result"]["validation"]
        r = analyst.get(f"{P}/{pid}/exports/{job['id']}/download")
        assert r.status_code == 200
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / j["result"]["filename"]
            path.write_bytes(r.content)
            if fmt == "gpkg":
                parcels = gpd.read_file(path, layer="parcels")
                buildings = gpd.read_file(path, layer="buildings")
            else:
                allf = gpd.read_file(path)
                parcels = allf[allf.feature_class == "parcel"]
                buildings = allf[allf.feature_class == "building"]
        # independent reload checks
        assert parcels.crs.to_epsg() == 4326
        assert len(parcels) == 30  # 31 municipal features minus the exact duplicate
        assert parcels.geometry.is_valid.all(), "approved repair must make P-007 valid"
        p7 = parcels[parcels.parcel_id == "P-007"].iloc[0]
        assert p7.integrated_version >= 2
        p4 = parcels[parcels.parcel_id == "P-004"].iloc[0]
        assert abs(p4.area_sqm_computed - 1200) < 1 and p4.area_sqm == 1800  # recorded vs computed preserved
        assert "owner_name" not in parcels.columns  # personal data never exported
        assert {"lineage", "provenance", "integrated_key"} <= set(parcels.columns)
        assert (parcels.provenance == "synthetic").all()
        linked = parcels[parcels.parcel_id == "P-012"].iloc[0]
        assert "PROP-1012" in linked.linked_property_ids
        assert len(buildings) > 0 and buildings.geometry.is_valid.all()
        crossing = buildings[buildings.source_building_id == "MSB-010"].iloc[0]
        assert bool(crossing.crosses_parcels) is True


def test_reprocessing_and_reupload_are_idempotent(scenario, analyst):
    pid = scenario["pid"]
    again = upload(analyst, pid, scenario["muni"], "municipal_parcels.gpkg")
    assert again["duplicate"] is True and again["version"]["id"] == scenario["v_muni"]
    before = analyst.ok(analyst.get(f"{P}/{pid}/versions/{scenario['v_rev']}/features", params={"limit": 1}))
    process(analyst, pid, scenario["v_rev"])
    after = analyst.ok(analyst.get(f"{P}/{pid}/versions/{scenario['v_rev']}/features", params={"limit": 1}))
    assert before["numberMatched"] == after["numberMatched"] == 29
    n_before = len(scenario["conflicts"]["features"])
    match(analyst, pid, scenario["v_muni"], scenario["v_rev"], "parcel_parcel")
    n_after = analyst.ok(analyst.get(f"{P}/{pid}/conflicts", params={"limit": 2000}))["numberMatched"]
    assert n_after == n_before  # conflicts are fingerprinted: re-detection does not duplicate them


def test_building_matching_metrics(scenario, analyst):
    pid, run = scenario["pid"], scenario["run_bb"]
    cands = analyst.ok(analyst.get(f"{P}/{pid}/match-runs/{run['id']}/candidates",
                                   params={"match_class": "match", "limit": 1000}))["items"]
    pred = {(c["left_id"], c["right_id"]) for c in cands}
    truth = {tuple(p) for p in TRUTH["buildings"]["pairs_a_b"]}
    tp = len(pred & truth)
    precision, recall = tp / max(1, len(pred)), tp / len(truth)
    assert precision >= 0.95 and recall >= 0.95, (precision, recall)


def test_record_linking_metrics(scenario, analyst):
    pid, run = scenario["pid"], scenario["run_rec"]
    items = analyst.ok(analyst.get(f"{P}/{pid}/match-runs/{run['id']}/candidates", params={"limit": 1000}))["items"]
    best, classes = {}, {}
    for c in items:
        if c["match_class"] == "non_match":
            continue
        if c["left_id"] not in best or c["score"] > best[c["left_id"]][1]:
            best[c["left_id"]] = (c["right_id"], c["score"])
            classes[c["left_id"]] = c["match_class"]
    in_a = set(TRUTH["parcels"]["a_ids"])
    links = {prop: parcel for prop, parcel in TRUTH["records"]["links"].items() if parcel in in_a}
    correct = sum(1 for prop, parcel in links.items() if best.get(prop, (None,))[0] == parcel)
    assert correct == len(links), (correct, len(links))
    # conflicting evidence (point lies in the neighbouring parcel) must not be a confident match
    assert classes[TRUTH["records"]["wrong_point"]] == "possible"
    # records whose parcel is absent from this layer, or unknown plots, stay unlinked
    assert "PROP-9001" not in best and "PROP-1030" not in best
