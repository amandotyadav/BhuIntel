"""Conflict engine: detection from match runs, explainable heuristic confidence, priority and idempotent upsert.

Confidence here means "how strongly the evidence indicates that this conflict is real" and is a
heuristic in [0, 1]; it is never a statement about ownership or legal validity.

priority = 100 * severity_weight * (0.5 + 0.5 * confidence) [+10 if legally sensitive, capped at 100]
"""

import hashlib
from collections import defaultdict

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import (
    Conflict,
    Correction,
    Dataset,
    DatasetVersion,
    MatchCandidate,
    MatchRun,
    SourceFeature,
    utcnow,
)

SEVERITY_WEIGHT = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}

CATEGORY_DEFAULTS: dict[str, dict] = {
    "parcel_id_mismatch": {"severity": "high", "legal": True,
                           "action": "Verify both identifiers against the authoritative register; do not auto-correct."},
    "attribute_conflict": {"severity": "medium", "legal": False,
                           "action": "Review the conflicting values; proposed value is from the more reliable/recent source."},
    "source_disagreement": {"severity": "high", "legal": True,
                            "action": "Geometries disagree beyond tolerance; field verification or survey check required."},
    "unmatched_feature": {"severity": "low", "legal": False,
                          "action": "Confirm whether the feature exists in the other source or is missing/outdated."},
    "unmatched_record": {"severity": "medium", "legal": False,
                         "action": "Record could not be linked to any parcel; check the plot identifier."},
    "ambiguous_record_link": {"severity": "medium", "legal": False,
                              "action": "Record links to multiple parcels; select the correct parcel."},
    "duplicate_record": {"severity": "medium", "legal": False,
                         "action": "Review duplicates; retain one record and retire the others."},
    "area_discrepancy": {"severity": "medium", "legal": True,
                         "action": "Recorded area differs from mapped area; verify with survey records."},
    "building_crosses_parcel": {"severity": "medium", "legal": False,
                                "action": "Building footprint spans parcels; check digitisation or encroachment."},
    "potentially_outdated": {"severity": "low", "legal": False,
                             "action": "Feature absent in a newer source; verify on recent imagery before retiring."},
    "invalid_geometry": {"severity": "high", "legal": False, "action": "Review proposed geometry repair."},
    "parcel_overlap": {"severity": "high", "legal": True,
                       "action": "Overlapping parcel boundaries are a survey/legal matter; do not auto-correct."},
    "parcel_gap": {"severity": "medium", "legal": True,
                   "action": "Unexpected gap between parcels; verify boundaries with survey records."},
    "sliver_polygon": {"severity": "medium", "legal": True,
                       "action": "Thin sliver polygon, likely a digitising artefact; verify before removal."},
    "duplicate_geometry": {"severity": "medium", "legal": False, "action": "Identical geometries; remove duplicates."},
    "unexpected_geometry_type": {"severity": "medium", "legal": False,
                                 "action": "Geometry type not expected for this dataset kind."},
    "dangling_segment": {"severity": "low", "legal": False, "action": "Line end not connected; check network snapping."},
    "disconnected_network": {"severity": "low", "legal": False, "action": "Isolated network component."},
    "uncertain_repair": {"severity": "medium", "legal": False, "action": "Approve or reject the proposed repair."},
}


def fingerprint(category: str, feature_ids: list[int], key: str = "") -> str:
    raw = f"{category}|{','.join(str(i) for i in sorted(feature_ids))}|{key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def priority(severity: str, confidence: float, legal: bool) -> float:
    p = 100.0 * SEVERITY_WEIGHT[severity] * (0.5 + 0.5 * max(0.0, min(1.0, confidence)))
    if legal:
        p += 10.0
    return round(min(p, 100.0), 2)


