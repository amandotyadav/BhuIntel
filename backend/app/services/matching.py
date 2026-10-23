"""Spatial matching engine.

Candidate generation uses PostGIS ``ST_DWithin`` on the projected working geometry (GiST-indexed)
within an effective tolerance; matching features are computed in SQL (areas, intersection, centroid
distance, Hausdorff distance) and in Python (identifier / attribute similarity). Scores are
*heuristic match scores* (weighted evidence), not probabilities, unless an ML model is used.

Match types
-----------
* building_building  - footprints from two sources (e.g. Microsoft ML vs OpenStreetMap).
* parcel_parcel      - two cadastral/parcel layers.
* building_parcel    - which parcel(s) a building lies in. Uses building coverage share, centroid
                       containment and attributes (IoU is NOT used: a building is much smaller than its
                       parcel, so IoU would be uninformative).
* record_feature     - attribute records (CSV, optionally with points) to parcel polygons using
                       identifier similarity plus point-in-polygon evidence.
"""

import math
from collections import defaultdict
from dataclasses import dataclass

from rapidfuzz import fuzz
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Dataset, DatasetVersion, MatchCandidate, MatchRun, Project
from app.services.schema_mapping import id_core, identifier_similarity, normalize_identifier

MATCH_TYPES = ("building_building", "parcel_parcel", "building_parcel", "record_feature")

DEFAULT_WEIGHTS = {
    "building_building": {"iou": 0.45, "centroid_closeness": 0.25, "area_ratio": 0.15, "id_similarity": 0.05,
                          "attribute_similarity": 0.10},
    "parcel_parcel": {"iou": 0.35, "centroid_closeness": 0.15, "area_ratio": 0.10, "max_containment": 0.10,
                      "id_similarity": 0.25, "attribute_similarity": 0.05},
    "building_parcel": {"building_coverage": 0.70, "centroid_inside": 0.20, "attribute_similarity": 0.10},
    "record_feature": {"id_similarity": 0.60, "point_inside": 0.30, "attribute_similarity": 0.10},
}
DEFAULT_THRESHOLDS = {"match": 0.75, "possible": 0.50, "store_min": 0.20}
COMPARABLE_ATTRS = ["land_use", "area_sqm", "ward", "village", "address", "building_levels", "property_id",
                    "parcel_id", "ulpin"]

# Ordered feature schema shared by the deterministic scorer, the ML model and stored candidates.
FEATURE_SCHEMA = {
    "building_building": ["iou", "overlap_left", "overlap_right", "area_ratio", "centroid_distance_m",
                          "centroid_closeness", "hausdorff_m", "id_similarity", "attribute_similarity",
                          "source_quality"],
    "parcel_parcel": ["iou", "overlap_left", "overlap_right", "max_containment", "area_ratio",
                      "centroid_distance_m", "centroid_closeness", "hausdorff_m", "id_similarity",
                      "attribute_similarity", "source_quality"],
    "building_parcel": ["building_coverage", "parcel_share", "centroid_inside", "area_ratio",
                        "attribute_similarity", "source_quality"],
    "record_feature": ["id_similarity", "point_inside", "point_distance_m", "attribute_similarity",
                       "source_quality"],
}


@dataclass
class RunConfig:
    tolerance_m: float
    effective_tolerance_m: float
    weights: dict
    thresholds: dict
    min_building_share: float = 0.05


def effective_tolerance(tol: float, acc_a: float | None, acc_b: float | None) -> float:
    """Search tolerance widened to 2x the combined (RSS) positional accuracy of both sources."""
    if acc_a is None and acc_b is None:
        return tol
    rss = math.sqrt((acc_a or 0.0) ** 2 + (acc_b or 0.0) ** 2)
    return max(tol, 2.0 * rss)


def id_similarity(a: str | None, b: str | None) -> float | None:
    return identifier_similarity(a, b)


