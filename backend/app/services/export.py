"""Export the integrated dataset (GeoPackage or GeoJSON) and validate the written file by reloading it."""

import json
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Project, utcnow
from app.storage import get_storage

REQUIRED_ATTRS = {"parcel": ["integrated_key", "integrated_version", "provenance", "lineage"],
                  "building": ["integrated_key", "integrated_version", "provenance", "lineage"]}


def _frame(db: Session, project_id: int, cls: str, include_retired: bool) -> gpd.GeoDataFrame:
    rows = db.execute(text(
        "SELECT integrated_key, version, status, confidence, attributes, lineage, source_feature_ids, updated_at, "
        "ST_AsBinary(geom) AS wkb FROM integrated_features WHERE project_id=:p AND feature_class=:c "
        + ("" if include_retired else "AND status='active' ") + "ORDER BY id"), {"p": project_id, "c": cls}).mappings().all()
    recs = []
    for r in rows:
        a = dict(r["attributes"] or {})
        rec = {k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in a.items()}
        rec.update({"integrated_key": r["integrated_key"], "integrated_version": r["version"], "status": r["status"],
                    "match_confidence": r["confidence"], "lineage": json.dumps(r["lineage"]),
                    "source_feature_ids": json.dumps(list(r["source_feature_ids"] or [])),
                    "updated_at": r["updated_at"].isoformat(), "geometry": shapely.from_wkb(bytes(r["wkb"]))})
        rec.setdefault("provenance", "unknown")
        recs.append(rec)
    if not recs:
        return gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=4326)
    return gpd.GeoDataFrame(pd.DataFrame(recs), geometry="geometry", crs=4326)


def export_integrated(db: Session, project_id: int, fmt: str, include_retired: bool = False) -> dict:
    if fmt not in ("gpkg", "geojson"):
        raise ValueError("format must be gpkg or geojson")
    project = db.get(Project, project_id)
    frames = {cls: _frame(db, project_id, cls, include_retired) for cls in ("parcel", "building")}
    provenance = db.execute(text(
        "SELECT d.name, d.kind, d.provenance, d.provider, d.license, d.attribution, d.source_url FROM datasets d "
        "WHERE d.project_id=:p AND d.status='active' ORDER BY d.id"), {"p": project_id}).mappings().all()
    meta = {"project": project.name, "exported_at": utcnow().isoformat(), "crs": "EPSG:4326",
            "working_crs": f"EPSG:{project.working_crs}",
            "disclaimer": "Derived integration product. Matches and scores are not proof of ownership or legal validity.",
            "datasets": [dict(r) for r in provenance]}
    with tempfile.TemporaryDirectory() as tmp:
        if fmt == "gpkg":
            path = Path(tmp) / f"integrated_project_{project_id}.gpkg"
            for cls, gdf in frames.items():
                if len(gdf):
                    gdf.to_file(path, layer=f"{cls}s", driver="GPKG")
            pd_meta = pd.DataFrame([{"key": "metadata", "value": json.dumps(meta, default=str)}])
            pyogrio.write_dataframe(pd_meta, path, layer="export_metadata", driver="GPKG")
        else:
            path = Path(tmp) / f"integrated_project_{project_id}.geojson"
            parts = []
            for cls, gdf in frames.items():
                if len(gdf):
                    g = gdf.copy()
                    g["feature_class"] = cls
                    parts.append(g)
            allg = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=4326) if parts else frames["parcel"]
            fc = json.loads(allg.to_json(drop_id=True))
            fc["metadata"] = meta
            path.write_text(json.dumps(fc, default=str))
        validation = validate_export(path, fmt, {cls: len(g) for cls, g in frames.items()})
        key = get_storage().put_bytes(f"exports/{project_id}", path.name, path.read_bytes())
    return {"storage_key": key, "filename": path.name, "format": fmt, "counts": {c: len(g) for c, g in frames.items()},
            "validation": validation}


def validate_export(path: Path, fmt: str, expected_counts: dict[str, int]) -> dict:
    """Reload the written file with GDAL and check counts, CRS, geometry validity and required attributes."""
    report: dict = {"ok": True, "layers": {}, "errors": []}
    if fmt == "gpkg":
        layers = {str(r[0]) for r in pyogrio.list_layers(path)}
        targets = {cls: f"{cls}s" for cls, n in expected_counts.items() if n}
        loaded = {cls: gpd.read_file(path, layer=lyr) for cls, lyr in targets.items() if lyr in layers}
        if "export_metadata" not in layers:
            report["errors"].append("export_metadata layer missing")
    else:
        g = gpd.read_file(path)
        loaded = {cls: g[g["feature_class"] == cls] for cls, n in expected_counts.items() if n} if len(g) else {}
    for cls, n in expected_counts.items():
        if n == 0:
            continue
        g = loaded.get(cls)
        if g is None:
            report["errors"].append(f"layer for {cls} missing")
            continue
        invalid = int((~g.geometry.is_valid).sum())
        empty = int(g.geometry.is_empty.sum() + g.geometry.isna().sum())
        missing = [a for a in REQUIRED_ATTRS[cls] if a not in g.columns]
        crs_ok = g.crs is not None and g.crs.to_epsg() == 4326
        flagged_invalid = 0
        if invalid and "qa_flags" in g.columns:
            flagged_invalid = int(g[~g.geometry.is_valid]["qa_flags"].astype(str).str.contains(
                "geometry_invalid_pending_review").sum())
        report["layers"][cls] = {"features": int(len(g)), "expected": n, "invalid_geometries": invalid,
                                 "invalid_but_flagged_pending_review": flagged_invalid, "empty_geometries": empty,
                                 "missing_required_attributes": missing, "crs_epsg_4326": crs_ok}
        if len(g) != n:
            report["errors"].append(f"{cls}: {len(g)} features reloaded, expected {n}")
        if invalid > flagged_invalid:
            report["errors"].append(f"{cls}: {invalid - flagged_invalid} invalid geometries without a QA flag")
        if empty:
            report["errors"].append(f"{cls}: {empty} empty geometries")
        if missing:
            report["errors"].append(f"{cls}: missing attributes {missing}")
        if not crs_ok:
            report["errors"].append(f"{cls}: CRS is not EPSG:4326")
    report["ok"] = not report["errors"]
    return report
