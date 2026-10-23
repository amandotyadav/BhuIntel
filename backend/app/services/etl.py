"""ETL: transform and load a dataset version into PostGIS.

Steps (each recorded in ``dataset_versions.processing_log``):
1. Read source (original file is kept untouched in storage).
2. Require a known CRS (declared in file or explicitly confirmed by a user). Never guessed.
3. Reproject to EPSG:4326 (canonical) and the project's projected working CRS (metric).
4. Validate geometry; apply only documented ``auto_safe`` repairs automatically; queue others as
   proposed corrections (original geometry preserved).
5. Normalize identifiers and attributes through approved field mappings; nulls normalised.
6. Detect exact duplicate records (same geometry + attributes) without dropping them.
7. Optional study-area filter (features not intersecting the study area are excluded and counted;
   cadastral geometries are never clipped).
8. Load into PostGIS in a single transaction (re-processing replaces this version's rows: idempotent).
"""

import hashlib
import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from pyproj import CRS
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.models import (
    Correction,
    Dataset,
    DatasetVersion,
    FieldMapping,
    Project,
    RasterAsset,
    SourceFeature,
    StudyArea,
    utcnow,
)
from app.services import audit
from app.services.crs import area_of_use_check, crs_identifier, parse_crs
from app.services.geometry import validate_and_repair
from app.services.ingest import IngestError, inspect_raster, normalize_null, read_vector, resolve_archive
from app.services.schema_mapping import apply_mappings, normalize_identifier
from app.storage import get_storage

log = logging.getLogger("bhuintel.etl")

ID_TARGET_BY_KIND = {
    "parcels": "parcel_id",
    "buildings": "building_id",
    "predicted_buildings": "building_id",
    "property_records": "property_id",
}
EXPECTED_GEOMETRY = {
    "parcels": {"Polygon", "MultiPolygon"},
    "buildings": {"Polygon", "MultiPolygon"},
    "predicted_buildings": {"Polygon", "MultiPolygon"},
    "utility_lines": {"LineString", "MultiLineString"},
}
BATCH = 1000


class ProcessingError(Exception):
    pass


def _json_safe(v):
    v = normalize_null(v)
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp,)):
        return v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        return v.item()
    if isinstance(v, (bytes, bytearray)):
        return None
    return v


def approved_mapping(db: Session, dataset_id: int) -> dict[str, str]:
    rows = db.execute(
        select(FieldMapping).where(
            FieldMapping.dataset_id == dataset_id,
            FieldMapping.status == "approved",
            FieldMapping.target_field.is_not(None),
        )
    ).scalars()
    return {m.source_field: m.target_field for m in rows}


def open_version_data(version: DatasetVersion, work: Path) -> tuple[str, Path]:
    path = get_storage().local_path(version.storage_key)
    fmt = version.format
    if fmt == "zip":
        fmt, path = resolve_archive(path, work / "x")
    return fmt, path