def attribute_similarity(a: dict, b: dict, fields=COMPARABLE_ATTRS, exclude=()) -> tuple[float | None, dict]:
    per: dict[str, float] = {}
    for f in fields:
        if f in exclude:
            continue
        va, vb = a.get(f), b.get(f)
        if va is None or vb is None:
            continue
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            denom = max(abs(va), abs(vb), 1e-9)
            per[f] = round(max(0.0, 1.0 - abs(va - vb) / denom), 4)
        elif f in ("parcel_id", "property_id", "ulpin"):
            per[f] = id_similarity(normalize_identifier(va), normalize_identifier(vb)) or 0.0
        else:
            sa, sb = str(va).strip().lower(), str(vb).strip().lower()
            per[f] = 1.0 if sa == sb else round(fuzz.token_set_ratio(sa, sb) / 100.0, 4)
    if not per:
        return None, per
    return round(sum(per.values()) / len(per), 4), per


def score_features(match_type: str, feats: dict, weights: dict) -> tuple[float, list[str]]:
    """Weighted average over *available* evidence; missing evidence is reported, weight redistributed."""
    total_w = 0.0
    acc = 0.0
    used, missing = [], []
    for name, w in weights.items():
        v = feats.get(name)
        if v is None:
            missing.append(name)
            continue
        acc += w * float(v)
        total_w += w
        used.append(f"{name}={float(v):.3f} (weight {w:.2f})")
    score = acc / total_w if total_w > 0 else 0.0
    expl = ["Evidence: " + "; ".join(used)] if used else ["No evidence available"]
    if missing:
        expl.append("Missing evidence (weight redistributed): " + ", ".join(missing))
    return round(score, 4), expl


def classify(score: float, thresholds: dict) -> str:
    if score >= thresholds["match"]:
        return "match"
    if score >= thresholds["possible"]:
        return "possible"
    return "non_match"


def _narrative(match_type: str, f: dict, tol: float) -> list[str]:
    out = []
    if f.get("iou") is not None:
        out.append(f"Intersection-over-union {f['iou']:.2f}")
    if f.get("centroid_distance_m") is not None:
        rel = "within" if f["centroid_distance_m"] <= tol else "beyond"
        out.append(f"Centroid distance {f['centroid_distance_m']:.2f} m ({rel} tolerance {tol:.2f} m)")
    if f.get("area_ratio") is not None:
        out.append(f"Area ratio {f['area_ratio']:.2f}")
    if f.get("building_coverage") is not None:
        out.append(f"{f['building_coverage'] * 100:.0f}% of the building footprint lies in this parcel")
    if f.get("parcel_share") is not None:
        out.append(f"Building covers {f['parcel_share'] * 100:.1f}% of the parcel")
    if f.get("id_similarity") is not None:
        out.append(f"Identifier similarity {f['id_similarity']:.2f} ('{f.get('left_id')}' vs '{f.get('right_id')}')")
    if f.get("point_inside") is not None:
        out.append("Record point lies inside the polygon" if f["point_inside"] else "Record point outside polygon")
    if f.get("attribute_similarity") is not None:
        out.append(f"Attribute similarity {f['attribute_similarity']:.2f} over {list(f.get('attr_detail', {}))}")
    return out


def _polygon_candidates(db: Session, left: int, right: int, tol: float) -> list[dict]:
    sql = text(
        """
        SELECT a.id AS lid, b.id AS rid, a.normalized_id AS l_nid, b.normalized_id AS r_nid,
               a.normalized_attributes AS l_attrs, b.normalized_attributes AS r_attrs,
               ST_Area(a.geom_work) AS la, ST_Area(b.geom_work) AS ra,
               ST_Area(ST_Intersection(a.geom_work, b.geom_work)) AS ia,
               ST_Distance(ST_Centroid(a.geom_work), ST_Centroid(b.geom_work)) AS cd,
               ST_HausdorffDistance(a.geom_work, b.geom_work) AS hd,
               ST_Contains(b.geom_work, ST_PointOnSurface(a.geom_work)) AS l_center_in_r
        FROM source_features a
        JOIN source_features b
          ON b.dataset_version_id = :right AND ST_DWithin(a.geom_work, b.geom_work, :tol)
        WHERE a.dataset_version_id = :left AND a.geom_work IS NOT NULL AND b.geom_work IS NOT NULL
          AND a.duplicate_of IS NULL AND b.duplicate_of IS NULL
          AND (a.review_status IS NULL OR a.review_status <> 'rejected')
          AND (b.review_status IS NULL OR b.review_status <> 'rejected')
        """
    )
    return [dict(r._mapping) for r in db.execute(sql, {"left": left, "right": right, "tol": tol})]


