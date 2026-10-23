"""Integrated dataset construction and version-aware synchronisation.

The integrated layer is *derived*: source features are never modified. Each integrated feature
records lineage (which source features/datasets contributed which values) and every change creates an
append-only history row. Rebuilding is idempotent: unchanged features keep their version; changed
features get version+1; features that disappear are retired (not deleted).

Personal data (e.g. owner names from property records) is NOT copied into the integrated layer; only
the linked property identifiers are.
"""

import json
from collections import defaultdict

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    Correction,
    Dataset,
    DatasetVersion,
    IntegratedFeature,
    IntegratedFeatureHistory,
    MatchRun,
    utcnow,
)
from app.services import audit

PERSONAL_FIELDS = {"owner_name"}


class VersionConflict(Exception):
    pass


def _features(db: Session, version_id: int) -> list[dict]:
    return [dict(r) for r in db.execute(text(
        "SELECT id, source_id_raw, normalized_id, normalized_attributes, area_m2, repair_status, review_status, "
        "ST_AsText(geom) AS wkt FROM source_features WHERE dataset_version_id=:v AND duplicate_of IS NULL "
        "AND geom IS NOT NULL ORDER BY id"), {"v": version_id}).mappings()]


def _dataset_of(db: Session, version_id: int) -> Dataset:
    return db.get(Dataset, db.get(DatasetVersion, version_id).dataset_id)


def _run(db: Session, run_id: int | None, match_type: str) -> MatchRun | None:
    if not run_id:
        return None
    r = db.get(MatchRun, run_id)
    if r is None or r.match_type != match_type or r.status != "succeeded":
        raise ValueError(f"Run {run_id} is not a succeeded {match_type} run")
    return r


def _best_pairs(db: Session, run: MatchRun, classes=("match",)) -> dict[int, tuple[int, float]]:
    """Greedy one-to-one assignment by descending score."""
    rows = db.execute(text(
        "SELECT left_feature_id AS l, right_feature_id AS r, score FROM match_candidates WHERE run_id=:r "
        "AND match_class = ANY(:c) AND status <> 'rejected' ORDER BY score DESC"),
        {"r": run.id, "c": list(classes)}).mappings().all()
    used_l, used_r, out = set(), set(), {}
    for row in rows:
        if row["l"] in used_l or row["r"] in used_r:
            continue
        out[row["l"]] = (row["r"], float(row["score"]))
        used_l.add(row["l"])
        used_r.add(row["r"])
    # reviewer-accepted candidates always win
    for row in db.execute(text("SELECT left_feature_id AS l, right_feature_id AS r, score FROM match_candidates "
                               "WHERE run_id=:r AND status='accepted'"), {"r": run.id}).mappings():
        out[row["l"]] = (row["r"], float(row["score"]))
    return out


def _approved_geometry(db: Session, source_feature_id: int) -> str | None:
    c = db.execute(select(Correction).where(
        Correction.source_feature_id == source_feature_id, Correction.kind == "geometry_repair",
        Correction.status.in_(("approved", "applied"))).order_by(Correction.id.desc())).scalars().first()
    if c is None:
        return None
    return db.execute(text("SELECT ST_AsText(proposed_geom) FROM corrections WHERE id=:i"), {"i": c.id}).scalar()


