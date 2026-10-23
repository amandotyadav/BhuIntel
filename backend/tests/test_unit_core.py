"""Unit tests (no database): geometry repair policy, CRS, identifiers, attribute mapping, scoring."""

import math

import pytest
from shapely.geometry import MultiPolygon, Polygon, box

from app.services.acquisition.fetch import quadkey, quadkeys_for_bbox, tile_xy
from app.services.crs import CrsError, area_of_use_check, parse_crs, suggest_utm_epsg, validate_working_crs
from app.services.geometry import thinness_ratio, validate_and_repair
from app.services.matching import (
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
    FEATURE_SCHEMA,
    assign_relationships,
    attribute_similarity,
    classify,
    compute_features,
    effective_tolerance,
    score_features,
)
from app.services.ml import FeatureValidationError, feature_vector
from app.services.schema_mapping import (
    coerce_value,
    id_core,
    identifier_similarity,
    normalize_identifier,
    normalize_land_use,
    suggest_mappings,
)

# ---------------- geometry repair regression tests ----------------

BOWTIE = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])


def test_valid_geometry_needs_no_repair():
    r = validate_and_repair(box(0, 0, 10, 10), "parcels")
    assert r.valid and r.repaired is None and r.safety is None


def test_bowtie_parcel_is_legal_no_auto():
    r = validate_and_repair(BOWTIE, "parcels")
    assert not r.valid and "Self-intersection" in r.reason
    assert r.safety == "legal_no_auto"
    assert r.repaired.is_valid and r.repaired.geom_type == "MultiPolygon"
    assert r.metrics["parts_after"] == 2 and r.metrics["repaired_area"] == pytest.approx(50.0)


def test_bowtie_building_is_review_required():
    assert validate_and_repair(BOWTIE, "buildings").safety == "review_required"


def test_duplicate_vertex_ring_self_touch_is_auto_safe():
    # Ring that revisits a vertex (self-touching "inverted" hole): make_valid only restructures it,
    # area and boundary are unchanged -> classified auto_safe.
    poly = Polygon([(0, 0), (10, 0), (10, 10), (5, 10), (7, 5), (3, 5), (5, 10), (0, 10)])
    r = validate_and_repair(poly, "parcels")
    assert not r.valid and "Ring Self-intersection" in r.reason
    assert r.safety == "auto_safe"
    assert r.metrics["area_change_ratio"] == 0.0 and r.metrics["hausdorff"] == 0.0
    assert r.repaired.area == pytest.approx(90.0) and len(r.repaired.interiors) == 1


def test_spike_removal_on_parcel_is_not_auto_safe():
    # A zero-area spike: area is unchanged but the boundary moves 5 m -> must not be auto-applied to a parcel
    poly = Polygon([(0, 0), (10, 0), (10, 10), (10, 15), (10, 10), (0, 10)])
    r = validate_and_repair(poly, "parcels")
    assert r.metrics["area_change_ratio"] == 0.0 and r.metrics["hausdorff"] == pytest.approx(5.0)
    assert r.safety == "legal_no_auto"
    assert validate_and_repair(poly, "buildings").safety == "review_required"


def test_empty_geometry_reported():
    r = validate_and_repair(Polygon(), "parcels")
    assert not r.valid and r.reason.startswith("Empty")


def test_thinness_identifies_slivers():
    assert thinness_ratio(box(0, 0, 30, 0.3)) < 0.05
    assert thinness_ratio(box(0, 0, 10, 10)) == pytest.approx(math.pi / 4)


# ---------------- CRS ----------------

def test_working_crs_must_be_projected_metres():
    validate_working_crs(32643)
    with pytest.raises(CrsError):
        validate_working_crs(4326)
    with pytest.raises(CrsError):
        validate_working_crs(2263)  # NY state plane, US survey feet


def test_utm_suggestions_for_indian_cities():
    assert suggest_utm_epsg(73.85, 18.52) == 32643  # Pune
    assert suggest_utm_epsg(77.21, 28.61) == 32643  # Delhi
    assert suggest_utm_epsg(88.36, 22.57) == 32645  # Kolkata
    assert suggest_utm_epsg(80.27, 13.08) == 32644  # Chennai


