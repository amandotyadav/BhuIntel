import json

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from app.api.common import feature_collection
from app.db import get_db
from app.errors import ApiError
from app.models import FieldAlias, Project, ProjectMember, StudyArea, User
from app.schemas import AliasIn, MemberIn, ProjectCreate, ProjectOut, ProjectUpdate, StudyAreaIn
from app.security import authorize_project, get_current_user, require_role
from app.services import audit
from app.services.crs import CrsError, suggest_utm_epsg, validate_working_crs
from app.services.schema_mapping import CANONICAL_FIELDS

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _validate_area(db: Session, geojson: dict) -> str:
    gj = json.dumps(geojson)
    ok, reason = db.execute(text("SELECT ST_IsValid(g), ST_IsValidReason(g) FROM (SELECT "
                                 "ST_SetSRID(ST_GeomFromGeoJSON(:g),4326) AS g) s"), {"g": gj}).one()
    if not ok:
        raise ApiError(422, "invalid_geometry", f"Study area geometry is invalid: {reason}")
    bounds = db.execute(text("SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g) FROM (SELECT "
                             "ST_GeomFromGeoJSON(:g) AS g) s"), {"g": gj}).one()
    if not (-180 <= bounds[0] <= 180 and -90 <= bounds[1] <= 90 and -180 <= bounds[2] <= 180 and -90 <= bounds[3] <= 90):
        raise ApiError(422, "invalid_geometry", "Study area must be in EPSG:4326 longitude/latitude")
    return gj


@router.get("", response_model=list[ProjectOut])
def list_projects(include_archived: bool = False, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    q = select(Project).order_by(Project.id.desc())
    if user.role != "admin":
        q = q.where(or_(Project.owner_id == user.id, Project.id.in_(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user.id))))
    if not include_archived:
        q = q.where(Project.status == "active")
    return db.execute(q).scalars().all()


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db), user: User = Depends(require_role("analyst"))):
    gj = _validate_area(db, body.study_area_geojson) if body.study_area_geojson else None
    epsg = body.working_crs
    if epsg is None:
        if gj is None:
            raise ApiError(422, "working_crs_required", "Provide working_crs or a study area to derive a UTM zone")
        lon, lat = db.execute(text("SELECT ST_X(c), ST_Y(c) FROM (SELECT ST_Centroid(ST_GeomFromGeoJSON(:g)) c) s"),
                              {"g": gj}).one()
        epsg = suggest_utm_epsg(lon, lat)
    try:
        validate_working_crs(epsg)
    except CrsError as exc:
        raise ApiError(422, "invalid_working_crs", str(exc)) from None
    p = Project(name=body.name, description=body.description, owner_id=user.id, working_crs=epsg,
                default_tolerance_m=body.default_tolerance_m, settings=body.settings)
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role=user.role))
    if gj:
        sa = StudyArea(project_id=p.id, name="Study area")
        db.add(sa)
        db.flush()
        db.execute(text("UPDATE study_areas SET geom=ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:g),4326)) WHERE id=:i"),
                   {"g": gj, "i": sa.id})
    audit.record(db, "project.created", "project", p.id, actor_id=user.id, project_id=p.id,
                 details={"working_crs": epsg})
    db.commit()
    return p


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return authorize_project(db, user, project_id)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: int, body: ProjectUpdate, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id, "reviewer")
    changes = body.model_dump(exclude_none=True)
    for k, v in changes.items():
        setattr(p, k, v)
    audit.record(db, "project.updated", "project", p.id, actor_id=user.id, project_id=p.id,
                 details={"fields": sorted(changes)})
    db.commit()
    return p