def process_version(
    db: Session,
    version_id: int,
    progress: Callable[[float, str], None],
    actor_id: int | None = None,
    study_area_filter: bool = False,
) -> dict:
    version = db.get(DatasetVersion, version_id)
    if version is None:
        raise ProcessingError(f"Dataset version {version_id} not found")
    dataset = db.get(Dataset, version.dataset_id)
    project = db.get(Project, dataset.project_id)
    plog: list[dict] = []

    def step(msg: str, **kw):
        entry = {"ts": utcnow().isoformat(), "step": msg, **kw}
        plog.append(entry)
        log.info("etl step", extra={"ctx": {"version_id": version_id, "step": msg}})

    with tempfile.TemporaryDirectory(prefix="bhu_etl_") as tmp:
        fmt, path = open_version_data(version, Path(tmp))
        if fmt == "geotiff":
            return _process_raster(db, version, path, step, plog)

        progress(0.05, "reading source")
        crs_override = version.source_crs if (version.crs_confirmed and version.source_crs) else None
        data = read_vector(path, fmt, version.layer, crs_override=crs_override, csv_options=version.csv_options)
        step("read", format=fmt, rows=len(data), layer=version.layer)

        has_geom = isinstance(data, gpd.GeoDataFrame)
        if has_geom and data.crs is None:
            version.status = "needs_crs"
            version.error = "Coordinate reference system unknown; confirm it before processing."
            version.processing_log = plog
            db.commit()
            raise ProcessingError(version.error)

        mapping = approved_mapping(db, dataset.id)
        step("field_mapping", approved=len(mapping), mapping=mapping)
        id_target = ID_TARGET_BY_KIND.get(dataset.kind)
        id_source_field = next((s for s, t in mapping.items() if t == id_target), None)
        if id_target and id_source_field is None:
            step("warning", message=f"No approved mapping to '{id_target}'; identifier matching disabled.")

        working = CRS.from_epsg(project.working_crs)
        warnings: list[str] = []
        if has_geom:
            src_crs = CRS.from_user_input(data.crs)
            progress(0.15, "reprojecting")
            g4326 = data.geometry.to_crs(4326)
            gwork = data.geometry.to_crs(working)
            bounds = g4326[g4326.notna() & ~g4326.is_empty].total_bounds.tolist() if len(g4326) else None
            msg = area_of_use_check(src_crs, bounds) if bounds else None
            if msg:
                warnings.append(msg)
            step("reproject", source_crs=crs_identifier(src_crs), canonical="EPSG:4326",
                 working=f"EPSG:{project.working_crs}", method="pyproj (always_xy)")
            src_srid = src_crs.to_epsg(min_confidence=70) or 0
        else:
            g4326 = gwork = None
            src_srid = 0

        study_geom = None
        if study_area_filter and has_geom:
            sa = db.execute(
                select(StudyArea).where(StudyArea.project_id == project.id, StudyArea.is_active.is_(True))
            ).scalars().first()
            if sa is not None:
                wkb = db.execute(text("SELECT ST_AsBinary(geom) FROM study_areas WHERE id=:i"), {"i": sa.id}).scalar()
                study_geom = shapely.from_wkb(bytes(wkb))

        progress(0.3, "validating geometry")
        expected = EXPECTED_GEOMETRY.get(dataset.kind)
        records = []
        corrections = []
        n_invalid = n_auto = n_review = n_unexpected = n_outside = n_null = 0
        attr_cols = [c for c in data.columns if c != "geometry"]
        seen_hash: dict[str, int] = {}
        # to_dict keeps arbitrary column names intact (itertuples would rename e.g. "building:levels")
        attr_rows = pd.DataFrame(data[attr_cols]).to_dict("records")
        for i, rowd in enumerate(attr_rows):
            attrs = {c: _json_safe(rowd.get(c)) for c in attr_cols}
            gs = g4326.iloc[i] if has_geom else None
            gw = gwork.iloc[i] if has_geom else None
            g_src = data.geometry.iloc[i] if has_geom else None
            if has_geom and (gs is None or gs.is_empty):
                n_null += 1
                gs = gw = g_src = None
            if study_geom is not None and gs is not None and not gs.intersects(study_geom):
                n_outside += 1
                continue
            rec = {
                "source_fid": str(i),
                "attributes": attrs,
                "geom_source": g_src,
                "geom": gs,
                "geom_work": gw,
                "geometry_type": gs.geom_type if gs is not None else None,
                "is_valid_source": None,
                "validity_reason": None,
                "repair_status": "none",
            }
            if gw is not None:
                if expected and gw.geom_type not in expected:
                    n_unexpected += 1
                rr = validate_and_repair(gw, dataset.kind, metric=True)
                rec["is_valid_source"] = rr.valid
                rec["validity_reason"] = rr.reason
                if not rr.valid:
                    n_invalid += 1
                if rr.repaired is not None:
                    repaired_4326 = gpd.GeoSeries([rr.repaired], crs=working).to_crs(4326).iloc[0]
                    corrections.append((len(records), rr, repaired_4326))
                    rec["geom_work"] = rr.repaired  # computations need valid geometry
                    if rr.safety == "auto_safe":
                        rec["geom"] = repaired_4326
                        rec["repair_status"] = "auto_repaired"
                        n_auto += 1
                    else:
                        rec["repair_status"] = "needs_review"
                        n_review += 1
                rec["area_m2"] = float(rec["geom_work"].area) if rec["geom_work"].geom_type in (
                    "Polygon", "MultiPolygon") else None
            else:
                rec["area_m2"] = None
            raw_id = attrs.get(id_source_field) if id_source_field else None
            rec["source_id_raw"] = None if raw_id is None else str(raw_id)
            rec["normalized_id"] = normalize_identifier(raw_id)
            rec["normalized_attributes"] = apply_mappings(attrs, mapping)
            h = hashlib.sha256()
            h.update(json.dumps(attrs, sort_keys=True, default=str).encode())
            if gs is not None:
                h.update(shapely.to_wkb(shapely.normalize(gs), output_dimension=2))
            rec["row_hash"] = h.hexdigest()
            rec["duplicate_of_idx"] = seen_hash.get(rec["row_hash"])
            if rec["duplicate_of_idx"] is None:
                seen_hash[rec["row_hash"]] = len(records)
            records.append(rec)
            if i % 2000 == 0:
                progress(0.3 + 0.4 * i / max(1, len(data)), "validating geometry")

        step("validate", invalid=n_invalid, auto_repaired=n_auto, repair_needs_review=n_review,
             unexpected_geometry_type=n_unexpected, null_geometry=n_null)
        if study_geom is not None:
            step("study_area_filter", excluded_outside=n_outside, method="ST_Intersects (no clipping)")
        if n_unexpected:
            warnings.append(f"{n_unexpected} feature(s) have geometry types not expected for '{dataset.kind}'.")

        # ---- Load (single transaction) --------------------------------------------------------
        progress(0.75, "loading into PostGIS")
        # Re-processing upserts on (version, source_fid) so feature ids stay stable (conflict fingerprints,
        # match candidates and integrated lineage keep pointing at the same rows). Only still-*proposed*
        # corrections are regenerated; reviewed corrections are history and are kept.
        db.execute(delete(Correction).where(Correction.status == "proposed", Correction.source_feature_id.in_(
            select(SourceFeature.id).where(SourceFeature.dataset_version_id == version.id))))
        ids = _bulk_insert(db, version.id, records, src_srid, project.working_crs)
        db.execute(text("UPDATE source_features SET duplicate_of=NULL WHERE dataset_version_id=:v"), {"v": version.id})
        stale = db.execute(delete(SourceFeature).where(SourceFeature.dataset_version_id == version.id,
                                                       SourceFeature.id.not_in(ids)))
        if stale.rowcount:
            step("removed_stale_features", count=stale.rowcount)
        reviewed = set(db.execute(select(Correction.source_feature_id).where(
            Correction.source_feature_id.in_(ids), Correction.kind == "geometry_repair",
            Correction.status != "proposed")).scalars())
        dup_n = 0
        for idx, rec in enumerate(records):
            if rec["duplicate_of_idx"] is not None:
                dup_n += 1
                db.execute(text("UPDATE source_features SET duplicate_of=:d WHERE id=:i"),
                           {"d": ids[rec["duplicate_of_idx"]], "i": ids[idx]})
        step("duplicates", exact_duplicates=dup_n)

        for idx, rr, repaired_4326 in corrections:
            rec = records[idx]
            if ids[idx] in reviewed:
                continue
            c = Correction(
                project_id=project.id,
                source_feature_id=ids[idx],
                kind="geometry_repair",
                safety=rr.safety,
                original_geom=f"SRID=4326;{rec['geom'].wkt}" if rr.safety != "auto_safe" else None,
                proposed_geom=f"SRID=4326;{repaired_4326.wkt}",
                reason=f"Invalid geometry: {rr.reason}",
                method=rr.method,
                metrics=rr.metrics,
                status="applied" if rr.safety == "auto_safe" else "proposed",
                applied_at=utcnow() if rr.safety == "auto_safe" else None,
            )
            if rr.safety == "auto_safe":
                # original (pre-repair) geometry in EPSG:4326 for the record
                orig = gpd.GeoSeries([rec["geom_source"]], crs=data.crs).to_crs(4326).iloc[0]
                c.original_geom = f"SRID=4326;{orig.wkt}"
            db.add(c)
        step("corrections", proposed=n_review, auto_applied=n_auto)

        version.feature_count = len(records)
        version.geometry_type = version.geometry_type or (records[0]["geometry_type"] if records else None)
        version.status = "processed"
        version.error = None
        version.processed_at = utcnow()
        version.stats = {
            "features_loaded": len(records),
            "invalid_geometries": n_invalid,
            "auto_repaired": n_auto,
            "repairs_need_review": n_review,
            "exact_duplicates": dup_n,
            "null_geometries": n_null,
            "excluded_outside_study_area": n_outside,
            "unexpected_geometry_type": n_unexpected,
            "warnings": warnings,
        }
        if has_geom:
            db.execute(text("UPDATE dataset_versions SET extent = (SELECT ST_Envelope(ST_Collect(geom)) FROM "
                            "source_features WHERE dataset_version_id=:v) WHERE id=:v"), {"v": version.id})
        version.processing_log = (version.processing_log or []) + plog
        audit.record(db, "dataset_version.processed", "dataset_version", version.id, actor_id=actor_id,
                     project_id=project.id, details={k: v for k, v in version.stats.items() if k != "warnings"})
        db.commit()
        progress(1.0, "done")
        return version.stats