def test_area_of_use_flags_mislabelled_crs():
    assert area_of_use_check(parse_crs(32643), [73.8, 18.5, 73.9, 18.6]) is None
    msg = area_of_use_check(parse_crs(32643), [-74.0, 40.7, -73.9, 40.8])
    assert msg and "outside the area of use" in msg


def test_parse_crs_rejects_garbage():
    with pytest.raises(CrsError):
        parse_crs("EPSG:99999999")


# ---------------- identifiers ----------------

@pytest.mark.parametrize("raw,expected", [
    ("Plot No. 012/A", "12A"), ("P-0012", "P12"), ("p 12", "P12"), (" sy 45 ", "45"), (12.0, "12"),
    (None, None), ("", None), ("CTS No 7", "7"),
])
def test_normalize_identifier(raw, expected):
    assert normalize_identifier(raw) == expected


def test_identifier_similarity_semantics():
    n = normalize_identifier
    assert identifier_similarity(n("P-012"), n("p12")) == 1.0
    assert identifier_similarity(n("Plot No. 12"), n("P-012")) == 0.95  # prefix dropped in one register
    assert identifier_similarity(n("A-12"), n("B-12")) == 0.5  # different series
    assert identifier_similarity(n("P-015"), n("P-051")) < 0.6
    assert identifier_similarity(None, "P1") is None
    assert id_core("P12") == "12" and id_core("12A") == "12A"


# ---------------- attribute mapping ----------------

def _prof(t="string", **kw):
    return {"type": t, "unique_ratio": kw.get("u", 0.5), "sample_values": kw.get("s", []),
            "numeric_parse_rate": kw.get("num", 0.0), "date_parse_rate": kw.get("date", 0.0),
            "alnum_id_like_rate": kw.get("idlike", 0.0)}


def test_mapping_aliases_and_examples_from_spec():
    profile = {"plot_no": _prof(u=1.0, idlike=1.0), "property_number": _prof(u=1.0, idlike=1.0),
               "landuse": _prof(s=["RES", "COM"]), "survey_date": _prof(date=1.0)}
    s = {x.source_field: x for x in suggest_mappings(profile)}
    assert s["plot_no"].target_field == "parcel_id" and s["plot_no"].method == "alias"
    assert s["property_number"].target_field == "property_id"
    assert s["landuse"].target_field == "land_use"
    assert s["survey_date"].target_field == "surveyed_at"
    assert not any(x.ambiguous for x in s.values())


def test_mapping_flags_ambiguity_and_unknowns():
    profile = {"area": _prof("float", num=1.0), "plot_area": _prof("float", num=1.0), "zzz_misc": _prof()}
    s = {x.source_field: x for x in suggest_mappings(profile)}
    assert s["zzz_misc"].target_field is None
    assert s["area"].target_field == s["plot_area"].target_field == "area_sqm"
    assert s["area"].ambiguous and s["plot_area"].ambiguous  # two fields compete for one target


def test_mapping_uses_project_aliases_and_approved_history():
    profile = {"khata_ref": _prof(u=1.0), "bldg_code_x": _prof(u=1.0)}
    s = {x.source_field: x for x in suggest_mappings(profile, {"khata_ref": "parcel_id"},
                                                     {"bldg_code_x": "building_id"})}
    assert s["khata_ref"].target_field == "parcel_id" and s["khata_ref"].method == "project_alias"
    assert s["bldg_code_x"].method == "approved_reuse"


def test_type_compatibility_matters():
    s = {x.source_field: x for x in suggest_mappings({"area_txt": _prof("string", num=0.0)})}
    sim = s["area_txt"]
    assert sim.target_field != "area_sqm" or sim.score < 0.8


def test_value_coercion():
    assert coerce_value("area_sqm", "1,200.5") == 1200.5
    assert coerce_value("area_sqm", "n/a") is None
    assert coerce_value("surveyed_at", "2019-03-15") == "2019-03-15"
    assert coerce_value("building_levels", "3.0") == 3
    assert coerce_value("land_use", "RES") == "residential"
    assert normalize_land_use("Commercial") == "commercial"


# ---------------- matching / scoring ----------------

def test_effective_tolerance_uses_positional_accuracy():
    assert effective_tolerance(5, None, None) == 5
    assert effective_tolerance(1, 3, 4) == pytest.approx(10.0)