def _upsert(db: Session, project_id: int, cls: str, key: str, attrs: dict, wkt: str, sources: list[int],
            lineage: dict, confidence: float | None, actor_id: int | None, reason: str, seen: set) -> str:
    seen.add(key)
    f = db.execute(select(IntegratedFeature).where(
        IntegratedFeature.project_id == project_id, IntegratedFeature.feature_class == cls,
        IntegratedFeature.integrated_key == key)).scalar_one_or_none()
    # re-apply reviewer-approved attribute corrections so a rebuild never silently reverts them
    if f is not None:
        for c in db.execute(select(Correction).where(Correction.integrated_feature_id == f.id,
                                                     Correction.kind == "attribute_update",
                                                     Correction.status == "applied").order_by(Correction.id)).scalars():
            attrs = {**attrs, **c.proposed_attributes}
    norm_wkt = db.execute(text("SELECT ST_AsText(ST_GeomFromText(:w, 4326))"), {"w": wkt}).scalar()
    if f is None:
        f = IntegratedFeature(project_id=project_id, feature_class=cls, integrated_key=key, attributes=attrs,
                              geom=f"SRID=4326;{norm_wkt}", source_feature_ids=sources, lineage=lineage,
                              confidence=confidence, status="active", version=1, updated_by=actor_id)
        db.add(f)
        db.flush()
        _history(db, f, norm_wkt, reason, None, actor_id)
        return "created"
    cur_wkt = db.execute(text("SELECT ST_AsText(geom) FROM integrated_features WHERE id=:i"), {"i": f.id}).scalar()
    same_geom = db.execute(text("SELECT ST_Equals(ST_GeomFromText(:a,4326), ST_GeomFromText(:b,4326))"),
                           {"a": cur_wkt, "b": norm_wkt}).scalar()
    if (same_geom and json.dumps(f.attributes, sort_keys=True, default=str) == json.dumps(attrs, sort_keys=True,
                                                                                          default=str)
            and f.status == "active"):
        f.lineage, f.source_feature_ids, f.confidence = lineage, sources, confidence
        return "unchanged"
    f.attributes, f.geom, f.source_feature_ids, f.lineage = attrs, f"SRID=4326;{norm_wkt}", sources, lineage
    f.confidence, f.status, f.version, f.updated_by, f.updated_at = confidence, "active", f.version + 1, actor_id, utcnow()
    db.flush()
    _history(db, f, norm_wkt, reason, None, actor_id)
    return "updated"


def _history(db: Session, f: IntegratedFeature, wkt: str, reason: str, correction_id: int | None, actor_id):
    db.add(IntegratedFeatureHistory(integrated_feature_id=f.id, version=f.version, attributes=f.attributes,
                                    geom=f"SRID=4326;{wkt}", status=f.status, change_reason=reason,
                                    correction_id=correction_id, changed_by=actor_id))