def _bulk_insert(db: Session, version_id: int, records: list[dict], src_srid: int, work_srid: int) -> list[int]:
    sql = text(
        """
        INSERT INTO source_features (dataset_version_id, source_fid, source_id_raw, normalized_id, attributes,
            normalized_attributes, geom_source, geom, geom_work, geometry_type, area_m2, is_valid_source,
            validity_reason, repair_status, row_hash, created_at)
        VALUES (:v, :fid, :rid, :nid, CAST(:attrs AS jsonb), CAST(:nattrs AS jsonb),
            ST_SetSRID(ST_GeomFromWKB(:gsrc), :ssrid), ST_SetSRID(ST_GeomFromWKB(:g), 4326),
            ST_SetSRID(ST_GeomFromWKB(:gw), :wsrid), :gt, :area, :valid, :reason, :rstatus, :hash, now())
        ON CONFLICT (dataset_version_id, source_fid) DO UPDATE SET
            source_id_raw=EXCLUDED.source_id_raw, normalized_id=EXCLUDED.normalized_id,
            attributes=EXCLUDED.attributes, normalized_attributes=EXCLUDED.normalized_attributes,
            geom_source=EXCLUDED.geom_source, geom=EXCLUDED.geom, geom_work=EXCLUDED.geom_work,
            geometry_type=EXCLUDED.geometry_type, area_m2=EXCLUDED.area_m2, is_valid_source=EXCLUDED.is_valid_source,
            validity_reason=EXCLUDED.validity_reason,
            repair_status=CASE WHEN source_features.repair_status='repair_approved' THEN 'repair_approved'
                               ELSE EXCLUDED.repair_status END,
            row_hash=EXCLUDED.row_hash
        RETURNING id
        """
    )
    ids: list[int] = []
    for start in range(0, len(records), BATCH):
        chunk = records[start:start + BATCH]
        params = [
            {
                "v": version_id,
                "fid": r["source_fid"],
                "rid": r["source_id_raw"],
                "nid": r["normalized_id"],
                "attrs": json.dumps(r["attributes"], default=str),
                "nattrs": json.dumps(r["normalized_attributes"], default=str),
                "gsrc": None if r["geom_source"] is None else shapely.to_wkb(r["geom_source"]),
                "ssrid": src_srid,
                "g": None if r["geom"] is None else shapely.to_wkb(r["geom"]),
                "gw": None if r["geom_work"] is None else shapely.to_wkb(r["geom_work"]),
                "wsrid": work_srid,
                "gt": r["geometry_type"],
                "area": r["area_m2"],
                "valid": r["is_valid_source"],
                "reason": r["validity_reason"],
                "rstatus": r["repair_status"],
                "hash": r["row_hash"],
            }
            for r in chunk
        ]
        for p in params:  # RETURNING requires per-row execution with psycopg executemany semantics
            ids.append(db.execute(sql, p).scalar_one())
    return ids


