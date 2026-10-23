"""Shared API helpers: GeoJSON assembly from PostGIS, paging, project-scoped lookups."""

import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models import Dataset, DatasetVersion


def parse_bbox(bbox: str | None) -> list[float] | None:
    if not bbox:
        return None
    try:
        vals = [float(x) for x in bbox.split(",")]
    except ValueError:
        raise ApiError(422, "invalid_bbox", "bbox must be minx,miny,maxx,maxy") from None
    if len(vals) != 4 or vals[0] > vals[2] or vals[1] > vals[3]:
        raise ApiError(422, "invalid_bbox", "bbox must be minx,miny,maxx,maxy with min <= max")
    return vals


def feature_collection(rows, id_key: str = "id", props_fn=None) -> dict:
    feats = []
    for r in rows:
        r = dict(r)
        geom = json.loads(r.pop("geojson")) if r.get("geojson") else None
        props = props_fn(r) if props_fn else r
        feats.append({"type": "Feature", "id": r.get(id_key), "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def get_version(db: Session, project_id: int, version_id: int) -> tuple[DatasetVersion, Dataset]:
    v = db.get(DatasetVersion, version_id)
    if v is None:
        raise ApiError(404, "not_found", "Dataset version not found")
    d = db.get(Dataset, v.dataset_id)
    if d.project_id != project_id:
        raise ApiError(404, "not_found", "Dataset version not found")
    return v, d


def get_dataset(db: Session, project_id: int, dataset_id: int) -> Dataset:
    d = db.get(Dataset, dataset_id)
    if d is None or d.project_id != project_id:
        raise ApiError(404, "not_found", "Dataset not found")
    return d


def features_geojson(db: Session, where: str, params: dict, limit: int, offset: int,
                     include_attributes: bool = True) -> dict:
    attrs = "f.attributes, f.normalized_attributes," if include_attributes else ""
    rows = db.execute(text(
        f"""SELECT f.id, f.dataset_version_id, f.source_fid, f.source_id_raw, f.normalized_id, {attrs}
                   f.geometry_type, f.area_m2, f.is_valid_source, f.validity_reason, f.repair_status, f.duplicate_of,
                   f.review_status, ST_AsGeoJSON(f.geom, 7) AS geojson
            FROM source_features f WHERE {where} ORDER BY f.id LIMIT :limit OFFSET :offset"""),
        {**params, "limit": limit, "offset": offset}).mappings().all()
    total = db.execute(text(f"SELECT count(*) FROM source_features f WHERE {where}"), params).scalar()
    fc = feature_collection(rows)
    fc["numberMatched"] = total
    fc["numberReturned"] = len(rows)
    return fc


PERSONAL_ATTRIBUTE_KEYS = {"owner_name", "owner", "owner_nm", "khatedar", "holder_name", "occupant"}


def redact(attrs: dict | None, role: str) -> dict | None:
    """Personal fields are visible only to reviewer+ roles (never in unauthenticated/public outputs)."""
    if attrs is None or role in ("reviewer", "admin"):
        return attrs
    return {k: ("[redacted]" if k.lower() in PERSONAL_ATTRIBUTE_KEYS else v) for k, v in attrs.items()}