def _record_candidates(db: Session, left: int, right: int, tol: float) -> list[dict]:
    """Records (left) to polygons (right): blocking by identifier similarity and by point containment."""
    recs = db.execute(text(
        "SELECT id, normalized_id, normalized_attributes, geom_work IS NOT NULL AS has_pt FROM source_features "
        "WHERE dataset_version_id=:l AND duplicate_of IS NULL"), {"l": left}).mappings().all()
    polys = db.execute(text(
        "SELECT id, normalized_id, normalized_attributes FROM source_features WHERE dataset_version_id=:r "
        "AND duplicate_of IS NULL"), {"r": right}).mappings().all()
    by_core: dict[str, list] = defaultdict(list)
    for p in polys:
        if p["normalized_id"]:
            by_core[id_core(p["normalized_id"])].append(p)
    pairs: dict[tuple[int, int], dict] = {}
    for r in recs:
        link = normalize_identifier((r["normalized_attributes"] or {}).get("parcel_id"))
        if link is None:
            continue
        core = id_core(link)
        if core in by_core:
            for p in by_core[core]:
                pairs[(r["id"], p["id"])] = {"link": link, "r_nid": p["normalized_id"]}
        else:  # fuzzy blocking only when no identifier with the same core exists
            for p in polys:
                if p["normalized_id"] and fuzz.ratio(core, id_core(p["normalized_id"])) >= 80:
                    pairs[(r["id"], p["id"])] = {"link": link, "r_nid": p["normalized_id"]}
    spatial = db.execute(text(
        """
        SELECT a.id AS lid, b.id AS rid, ST_Contains(b.geom_work, a.geom_work) AS inside,
               ST_Distance(a.geom_work, b.geom_work) AS dist
        FROM source_features a JOIN source_features b
          ON b.dataset_version_id=:r AND ST_DWithin(a.geom_work, b.geom_work, :tol)
        WHERE a.dataset_version_id=:l AND a.geom_work IS NOT NULL AND b.duplicate_of IS NULL
        """), {"l": left, "r": right, "tol": tol}).mappings().all()
    sp = {(s["lid"], s["rid"]): s for s in spatial}
    keys = set(pairs) | set(sp)
    rec_by = {r["id"]: r for r in recs}
    poly_by = {p["id"]: p for p in polys}
    out = []
    for lid, rid in keys:
        r, p = rec_by.get(lid), poly_by.get(rid)
        if r is None or p is None:
            continue
        link = normalize_identifier((r["normalized_attributes"] or {}).get("parcel_id"))
        s = sp.get((lid, rid))
        out.append({
            "lid": lid, "rid": rid, "l_nid": link, "r_nid": p["normalized_id"],
            "l_attrs": r["normalized_attributes"] or {}, "r_attrs": p["normalized_attributes"] or {},
            "has_pt": r["has_pt"], "inside": None if s is None else bool(s["inside"]),
            "dist": None if s is None else float(s["dist"]),
        })
    # id-linked pairs whose point lies beyond the search radius: point is outside; measure the real distance
    far = [(o["lid"], o["rid"]) for o in out if o["has_pt"] and o["inside"] is None]
    if far:
        dist = {(r["lid"], r["rid"]): float(r["d"]) for r in db.execute(text(
            "SELECT a.id AS lid, b.id AS rid, ST_Distance(a.geom_work, b.geom_work) AS d FROM source_features a, "
            "source_features b, unnest(CAST(:l AS bigint[]), CAST(:r AS bigint[])) AS p(l, r) WHERE a.id = p.l AND b.id = p.r"),
            {"l": [p[0] for p in far], "r": [p[1] for p in far]}).mappings()}
        for o in out:
            if (o["lid"], o["rid"]) in dist:
                o["inside"], o["dist"] = False, dist[(o["lid"], o["rid"])]
    return out