def _process_raster(db: Session, version: DatasetVersion, path: Path, step, plog: list) -> dict:
    ins = inspect_raster(path)
    r = ins.raster or {}
    db.execute(delete(RasterAsset).where(RasterAsset.dataset_version_id == version.id))
    footprint = None
    if ins.extent_4326:
        x0, y0, x1, y1 = ins.extent_4326
        footprint = f"SRID=4326;POLYGON(({x0} {y0},{x1} {y0},{x1} {y1},{x0} {y1},{x0} {y0}))"
    db.add(RasterAsset(
        dataset_version_id=version.id, storage_key=version.storage_key, crs=ins.crs, width=r["width"],
        height=r["height"], band_count=r["band_count"], dtype=str(r["dtype"]), nodata=r["nodata"],
        resolution_x=r["resolution"][0], resolution_y=r["resolution"][1], resolution_m=r["resolution_m"],
        is_cog=r["is_cog"], footprint=footprint, meta={"tags": r.get("tags", {}), "overviews": r.get("overviews")},
    ))
    step("raster_registered", crs=ins.crs, resolution_m=r.get("resolution_m"), is_cog=r.get("is_cog"))
    version.status = "processed"
    version.processed_at = utcnow()
    version.stats = {"raster": r, "warnings": ins.warnings}
    version.processing_log = (version.processing_log or []) + plog
    if footprint:
        version.extent = footprint
    db.commit()
    return version.stats


def confirm_crs(version: DatasetVersion, crs_value: str) -> str:
    c = parse_crs(crs_value)
    ident = crs_identifier(c)
    version.source_crs = ident
    version.crs_confirmed = True
    if version.status == "needs_crs":
        version.status = "inspected"
    return ident


def ensure_processable(version: DatasetVersion) -> None:
    if version.status in ("processing", "queued"):
        raise IngestError("This version is already queued or processing.", "already_processing")
    if version.geometry_type and not version.source_crs:
        raise IngestError(
            "Coordinate reference system is unknown. Confirm the CRS (from supplier metadata or survey "
            "control) before processing; it will not be guessed.",
            "crs_required",
        )
