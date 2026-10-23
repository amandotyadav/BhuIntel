"""Geometry validation and documented repair operations (Shapely 2 / GEOS).

Repair policy
-------------
Every repair keeps the original geometry and is classified:

* ``auto_safe``       - result is polygonal/same family and differs from the input by no more than a
                        numerical tolerance (area change <= AREA_EPS relative AND Hausdorff <= DIST_EPS_M).
                        Typical causes: duplicate consecutive vertices, wrong ring orientation, a
                        closing-vertex issue. Applied automatically and recorded.
* ``review_required`` - repair changes shape/area (e.g. a self-intersecting "bow-tie" split into parts)
                        for non-cadastral data such as building footprints.
* ``legal_no_auto``   - non-trivial change to a cadastral/parcel geometry. Never applied without an
                        explicit, audited decision by an authorised reviewer.
"""

from dataclasses import dataclass

import shapely
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity

AREA_EPS = 1e-6
DIST_EPS_M = 1e-3
CADASTRAL_KINDS = {"parcels"}
POLYGONAL = {"Polygon", "MultiPolygon"}


@dataclass
class RepairResult:
    valid: bool
    reason: str | None
    repaired: BaseGeometry | None
    safety: str | None  # auto_safe | review_required | legal_no_auto | None (no repair needed)
    method: str | None
    metrics: dict


def _family(geom_type: str) -> str:
    return geom_type.replace("Multi", "")


def _polygonal_part(geom: BaseGeometry) -> BaseGeometry:
    """make_valid may return a GeometryCollection with stray lines/points; keep the polygonal part."""
    if geom.geom_type == "GeometryCollection":
        polys = [g for g in geom.geoms if g.geom_type in POLYGONAL]
        if not polys:
            return geom
        return shapely.union_all(polys)
    return geom


def validate_and_repair(geom: BaseGeometry | None, dataset_kind: str, metric: bool = True) -> RepairResult:
    """Validate ``geom`` (expressed in a metric CRS when ``metric``) and classify the documented repair."""
    if geom is None or geom.is_empty:
        return RepairResult(False, "Empty or missing geometry", None, None, None, {})
    if geom.is_valid:
        return RepairResult(True, None, None, None, None, {})

    reason = explain_validity(geom)
    repaired = shapely.make_valid(geom, method="structure", keep_collapsed=False)
    method = "shapely.make_valid(method='structure')"
    if geom.geom_type in POLYGONAL:
        repaired = _polygonal_part(repaired)
    metrics: dict = {}
    if geom.geom_type in POLYGONAL and repaired.geom_type in POLYGONAL:
        # Area of an invalid polygon is ill-defined; compare against the shoelace area of the
        # exterior ring(s) which is what most software reports for such input.
        orig_area = abs(_shoelace_area(geom))
        new_area = repaired.area
        metrics["original_area"] = float(orig_area)
        metrics["repaired_area"] = float(new_area)
        metrics["area_change_ratio"] = abs(new_area - orig_area) / orig_area if orig_area > 0 else 1.0
        try:
            metrics["hausdorff"] = float(shapely.hausdorff_distance(shapely.boundary(geom), repaired.boundary))
        except Exception:
            metrics["hausdorff"] = None
    same_family = _family(repaired.geom_type) == _family(geom.geom_type)
    metrics["result_type"] = repaired.geom_type
    metrics["parts_before"] = int(shapely.get_num_geometries(geom))
    metrics["parts_after"] = int(shapely.get_num_geometries(repaired))

    trivial = (
        same_family
        and metrics.get("area_change_ratio", 1.0) <= AREA_EPS
        and (metrics.get("hausdorff") or 0.0) <= (DIST_EPS_M if metric else 1e-8)
        and metrics["parts_after"] == metrics["parts_before"]
    )
    if trivial:
        safety = "auto_safe"
    elif dataset_kind in CADASTRAL_KINDS:
        safety = "legal_no_auto"
    else:
        safety = "review_required"
    return RepairResult(False, reason, repaired, safety, method, metrics)


def _shoelace_area(geom: BaseGeometry) -> float:
    if geom.geom_type == "MultiPolygon":
        return sum(abs(_shoelace_area(p)) for p in geom.geoms)
    if geom.geom_type != "Polygon":
        return 0.0
    import numpy as np

    def ring_area(coords):
        a = np.asarray(coords)
        x, y = a[:, 0], a[:, 1]
        return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))

    area = abs(ring_area(geom.exterior.coords))
    for ring in geom.interiors:
        area -= abs(ring_area(ring.coords))
    return area


def thinness_ratio(geom: BaseGeometry) -> float:
    """Polsby-Popper compactness 4*pi*A/P^2 (1 = circle, ->0 = sliver)."""
    p = geom.length
    if p == 0:
        return 0.0
    import math

    return 4 * math.pi * geom.area / (p * p)