def compute_features(match_type: str, row: dict, tol: float, quality: float) -> dict:
    f: dict = {"source_quality": round(quality, 4), "left_id": row.get("l_nid"), "right_id": row.get("r_nid")}
    l_attrs, r_attrs = row.get("l_attrs") or {}, row.get("r_attrs") or {}
    if match_type == "record_feature":
        f["id_similarity"] = id_similarity(row.get("l_nid"), row.get("r_nid"))
        f["point_inside"] = None if row.get("inside") is None else (1.0 if row["inside"] else 0.0)
        f["point_distance_m"] = row.get("dist")
        sim, detail = attribute_similarity(l_attrs, r_attrs, exclude=("parcel_id", "property_id"))
        f["attribute_similarity"], f["attr_detail"] = sim, detail
        return f
    la, ra, ia = float(row["la"] or 0), float(row["ra"] or 0), float(row["ia"] or 0)
    union = la + ra - ia
    f["left_area_m2"], f["right_area_m2"], f["intersection_area_m2"] = round(la, 3), round(ra, 3), round(ia, 3)
    f["iou"] = round(ia / union, 4) if union > 0 else 0.0
    f["overlap_left"] = round(ia / la, 4) if la > 0 else 0.0
    f["overlap_right"] = round(ia / ra, 4) if ra > 0 else 0.0
    f["max_containment"] = max(f["overlap_left"], f["overlap_right"])
    f["area_ratio"] = round(min(la, ra) / max(la, ra), 4) if max(la, ra) > 0 else 0.0
    f["centroid_distance_m"] = round(float(row["cd"]), 3)
    f["centroid_closeness"] = round(max(0.0, 1.0 - float(row["cd"]) / max(tol, 1e-9)), 4)
    f["hausdorff_m"] = round(float(row["hd"]), 3)
    if match_type == "building_parcel":
        f["building_coverage"] = f["overlap_left"]
        f["parcel_share"] = f["overlap_right"]
        f["centroid_inside"] = 1.0 if row.get("l_center_in_r") else 0.0
        sim, detail = attribute_similarity(l_attrs, r_attrs, fields=["property_id", "parcel_id", "address"])
    else:
        f["id_similarity"] = id_similarity(row.get("l_nid"), row.get("r_nid"))
        sim, detail = attribute_similarity(l_attrs, r_attrs, exclude=("parcel_id", "building_id"))
    f["attribute_similarity"], f["attr_detail"] = sim, detail
    return f


def assign_relationships(cands: list[MatchCandidate], match_type: str, min_share: float) -> None:
    """Relationship cardinality over the graph of match/possible edges (n:m via connected components)."""
    def is_edge(c: MatchCandidate) -> bool:
        if match_type == "building_parcel":
            return (c.features.get("building_coverage") or 0) >= min_share
        return c.match_class in ("match", "possible")

    edges = [c for c in cands if is_edge(c)]
    ldeg, rdeg = defaultdict(int), defaultdict(int)
    for c in edges:
        ldeg[c.left_feature_id] += 1
        rdeg[c.right_feature_id] += 1
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in edges:
        parent[find(("L", c.left_feature_id))] = find(("R", c.right_feature_id))
    comp_l, comp_r = defaultdict(set), defaultdict(set)
    for c in edges:
        root = find(("L", c.left_feature_id))
        comp_l[root].add(c.left_feature_id)
        comp_r[root].add(c.right_feature_id)
    for c in cands:
        if not is_edge(c):
            c.relationship = None
            continue
        root = find(("L", c.left_feature_id))
        nl, nr = len(comp_l[root]), len(comp_r[root])
        if nl == 1 and nr == 1:
            c.relationship = "one_to_one"
        elif nl == 1:
            c.relationship = "one_to_many"
        elif nr == 1:
            c.relationship = "many_to_one"
        else:
            c.relationship = "many_to_many"