def test_score_redistributes_missing_evidence():
    w = DEFAULT_WEIGHTS["building_building"]
    full = {"iou": 0.9, "centroid_closeness": 0.9, "area_ratio": 0.9, "id_similarity": 0.9, "attribute_similarity": 0.9}
    s, _ = score_features("building_building", full, w)
    s2, expl = score_features("building_building", {k: v for k, v in full.items() if k != "id_similarity"}, w)
    assert s == pytest.approx(0.9) and s2 == pytest.approx(0.9)
    assert any("Missing evidence" in e for e in expl)
    assert classify(0.8, DEFAULT_THRESHOLDS) == "match" and classify(0.6, DEFAULT_THRESHOLDS) == "possible"
    assert classify(0.1, DEFAULT_THRESHOLDS) == "non_match"


def test_building_parcel_does_not_rely_on_iou():
    # 200 m2 building fully inside a 1200 m2 parcel: IoU is only 0.17, but coverage is 1.0
    row = {"la": 200.0, "ra": 1200.0, "ia": 200.0, "cd": 3.0, "hd": 20.0, "l_center_in_r": True}
    f = compute_features("building_parcel", row, 5.0, 0.5)
    assert f["iou"] == pytest.approx(200 / 1200, rel=1e-3)
    s, _ = score_features("building_parcel", f, DEFAULT_WEIGHTS["building_parcel"])
    assert f["building_coverage"] == 1.0 and s >= 0.9


def test_attribute_similarity_numeric_and_text():
    sim, per = attribute_similarity({"land_use": "residential", "area_sqm": 1000.0},
                                    {"land_use": "Residential", "area_sqm": 900.0})
    assert per["land_use"] == 1.0 and per["area_sqm"] == pytest.approx(0.9)
    assert sim == pytest.approx(0.95)


class _C:
    def __init__(self, left, right, cls="match", cov=None):
        self.left_feature_id, self.right_feature_id, self.match_class = left, right, cls
        self.features = {"building_coverage": cov} if cov is not None else {}
        self.relationship = None


def test_relationship_cardinalities():
    cs = [_C(1, 10), _C(2, 11), _C(2, 12), _C(3, 13), _C(4, 13), _C(5, 14), _C(6, 14), _C(6, 15), _C(7, 16, "non_match")]
    assign_relationships(cs, "parcel_parcel", 0.05)
    rel = [c.relationship for c in cs]
    assert rel[0] == "one_to_one" and rel[1] == rel[2] == "one_to_many"
    assert rel[3] == rel[4] == "many_to_one" and rel[5] == rel[6] == rel[7] == "many_to_many"
    assert rel[8] is None


def test_feature_vector_validation():
    f = {n: 0.5 for n in FEATURE_SCHEMA["building_building"]}
    f["id_similarity"] = None
    v = feature_vector("building_building", f)
    assert math.isnan(v[FEATURE_SCHEMA["building_building"].index("id_similarity")])
    with pytest.raises(FeatureValidationError):
        feature_vector("building_building", {**f, "iou": "high"})
    with pytest.raises(FeatureValidationError):
        feature_vector("building_building", {**f, "iou": float("inf")})


# ---------------- Bing quadkeys (Microsoft footprint partitioning) ----------------

def test_quadkey_matches_bing_reference():
    # Bing Maps Tile System documentation example: tile (3, 5) at level 3 -> "213"
    assert quadkey(3, 5, 3) == "213"
    x, y = tile_xy(73.85, 18.52, 9)
    assert len(quadkey(x, y, 9)) == 9
    assert len(quadkeys_for_bbox([73.80, 18.50, 73.82, 18.52], 9)) == 1


def test_multipolygon_family_preserved_on_repair():
    mp = MultiPolygon([box(0, 0, 1, 1), box(0.5, 0, 1.5, 1)])  # overlapping parts -> invalid
    r = validate_and_repair(mp, "buildings")
    assert not r.valid and r.repaired.is_valid and r.repaired.geom_type in ("Polygon", "MultiPolygon")


def test_cors_origins_accepts_comma_separated_env(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("BHU_CORS_ORIGINS", "http://localhost:8080, https://gis.example.org")
    assert Settings().cors_origins == ["http://localhost:8080", "https://gis.example.org"]
