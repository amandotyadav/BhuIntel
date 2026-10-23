import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.api.common import features_geojson, get_dataset, get_version, parse_bbox, redact
from app.config import get_settings
from app.db import get_db
from app.errors import ApiError
from app.jobs import enqueue
from app.models import Dataset, DatasetVersion, FieldAlias, FieldMapping, User, utcnow
from app.schemas import (
    CrsConfirm,
    CsvOptions,
    DatasetCreate,
    DatasetOut,
    DatasetUpdate,
    JobOut,
    MappingDecision,
    MappingOut,
    ProcessIn,
    VersionOut,
)
from app.security import authorize_project, effective_project_role, get_current_user
from app.services import audit
from app.services.crs import CrsError
from app.services.etl import confirm_crs, ensure_processable
from app.services.ingest import IngestError, detect_format, inspect_file, inspection_to_dict
from app.services.schema_mapping import CANONICAL_FIELDS, suggest_mappings
from app.storage import UploadTooLarge, get_storage

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["datasets"])


@router.get("/datasets", response_model=list[DatasetOut])
def list_datasets(project_id: int, include_archived: bool = False, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    q = select(Dataset).where(Dataset.project_id == project_id).order_by(Dataset.id)
    if not include_archived:
        q = q.where(Dataset.status == "active")
    return db.execute(q).scalars().all()


@router.post("/datasets", response_model=DatasetOut, status_code=201)
def create_dataset(project_id: int, body: DatasetCreate, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    d = Dataset(project_id=project_id, created_by=user.id, **body.model_dump())
    db.add(d)
    db.flush()
    audit.record(db, "dataset.created", "dataset", d.id, actor_id=user.id, project_id=project_id,
                 details={"kind": d.kind, "provenance": d.provenance, "license": d.license})
    db.commit()
    return d


@router.get("/datasets/{dataset_id}", response_model=DatasetOut)
def get_dataset_ep(project_id: int, dataset_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    return get_dataset(db, project_id, dataset_id)


@router.patch("/datasets/{dataset_id}", response_model=DatasetOut)
def update_dataset(project_id: int, dataset_id: int, body: DatasetUpdate, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    d = get_dataset(db, project_id, dataset_id)
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(d, k, v)
    audit.record(db, "dataset.updated", "dataset", d.id, actor_id=user.id, project_id=project_id,
                 details={"fields": sorted(changes)})
    db.commit()
    return d


def _dependencies(db: Session, dataset_id: int) -> dict:
    return dict(db.execute(text(
        """SELECT
             (SELECT count(*) FROM match_runs r JOIN dataset_versions v ON v.id IN (r.left_version_id, r.right_version_id)
              WHERE v.dataset_id=:d) AS match_runs,
             (SELECT count(*) FROM integrated_features i WHERE i.status='active' AND i.source_feature_ids &&
                (SELECT coalesce(array_agg(f.id), '{}') FROM source_features f JOIN dataset_versions v
                 ON v.id=f.dataset_version_id WHERE v.dataset_id=:d)) AS integrated_features,
             (SELECT count(*) FROM corrections c JOIN source_features f ON f.id=c.source_feature_id
                JOIN dataset_versions v ON v.id=f.dataset_version_id WHERE v.dataset_id=:d
                AND c.status IN ('approved','applied')) AS approved_corrections,
             (SELECT count(*) FROM conflicts c WHERE c.status <> 'pending' AND c.dataset_version_ids &&
                (SELECT coalesce(array_agg(v.id), '{}') FROM dataset_versions v WHERE v.dataset_id=:d))
                AS reviewed_conflicts"""), {"d": dataset_id}).mappings().one())


@router.delete("/datasets/{dataset_id}")
def delete_dataset(project_id: int, dataset_id: int, mode: str = "archive", db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    """mode=archive (default, always allowed) or mode=delete (only when nothing depends on the dataset)."""
    authorize_project(db, user, project_id, "reviewer")
    d = get_dataset(db, project_id, dataset_id)
    deps = _dependencies(db, dataset_id)
    if mode == "delete":
        blocking = {k: v for k, v in deps.items() if v}
        if blocking:
            raise ApiError(409, "has_dependencies", "Dataset has dependent results; archive it instead.", blocking)
        keys = [v.storage_key for v in d.versions]
        db.execute(text("DELETE FROM conflicts WHERE status='pending' AND dataset_version_ids && CAST(:v AS int[])"),
                   {"v": [v.id for v in d.versions]})
        audit.record(db, "dataset.deleted", "dataset", d.id, actor_id=user.id, project_id=project_id,
                     details={"name": d.name})
        db.delete(d)
        db.commit()
        for k in keys:
            get_storage().delete(k)
        return {"deleted": True}
    if mode != "archive":
        raise ApiError(422, "invalid_mode", "mode must be archive or delete")
    d.status = "archived"
    audit.record(db, "dataset.archived", "dataset", d.id, actor_id=user.id, project_id=project_id, details=deps)
    db.commit()
    return {"archived": True, "dependencies": deps}


@router.get("/datasets/{dataset_id}/versions", response_model=list[VersionOut])
def list_versions(project_id: int, dataset_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    get_dataset(db, project_id, dataset_id)
    return db.execute(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id)
                      .order_by(DatasetVersion.version_no)).scalars().all()


@router.post("/datasets/{dataset_id}/versions", status_code=201,
             summary="Upload a file as a new dataset version (idempotent on file content)")
def upload_version(project_id: int, dataset_id: int, file: UploadFile = File(...), layer: str | None = Form(None),
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    d = get_dataset(db, project_id, dataset_id)
    if d.status != "active":
        raise ApiError(409, "archived", "Dataset is archived")
    filename = file.filename or "upload"
    try:
        fmt = detect_format(filename)
    except IngestError as exc:
        raise ApiError(415, exc.code, str(exc)) from None
    st = get_storage()
    try:
        key, sha, size = st.put_stream(f"projects/{project_id}/datasets/{dataset_id}", filename, file.file,
                                       get_settings().max_upload_mb * 1024 * 1024)
    except UploadTooLarge:
        raise ApiError(413, "too_large", f"File exceeds the {get_settings().max_upload_mb} MB upload limit") from None
    if size == 0:
        st.delete(key)
        raise ApiError(422, "empty_file", "Uploaded file is empty")
    existing = db.execute(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset_id,
                                                       DatasetVersion.file_sha256 == sha)).scalar_one_or_none()
    if existing is not None:
        st.delete(key)
        return {"version": VersionOut.model_validate(existing).model_dump(mode="json", by_alias=True), "duplicate": True,
                "message": f"Identical file already uploaded as version {existing.version_no}; no new version created."}
    try:
        with tempfile.TemporaryDirectory(prefix="bhu_inspect_") as tmp:
            ins = inspect_file(st.local_path(key), fmt, Path(tmp), layer=layer)
    except IngestError as exc:
        st.delete(key)
        raise ApiError(422, exc.code, str(exc)) from None
    if d.kind == "imagery" and ins.kind != "raster":
        st.delete(key)
        raise ApiError(422, "kind_mismatch", "Imagery datasets accept GeoTIFF/COG rasters only")
    if d.kind != "imagery" and ins.kind == "raster":
        st.delete(key)
        raise ApiError(422, "kind_mismatch", "Rasters must be uploaded to an 'imagery' dataset")
    next_no = (db.execute(select(func.max(DatasetVersion.version_no)).where(
        DatasetVersion.dataset_id == dataset_id)).scalar() or 0) + 1
    needs_crs = ins.kind != "table" and ins.crs is None
    v = DatasetVersion(dataset_id=dataset_id, version_no=next_no, file_sha256=sha, original_filename=filename[:500],
                       storage_key=key, format=fmt, size_bytes=size, layer=ins.layer, source_crs=ins.crs,
                       geometry_type=ins.geometry_type if ins.kind != "raster" else None,
                       feature_count=ins.feature_count, schema=ins.fields, field_profile=ins.field_profile,
                       csv_options=ins.csv_options, status="needs_crs" if needs_crs else "inspected",
                       created_by=user.id,
                       stats={"inspection": {k: v for k, v in inspection_to_dict(ins).items()
                                             if k not in ("field_profile", "fields", "data_path")}},
                       processing_log=[{"ts": utcnow().isoformat(), "step": "uploaded_and_inspected",
                                        "format": ins.format, "warnings": ins.warnings}])
    db.add(v)
    db.flush()
    if ins.extent_4326:
        x0, y0, x1, y1 = ins.extent_4326
        db.execute(text("UPDATE dataset_versions SET extent=ST_MakeEnvelope(:a,:b,:c,:d,4326) WHERE id=:i"),
                   {"a": x0, "b": y0, "c": x1, "d": y1, "i": v.id})
    if ins.field_profile:
        _store_suggestions(db, d, ins.field_profile)
    audit.record(db, "dataset_version.uploaded", "dataset_version", v.id, actor_id=user.id, project_id=project_id,
                 details={"sha256": sha, "size": size, "format": fmt, "crs": ins.crs})
    db.commit()
    db.refresh(v)
    return {"version": VersionOut.model_validate(v).model_dump(mode="json", by_alias=True), "duplicate": False,
            "inspection": {"warnings": ins.warnings, "crs_name": ins.crs_name, "crs_is_projected": ins.crs_is_projected,
                           "crs_suggestion": ins.crs_suggestion, "layers": ins.layers, "extent_4326": ins.extent_4326,
                           "raster": ins.raster}}


def _store_suggestions(db: Session, d: Dataset, profile: dict) -> None:
    aliases = {a.alias: a.target_field for a in db.execute(select(FieldAlias).where(
        FieldAlias.project_id == d.project_id)).scalars()}
    history = {m.source_field: m.target_field for m in db.execute(select(FieldMapping).where(
        FieldMapping.project_id == d.project_id, FieldMapping.status == "approved",
        FieldMapping.dataset_id != d.id, FieldMapping.decided_by.is_not(None))).scalars()}
    for s in suggest_mappings(profile, aliases, history):
        m = db.execute(select(FieldMapping).where(FieldMapping.dataset_id == d.id,
                                                  FieldMapping.source_field == s.source_field)).scalar_one_or_none()
        if m is not None and m.status in ("approved", "rejected") and m.decided_by is not None:
            continue  # never overwrite a human decision
        if m is None:
            m = FieldMapping(project_id=d.project_id, dataset_id=d.id, source_field=s.source_field)
            db.add(m)
        m.target_field, m.method, m.score, m.evidence, m.ambiguous = (s.target_field, s.method, s.score, s.evidence,
                                                                      s.ambiguous)
        # Deterministic exact-alias rules are auto-approved (documented); everything else needs a human.
        auto = s.target_field is not None and s.method in ("alias", "project_alias") and not s.ambiguous
        m.status = "approved" if auto else "suggested"
        m.decided_by, m.decided_at = None, (utcnow() if auto else None)
    db.flush()


@router.get("/versions/{version_id}", response_model=VersionOut)
def get_version_ep(project_id: int, version_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    v, _ = get_version(db, project_id, version_id)
    return v


@router.get("/versions/{version_id}/download", summary="Download the original uploaded file (unaltered)")
def download_original(project_id: int, version_id: int, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    v, _ = get_version(db, project_id, version_id)
    path = get_storage().local_path(v.storage_key)
    if not path.exists():
        raise ApiError(404, "not_found", "Stored file missing")
    return FileResponse(path, filename=v.original_filename)


@router.post("/versions/{version_id}/crs", response_model=VersionOut)
def confirm_crs_ep(project_id: int, version_id: int, body: CrsConfirm, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    v, _ = get_version(db, project_id, version_id)
    if v.status in ("processing", "queued"):
        raise ApiError(409, "busy", "Version is being processed")
    previous = v.source_crs
    try:
        ident = confirm_crs(v, body.crs)
    except CrsError as exc:
        raise ApiError(422, "invalid_crs", str(exc)) from None
    v.crs_confirmed_by = user.id
    v.processing_log = [*v.processing_log, {"ts": utcnow().isoformat(), "step": "crs_confirmed", "crs": ident,
                                            "previous": previous, "justification": body.justification,
                                            "by_user": user.id}]
    audit.record(db, "dataset_version.crs_confirmed", "dataset_version", v.id, actor_id=user.id,
                 project_id=project_id, details={"crs": ident, "previous": previous,
                                                 "justification": body.justification[:300]})
    db.commit()
    return v


@router.post("/versions/{version_id}/csv-options", response_model=VersionOut)
def csv_options(project_id: int, version_id: int, body: CsvOptions, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    v, _ = get_version(db, project_id, version_id)
    if v.format != "csv":
        raise ApiError(422, "not_csv", "CSV options apply to CSV uploads only")
    opts = body.model_dump(exclude_none=True)
    missing = [c for c in opts.values() if c not in v.schema and c not in (v.csv_options or {}).values()]
    if missing:
        raise ApiError(422, "unknown_column", f"Columns not in CSV: {missing}")
    v.csv_options = opts
    v.geometry_type = ("Point" if "x_column" in opts else "Unknown") if opts else None
    if opts and not v.crs_confirmed:
        v.status = "needs_crs"
    db.commit()
    return v


@router.post("/versions/{version_id}/process", response_model=JobOut, status_code=202)
def process(project_id: int, version_id: int, body: ProcessIn | None = None, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    v, _ = get_version(db, project_id, version_id)
    try:
        ensure_processable(v)
    except IngestError as exc:
        raise ApiError(409 if exc.code == "already_processing" else 422, exc.code, str(exc)) from None
    v.status = "queued"
    job = enqueue(db, "process_version", {"version_id": v.id, "study_area_filter": bool(body and
                                                                                       body.study_area_filter)},
                  project_id, user.id)
    db.commit()
    return job


@router.get("/versions/{version_id}/features", summary="Source features as GeoJSON (paged, bbox filter)")
def version_features(project_id: int, version_id: int, bbox: str | None = None, limit: int = 1000, offset: int = 0,
                     db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id)
    get_version(db, project_id, version_id)
    limit = max(1, min(limit, 5000))
    where, params = "f.dataset_version_id = :v", {"v": version_id}
    b = parse_bbox(bbox)
    if b:
        where += " AND f.geom && ST_MakeEnvelope(:x0,:y0,:x1,:y1,4326)"
        params.update({"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
    fc = features_geojson(db, where, params, limit, max(0, offset))
    role = effective_project_role(db, user, p)
    for f in fc["features"]:
        f["properties"]["attributes"] = redact(f["properties"].get("attributes"), role)
        f["properties"]["normalized_attributes"] = redact(f["properties"].get("normalized_attributes"), role)
    return fc


@router.get("/features/{feature_id}")
def get_feature(project_id: int, feature_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id)
    fc = features_geojson(db, "f.id = :i AND f.dataset_version_id IN (SELECT v.id FROM dataset_versions v JOIN datasets d "
                              "ON d.id=v.dataset_id WHERE d.project_id=:p)", {"i": feature_id, "p": project_id}, 1, 0)
    if not fc["features"]:
        raise ApiError(404, "not_found", "Feature not found")
    f = fc["features"][0]
    role = effective_project_role(db, user, p)
    f["properties"]["attributes"] = redact(f["properties"].get("attributes"), role)
    f["properties"]["normalized_attributes"] = redact(f["properties"].get("normalized_attributes"), role)
    src = db.execute(text("SELECT ST_AsText(geom_source) AS source_wkt, ST_SRID(geom_source) AS source_srid, "
                          "d.name AS dataset, d.provenance, d.license, v.version_no FROM source_features f JOIN "
                          "dataset_versions v ON v.id=f.dataset_version_id JOIN datasets d ON d.id=v.dataset_id "
                          "WHERE f.id=:i"), {"i": feature_id}).mappings().one()
    f["properties"]["source"] = dict(src)
    return f


# ---------------- Field mappings ----------------

@router.get("/datasets/{dataset_id}/mappings", response_model=list[MappingOut])
def list_mappings(project_id: int, dataset_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    get_dataset(db, project_id, dataset_id)
    return db.execute(select(FieldMapping).where(FieldMapping.dataset_id == dataset_id)
                      .order_by(FieldMapping.id)).scalars().all()


@router.post("/datasets/{dataset_id}/mappings/suggest", response_model=list[MappingOut])
def resuggest(project_id: int, dataset_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    d = get_dataset(db, project_id, dataset_id)
    v = db.execute(select(DatasetVersion).where(DatasetVersion.dataset_id == d.id)
                   .order_by(DatasetVersion.version_no.desc())).scalars().first()
    if v is None:
        raise ApiError(409, "no_versions", "Upload a file first")
    _store_suggestions(db, d, v.field_profile)
    db.commit()
    return db.execute(select(FieldMapping).where(FieldMapping.dataset_id == dataset_id)
                      .order_by(FieldMapping.id)).scalars().all()


@router.patch("/mappings/{mapping_id}", response_model=MappingOut)
def decide_mapping(project_id: int, mapping_id: int, body: MappingDecision, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    m = db.get(FieldMapping, mapping_id)
    if m is None or m.project_id != project_id:
        raise ApiError(404, "not_found", "Mapping not found")
    target = body.target_field if body.target_field is not None else m.target_field
    if body.status == "approved":
        if target is None or target not in CANONICAL_FIELDS:
            raise ApiError(422, "unknown_field", f"Choose a canonical target field: {sorted(CANONICAL_FIELDS)}")
        if m.ambiguous and not body.confirm_ambiguous and body.target_field is None:
            raise ApiError(422, "ambiguous_mapping", "This suggestion is ambiguous; set confirm_ambiguous=true or "
                                                     "choose target_field explicitly.", m.evidence)
        clash = db.execute(select(FieldMapping).where(FieldMapping.dataset_id == m.dataset_id, FieldMapping.id != m.id,
                                                      FieldMapping.status == "approved",
                                                      FieldMapping.target_field == target)).scalar_one_or_none()
        if clash:
            raise ApiError(409, "target_taken", f"'{clash.source_field}' is already mapped to '{target}'. Reject "
                                                "that mapping first (mappings never merge fields silently).")
    before = {"status": m.status, "target": m.target_field}
    m.status, m.target_field = body.status, target
    if body.target_field is not None and body.target_field != before["target"]:
        m.method = "manual"
    m.decided_by, m.decided_at = user.id, utcnow()
    audit.record(db, "mapping.decided", "field_mapping", m.id, actor_id=user.id, project_id=project_id,
                 details={"source_field": m.source_field, "before": before, "after": {"status": m.status,
                                                                                     "target": m.target_field}})
    db.commit()
    return m