def run_matching(db: Session, run: MatchRun, progress, model=None) -> dict:
    """Execute a match run, persisting all candidates with score >= store_min."""
    project = db.get(Project, run.project_id)
    lv, rv = db.get(DatasetVersion, run.left_version_id), db.get(DatasetVersion, run.right_version_id)
    for v in (lv, rv):
        if v is None or v.status != "processed":
            raise ValueError("Both dataset versions must be processed before matching")
    ld, rd = db.get(Dataset, lv.dataset_id), db.get(Dataset, rv.dataset_id)
    params = run.params or {}
    tol = float(params.get("tolerance_m") or project.default_tolerance_m)
    eff = effective_tolerance(tol, ld.positional_accuracy_m, rd.positional_accuracy_m)
    weights = {**DEFAULT_WEIGHTS[run.match_type], **(params.get("weights") or {})}
    thresholds = {**DEFAULT_THRESHOLDS, **(params.get("thresholds") or {})}
    cfg = RunConfig(tol, eff, weights, thresholds, float(params.get("min_building_share", 0.05)))
    run.params = {**params, "tolerance_m": tol, "effective_tolerance_m": eff, "weights": weights,
                  "thresholds": thresholds, "min_building_share": cfg.min_building_share}
    quality = (ld.reliability + rd.reliability) / 2.0

    progress(0.1, "generating candidates")
    if run.match_type == "record_feature":
        rows = _record_candidates(db, lv.id, rv.id, eff)
    else:
        rows = _polygon_candidates(db, lv.id, rv.id, eff)
    progress(0.5, f"scoring {len(rows)} candidate pairs")

    db.execute(text("DELETE FROM match_candidates WHERE run_id=:r"), {"r": run.id})
    cands: list[MatchCandidate] = []
    for row in rows:
        f = compute_features(run.match_type, row, eff, quality)
        base_score, expl = score_features(run.match_type, f, weights)
        f["baseline_score"] = base_score
        score, kind, method = base_score, "heuristic_match_score", "deterministic_weighted_evidence"
        if model is not None:
            prob, model_expl = model.predict_one(f)
            score, kind, method = prob, "model_probability_uncalibrated", f"random_forest_v{model.version}"
            expl = [*model_expl, f"Deterministic baseline score {base_score:.3f}"]
        if score < thresholds["store_min"] and run.match_type != "building_parcel":
            continue
        if run.match_type == "building_parcel" and (f.get("building_coverage") or 0) < 0.01:
            continue
        cands.append(MatchCandidate(
            run_id=run.id, left_feature_id=row["lid"], right_feature_id=row["rid"], features=f, method=method,
            score=round(score, 4), score_kind=kind, match_class=classify(score, thresholds),
            explanation=[*_narrative(run.match_type, f, eff), *expl],
        ))
    assign_relationships(cands, run.match_type, cfg.min_building_share)
    db.add_all(cands)
    db.flush()
    stats = summarize(db, run, cands)
    run.stats = stats
    return stats


def summarize(db: Session, run: MatchRun, cands: list[MatchCandidate]) -> dict:
    n_left = db.execute(text("SELECT count(*) FROM source_features WHERE dataset_version_id=:v AND duplicate_of "
                             "IS NULL"), {"v": run.left_version_id}).scalar()
    n_right = db.execute(text("SELECT count(*) FROM source_features WHERE dataset_version_id=:v AND duplicate_of "
                              "IS NULL"), {"v": run.right_version_id}).scalar()
    by_class = defaultdict(int)
    by_rel = defaultdict(int)
    matched_l, matched_r = set(), set()
    for c in cands:
        by_class[c.match_class] += 1
        if c.relationship:
            by_rel[c.relationship] += 1
        if c.match_class in ("match", "possible") or (run.match_type == "building_parcel" and c.relationship):
            matched_l.add(c.left_feature_id)
            matched_r.add(c.right_feature_id)
    return {
        "candidates": len(cands),
        "by_class": dict(by_class),
        "by_relationship": dict(by_rel),
        "left_features": n_left,
        "right_features": n_right,
        "left_matched": len(matched_l),
        "right_matched": len(matched_r),
        "left_unmatched": n_left - len(matched_l),
        "right_unmatched": n_right - len(matched_r),
        "score_kind": cands[0].score_kind if cands else "heuristic_match_score",
    }