def upsert_conflict(
    db: Session,
    project_id: int,
    category: str,
    feature_ids: list[int],
    version_ids: list[int],
    confidence: float,
    explanation: str,
    evidence: dict,
    geom_sql: str | None = None,
    geom_params: dict | None = None,
    run_id: int | None = None,
    key: str = "",
    severity: str | None = None,
    action: str | None = None,
) -> tuple[Conflict, bool]:
    """Insert or refresh a conflict. Reviewed conflicts keep their decision (idempotent re-detection)."""
    d = CATEGORY_DEFAULTS[category]
    sev = severity or d["severity"]
    fp = fingerprint(category, feature_ids, key)
    c = db.execute(select(Conflict).where(Conflict.project_id == project_id, Conflict.fingerprint == fp)).scalar_one_or_none()
    created = c is None
    if c is None:
        c = Conflict(project_id=project_id, fingerprint=fp, category=category, status="pending")
        db.add(c)
    c.run_id = run_id or c.run_id
    c.severity = sev
    c.confidence = round(float(confidence), 4)
    c.legal_sensitive = d["legal"]
    c.priority = priority(sev, c.confidence, d["legal"])
    c.feature_ids = sorted(int(i) for i in feature_ids)
    c.dataset_version_ids = sorted({int(v) for v in version_ids})
    c.evidence = evidence
    c.explanation = explanation
    c.proposed_action = action or d["action"]
    c.updated_at = utcnow()
    db.flush()
    if geom_sql:
        db.execute(text(f"UPDATE conflicts SET geom = ({geom_sql}) WHERE id = :cid"), {**(geom_params or {}),
                                                                                     "cid": c.id})
    elif feature_ids:
        # ST_Collect (not ST_Union): display geometry only, and source geometries may be invalid
        db.execute(text("UPDATE conflicts SET geom = (SELECT ST_Collect(geom) FROM source_features WHERE id = ANY(:ids))"
                        " WHERE id = :cid"), {"ids": list(feature_ids), "cid": c.id})
    return c, created


