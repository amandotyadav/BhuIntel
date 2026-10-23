"""Conflicts, corrections, review queue, decisions and audit trail."""

import json

from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.common import feature_collection, parse_bbox, redact
from app.db import get_db
from app.errors import ApiError
from app.models import Conflict, Correction, ReviewDecision, User
from app.schemas import DecisionIn
from app.security import authorize_project, effective_project_role, get_current_user
from app.services import audit, review

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["review"])


@router.get("/conflicts")
def list_conflicts(project_id: int, status: str | None = None, category: str | None = None,
                   severity: str | None = None, bbox: str | None = None, limit: int = 200, offset: int = 0,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    where, params = ["project_id=:p"], {"p": project_id}
    for col, val in (("status", status), ("category", category), ("severity", severity)):
        if val:
            where.append(f"{col} = :{col}")
            params[col] = val
    b = parse_bbox(bbox)
    if b:
        where.append("geom && ST_MakeEnvelope(:x0,:y0,:x1,:y1,4326)")
        params.update({"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
    w = " AND ".join(where)
    rows = db.execute(text(
        f"""SELECT id, category, severity, priority, confidence, status, legal_sensitive, feature_ids,
                   dataset_version_ids, explanation, proposed_action, run_id, created_at, updated_at,
                   ST_AsGeoJSON(geom, 7) AS geojson
            FROM conflicts WHERE {w} ORDER BY priority DESC, id LIMIT :l OFFSET :o"""),
        {**params, "l": max(1, min(limit, 2000)), "o": max(0, offset)}).mappings().all()
    fc = feature_collection(rows)
    fc["numberMatched"] = db.execute(text(f"SELECT count(*) FROM conflicts WHERE {w}"), params).scalar()
    fc["summary"] = [dict(r) for r in db.execute(text(
        f"SELECT category, status, count(*) AS n FROM conflicts WHERE {w} GROUP BY category, status ORDER BY 1,2"),
        params).mappings()]
    return fc


@router.get("/conflicts/{conflict_id}")
def get_conflict(project_id: int, conflict_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id)
    c = db.get(Conflict, conflict_id)
    if c is None or c.project_id != project_id:
        raise ApiError(404, "not_found", "Conflict not found")
    role = effective_project_role(db, user, p)
    feats = db.execute(text(
        """SELECT f.id, f.source_id_raw, f.normalized_id, f.attributes, f.normalized_attributes, f.area_m2,
                  f.is_valid_source, f.validity_reason, f.repair_status, d.name AS dataset, d.id AS dataset_id,
                  d.kind, d.provenance, d.reliability, d.source_date, d.license, v.version_no, v.id AS version_id,
                  ST_AsGeoJSON(f.geom, 7) AS geojson
           FROM source_features f JOIN dataset_versions v ON v.id=f.dataset_version_id
           JOIN datasets d ON d.id=v.dataset_id WHERE f.id = ANY(:ids) ORDER BY f.id"""),
        {"ids": list(c.feature_ids)}).mappings().all()
    fc = feature_collection(feats)
    for f in fc["features"]:
        f["properties"]["attributes"] = redact(f["properties"]["attributes"], role)
        f["properties"]["normalized_attributes"] = redact(f["properties"]["normalized_attributes"], role)
    corrections = db.execute(text(
        """SELECT id, kind, safety, status, reason, method, metrics, original_attributes, proposed_attributes,
                  integrated_feature_id, base_version, source_feature_id,
                  ST_AsGeoJSON(original_geom, 7) AS original_geojson, ST_AsGeoJSON(proposed_geom, 7) AS proposed_geojson
           FROM corrections WHERE conflict_id=:c OR (source_feature_id = ANY(:ids) AND kind='geometry_repair')
           ORDER BY id"""), {"c": c.id, "ids": list(c.feature_ids)}).mappings().all()
    geom = db.execute(text("SELECT ST_AsGeoJSON(geom, 7) FROM conflicts WHERE id=:i"), {"i": c.id}).scalar()
    history = db.execute(select(ReviewDecision).where(ReviewDecision.project_id == project_id,
                                                      ReviewDecision.subject_type == "conflict",
                                                      ReviewDecision.subject_id == c.id).order_by(ReviewDecision.id)
                         ).scalars().all()
    return {
        "id": c.id, "category": c.category, "severity": c.severity, "priority": c.priority, "confidence": c.confidence,
        "confidence_kind": "heuristic (not a probability; not evidence of ownership)", "status": c.status,
        "legal_sensitive": c.legal_sensitive, "explanation": c.explanation, "proposed_action": c.proposed_action,
        "evidence": c.evidence, "run_id": c.run_id, "geometry": json.loads(geom) if geom else None,
        "features": fc, "corrections": [dict(r) for r in corrections],
        "decisions": [{"id": d.id, "decision": d.decision, "comment": d.comment, "reviewer_id": d.reviewer_id,
                       "created_at": d.created_at.isoformat()} for d in history],
    }


@router.post("/conflicts/{conflict_id}/decision")
def decide_conflict(project_id: int, conflict_id: int, body: DecisionIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id, "reviewer")
    c = db.get(Conflict, conflict_id)
    if c is None or c.project_id != project_id:
        raise ApiError(404, "not_found", "Conflict not found")
    res = review.decide_conflict(db, c, user, effective_project_role(db, user, p), body.decision, body.comment,
                                 body.acknowledge_legal, body.expected_version)
    db.commit()
    return res


@router.get("/corrections")
def list_corrections(project_id: int, status: str | None = None, safety: str | None = None,
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    where, params = ["project_id=:p"], {"p": project_id}
    if status:
        where.append("status=:s")
        params["s"] = status
    if safety:
        where.append("safety=:sf")
        params["sf"] = safety
    rows = db.execute(text(
        f"""SELECT id, conflict_id, source_feature_id, integrated_feature_id, base_version, kind, safety, status, reason,
                   method, metrics, original_attributes, proposed_attributes, created_at, applied_at,
                   ST_AsGeoJSON(original_geom, 7) AS original_geojson, ST_AsGeoJSON(proposed_geom, 7) AS geojson
            FROM corrections WHERE {' AND '.join(where)} ORDER BY id LIMIT 2000"""), params).mappings().all()
    return feature_collection(rows)


@router.post("/corrections/{correction_id}/decision")
def decide_correction(project_id: int, correction_id: int, body: DecisionIn, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id, "reviewer")
    c = db.get(Correction, correction_id)
    if c is None or c.project_id != project_id:
        raise ApiError(404, "not_found", "Correction not found")
    res = review.decide_correction(db, c, user, effective_project_role(db, user, p), body.decision, body.comment,
                                   body.acknowledge_legal, body.expected_version)
    db.commit()
    return res


@router.get("/review/queue", summary="Review queue counts and the next highest-priority items")
def queue(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    q = lambda sql: [dict(r) for r in db.execute(text(sql), {"p": project_id}).mappings()]  # noqa: E731
    return {
        "conflicts_by_status": q("SELECT status, count(*) AS n FROM conflicts WHERE project_id=:p GROUP BY status"),
        "corrections_by_status": q("SELECT status, safety, count(*) AS n FROM corrections WHERE project_id=:p "
                                   "GROUP BY status, safety"),
        "predictions_by_status": q("SELECT f.review_status AS status, count(*) AS n FROM source_features f JOIN "
                                   "dataset_versions v ON v.id=f.dataset_version_id JOIN datasets d ON d.id=v.dataset_id "
                                   "WHERE d.project_id=:p AND f.review_status IS NOT NULL GROUP BY 1"),
        "next": q("SELECT id, category, severity, priority, explanation FROM conflicts WHERE project_id=:p AND "
                  "status='pending' ORDER BY priority DESC, id LIMIT 10"),
    }


@router.get("/decisions")
def list_decisions(project_id: int, subject_type: str | None = None, subject_id: int | None = None,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    q = select(ReviewDecision, User.email).join(User, User.id == ReviewDecision.reviewer_id).where(
        ReviewDecision.project_id == project_id)
    if subject_type:
        q = q.where(ReviewDecision.subject_type == subject_type)
    if subject_id is not None:
        q = q.where(ReviewDecision.subject_id == subject_id)
    rows = db.execute(q.order_by(ReviewDecision.id.desc()).limit(1000)).all()
    return [{"id": d.id, "subject_type": d.subject_type, "subject_id": d.subject_id, "decision": d.decision,
             "comment": d.comment, "reviewer": email, "created_at": d.created_at.isoformat()} for d, email in rows]


@router.get("/audit")
def audit_events(project_id: int, limit: int = 200, offset: int = 0, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    rows = db.execute(text(
        "SELECT a.id, a.ts, a.action, a.entity_type, a.entity_id, a.details, a.request_id, a.hash, u.email AS actor "
        "FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id WHERE a.project_id=:p ORDER BY a.id DESC "
        "LIMIT :l OFFSET :o"), {"p": project_id, "l": max(1, min(limit, 1000)), "o": max(0, offset)}).mappings().all()
    return [dict(r) for r in rows]


@router.get("/audit/verify", summary="Verify the tamper-evident hash chain of the whole audit log")
def verify(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    ok, n, bad = audit.verify_chain(db)
    return {"ok": ok, "events_checked": n, "first_invalid_event_id": bad}
