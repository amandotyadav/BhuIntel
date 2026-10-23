"""Matching, topology, change detection, ML, vision, integration, export, acquisition and job endpoints."""

import csv
import io

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.api.common import feature_collection, get_version, parse_bbox
from app.db import get_db
from app.errors import ApiError
from app.jobs import enqueue
from app.models import (
    AcquisitionManifest,
    MatchCandidate,
    MatchRun,
    ModelVersion,
    ProcessingJob,
    SourceFeature,
    User,
)
from app.schemas import (
    AcquireIn,
    ChangeIn,
    DecisionIn,
    ExportIn,
    ExtractIn,
    IntegrationIn,
    JobOut,
    LabelIn,
    MatchRunCreate,
    MatchRunOut,
    TopologyIn,
    TrainIn,
)
from app.security import authorize_project, get_current_user
from app.services import audit, ml, review
from app.services.acquisition.sources import SOURCES
from app.services.matching import FEATURE_SCHEMA
from app.storage import get_storage

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["analysis"])

VALID_KINDS = {
    "building_building": ({"buildings", "predicted_buildings"}, {"buildings", "predicted_buildings"}),
    "parcel_parcel": ({"parcels"}, {"parcels"}),
    "building_parcel": ({"buildings", "predicted_buildings"}, {"parcels"}),
    "record_feature": ({"property_records"}, {"parcels"}),
}


# ---------------- Match runs ----------------

