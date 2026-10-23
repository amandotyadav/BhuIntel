"""Human review decisions for conflicts, corrections, match candidates and predicted buildings.

Rules
-----
* Decisions require project role ``reviewer`` or higher.
* Approving anything legally sensitive (legal-sensitive conflicts, ``legal_no_auto`` corrections)
  requires the ``admin`` role AND ``acknowledge_legal=True``; the acknowledgement is audited.
* Every decision is stored append-only (review_decisions) and in the hash-chained audit log.
* Approving a correction applies it to the integrated layer with optimistic version checks.
"""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import Conflict, Correction, MatchCandidate, ReviewDecision, SourceFeature, User, utcnow
from app.security import ROLE_RANK
from app.services import audit
from app.services.integration import VersionConflict, apply_correction

STATUS_FOR = {"approve": "approved", "reject": "rejected", "defer": "deferred"}


def _record(db: Session, project_id: int, subject_type: str, subject_id: int, decision: str, comment: str,
            user: User, extra: dict | None = None) -> ReviewDecision:
    rd = ReviewDecision(project_id=project_id, subject_type=subject_type, subject_id=subject_id, decision=decision,
                        comment=comment or "", reviewer_id=user.id)
    db.add(rd)
    db.flush()
    audit.record(db, f"review.{subject_type}.{decision}", subject_type, subject_id, actor_id=user.id,
                 project_id=project_id, details={"comment": (comment or "")[:500], **(extra or {})})
    return rd


def _require_legal(user: User, role: str, acknowledge_legal: bool, what: str) -> None:
    if ROLE_RANK[role] < ROLE_RANK["admin"]:
        raise ApiError(403, "legal_approval_requires_admin",
                       f"{what} is legally sensitive; only an admin may approve it.")
    if not acknowledge_legal:
        raise ApiError(422, "legal_acknowledgement_required",
                       f"{what} is legally sensitive. Set acknowledge_legal=true to confirm this approval is backed "
                       "by authoritative evidence (e.g. survey records); the acknowledgement is audited.")


def decide_correction(db: Session, c: Correction, user: User, role: str, decision: str, comment: str,
                      acknowledge_legal: bool = False, expected_version: int | None = None) -> dict:
    if decision not in ("approve", "reject", "defer", "comment"):
        raise ApiError(422, "invalid_decision", "decision must be approve, reject, defer or comment")
    if c.status in ("applied", "rejected") and decision != "comment":
        raise ApiError(409, "already_decided", f"Correction is already {c.status}")
    outcome = None
    if decision == "approve":
        if c.safety == "legal_no_auto":
            _require_legal(user, role, acknowledge_legal, "This correction")
        try:
            outcome = apply_correction(db, c, user.id, expected_version)
        except VersionConflict as exc:
            raise ApiError(409, "version_conflict", str(exc)) from None
        if c.source_feature_id:
            db.execute(text("UPDATE source_features SET repair_status='repair_approved' WHERE id=:i AND "
                            "repair_status='needs_review'"), {"i": c.source_feature_id})
    elif decision in ("reject", "defer"):
        c.status = STATUS_FOR[decision]
    _record(db, c.project_id, "correction", c.id, decision, comment, user,
            {"safety": c.safety, "outcome": outcome, "acknowledge_legal": acknowledge_legal})
    # keep the linked conflict in step with its correction
    if c.conflict_id and decision != "comment":
        conf = db.get(Conflict, c.conflict_id)
        if conf and conf.status == "pending":
            conf.status = STATUS_FOR[decision]
            conf.updated_at = utcnow()
    db.flush()
    return {"status": c.status, "outcome": outcome}