@router.get("/{project_id}/members")
def list_members(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    rows = db.execute(select(ProjectMember, User).join(User, User.id == ProjectMember.user_id).where(
        ProjectMember.project_id == project_id)).all()
    return [{"user_id": u.id, "email": u.email, "full_name": u.full_name, "global_role": u.role, "project_role": m.role}
            for m, u in rows]


@router.put("/{project_id}/members")
def set_member(project_id: int, body: MemberIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id)
    if p.owner_id != user.id and user.role != "admin":
        raise ApiError(403, "forbidden", "Only the project owner or an admin can manage members")
    if db.get(User, body.user_id) is None:
        raise ApiError(404, "not_found", "User not found")
    m = db.get(ProjectMember, (project_id, body.user_id))
    if m is None:
        db.add(ProjectMember(project_id=project_id, user_id=body.user_id, role=body.role))
    else:
        m.role = body.role
    audit.record(db, "project.member_set", "project", project_id, actor_id=user.id, project_id=project_id,
                 details={"user_id": body.user_id, "role": body.role})
    db.commit()
    return {"ok": True}


@router.get("/{project_id}/study-areas")
def list_study_areas(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    rows = db.execute(text("SELECT id, name, description, is_active, ST_Area(geom::geography) AS area_m2, "
                           "ST_AsGeoJSON(geom, 7) AS geojson FROM study_areas WHERE project_id=:p ORDER BY id"),
                      {"p": project_id}).mappings().all()
    return feature_collection(rows)


@router.post("/{project_id}/study-areas", status_code=201)
def add_study_area(project_id: int, body: StudyAreaIn, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    gj = _validate_area(db, body.geojson)
    db.execute(text("UPDATE study_areas SET is_active=false WHERE project_id=:p"), {"p": project_id})
    sa = StudyArea(project_id=project_id, name=body.name, description=body.description, is_active=True)
    db.add(sa)
    db.flush()
    db.execute(text("UPDATE study_areas SET geom=ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:g),4326)) WHERE id=:i"),
               {"g": gj, "i": sa.id})
    audit.record(db, "study_area.created", "study_area", sa.id, actor_id=user.id, project_id=project_id)
    db.commit()
    return {"id": sa.id}


@router.get("/{project_id}/suggest-crs")
def suggest_crs(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    c = db.execute(text("SELECT ST_X(ST_Centroid(geom)), ST_Y(ST_Centroid(geom)) FROM study_areas WHERE project_id=:p "
                        "AND is_active LIMIT 1"), {"p": project_id}).first()
    if c is None:
        raise ApiError(404, "not_found", "No active study area")
    return {"epsg": suggest_utm_epsg(c[0], c[1]), "method": "WGS84 UTM zone of study-area centroid"}


@router.get("/{project_id}/canonical-fields")
def canonical_fields(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    aliases = db.execute(select(FieldAlias).where(FieldAlias.project_id == project_id)).scalars().all()
    return {"fields": {k: {"type": v["type"], "role": v["role"], "aliases": v["aliases"]} for k, v in
                       CANONICAL_FIELDS.items()},
            "project_aliases": [{"id": a.id, "alias": a.alias, "target_field": a.target_field} for a in aliases]}


@router.post("/{project_id}/aliases", status_code=201)
def add_alias(project_id: int, body: AliasIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    if body.target_field not in CANONICAL_FIELDS:
        raise ApiError(422, "unknown_field", f"Unknown canonical field '{body.target_field}'")
    a = FieldAlias(project_id=project_id, alias=body.alias.strip().lower(), target_field=body.target_field)
    db.add(a)
    audit.record(db, "alias.created", "field_alias", None, actor_id=user.id, project_id=project_id,
                 details={"alias": a.alias, "target": a.target_field})
    db.commit()
    return {"id": a.id}


@router.get("/{project_id}/metrics", summary="Project metrics computed from live database queries")
def metrics(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    q = lambda sql: db.execute(text(sql), {"p": project_id}).mappings().all()  # noqa: E731
    ds = q("SELECT d.kind, d.provenance, count(DISTINCT d.id) AS datasets, count(v.id) AS versions, "
           "coalesce(sum(v.feature_count),0) AS features FROM datasets d LEFT JOIN dataset_versions v ON "
           "v.dataset_id=d.id WHERE d.project_id=:p AND d.status='active' GROUP BY d.kind, d.provenance")
    conf = q("SELECT category, status, count(*) AS n FROM conflicts WHERE project_id=:p GROUP BY category, status")
    jobs = q("SELECT status, count(*) AS n FROM processing_jobs WHERE project_id=:p GROUP BY status")
    integ = q("SELECT feature_class, status, count(*) AS n FROM integrated_features WHERE project_id=:p "
              "GROUP BY feature_class, status")
    runs = q("SELECT r.match_type, count(DISTINCT r.id) AS runs, count(c.id) AS candidates, "
             "count(c.id) FILTER (WHERE c.match_class='match') AS matches FROM match_runs r LEFT JOIN match_candidates c "
             "ON c.run_id=r.id WHERE r.project_id=:p GROUP BY r.match_type")
    decisions = q("SELECT subject_type, decision, count(*) AS n FROM review_decisions WHERE project_id=:p "
                  "GROUP BY subject_type, decision")
    return {"datasets": [dict(r) for r in ds], "conflicts": [dict(r) for r in conf], "jobs": [dict(r) for r in jobs],
            "integrated": [dict(r) for r in integ], "match_runs": [dict(r) for r in runs],
            "review_decisions": [dict(r) for r in decisions]}
