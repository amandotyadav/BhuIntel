"""Background processing jobs backed by PostgreSQL (no extra broker needed).

* Enqueue: rows in ``processing_jobs`` (optional idempotency key -> duplicate submissions return the
  existing job).
* Worker: ``python -m app.worker`` claims jobs with ``FOR UPDATE SKIP LOCKED`` (safe with several
  workers), heartbeats, enforces a timeout and marks jobs failed/partial with an error message. Transient
  database errors are retried up to ``max_attempts``; stale 'running' jobs (dead worker) are re-queued.
* Progress values are reported by the job code itself (never simulated).
"""

import logging
import tempfile
import time
import traceback
from datetime import timedelta
from pathlib import Path

import shapely
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    AcquisitionManifest,
    Dataset,
    DatasetVersion,
    MatchRun,
    ProcessingJob,
    Project,
    RasterAsset,
    utcnow,
)
from app.services import audit

log = logging.getLogger("bhuintel.jobs")


class JobTimeout(Exception):
    pass


class PartialFailure(Exception):
    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def enqueue(db: Session, job_type: str, params: dict, project_id: int | None, user_id: int | None,
            idempotency_key: str | None = None) -> ProcessingJob:
    if job_type not in HANDLERS:
        raise ValueError(f"Unknown job type {job_type}")
    if idempotency_key:
        existing = db.execute(select(ProcessingJob).where(ProcessingJob.idempotency_key == idempotency_key)).scalar_one_or_none()
        if existing is not None and existing.status in ("queued", "running", "succeeded"):
            return existing
        if existing is not None:  # failed/cancelled: allow a fresh attempt under the same key
            existing.idempotency_key = None
            db.flush()
    job = ProcessingJob(project_id=project_id, job_type=job_type, params=params, created_by=user_id,
                        idempotency_key=idempotency_key, status="queued", message="queued")
    db.add(job)
    db.flush()
    audit.record(db, "job.enqueued", "job", job.id, actor_id=user_id, project_id=project_id,
                 details={"job_type": job_type})
    return job


def claim_next(db: Session) -> ProcessingJob | None:
    row = db.execute(text(
        "SELECT id FROM processing_jobs WHERE status='queued' ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")).first()
    if row is None:
        return None
    job = db.get(ProcessingJob, row[0])
    job.status, job.started_at, job.heartbeat_at = "running", utcnow(), utcnow()
    job.attempts += 1
    db.commit()
    return job


def recover_stale(db: Session) -> int:
    cutoff = utcnow() - timedelta(seconds=get_settings().job_timeout_seconds + 300)
    n = 0
    for job in db.execute(select(ProcessingJob).where(ProcessingJob.status == "running",
                                                      ProcessingJob.heartbeat_at < cutoff)).scalars():
        if job.attempts < job.max_attempts:
            job.status, job.message = "queued", "re-queued after worker loss"
        else:
            job.status, job.error, job.finished_at = "failed", "worker lost; attempts exhausted", utcnow()
        n += 1
    db.commit()
    return n