def build_integrated(db: Session, project_id: int, params: dict, actor_id: int | None) -> dict:
    stats: dict[str, int] = defaultdict(int)
    seen: set[str] = set()
    pv = int(params["primary_parcel_version_id"])
    pds = _dataset_of(db, pv)
    if pds.kind != "parcels" or pds.project_id != project_id:
        raise ValueError("primary_parcel_version_id must be a parcels dataset version in this project")
    pp_run = _run(db, params.get("parcel_match_run_id"), "parcel_parcel")
    rec_run = _run(db, params.get("record_match_run_id"), "record_feature")
    bb_run = _run(db, params.get("building_match_run_id"), "building_building")
    bp_run = _run(db, params.get("building_parcel_run_id"), "building_parcel")
    reason = f"integration build ({utcnow().isoformat()})"

    pending = defaultdict(int)
    for r in db.execute(text("SELECT unnest(feature_ids) AS f, count(*) AS n FROM conflicts WHERE project_id=:p AND "
                             "status IN ('pending','deferred') GROUP BY 1"), {"p": project_id}).mappings():
        pending[r["f"]] = r["n"]

    parcels = _features(db, pv)
    pairs = _best_pairs(db, pp_run) if pp_run else {}
    sec_ds = _dataset_of(db, pp_run.right_version_id) if pp_run else None
    sec_feats = {f["id"]: f for f in _features(db, pp_run.right_version_id)} if pp_run else {}
    links: dict[int, list[dict]] = defaultdict(list)
    if rec_run:
        rec_rows = {f["id"]: f for f in db.execute(text(
            "SELECT id, source_id_raw, normalized_attributes FROM source_features WHERE dataset_version_id=:v"),
            {"v": rec_run.left_version_id}).mappings()}
        best_rec: dict[int, tuple[int, float]] = {}
        for row in db.execute(text("SELECT left_feature_id AS l, right_feature_id AS r, score FROM match_candidates "
                                   "WHERE run_id=:r AND match_class='match' AND status <> 'rejected' ORDER BY score"),
                              {"r": rec_run.id}).mappings():
            best_rec[row["l"]] = (row["r"], float(row["score"]))
        for rid, (pid_, sc) in best_rec.items():
            links[pid_].append({"record_feature_id": rid, "property_id": rec_rows[rid]["source_id_raw"],
                                "score": sc, "land_use": (rec_rows[rid]["normalized_attributes"] or {}).get("land_use")})

    parcel_key_by_feature: dict[int, str] = {}
    used_keys: set[str] = set()
    for p in parcels:
        key = f"parcel:{p['normalized_id'] or 'f' + str(p['id'])}"
        if key in used_keys:
            key = f"{key}#f{p['id']}"
        used_keys.add(key)
        parcel_key_by_feature[p["id"]] = key
        na = {k: v for k, v in (p["normalized_attributes"] or {}).items() if k not in PERSONAL_FIELDS}
        attrs = {**na, "parcel_id": p["source_id_raw"], "area_sqm_computed": round(p["area_m2"] or 0, 2),
                 "provenance": pds.provenance}
        lineage = {"geometry": {"dataset": pds.name, "feature_id": p["id"]},
                   "attributes": {k: pds.name for k in na}, "alternatives": {}}
        sources, conf = [p["id"]], None
        if p["id"] in pairs:
            sid, score = pairs[p["id"]]
            s = sec_feats.get(sid)
            if s:
                sources.append(sid)
                conf = score
                attrs["secondary_parcel_id"] = s["source_id_raw"]
                for k, v in (s["normalized_attributes"] or {}).items():
                    if k in PERSONAL_FIELDS or k == "parcel_id":
                        continue
                    if attrs.get(k) is None:
                        attrs[k] = v
                        lineage["attributes"][k] = sec_ds.name
                    elif v is not None and v != attrs.get(k):
                        lineage["alternatives"][k] = {"value": v, "dataset": sec_ds.name}
        if links.get(p["id"]):
            attrs["linked_property_ids"] = sorted(lk["property_id"] for lk in links[p["id"]])
            sources += [lk["record_feature_id"] for lk in links[p["id"]]]
            lineage["records"] = links[p["id"]]
        wkt = p["wkt"]
        qa = []
        if p["repair_status"] in ("needs_review", "repair_approved"):
            approved = _approved_geometry(db, p["id"])
            if approved:
                wkt = approved
                lineage["geometry"]["repair"] = "reviewer-approved correction"
            else:
                qa.append("geometry_invalid_pending_review")
        n_pending = sum(pending.get(fid, 0) for fid in sources)
        if n_pending:
            qa.append(f"open_conflicts:{n_pending}")
        attrs["qa_flags"] = qa
        res = _upsert(db, project_id, "parcel", key, attrs, wkt, sources, lineage, conf, actor_id, reason, seen)
        stats[f"parcels_{res}"] += 1

    # ---- buildings -----------------------------------------------------------------------------
    bversions = [int(v) for v in params.get("building_version_ids", [])]
    if bb_run and not bversions:
        bversions = [bb_run.left_version_id, bb_run.right_version_id]
    parcel_of: dict[int, list[tuple[float, int]]] = defaultdict(list)
    if bp_run:
        for row in db.execute(text("SELECT left_feature_id AS b, right_feature_id AS p, (features->>'building_coverage')"
                                   "::float AS s FROM match_candidates WHERE run_id=:r"), {"r": bp_run.id}).mappings():
            parcel_of[row["b"]].append((row["s"] or 0.0, row["p"]))
    bpairs = _best_pairs(db, bb_run) if bb_run else {}
    paired_right = {r for r, _ in bpairs.values()}
    for vid in bversions:
        ds = _dataset_of(db, vid)
        for b in _features(db, vid):
            if ds.kind == "predicted_buildings" and b["review_status"] != "accepted":
                continue
            if bb_run and vid == bb_run.right_version_id and b["id"] in paired_right:
                continue  # already merged into its left partner
            sources = [b["id"]]
            lineage = {"geometry": {"dataset": ds.name, "feature_id": b["id"]}, "sources": [ds.name]}
            attrs = {k: v for k, v in (b["normalized_attributes"] or {}).items() if k not in PERSONAL_FIELDS}
            attrs.update({"source_building_id": b["source_id_raw"], "provenance": ds.provenance,
                          "source_class": "predicted_accepted" if ds.kind == "predicted_buildings" else "observed"})
            wkt, conf = b["wkt"], None
            if bb_run and vid == bb_run.left_version_id and b["id"] in bpairs:
                other_id, conf = bpairs[b["id"]]
                ods = _dataset_of(db, bb_run.right_version_id)
                orow = db.execute(text("SELECT source_id_raw, normalized_attributes, ST_AsText(geom) AS wkt FROM "
                                       "source_features WHERE id=:i"), {"i": other_id}).mappings().one()
                sources.append(other_id)
                lineage["sources"].append(ods.name)
                attrs["other_source_building_id"] = orow["source_id_raw"]
                for k, v in (orow["normalized_attributes"] or {}).items():
                    attrs.setdefault(k, v)
                if ods.reliability > ds.reliability:
                    wkt = orow["wkt"]
                    lineage["geometry"] = {"dataset": ods.name, "feature_id": other_id}
            shares = sorted(parcel_of.get(b["id"], []), reverse=True)
            if shares:
                attrs["parcel_key"] = parcel_key_by_feature.get(shares[0][1])
                attrs["parcel_share"] = round(shares[0][0], 4)
                attrs["crosses_parcels"] = sum(1 for s, _ in shares if s >= 0.05) > 1
            key = f"building:{ds.id}:{b['source_id_raw'] or b['id']}"
            res = _upsert(db, project_id, "building", key, attrs, wkt, sources, lineage, conf, actor_id, reason, seen)
            stats[f"buildings_{res}"] += 1

    # retire features no longer produced
    for f in db.execute(select(IntegratedFeature).where(IntegratedFeature.project_id == project_id,
                                                        IntegratedFeature.status == "active")).scalars():
        if f.integrated_key not in seen:
            f.status, f.version, f.updated_at, f.updated_by = "retired", f.version + 1, utcnow(), actor_id
            wkt = db.execute(text("SELECT ST_AsText(geom) FROM integrated_features WHERE id=:i"), {"i": f.id}).scalar()
            _history(db, f, wkt, reason + ": no longer present in sources", None, actor_id)
            stats["retired"] += 1
    audit.record(db, "integration.built", "project", project_id, actor_id=actor_id, project_id=project_id,
                 details={"params": params, "stats": dict(stats)})
    db.flush()
    return dict(stats)