@router.post("/match-runs", response_model=MatchRunOut, status_code=202)
def create_match_run(project_id: int, body: MatchRunCreate, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    lv, ld = get_version(db, project_id, body.left_version_id)
    rv, rd = get_version(db, project_id, body.right_version_id)
    lk, rk = VALID_KINDS[body.match_type]
    if ld.kind not in lk or rd.kind not in rk:
        raise ApiError(422, "kind_mismatch", f"{body.match_type} expects left {sorted(lk)} and right {sorted(rk)}; got "
                                             f"{ld.kind} / {rd.kind}")
    if lv.id == rv.id:
        raise ApiError(422, "same_version", "Choose two different dataset versions")
    for v in (lv, rv):
        if v.status != "processed":
            raise ApiError(409, "not_processed", f"Version {v.id} is '{v.status}'; process it first")
    if body.method == "random_forest" and ml.active_model(db, project_id, body.match_type) is None:
        raise ApiError(409, "no_active_model", "No trained and activated model for this match type")
    params = body.model_dump(include={"tolerance_m", "weights", "thresholds", "min_building_share"}, exclude_none=True)
    run = MatchRun(project_id=project_id, left_version_id=lv.id, right_version_id=rv.id, match_type=body.match_type,
                   method=body.method, params=params, status="queued", created_by=user.id)
    db.add(run)
    db.flush()
    enqueue(db, "match", {"run_id": run.id}, project_id, user.id)
    db.commit()
    return run


@router.get("/match-runs", response_model=list[MatchRunOut])
def list_runs(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    return db.execute(select(MatchRun).where(MatchRun.project_id == project_id).order_by(MatchRun.id.desc())).scalars().all()


@router.get("/match-runs/{run_id}", response_model=MatchRunOut)
def get_run(project_id: int, run_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    r = db.get(MatchRun, run_id)
    if r is None or r.project_id != project_id:
        raise ApiError(404, "not_found", "Run not found")
    return r


@router.get("/match-runs/{run_id}/candidates")
def list_candidates(project_id: int, run_id: int, match_class: str | None = None, status: str | None = None,
                    min_score: float = 0.0, limit: int = 200, offset: int = 0, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    run = db.get(MatchRun, run_id)
    if run is None or run.project_id != project_id:
        raise ApiError(404, "not_found", "Run not found")
    where = ["c.run_id=:r", "c.score >= :ms"]
    params = {"r": run_id, "ms": min_score, "limit": max(1, min(limit, 1000)), "offset": max(0, offset)}
    if match_class:
        where.append("c.match_class = :mc")
        params["mc"] = match_class
    if status:
        where.append("c.status = :st")
        params["st"] = status
    w = " AND ".join(where)
    rows = db.execute(text(
        f"""SELECT c.id, c.left_feature_id, c.right_feature_id, c.features, c.method, c.score, c.score_kind,
                   c.match_class, c.relationship, c.explanation, c.status, c.label, c.label_source,
                   l.source_id_raw AS left_id, r.source_id_raw AS right_id
            FROM match_candidates c JOIN source_features l ON l.id=c.left_feature_id
            JOIN source_features r ON r.id=c.right_feature_id WHERE {w}
            ORDER BY c.score DESC, c.id LIMIT :limit OFFSET :offset"""), params).mappings().all()
    total = db.execute(text(f"SELECT count(*) FROM match_candidates c WHERE {w}"), params).scalar()
    return {"total": total, "items": [dict(r) for r in rows]}


@router.post("/match-candidates/{cand_id}/decision")
def decide_candidate(project_id: int, cand_id: int, body: DecisionIn, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    m = db.get(MatchCandidate, cand_id)
    if m is None or db.get(MatchRun, m.run_id).project_id != project_id:
        raise ApiError(404, "not_found", "Candidate not found")
    res = review.decide_match(db, m, project_id, user, body.decision, body.comment)
    db.commit()
    return res


@router.put("/match-candidates/{cand_id}/label", summary="Set a training label without changing review status")
def label_candidate(project_id: int, cand_id: int, body: LabelIn, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    m = db.get(MatchCandidate, cand_id)
    if m is None or db.get(MatchRun, m.run_id).project_id != project_id:
        raise ApiError(404, "not_found", "Candidate not found")
    m.label, m.label_source = body.label, "reviewer"
    audit.record(db, "match.labelled", "match", m.id, actor_id=user.id, project_id=project_id,
                 details={"label": body.label})
    db.commit()
    return {"id": m.id, "label": m.label}


# ---------------- Topology / change detection ----------------

@router.post("/topology", response_model=JobOut, status_code=202)
def topology(project_id: int, body: TopologyIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    v, _ = get_version(db, project_id, body.version_id)
    if v.status != "processed":
        raise ApiError(409, "not_processed", "Process the version first")
    job = enqueue(db, "topology", body.model_dump(), project_id, user.id)
    db.commit()
    return job


@router.post("/changes", response_model=JobOut, status_code=202)
def run_changes(project_id: int, body: ChangeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    o, od = get_version(db, project_id, body.old_version_id)
    n, nd = get_version(db, project_id, body.new_version_id)
    if od.id != nd.id:
        raise ApiError(422, "different_datasets", "Compare two versions of the same dataset")
    if o.status != "processed" or n.status != "processed":
        raise ApiError(409, "not_processed", "Both versions must be processed")
    job = enqueue(db, "change_detection", body.model_dump(), project_id, user.id)
    db.commit()
    return job


@router.get("/changes")
def list_changes(project_id: int, old_version_id: int | None = None, new_version_id: int | None = None,
                 change_type: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    where, params = ["project_id=:p"], {"p": project_id}
    for col, val in (("old_version_id", old_version_id), ("new_version_id", new_version_id),
                     ("change_type", change_type)):
        if val is not None:
            where.append(f"{col} = :{col}")
            params[col] = val
    rows = db.execute(text(f"SELECT id, old_version_id, new_version_id, change_type, old_feature_id, new_feature_id, "
                           f"metrics, explanation, ST_AsGeoJSON(geom, 7) AS geojson FROM change_records WHERE "
                           f"{' AND '.join(where)} ORDER BY id LIMIT 5000"), params).mappings().all()
    fc = feature_collection(rows)
    fc["summary"] = {r["change_type"]: r["n"] for r in db.execute(text(
        f"SELECT change_type, count(*) AS n FROM change_records WHERE {' AND '.join(where)} GROUP BY change_type"),
        params).mappings()}
    return fc


# ---------------- ML ----------------

@router.get("/ml/schema")
def ml_schema(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    counts = {mt: dict(db.execute(text(
        "SELECT count(*) FILTER (WHERE c.label=1) AS match, count(*) FILTER (WHERE c.label=0) AS non_match "
        "FROM match_candidates c JOIN match_runs r ON r.id=c.run_id WHERE r.project_id=:p AND r.match_type=:t"),
        {"p": project_id, "t": mt}).mappings().one()) for mt in FEATURE_SCHEMA}
    return {"feature_schema": FEATURE_SCHEMA, "label_values": {"1": "same real-world feature", "0": "different"},
            "min_labels": ml.MIN_LABELS, "min_per_class": ml.MIN_PER_CLASS, "labelled_counts": counts,
            "label_import_csv_columns": ["candidate_id", "label"]}


@router.post("/ml/train", response_model=JobOut, status_code=202)
def train(project_id: int, body: TrainIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    job = enqueue(db, "train_model", body.model_dump(), project_id, user.id)
    db.commit()
    return job


@router.get("/ml/models")
def list_models(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    rows = db.execute(select(ModelVersion).where(ModelVersion.project_id == project_id)
                      .order_by(ModelVersion.id.desc())).scalars().all()
    return [{"id": m.id, "name": m.name, "version": m.version, "algorithm": m.algorithm, "match_type": m.match_type,
             "status": m.status, "is_active": m.is_active, "metrics": m.metrics, "training_info": m.training_info,
             "feature_schema": m.feature_schema, "artifact_sha256": m.artifact_sha256,
             "created_at": m.created_at.isoformat()} for m in rows]


@router.post("/ml/models/{model_id}/activate")
def activate(project_id: int, model_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "admin")
    m = db.get(ModelVersion, model_id)
    if m is None or m.project_id != project_id:
        raise ApiError(404, "not_found", "Model not found")
    try:
        ml.set_active(db, m)
    except ValueError as exc:
        raise ApiError(409, "not_trained", str(exc)) from None
    audit.record(db, "model.activated", "model_version", m.id, actor_id=user.id, project_id=project_id)
    db.commit()
    return {"activated": m.id}


@router.get("/ml/labels.csv", summary="Export labelled examples (training-data schema)")
def export_labels(project_id: int, match_type: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    if match_type not in FEATURE_SCHEMA:
        raise ApiError(422, "invalid_match_type", f"match_type must be one of {sorted(FEATURE_SCHEMA)}")
    ex = ml.labelled_examples(db, project_id, match_type)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["candidate_id", "group", "label", "label_source", *FEATURE_SCHEMA[match_type]])
    for e in ex:
        w.writerow([e["id"], e["group"], e["label"], e["label_source"],
                    *[e["features"].get(f) for f in FEATURE_SCHEMA[match_type]]])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=labels_{match_type}.csv"})


@router.post("/ml/labels/import", summary="Import labels: CSV with columns candidate_id,label")
def import_labels(project_id: int, file: UploadFile = File(...), db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    raw = file.file.read(5_000_001)
    if len(raw) > 5_000_000:
        raise ApiError(413, "too_large", "Label file limited to 5 MB")
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    except UnicodeDecodeError:
        raise ApiError(422, "bad_encoding", "Label CSV must be UTF-8") from None
    errors, n = [], 0
    for i, r in enumerate(rows, start=2):
        try:
            cid, lab = int(r["candidate_id"]), int(r["label"])
            if lab not in (0, 1):
                raise ValueError("label must be 0 or 1")
        except (KeyError, ValueError, TypeError) as exc:
            errors.append({"line": i, "error": str(exc)})
            continue
        m = db.get(MatchCandidate, cid)
        if m is None or db.get(MatchRun, m.run_id).project_id != project_id:
            errors.append({"line": i, "error": f"candidate {cid} not in project"})
            continue
        m.label, m.label_source = lab, "imported"
        n += 1
    audit.record(db, "labels.imported", "project", project_id, actor_id=user.id, project_id=project_id,
                 details={"imported": n, "errors": len(errors)})
    db.commit()
    return {"imported": n, "errors": errors[:100]}


# ---------------- Vision ----------------

@router.post("/vision/extract", response_model=JobOut, status_code=202)
def extract(project_id: int, body: ExtractIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    v, d = get_version(db, project_id, body.raster_version_id)
    if d.kind != "imagery" or v.status != "processed":
        raise ApiError(422, "not_imagery", "Choose a processed imagery version")
    if body.backend == "onnx" and not (body.model_path and body.model_meta_path):
        raise ApiError(422, "model_required", "ONNX backend needs model_path and model_meta_path (server paths)")
    job = enqueue(db, "extract_buildings", body.model_dump(), project_id, user.id)
    db.commit()
    return job


@router.get("/vision/predictions")
def predictions(project_id: int, review_status: str | None = None, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    where = ("f.review_status IS NOT NULL AND f.dataset_version_id IN (SELECT v.id FROM dataset_versions v JOIN "
             "datasets d ON d.id=v.dataset_id WHERE d.project_id=:p AND d.kind='predicted_buildings')")
    params: dict = {"p": project_id}
    if review_status:
        where += " AND f.review_status=:rs"
        params["rs"] = review_status
    rows = db.execute(text(f"SELECT f.id, f.source_id_raw, f.area_m2, f.review_status, f.dataset_version_id, "
                           f"f.attributes, ST_AsGeoJSON(f.geom, 7) AS geojson FROM source_features f WHERE {where} "
                           f"ORDER BY f.id LIMIT 5000"), params).mappings().all()
    return feature_collection(rows)


@router.post("/vision/predictions/{feature_id}/decision")
def decide_prediction(project_id: int, feature_id: int, body: DecisionIn, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    f = db.get(SourceFeature, feature_id)
    if f is None:
        raise ApiError(404, "not_found", "Prediction not found")
    get_version(db, project_id, f.dataset_version_id)
    res = review.decide_prediction(db, f, project_id, user, body.decision, body.comment)
    db.commit()
    return res


# ---------------- Integration & export ----------------

@router.post("/integration/build", response_model=JobOut, status_code=202)
def build(project_id: int, body: IntegrationIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "reviewer")
    get_version(db, project_id, body.primary_parcel_version_id)
    for rid in (body.parcel_match_run_id, body.record_match_run_id, body.building_match_run_id,
                body.building_parcel_run_id):
        if rid is not None:
            r = db.get(MatchRun, rid)
            if r is None or r.project_id != project_id:
                raise ApiError(404, "not_found", f"Match run {rid} not found")
    job = enqueue(db, "build_integration", body.model_dump(), project_id, user.id)
    db.commit()
    return job


@router.get("/integrated")
def integrated(project_id: int, feature_class: str | None = None, bbox: str | None = None, status: str = "active",
               limit: int = 2000, offset: int = 0, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    where, params = ["project_id=:p", "status=:s"], {"p": project_id, "s": status}
    if feature_class:
        where.append("feature_class=:c")
        params["c"] = feature_class
    b = parse_bbox(bbox)
    if b:
        where.append("geom && ST_MakeEnvelope(:x0,:y0,:x1,:y1,4326)")
        params.update({"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
    w = " AND ".join(where)
    rows = db.execute(text(f"SELECT id, feature_class, integrated_key, attributes, lineage, confidence, status, version, "
                           f"source_feature_ids, updated_at, ST_AsGeoJSON(geom, 7) AS geojson FROM integrated_features "
                           f"WHERE {w} ORDER BY id LIMIT :l OFFSET :o"),
                      {**params, "l": max(1, min(limit, 5000)), "o": max(0, offset)}).mappings().all()
    fc = feature_collection(rows)
    fc["numberMatched"] = db.execute(text(f"SELECT count(*) FROM integrated_features WHERE {w}"), params).scalar()
    return fc


@router.get("/integrated/{feature_id}/history")
def integrated_history(project_id: int, feature_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    rows = db.execute(text("SELECT h.id, h.version, h.attributes, h.status, h.change_reason, h.correction_id, "
                           "h.changed_by, h.changed_at, ST_AsGeoJSON(h.geom, 7) AS geojson FROM "
                           "integrated_feature_history h JOIN integrated_features f ON f.id=h.integrated_feature_id "
                           "WHERE f.id=:i AND f.project_id=:p ORDER BY h.version"),
                      {"i": feature_id, "p": project_id}).mappings().all()
    return feature_collection(rows)


@router.post("/exports", response_model=JobOut, status_code=202)
def export(project_id: int, body: ExportIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    job = enqueue(db, "export", body.model_dump(), project_id, user.id)
    db.commit()
    return job


@router.get("/exports/{job_id}/download")
def download_export(project_id: int, job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    job = db.get(ProcessingJob, job_id)
    if job is None or job.project_id != project_id or job.job_type != "export" or job.status != "succeeded":
        raise ApiError(404, "not_found", "Completed export not found")
    path = get_storage().local_path(job.result["storage_key"])
    audit.record(db, "export.downloaded", "job", job.id, actor_id=user.id, project_id=project_id)
    db.commit()
    media = "application/geopackage+sqlite3" if job.result["format"] == "gpkg" else "application/geo+json"
    return FileResponse(path, filename=job.result["filename"], media_type=media)


# ---------------- Acquisition ----------------

@router.get("/acquisition/sources")
def acquisition_sources(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    return [{"key": k, **{f: v.get(f) for f in ("name", "provider", "kind", "access", "automated", "license",
                                                "attribution", "homepage", "notes", "format")}}
            for k, v in SOURCES.items()]


@router.post("/acquisition", response_model=JobOut, status_code=202)
def acquire(project_id: int, body: AcquireIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    if body.source_key not in SOURCES:
        raise ApiError(404, "unknown_source", f"Unknown source; choose from {sorted(SOURCES)}")
    job = enqueue(db, "acquire", body.model_dump(exclude_none=True), project_id, user.id)
    db.commit()
    return job


@router.get("/acquisition/manifests")
def manifests(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    rows = db.execute(select(AcquisitionManifest).where(AcquisitionManifest.project_id == project_id)
                      .order_by(AcquisitionManifest.id.desc())).scalars().all()
    return [{"id": m.id, "source_key": m.source_key, "status": m.status, "reachable": m.reachable, "message": m.message,
             "files": m.files, "source_meta": m.source_meta, "request": m.request,
             "dataset_version_id": m.dataset_version_id, "created_at": m.created_at.isoformat()} for m in rows]


# ---------------- Jobs ----------------

@router.get("/jobs", response_model=list[JobOut])
def list_jobs(project_id: int, status: str | None = None, limit: int = 100, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    q = select(ProcessingJob).where(ProcessingJob.project_id == project_id)
    if status:
        q = q.where(ProcessingJob.status == status)
    return db.execute(q.order_by(ProcessingJob.id.desc()).limit(max(1, min(limit, 500)))).scalars().all()


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(project_id: int, job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    job = db.get(ProcessingJob, job_id)
    if job is None or job.project_id != project_id:
        raise ApiError(404, "not_found", "Job not found")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(project_id: int, job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    job = db.get(ProcessingJob, job_id)
    if job is None or job.project_id != project_id:
        raise ApiError(404, "not_found", "Job not found")
    if job.status != "queued":
        raise ApiError(409, "not_cancellable", "Only queued jobs can be cancelled")
    job.status, job.message = "cancelled", f"cancelled by user {user.id}"
    audit.record(db, "job.cancelled", "job", job.id, actor_id=user.id, project_id=project_id)
    db.commit()
    return job


@router.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=202)
def retry_job(project_id: int, job_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id, "analyst")
    job = db.get(ProcessingJob, job_id)
    if job is None or job.project_id != project_id:
        raise ApiError(404, "not_found", "Job not found")
    if job.status not in ("failed", "partial", "cancelled"):
        raise ApiError(409, "not_retryable", "Only failed, partial or cancelled jobs can be retried")
    if job.job_type == "process_version":
        from app.models import DatasetVersion

        v = db.get(DatasetVersion, job.params["version_id"])
        v.status = "queued"
    if job.job_type == "match":
        db.get(MatchRun, job.params["run_id"]).status = "queued"
    new = enqueue(db, job.job_type, job.params, project_id, user.id)
    db.commit()
    return new