def run_job(job_id: int) -> ProcessingJob:
    db = SessionLocal()
    try:
        job = db.get(ProcessingJob, job_id)
        deadline = time.monotonic() + get_settings().job_timeout_seconds

        def progress(frac: float, message: str = "") -> None:
            if time.monotonic() > deadline:
                raise JobTimeout(f"Job exceeded {get_settings().job_timeout_seconds}s timeout")
            with SessionLocal() as s2:  # separate session so progress is visible while the job transaction runs
                s2.execute(text("UPDATE processing_jobs SET progress=:p, message=:m, heartbeat_at=now() WHERE id=:i"),
                           {"p": max(0.0, min(1.0, frac)), "m": message[:500], "i": job_id})
                s2.commit()

        try:
            result = HANDLERS[job.job_type](db, job, progress)
            job = db.get(ProcessingJob, job_id)
            job.status, job.progress, job.result, job.message = "succeeded", 1.0, result or {}, "completed"
        except PartialFailure as pf:
            db.rollback()
            job = db.get(ProcessingJob, job_id)
            job.status, job.result, job.error = "partial", pf.result, str(pf)
        except OperationalError as exc:
            db.rollback()
            job = db.get(ProcessingJob, job_id)
            retry = job.attempts < job.max_attempts
            job.status = "queued" if retry else "failed"
            job.error = f"transient database error: {str(exc)[:300]}"
        except Exception as exc:  # job-level failure is recorded, never swallowed
            db.rollback()
            job = db.get(ProcessingJob, job_id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {str(exc)[:1000]}"
            log.warning("job failed", extra={"ctx": {"job_id": job_id, "type": job.job_type,
                                                     "trace": traceback.format_exc()[-2000:]}})
            _mark_targets_failed(db, job)
        job.finished_at = utcnow()
        audit.record(db, f"job.{job.status}", "job", job.id, actor_id=job.created_by, project_id=job.project_id,
                     details={"job_type": job.job_type, "error": (job.error or "")[:300]})
        db.commit()
        return job
    finally:
        db.close()


def _mark_targets_failed(db: Session, job: ProcessingJob) -> None:
    if job.job_type == "process_version":
        v = db.get(DatasetVersion, job.params.get("version_id"))
        if v is not None and v.status in ("queued", "processing"):
            v.status, v.error = "failed", job.error
    if job.job_type == "match":
        r = db.get(MatchRun, job.params.get("run_id"))
        if r is not None:
            r.status = "failed"


def run_pending(max_jobs: int = 1000) -> int:
    """Process queued jobs until the queue is empty (used by tests, inline mode and the CLI)."""
    n = 0
    while n < max_jobs:
        with SessionLocal() as db:
            job = claim_next(db)
        if job is None:
            return n
        run_job(job.id)
        n += 1
    return n


# ----------------------------------------------------------------------------------------------
# Handlers
# ----------------------------------------------------------------------------------------------

def h_process_version(db: Session, job: ProcessingJob, progress) -> dict:
    from app.services.conflicts import detect_dataset_issues
    from app.services.etl import process_version

    v = db.get(DatasetVersion, job.params["version_id"])
    v.status = "processing"
    db.commit()
    stats = process_version(db, v.id, progress, actor_id=job.created_by,
                            study_area_filter=bool(job.params.get("study_area_filter")))
    v = db.get(DatasetVersion, job.params["version_id"])
    if v.geometry_type is not None or v.format != "csv":
        issues = detect_dataset_issues(db, v.id)
        stats = {**stats, "dataset_issues": issues}
    db.commit()
    return stats


def h_topology(db: Session, job: ProcessingJob, progress) -> dict:
    from app.services.topology import run_topology

    progress(0.1, "running topology checks")
    res = run_topology(db, job.params["version_id"], job.params.get("params"))
    db.commit()
    return {"conflicts_by_category": res}


def h_match(db: Session, job: ProcessingJob, progress) -> dict:
    from app.services import ml
    from app.services.conflicts import detect_from_run
    from app.services.matching import run_matching

    run = db.get(MatchRun, job.params["run_id"])
    run.status = "running"
    db.commit()
    model = None
    if run.method == "random_forest":
        mv = ml.active_model(db, run.project_id, run.match_type)
        if mv is None:
            raise ValueError("No trained, active Random Forest model for this match type; use deterministic.")
        model = ml.load_model(db, mv)
        run.model_version_id = mv.id
    stats = run_matching(db, run, progress, model=model)
    progress(0.8, "detecting conflicts")
    stats["conflicts"] = detect_from_run(db, run, job.params.get("conflict_params"))
    run.stats = stats
    run.status = "succeeded"
    audit.record(db, "match_run.completed", "match_run", run.id, actor_id=job.created_by, project_id=run.project_id,
                 details={k: v for k, v in stats.items() if k in ("candidates", "by_class", "conflicts")})
    db.commit()
    return stats


def h_change_detection(db: Session, job: ProcessingJob, progress) -> dict:
    from app.services.change_detection import detect_image_changes, detect_vector_changes
    from app.storage import get_storage

    o, n = job.params["old_version_id"], job.params["new_version_id"]
    ro = db.execute(select(RasterAsset).where(RasterAsset.dataset_version_id == o)).scalar_one_or_none()
    rn = db.execute(select(RasterAsset).where(RasterAsset.dataset_version_id == n)).scalar_one_or_none()
    progress(0.1, "comparing versions")
    if ro and rn:
        st = get_storage()
        res = detect_image_changes(db, job.project_id, str(st.local_path(ro.storage_key)),
                                   str(st.local_path(rn.storage_key)), o, n, job.params.get("params"))
    else:
        res = detect_vector_changes(db, o, n, job.params.get("params"))
    db.commit()
    return res


def h_train_model(db: Session, job: ProcessingJob, progress) -> dict:
    from app.services import ml

    progress(0.1, "collecting labelled examples")
    mv = ml.train(db, job.project_id, job.params["match_type"], job.created_by, bool(job.params.get("activate")))
    audit.record(db, "model.trained" if mv.status == "trained" else "model.insufficient_data", "model_version", mv.id,
                 actor_id=job.created_by, project_id=job.project_id, details={"version": mv.version,
                                                                              "status": mv.status})
    db.commit()
    return {"model_version_id": mv.id, "status": mv.status, "metrics": mv.metrics, "training_info": mv.training_info}


def h_build_integration(db: Session, job: ProcessingJob, progress) -> dict:
    from app.services.integration import build_integrated

    progress(0.1, "building integrated layer")
    res = build_integrated(db, job.project_id, job.params, job.created_by)
    db.commit()
    return res


def h_export(db: Session, job: ProcessingJob, progress) -> dict:
    from app.services.export import export_integrated

    progress(0.1, "exporting")
    res = export_integrated(db, job.project_id, job.params.get("format", "gpkg"),
                            bool(job.params.get("include_retired")))
    audit.record(db, "export.created", "project", job.project_id, actor_id=job.created_by, project_id=job.project_id,
                 details={"format": res["format"], "counts": res["counts"], "validation_ok": res["validation"]["ok"]})
    db.commit()
    return res


def h_extract_buildings(db: Session, job: ProcessingJob, progress) -> dict:
    import hashlib
    import json

    import geopandas as gpd

    from app.services.etl import process_version
    from app.services.vision import OnnxSegmenter, ThresholdBaseline, extract_buildings
    from app.storage import get_storage

    p = job.params
    rv = db.get(DatasetVersion, p["raster_version_id"])
    ra = db.execute(select(RasterAsset).where(RasterAsset.dataset_version_id == rv.id)).scalar_one_or_none()
    if ra is None:
        raise ValueError("Raster version is not processed")
    backend_name = p.get("backend", "threshold_baseline")
    if backend_name == "onnx":
        backend = OnnxSegmenter(Path(p["model_path"]), Path(p["model_meta_path"]))
    elif backend_name == "threshold_baseline":
        backend = ThresholdBaseline()
    else:
        raise ValueError(f"Unknown backend {backend_name}")
    progress(0.1, f"tiled inference ({backend.info.name})")
    res = extract_buildings(get_storage().local_path(ra.storage_key), backend, overlap=int(p.get("overlap", 64)),
                            threshold=float(p.get("threshold", 0.5)), min_area_m2=float(p.get("min_area_m2", 10)))
    progress(0.7, f"{len(res['polygons'])} polygons; registering predictions")
    if not res["polygons"]:
        return {"polygons": 0, "warnings": res["warnings"], "model": res["model"]}
    src_ds = db.get(Dataset, rv.dataset_id)
    ds = Dataset(project_id=job.project_id, name=p.get("dataset_name") or f"Predicted buildings ({backend.info.name})",
                 kind="predicted_buildings", provenance="derived" if src_ds.provenance != "synthetic" else "synthetic",
                 provider=f"BhuIntel {backend.info.name} v{backend.info.version}",
                 license=f"model: {backend.info.license}; imagery: {src_ds.license}",
                 description=(f"{'Machine-learning' if backend.info.is_ml else 'NON-ML threshold baseline'} "
                              f"building extraction from '{src_ds.name}' (version {rv.version_no}) on "
                              f"{utcnow().date().isoformat()}. Unverified predictions."),
                 reliability=0.3, created_by=job.created_by)
    db.add(ds)
    db.flush()
    gdf = gpd.GeoDataFrame([{"pred_id": f"PRED-{i + 1:05d}", "area_m2": poly["area_m2"], "method": backend.info.name,
                             "model_version": backend.info.version, "imagery_version_id": rv.id,
                             "geometry": poly["geometry"]} for i, poly in enumerate(res["polygons"])], crs=res["crs"])
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "predictions.gpkg"
        gdf.to_file(out, layer="predictions", driver="GPKG")
        data = out.read_bytes()
    key = get_storage().put_bytes(f"projects/{job.project_id}/datasets/{ds.id}", "predictions.gpkg", data)
    v = DatasetVersion(dataset_id=ds.id, version_no=1, file_sha256=hashlib.sha256(data).hexdigest(),
                       original_filename="predictions.gpkg", storage_key=key, format="gpkg", size_bytes=len(data),
                       layer="predictions", source_crs=res["crs"], geometry_type="Polygon", feature_count=len(gdf),
                       status="inspected", created_by=job.created_by,
                       processing_log=[{"step": "building_extraction", "model": json.loads(json.dumps(res["model"],
                                                                                                      default=str)),
                                        "tiles": res["tiles"], "resolution_m": res["resolution_m"],
                                        "warnings": res["warnings"], "imagery_version_id": rv.id}])
    db.add(v)
    db.flush()
    from app.models import FieldMapping

    db.add(FieldMapping(project_id=job.project_id, dataset_id=ds.id, source_field="pred_id", target_field="building_id",
                        method="system", score=1.0, status="approved"))
    db.commit()
    process_version(db, v.id, lambda f, m="": progress(0.7 + 0.3 * f, m), actor_id=job.created_by)
    db.execute(text("UPDATE source_features SET review_status='pending' WHERE dataset_version_id=:v"), {"v": v.id})
    db.commit()
    return {"dataset_id": ds.id, "version_id": v.id, "polygons": len(gdf), "warnings": res["warnings"],
            "model": json.loads(json.dumps(res["model"], default=str)), "tiles": res["tiles"]}


def h_acquire(db: Session, job: ProcessingJob, progress) -> dict:
    import hashlib

    from app.services.acquisition.fetch import (
        AcquisitionError,
        acquire_microsoft,
        acquire_osm,
        check_reachable,
        client,
        search_copernicus,
    )
    from app.services.acquisition.sources import SOURCES
    from app.storage import get_storage

    p = job.params
    key = p["source_key"]
    src = SOURCES[key]
    project = db.get(Project, job.project_id)
    sa_wkb = db.execute(text("SELECT ST_AsBinary(geom) FROM study_areas WHERE project_id=:p AND is_active ORDER BY id "
                             "LIMIT 1"), {"p": project.id}).scalar()
    man = AcquisitionManifest(project_id=project.id, source_key=key, request=p, status="failed", created_by=job.created_by,
                              source_meta={k: src.get(k) for k in ("name", "provider", "license", "attribution",
                                                                   "homepage", "access")})
    db.add(man)
    if not src.get("automated"):
        man.status = "unauthorized" if src["access"] == "authorization_required" else "manual"
        man.message = src["notes"]
        db.commit()
        return {"manifest_id": man.id, "status": man.status, "message": man.message}
    if sa_wkb is None:
        raise ValueError("Configure a study area before acquiring data")
    study = shapely.from_wkb(bytes(sa_wkb))
    with client() as http, tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        progress(0.05, "checking source reachability")
        probe = src.get("index_urls", [src.get("endpoint") or src.get("catalogue") or src["homepage"]])[0]
        man.reachable, detail = check_reachable(probe, http)
        try:
            if key == "microsoft_buildings":
                res = acquire_microsoft(study, p.get("location", "India"), work, http)
            elif key == "osm_overpass":
                res = acquire_osm(study, work, http)
            elif key == "copernicus":
                res = search_copernicus(study, p.get("start", "2025-01-01"), p.get("end", "2025-12-31"),
                                        float(p.get("max_cloud", 20)), http)
                man.status, man.message, man.files = "completed", "catalogue search only", []
                man.source_meta = {**man.source_meta, "search": res}
                db.commit()
                return {"manifest_id": man.id, "status": man.status, **res}
            else:
                raise AcquisitionError("No automated connector", "manual")
        except AcquisitionError as exc:
            man.status, man.message = exc.status, f"{exc} (probe: {detail})"
            db.commit()
            return {"manifest_id": man.id, "status": man.status, "message": man.message}
        progress(0.8, "registering dataset")
        data = Path(res["output"]).read_bytes()
        ds = Dataset(project_id=project.id, name=p.get("dataset_name") or src["name"], kind=src["kind"],
                     provenance="real", provider=src["provider"], source_url=res.get("index_url") or src["homepage"],
                     license=src["license"], attribution=src.get("attribution", ""), reliability=0.6,
                     description=f"Acquired {utcnow().date().isoformat()} via BhuIntel acquisition ({key}).",
                     source_date=utcnow(), created_by=job.created_by)
        db.add(ds)
        db.flush()
        skey = get_storage().put_bytes(f"projects/{project.id}/datasets/{ds.id}", Path(res["output"]).name, data)
        v = DatasetVersion(dataset_id=ds.id, version_no=1, file_sha256=hashlib.sha256(data).hexdigest(),
                           original_filename=Path(res["output"]).name, storage_key=skey, format="gpkg",
                           size_bytes=len(data), layer="buildings", source_crs="EPSG:4326", geometry_type="Polygon",
                           feature_count=res["feature_count"], status="inspected", created_by=job.created_by)
        db.add(v)
        db.flush()
        man.status, man.dataset_version_id = "completed", v.id
        man.files = [{k: f.get(k) for k in ("url", "size", "sha256", "quadkey", "location", "declared_size")}
                     for f in res["files"]]
        man.message = f"{res['feature_count']} features within study area; CRS {res['crs']}; extent {res['extent']}"
        db.commit()
        return {"manifest_id": man.id, "status": "completed", "dataset_id": ds.id, "version_id": v.id,
                "feature_count": res["feature_count"]}


HANDLERS = {
    "process_version": h_process_version,
    "topology": h_topology,
    "match": h_match,
    "change_detection": h_change_detection,
    "train_model": h_train_model,
    "build_integration": h_build_integration,
    "export": h_export,
    "extract_buildings": h_extract_buildings,
    "acquire": h_acquire,
}