def apply_correction(db: Session, correction: Correction, actor_id: int, expected_version: int | None = None) -> str:
    """Apply an approved correction to the integrated layer with optimistic concurrency.

    Returns 'applied' or 'approved_pending_build' (no integrated feature exists yet).
    """
    f = None
    if correction.integrated_feature_id:
        f = db.get(IntegratedFeature, correction.integrated_feature_id)
    elif correction.source_feature_id:
        f = db.execute(select(IntegratedFeature).where(
            IntegratedFeature.project_id == correction.project_id,
            IntegratedFeature.source_feature_ids.contains([correction.source_feature_id]),
            IntegratedFeature.status == "active").order_by(IntegratedFeature.id)).scalars().first()
    if f is None:
        correction.status = "approved"
        return "approved_pending_build"
    base = expected_version if expected_version is not None else correction.base_version
    if base is not None and base != f.version:
        raise VersionConflict(
            f"Integrated feature {f.integrated_key} is at version {f.version}, correction was prepared against "
            f"version {base}. Reload and re-review.")
    if correction.kind == "geometry_repair":
        wkt = db.execute(text("SELECT ST_AsText(proposed_geom) FROM corrections WHERE id=:i"), {"i": correction.id}).scalar()
        f.geom = f"SRID=4326;{wkt}"
        f.attributes = {**f.attributes, "qa_flags": [q for q in f.attributes.get("qa_flags", [])
                                                     if q != "geometry_invalid_pending_review"]}
    elif correction.kind == "attribute_update":
        f.attributes = {**f.attributes, **correction.proposed_attributes}
        wkt = db.execute(text("SELECT ST_AsText(geom) FROM integrated_features WHERE id=:i"), {"i": f.id}).scalar()
    else:
        raise ValueError(f"Unsupported correction kind {correction.kind}")
    f.version += 1
    f.updated_by, f.updated_at = actor_id, utcnow()
    correction.integrated_feature_id = f.id
    correction.status = "applied"
    correction.applied_at = utcnow()
    db.flush()
    _history(db, f, wkt, f"correction {correction.id} ({correction.kind}) approved", correction.id, actor_id)
    db.flush()
    return "applied"
