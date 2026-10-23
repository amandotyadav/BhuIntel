"""OGC API - Features (Part 1: Core, GeoJSON) read-only endpoints for a project's integrated layer.

Authentication is required (the integrated layer may relate to land records). Personal data is never
copied into the integrated layer, so nothing personal is exposed here.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.common import feature_collection, parse_bbox
from app.db import get_db
from app.errors import ApiError
from app.models import User
from app.security import authorize_project, get_current_user

router = APIRouter(prefix="/api/v1/projects/{project_id}/ogc", tags=["ogc-api-features"])
COLLECTIONS = {"parcels": "parcel", "buildings": "building"}
CONFORMANCE = [
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core",
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson",
]


def _base(request: Request, project_id: int) -> str:
    return str(request.base_url).rstrip("/") + f"/api/v1/projects/{project_id}/ogc"


@router.get("")
def landing(project_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    p = authorize_project(db, user, project_id)
    b = _base(request, project_id)
    return {"title": f"BhuIntel integrated layer: {p.name}", "description": "Derived, reviewable integration product",
            "links": [{"href": b, "rel": "self", "type": "application/json"},
                      {"href": f"{b}/conformance", "rel": "conformance", "type": "application/json"},
                      {"href": f"{b}/collections", "rel": "data", "type": "application/json"},
                      {"href": str(request.base_url) + "openapi.json", "rel": "service-desc",
                       "type": "application/vnd.oai.openapi+json;version=3.0"}]}


@router.get("/conformance")
def conformance(project_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    return {"conformsTo": CONFORMANCE}


def _collection(b: str, cid: str, extent) -> dict:
    return {"id": cid, "title": f"Integrated {cid}", "itemType": "feature",
            "crs": ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"],
            "extent": {"spatial": {"bbox": [extent] if extent else [], "crs":
                                   "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
            "links": [{"href": f"{b}/collections/{cid}", "rel": "self", "type": "application/json"},
                      {"href": f"{b}/collections/{cid}/items", "rel": "items", "type": "application/geo+json"}]}


def _extent(db: Session, project_id: int, cls: str):
    r = db.execute(text("SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM (SELECT ST_Extent(geom) e FROM "
                        "integrated_features WHERE project_id=:p AND feature_class=:c AND status='active') s"),
                   {"p": project_id, "c": cls}).first()
    return list(r) if r and r[0] is not None else None


@router.get("/collections")
def collections(project_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    b = _base(request, project_id)
    return {"collections": [_collection(b, cid, _extent(db, project_id, cls)) for cid, cls in COLLECTIONS.items()],
            "links": [{"href": f"{b}/collections", "rel": "self", "type": "application/json"}]}


@router.get("/collections/{collection_id}")
def collection(project_id: int, collection_id: str, request: Request, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    if collection_id not in COLLECTIONS:
        raise ApiError(404, "not_found", "Collection not found")
    return _collection(_base(request, project_id), collection_id, _extent(db, project_id, COLLECTIONS[collection_id]))


@router.get("/collections/{collection_id}/items")
def items(project_id: int, collection_id: str, request: Request, bbox: str | None = None, limit: int = 100,
          offset: int = 0, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    authorize_project(db, user, project_id)
    if collection_id not in COLLECTIONS:
        raise ApiError(404, "not_found", "Collection not found")
    limit = max(1, min(limit, 10000))
    where, params = "project_id=:p AND feature_class=:c AND status='active'", {"p": project_id,
                                                                              "c": COLLECTIONS[collection_id]}
    b = parse_bbox(bbox)
    if b:
        where += " AND geom && ST_MakeEnvelope(:x0,:y0,:x1,:y1,4326)"
        params.update({"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
    rows = db.execute(text(f"SELECT id, integrated_key, version, confidence, attributes, ST_AsGeoJSON(geom, 7) AS "
                           f"geojson FROM integrated_features WHERE {where} ORDER BY id LIMIT :l OFFSET :o"),
                      {**params, "l": limit, "o": max(0, offset)}).mappings().all()
    total = db.execute(text(f"SELECT count(*) FROM integrated_features WHERE {where}"), params).scalar()
    fc = feature_collection(rows, props_fn=lambda r: {"integrated_key": r["integrated_key"], "version": r["version"],
                                                     "match_confidence": r["confidence"], **(r["attributes"] or {})})
    base = _base(request, project_id) + f"/collections/{collection_id}/items"
    links = [{"href": f"{base}?limit={limit}&offset={offset}", "rel": "self", "type": "application/geo+json"}]
    if offset + limit < total:
        links.append({"href": f"{base}?limit={limit}&offset={offset + limit}", "rel": "next",
                      "type": "application/geo+json"})
    fc.update({"numberMatched": total, "numberReturned": len(rows), "links": links})
    return fc
