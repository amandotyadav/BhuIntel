"""Append-only, hash-chained audit log.

Each event stores sha256(prev_hash + canonical event JSON). ``verify_chain`` recomputes the chain so
any out-of-band tampering (the table also has an UPDATE/DELETE-blocking trigger) is detectable.
"""

import hashlib
import json
from datetime import UTC

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.logging_setup import request_id_var
from app.models import AuditEvent, utcnow


def _digest(prev: str | None, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(((prev or "") + canonical).encode()).hexdigest()


def record(
    db: Session,
    action: str,
    entity_type: str,
    entity_id=None,
    *,
    actor_id: int | None = None,
    project_id: int | None = None,
    details: dict | None = None,
) -> AuditEvent:
    # Serialise chain appends across concurrent transactions.
    db.execute(text("SELECT pg_advisory_xact_lock(814207)"))
    prev = db.execute(select(AuditEvent.hash).order_by(AuditEvent.id.desc()).limit(1)).scalar_one_or_none()
    ts = utcnow()
    payload = {
        "ts": ts.astimezone(UTC).isoformat(),
        "actor_id": actor_id,
        "project_id": project_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": None if entity_id is None else str(entity_id),
        "details": details or {},
    }
    ev = AuditEvent(
        ts=ts,
        actor_id=actor_id,
        project_id=project_id,
        action=action,
        entity_type=entity_type,
        entity_id=payload["entity_id"],
        details=details or {},
        request_id=request_id_var.get(),
        prev_hash=prev,
        hash=_digest(prev, payload),
    )
    db.add(ev)
    db.flush()
    return ev


def verify_chain(db: Session) -> tuple[bool, int, int | None]:
    """Returns (ok, events_checked, first_bad_event_id)."""
    prev = None
    n = 0
    for ev in db.execute(select(AuditEvent).order_by(AuditEvent.id)).scalars():
        payload = {
            "ts": ev.ts.astimezone(UTC).isoformat(),
            "actor_id": ev.actor_id,
            "project_id": ev.project_id,
            "action": ev.action,
            "entity_type": ev.entity_type,
            "entity_id": ev.entity_id,
            "details": ev.details,
        }
        if ev.prev_hash != prev or ev.hash != _digest(prev, payload):
            return False, n, ev.id
        prev = ev.hash
        n += 1
    return True, n, None