def _feature_rows(db: Session, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    rows = db.execute(text(
        "SELECT f.id, f.normalized_id, f.source_id_raw, f.normalized_attributes, f.area_m2, f.dataset_version_id, "
        "d.name AS dataset_name, d.reliability, d.source_date, d.id AS dataset_id FROM source_features f "
        "JOIN dataset_versions v ON v.id=f.dataset_version_id JOIN datasets d ON d.id=v.dataset_id "
        "WHERE f.id = ANY(:ids)"), {"ids": ids}).mappings().all()
    return {r["id"]: dict(r) for r in rows}


def _src(r: dict) -> dict:
    return {"feature_id": r["id"], "dataset": r["dataset_name"], "identifier": r["source_id_raw"],
            "reliability": r["reliability"], "source_date": r["source_date"].isoformat() if r["source_date"] else None}


def detect_from_run(db: Session, run: MatchRun, cfg: dict | None = None) -> dict:
    """Generate conflicts for a completed match run. Returns counts by category."""
    cfg = {"area_tolerance": 0.10, "id_mismatch_min_iou": 0.8, "disagreement_iou": 0.5,
           "numeric_tolerance": 0.10, **(cfg or {})}
    counts: dict[str, int] = defaultdict(int)
    cands = db.execute(select(MatchCandidate).where(MatchCandidate.run_id == run.id)).scalars().all()
    tol = float(run.params.get("effective_tolerance_m", 5.0))
    pid = run.project_id
    vids = [run.left_version_id, run.right_version_id]
    feats = _feature_rows(db, list({c.left_feature_id for c in cands} | {c.right_feature_id for c in cands}))

    def add(cat, fids, conf, expl, ev, **kw):
        _, created = upsert_conflict(db, pid, cat, fids, vids, conf, expl, ev, run_id=run.id, **kw)
        counts[cat] += 1

    if run.match_type in ("parcel_parcel", "building_building"):
        best_l: dict[int, MatchCandidate] = {}
        for c in cands:
            if c.match_class in ("match", "possible"):
                if c.left_feature_id not in best_l or c.score > best_l[c.left_feature_id].score:
                    best_l[c.left_feature_id] = c
        for c in best_l.values():
            f = c.features
            L, R = feats[c.left_feature_id], feats[c.right_feature_id]
            ev = {"match_candidate_id": c.id, "match_score": c.score, "score_kind": c.score_kind,
                  "features": {k: f.get(k) for k in ("iou", "centroid_distance_m", "hausdorff_m", "id_similarity",
                                                     "area_ratio")},
                  "sources": [_src(L), _src(R)]}
            ids_present = bool(L["normalized_id"]) and bool(R["normalized_id"])
            if (run.match_type == "parcel_parcel" and ids_present and (f.get("iou") or 0) >= cfg["id_mismatch_min_iou"]
                    and (f.get("id_similarity") or 0) < 0.9):
                add("parcel_id_mismatch", [L["id"], R["id"]], f["iou"],
                    f"Geometries coincide (IoU {f['iou']:.2f}) but identifiers differ: "
                    f"'{L['source_id_raw']}' ({L['dataset_name']}) vs '{R['source_id_raw']}' ({R['dataset_name']}).",
                    ev)
            # attribute conflicts (one per field)
            la, ra = L["normalized_attributes"] or {}, R["normalized_attributes"] or {}
            for fld in ("land_use", "area_sqm", "ward", "village", "building_levels"):
                va, vb = la.get(fld), ra.get(fld)
                if va is None or vb is None:
                    continue
                differs = (abs(va - vb) / max(abs(va), abs(vb), 1e-9) > cfg["numeric_tolerance"]
                           if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else
                           str(va).strip().lower() != str(vb).strip().lower())
                if not differs:
                    continue
                pref = L if (L["reliability"], str(L["source_date"])) >= (R["reliability"], str(R["source_date"])) else R
                proposed = la.get(fld) if pref is L else ra.get(fld)
                add("attribute_conflict", [L["id"], R["id"]], c.score,
                    f"Matched features disagree on '{fld}': {va!r} ({L['dataset_name']}) vs {vb!r} "
                    f"({R['dataset_name']}).", {**ev, "field": fld, "values": {L["dataset_name"]: va,
                                                                              R["dataset_name"]: vb},
                                               "proposed_value": proposed, "proposed_source": pref["dataset_name"]},
                    key=fld)
        # identifier-linked pairs whose geometries disagree (same id, low IoU or large Hausdorff)
        if run.match_type == "parcel_parcel":
            id_pairs = db.execute(text(
                """
                SELECT a.id AS lid, b.id AS rid,
                  ST_Area(ST_Intersection(a.geom_work,b.geom_work)) /
                    NULLIF(ST_Area(ST_Union(a.geom_work,b.geom_work)),0) AS iou,
                  ST_HausdorffDistance(a.geom_work,b.geom_work) AS hd,
                  ST_Distance(ST_Centroid(a.geom_work),ST_Centroid(b.geom_work)) AS cd
                FROM source_features a JOIN source_features b
                  ON b.dataset_version_id=:r AND a.normalized_id = b.normalized_id
                WHERE a.dataset_version_id=:l AND a.normalized_id IS NOT NULL AND a.duplicate_of IS NULL
                  AND b.duplicate_of IS NULL AND a.geom_work IS NOT NULL AND b.geom_work IS NOT NULL
                """), {"l": run.left_version_id, "r": run.right_version_id}).mappings().all()
            more = _feature_rows(db, [p["lid"] for p in id_pairs] + [p["rid"] for p in id_pairs])
            feats.update(more)
            for p in id_pairs:
                iou = float(p["iou"] or 0)
                if iou < cfg["disagreement_iou"] or float(p["hd"]) > 2 * tol:
                    L, R = feats[p["lid"]], feats[p["rid"]]
                    add("source_disagreement", [L["id"], R["id"]], min(1.0, 1 - iou + 0.2),
                        f"Same identifier '{L['source_id_raw']}' but geometries disagree: IoU {iou:.2f}, centroid "
                        f"offset {float(p['cd']):.1f} m, Hausdorff {float(p['hd']):.1f} m (tolerance {tol:.1f} m).",
                        {"iou": iou, "hausdorff_m": float(p["hd"]), "centroid_distance_m": float(p["cd"]),
                         "tolerance_m": tol, "sources": [_src(L), _src(R)]})
        # unmatched on either side
        for side, vid in (("left", run.left_version_id), ("right", run.right_version_id)):
            col = "left_feature_id" if side == "left" else "right_feature_id"
            rows = db.execute(text(
                f"""
                SELECT f.id, (SELECT max(score) FROM match_candidates m WHERE m.run_id=:run AND m.{col}=f.id) AS best
                FROM source_features f WHERE f.dataset_version_id=:v AND f.duplicate_of IS NULL
                  AND f.geom IS NOT NULL AND (f.review_status IS NULL OR f.review_status <> 'rejected')
                  AND NOT EXISTS (SELECT 1 FROM match_candidates m WHERE m.run_id=:run AND m.{col}=f.id
                                  AND m.match_class IN ('match','possible'))
                """), {"run": run.id, "v": vid}).mappings().all()
            fr = _feature_rows(db, [r["id"] for r in rows])
            for r in rows:
                F = fr[r["id"]]
                best = float(r["best"] or 0.0)
                other = run.right_version_id if side == "left" else run.left_version_id
                other_ds = db.get(Dataset, db.get(DatasetVersion, other).dataset_id)
                newer_other = (other_ds.source_date and F["source_date"] and other_ds.source_date > F["source_date"])
                cat = "potentially_outdated" if (newer_other and run.match_type == "building_building") else \
                    "unmatched_feature"
                add(cat, [F["id"]], 1.0 - best,
                    f"Feature '{F['source_id_raw'] or F['id']}' in {F['dataset_name']} has no match in "
                    f"{other_ds.name} (best candidate score {best:.2f})."
                    + (" The other source is newer, so this feature may be outdated." if cat ==
                       "potentially_outdated" else ""),
                    {"best_candidate_score": best, "sources": [_src(F)], "other_dataset": other_ds.name})

    if run.match_type == "building_parcel":
        by_building: dict[int, list[MatchCandidate]] = defaultdict(list)
        min_share = float(run.params.get("min_building_share", 0.05))
        for c in cands:
            if (c.features.get("building_coverage") or 0) >= min_share:
                by_building[c.left_feature_id].append(c)
        for bid, cs in by_building.items():
            if len(cs) < 2:
                continue
            shares = sorted(((c.features["building_coverage"], c.right_feature_id) for c in cs), reverse=True)
            fr = _feature_rows(db, [bid] + [s[1] for s in shares])
            conf = 1.0 - shares[0][0] + shares[1][0]  # more even split -> more confident it truly crosses
            add("building_crosses_parcel", [bid] + [s[1] for s in shares], min(1.0, conf),
                f"Building {fr[bid]['source_id_raw'] or bid} spans {len(shares)} parcels: " + ", ".join(
                    f"{fr[p]['source_id_raw']} ({s * 100:.0f}%)" for s, p in shares) + ".",
                {"shares": [{"parcel_feature_id": p, "parcel": fr[p]["source_id_raw"], "share": s} for s, p in shares],
                 "sources": [_src(fr[bid])]},
                geom_sql="SELECT geom FROM source_features WHERE id=:b", geom_params={"b": bid})

    if run.match_type == "record_feature":
        by_rec: dict[int, list[MatchCandidate]] = defaultdict(list)
        for c in cands:
            if c.match_class in ("match", "possible"):
                by_rec[c.left_feature_id].append(c)
        recs = db.execute(text("SELECT id FROM source_features WHERE dataset_version_id=:v AND duplicate_of IS NULL"),
                          {"v": run.left_version_id}).scalars().all()
        fr = _feature_rows(db, list(recs) + [c.right_feature_id for c in cands])
        for rid in recs:
            cs = by_rec.get(rid, [])
            R = fr[rid]
            if not cs:
                add("unmatched_record", [rid], 0.9,
                    f"Record '{R['source_id_raw']}' (plot reference "
                    f"'{(R['normalized_attributes'] or {}).get('parcel_id')}') matches no parcel.",
                    {"sources": [_src(R)]})
                continue
            strong = [c for c in cs if c.match_class == "match"]
            if len(strong) > 1:
                add("ambiguous_record_link", [rid] + [c.right_feature_id for c in strong], 0.7,
                    f"Record '{R['source_id_raw']}' links to {len(strong)} parcels.",
                    {"candidates": [{"parcel_feature_id": c.right_feature_id, "score": c.score} for c in strong]})
            best = max(cs, key=lambda c: c.score)
            P = fr[best.right_feature_id]
            if best.features.get("point_inside") == 0.0 and (best.features.get("id_similarity") or 0) >= 0.9:
                add("attribute_conflict", [rid, P["id"]], 0.6,
                    f"Record '{R['source_id_raw']}' references parcel '{P['source_id_raw']}' but its coordinates "
                    f"fall outside that parcel ({(best.features.get('point_distance_m') or 0):.1f} m away).",
                    {"field": "location", "match_score": best.score, "sources": [_src(R), _src(P)]}, key="location")
            ra, pa = R["normalized_attributes"] or {}, P["normalized_attributes"] or {}
            for fld in ("land_use",):
                if ra.get(fld) and pa.get(fld) and str(ra[fld]).lower() != str(pa[fld]).lower():
                    add("attribute_conflict", [rid, P["id"]], best.score,
                        f"Record '{R['source_id_raw']}' land use {ra[fld]!r} differs from parcel "
                        f"'{P['source_id_raw']}' ({pa[fld]!r}).",
                        {"field": fld, "values": {R["dataset_name"]: ra[fld], P["dataset_name"]: pa[fld]},
                         "match_score": best.score, "sources": [_src(R), _src(P)],
                         "proposed_value": pa[fld] if P["reliability"] >= R["reliability"] else ra[fld]}, key=fld)
    db.flush()
    return dict(counts)


def detect_dataset_issues(db: Session, version_id: int, cfg: dict | None = None) -> dict:
    """Per-dataset checks: duplicates (exact rows and duplicate identifiers), recorded-area discrepancies,
    and invalid geometries with proposed repairs."""
    cfg = {"area_tolerance": 0.10, **(cfg or {})}
    v = db.get(DatasetVersion, version_id)
    ds = db.get(Dataset, v.dataset_id)
    pid = ds.project_id
    counts: dict[str, int] = defaultdict(int)
    dups = db.execute(text(
        "SELECT id, duplicate_of FROM source_features WHERE dataset_version_id=:v AND duplicate_of IS NOT NULL"),
        {"v": version_id}).mappings().all()
    for d in dups:
        fr = _feature_rows(db, [d["id"], d["duplicate_of"]])
        upsert_conflict(db, pid, "duplicate_record", [d["duplicate_of"], d["id"]], [version_id], 1.0,
                        f"Exact duplicate record (identical geometry and attributes) of "
                        f"'{fr[d['duplicate_of']]['source_id_raw'] or d['duplicate_of']}' in {ds.name}.",
                        {"duplicate_feature_id": d["id"], "original_feature_id": d["duplicate_of"],
                         "sources": [_src(fr[d["id"]])]})
        counts["duplicate_record"] += 1
    same_id = db.execute(text(
        "SELECT normalized_id, array_agg(id ORDER BY id) AS ids FROM source_features WHERE dataset_version_id=:v "
        "AND duplicate_of IS NULL AND normalized_id IS NOT NULL GROUP BY normalized_id HAVING count(*) > 1"),
        {"v": version_id}).mappings().all()
    for r in same_id:
        upsert_conflict(db, pid, "duplicate_record", list(r["ids"]), [version_id], 0.8,
                        f"Identifier '{r['normalized_id']}' is used by {len(r['ids'])} different features in {ds.name}.",
                        {"identifier": r["normalized_id"], "feature_ids": list(r["ids"])}, key="same_id")
        counts["duplicate_record"] += 1
    if ds.kind == "parcels":
        rows = db.execute(text(
            "SELECT id, source_id_raw, area_m2, (normalized_attributes->>'area_sqm')::float AS rec FROM source_features "
            "WHERE dataset_version_id=:v AND duplicate_of IS NULL AND area_m2 IS NOT NULL "
            "AND normalized_attributes ? 'area_sqm' AND normalized_attributes->>'area_sqm' IS NOT NULL"),
            {"v": version_id}).mappings().all()
        for r in rows:
            rec, comp = float(r["rec"]), float(r["area_m2"])
            if comp <= 0 or rec <= 0:
                continue
            rel = abs(rec - comp) / comp
            if rel > cfg["area_tolerance"]:
                upsert_conflict(db, pid, "area_discrepancy", [r["id"]], [version_id], min(1.0, rel),
                                f"Recorded area {rec:.1f} m² differs from mapped area {comp:.1f} m² by {rel * 100:.0f}% "
                                f"(tolerance {cfg['area_tolerance'] * 100:.0f}%).",
                                {"recorded_area_m2": rec, "computed_area_m2": round(comp, 2), "relative_difference":
                                 round(rel, 4), "computed_in": "project working CRS"})
                counts["area_discrepancy"] += 1
    corr = db.execute(
        select(Correction)
        .join(SourceFeature, Correction.source_feature_id == SourceFeature.id)
        .where(SourceFeature.dataset_version_id == version_id, Correction.kind == "geometry_repair")
    ).scalars().all()
    for c in corr:
        if c.safety == "auto_safe":
            continue
        m = c.metrics
        if m.get("original_area") == 0 and m.get("repaired_area"):
            area_txt = (f"the self-intersecting ring has zero net (signed) area; the repaired geometry covers "
                        f"{m['repaired_area']:.1f} m²")
        else:
            area_txt = f"changes area by {(m.get('area_change_ratio') or 0) * 100:.1f}%"
        conf, _ = upsert_conflict(
            db, pid, "invalid_geometry", [c.source_feature_id], [version_id], 1.0,
            f"{c.reason}. Proposed repair via {c.method}: {area_txt} "
            f"({m.get('parts_before')} -> {m.get('parts_after')} parts). Safety class: {c.safety}.",
            {"correction_id": c.id, "safety": c.safety, "metrics": c.metrics},
            severity="high" if c.safety == "legal_no_auto" else "medium")
        if c.conflict_id is None:
            c.conflict_id = conf.id
        counts["invalid_geometry"] += 1
    db.flush()
    return dict(counts)