def decide_conflict(db: Session, conf: Conflict, user: User, role: str, decision: str, comment: str,
                    acknowledge_legal: bool = False, expected_version: int | None = None) -> dict:
    """approve = accept the proposed action; reject = not a real conflict / proposal wrong; defer = later."""
    if decision not in ("approve", "reject", "defer", "comment"):
        raise ApiError(422, "invalid_decision", "decision must be approve, reject, defer or comment")
    if decision == "approve" and conf.legal_sensitive:
        _require_legal(user, role, acknowledge_legal, f"Conflict '{conf.category}'")
    outcome = None
    if decision == "approve":
        linked = db.execute(select(Correction).where(Correction.conflict_id == conf.id,
                                                     Correction.status.in_(("proposed", "deferred")))).scalars().all()
        if not linked and conf.category == "attribute_conflict" and conf.evidence.get("proposed_value") is not None:
            linked = [_attribute_correction(db, conf)]
        for c in linked:
            if c is None:
                continue
            if c.safety == "legal_no_auto" and not (ROLE_RANK[role] >= ROLE_RANK["admin"] and acknowledge_legal):
                _require_legal(user, role, acknowledge_legal, "The linked correction")
            try:
                outcome = apply_correction(db, c, user.id, expected_version)
            except VersionConflict as exc:
                raise ApiError(409, "version_conflict", str(exc)) from None
            _record(db, c.project_id, "correction", c.id, "approve", f"via conflict {conf.id}: {comment}", user,
                    {"outcome": outcome})
            if c.source_feature_id and c.kind == "geometry_repair":
                db.execute(text("UPDATE source_features SET repair_status='repair_approved' WHERE id=:i"),
                           {"i": c.source_feature_id})
    if decision in STATUS_FOR:
        conf.status = STATUS_FOR[decision]
        conf.updated_at = utcnow()
        if decision == "reject":
            for c in db.execute(select(Correction).where(Correction.conflict_id == conf.id,
                                                         Correction.status == "proposed")).scalars():
                c.status = "rejected"
    _record(db, conf.project_id, "conflict", conf.id, decision, comment, user,
            {"category": conf.category, "legal_sensitive": conf.legal_sensitive, "acknowledge_legal": acknowledge_legal,
             "correction_outcome": outcome})
    db.flush()
    return {"status": conf.status, "correction_outcome": outcome}


def _attribute_correction(db: Session, conf: Conflict) -> Correction | None:
    """Create the attribute correction implied by an attribute conflict (target: integrated feature)."""
    field = conf.evidence.get("field")
    if not field or field == "location":
        return None
    row = db.execute(text(
        "SELECT id, version, attributes FROM integrated_features WHERE project_id=:p AND status='active' AND "
        "source_feature_ids && CAST(:ids AS bigint[]) ORDER BY id LIMIT 1"),
        {"p": conf.project_id, "ids": list(conf.feature_ids)}).mappings().first()
    c = Correction(project_id=conf.project_id, conflict_id=conf.id, kind="attribute_update", safety="review_required",
                   integrated_feature_id=row["id"] if row else None, base_version=row["version"] if row else None,
                   original_attributes={field: (row["attributes"] or {}).get(field)} if row else {},
                   proposed_attributes={field: conf.evidence["proposed_value"]},
                   reason=f"Resolve attribute conflict on '{field}' using {conf.evidence.get('proposed_source', 'the more reliable source')}",
                   method="prefer_more_reliable_then_newer_source", status="proposed",
                   source_feature_id=conf.feature_ids[0] if conf.feature_ids else None)
    db.add(c)
    db.flush()
    return c


def decide_match(db: Session, m: MatchCandidate, project_id: int, user: User, decision: str, comment: str) -> dict:
    mapping = {"approve": ("accepted", 1), "reject": ("rejected", 0), "defer": ("deferred", None)}
    if decision not in mapping:
        raise ApiError(422, "invalid_decision", "decision must be approve, reject or defer")
    m.status, label = mapping[decision]
    if label is not None:
        m.label, m.label_source = label, "reviewer"
    m.decided_by, m.decided_at = user.id, utcnow()
    _record(db, project_id, "match", m.id, decision, comment, user, {"label": label})
    db.flush()
    return {"status": m.status, "label": m.label}


def decide_prediction(db: Session, f: SourceFeature, project_id: int, user: User, decision: str, comment: str) -> dict:
    mapping = {"approve": "accepted", "reject": "rejected", "defer": "pending"}
    if decision not in mapping:
        raise ApiError(422, "invalid_decision", "decision must be approve, reject or defer")
    if f.review_status is None:
        raise ApiError(422, "not_a_prediction", "Feature is not a predicted building")
    f.review_status = mapping[decision]
    _record(db, project_id, "prediction", f.id, decision, comment, user)
    db.flush()
    return {"review_status": f.review_status}
