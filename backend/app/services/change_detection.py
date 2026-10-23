"""Change detection between two versions of a dataset, and (optionally) between two co-registered images.

Vector comparison (primary method)
----------------------------------
1. Pair features by normalized identifier where the identifier is unique in both versions.
2. Pair remaining features spatially (best IoU >= ``min_pair_iou`` within tolerance), greedily.
3. Classify each pair using the positional uncertainty ``u`` (combined source accuracy or tolerance):
     - geometry_modified : IoU < iou_modified or |area change| > area_change, AND not explainable by u
     - uncertain         : differences of the order of u (shift between u and 2u, or IoU in the grey zone)
     - attribute_changed : geometry equivalent, attributes differ
     - unchanged         : otherwise (not stored unless ``store_unchanged``)
4. Unpaired old features -> removed; unpaired new -> added. If the feature lies outside the other
   version's coverage extent it is ``uncertain`` (coverage difference, not a real-world change).
"""

import math

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import ChangeRecord, Dataset, DatasetVersion, Project

DEFAULTS = {"min_pair_iou": 0.3, "iou_modified": 0.8, "area_change": 0.2, "store_unchanged": False}


def _uncertainty(project: Project, ds: Dataset, params: dict) -> float:
    if params.get("positional_uncertainty_m") is not None:
        return float(params["positional_uncertainty_m"])
    if ds.positional_accuracy_m:
        return math.sqrt(2) * ds.positional_accuracy_m
    return float(project.default_tolerance_m)


def detect_vector_changes(db: Session, old_vid: int, new_vid: int, params: dict | None = None) -> dict:
    p = {**DEFAULTS, **(params or {})}
    old_v, new_v = db.get(DatasetVersion, old_vid), db.get(DatasetVersion, new_vid)
    if old_v.dataset_id != new_v.dataset_id:
        raise ValueError("Change detection compares two versions of the same dataset")
    ds = db.get(Dataset, old_v.dataset_id)
    project = db.get(Project, ds.project_id)
    u = _uncertainty(project, ds, p)
    db.execute(text("DELETE FROM change_records WHERE old_version_id=:o AND new_version_id=:n"),
               {"o": old_vid, "n": new_vid})

    olds = {r["id"]: dict(r) for r in db.execute(text(
        "SELECT id, normalized_id, attributes, area_m2 FROM source_features WHERE dataset_version_id=:v "
        "AND duplicate_of IS NULL AND geom_work IS NOT NULL"), {"v": old_vid}).mappings()}
    news = {r["id"]: dict(r) for r in db.execute(text(
        "SELECT id, normalized_id, attributes, area_m2 FROM source_features WHERE dataset_version_id=:v "
        "AND duplicate_of IS NULL AND geom_work IS NOT NULL"), {"v": new_vid}).mappings()}

    def unique_ids(rows):
        seen: dict[str, list[int]] = {}
        for i, r in rows.items():
            if r["normalized_id"]:
                seen.setdefault(r["normalized_id"], []).append(i)
        return {k: v[0] for k, v in seen.items() if len(v) == 1}

    uo, un = unique_ids(olds), unique_ids(news)
    pairs: list[tuple[int, int, str]] = [(uo[k], un[k], "identifier") for k in uo.keys() & un.keys()]
    used_o, used_n = {a for a, _, _ in pairs}, {b for _, b, _ in pairs}
    spatial = db.execute(text(
        """SELECT a.id AS o, b.id AS n,
                  ST_Area(ST_Intersection(a.geom_work,b.geom_work))/NULLIF(ST_Area(ST_Union(a.geom_work,b.geom_work)),0) AS iou
           FROM source_features a JOIN source_features b ON b.dataset_version_id=:n AND b.duplicate_of IS NULL
             AND ST_DWithin(a.geom_work, b.geom_work, :tol)
           WHERE a.dataset_version_id=:o AND a.duplicate_of IS NULL ORDER BY iou DESC NULLS LAST"""),
        {"o": old_vid, "n": new_vid, "tol": max(u, 0.5)}).mappings().all()
    for s in spatial:
        if s["o"] in used_o or s["n"] in used_n or (s["iou"] or 0) < p["min_pair_iou"]:
            continue
        pairs.append((s["o"], s["n"], "spatial"))
        used_o.add(s["o"])
        used_n.add(s["n"])

    counts: dict[str, int] = {}

    def add(ctype, o, n, metrics, expl, geom_from):
        counts[ctype] = counts.get(ctype, 0) + 1
        rec = ChangeRecord(project_id=project.id, old_version_id=old_vid, new_version_id=new_vid, change_type=ctype,
                           old_feature_id=o, new_feature_id=n, metrics=metrics, explanation=expl)
        db.add(rec)
        db.flush()
        db.execute(text("UPDATE change_records SET geom=(SELECT geom FROM source_features WHERE id=:f) WHERE id=:r"),
                   {"f": geom_from, "r": rec.id})

    for o, n, how in pairs:
        m = db.execute(text(
            """SELECT ST_Area(ST_Intersection(a.geom_work,b.geom_work))/NULLIF(ST_Area(ST_Union(a.geom_work,b.geom_work)),0) AS iou,
                      ST_Distance(ST_Centroid(a.geom_work),ST_Centroid(b.geom_work)) AS shift,
                      ST_Area(a.geom_work) AS ao, ST_Area(b.geom_work) AS an,
                      ST_HausdorffDistance(a.geom_work,b.geom_work) AS hd
               FROM source_features a, source_features b WHERE a.id=:o AND b.id=:n"""), {"o": o, "n": n}).mappings().one()
        iou, shift, hd = float(m["iou"] or 0), float(m["shift"]), float(m["hd"])
        area_rel = abs(m["an"] - m["ao"]) / m["ao"] if m["ao"] else 1.0
        attr_changes = {k: {"old": olds[o]["attributes"].get(k), "new": news[n]["attributes"].get(k)}
                        for k in set(olds[o]["attributes"]) | set(news[n]["attributes"])
                        if olds[o]["attributes"].get(k) != news[n]["attributes"].get(k)}
        metrics = {"iou": round(iou, 4), "centroid_shift_m": round(shift, 3), "hausdorff_m": round(hd, 3),
                   "area_change_ratio": round(area_rel, 4), "paired_by": how, "positional_uncertainty_m": round(u, 3),
                   "attribute_changes": attr_changes}
        big = (iou < p["iou_modified"] or area_rel > p["area_change"])
        if big and hd > 2 * u:
            add("geometry_modified", o, n, metrics,
                f"Footprint changed: IoU {iou:.2f}, area change {area_rel * 100:.0f}%, Hausdorff {hd:.1f} m exceeds 2x "
                f"positional uncertainty ({2 * u:.1f} m).", n)
        elif big or (hd > u and shift > 0.5 * u):
            add("uncertain", o, n, metrics,
                f"Difference (IoU {iou:.2f}, shift {shift:.1f} m, Hausdorff {hd:.1f} m) is comparable to the "
                f"positional uncertainty ({u:.1f} m); may be registration error rather than a real change.", n)
        elif attr_changes:
            add("attribute_changed", o, n, metrics, f"Attributes changed: {sorted(attr_changes)}.", n)
        elif p["store_unchanged"]:
            add("unchanged", o, n, metrics, "No significant change.", n)
        else:
            counts["unchanged"] = counts.get("unchanged", 0) + 1

    def in_extent(fid: int, other_vid: int) -> bool:
        return bool(db.execute(text(
            "SELECT ST_Intersects(ST_Envelope(f.geom), v.extent) FROM source_features f, dataset_versions v WHERE f.id=:f AND v.id=:v"),
            {"f": fid, "v": other_vid}).scalar())

    for o in olds.keys() - used_o:
        if in_extent(o, new_vid):
            add("removed", o, None, {"area_m2": olds[o]["area_m2"]}, "Feature present in the old version has no "
                "counterpart in the new version.", o)
        else:
            add("uncertain", o, None, {"reason": "outside_new_coverage"}, "Feature lies outside the new version's "
                "coverage extent; absence may reflect coverage, not removal.", o)
    for n in news.keys() - used_n:
        if in_extent(n, old_vid):
            add("added", None, n, {"area_m2": news[n]["area_m2"]}, "New feature with no counterpart in the old version.", n)
        else:
            add("uncertain", None, n, {"reason": "outside_old_coverage"}, "Feature lies outside the old version's "
                "coverage extent; may be a coverage difference.", n)
    db.flush()
    old_d, new_d = old_v.created_at, new_v.created_at
    return {"counts": counts, "positional_uncertainty_m": u, "pairs": len(pairs),
            "source_dates": {"old_dataset_date": ds.source_date.isoformat() if ds.source_date else None,
                             "old_uploaded": old_d.isoformat(), "new_uploaded": new_d.isoformat()}}


def detect_image_changes(db: Session, project_id: int, old_raster_path: str, new_raster_path: str,
                         old_vid: int, new_vid: int, params: dict | None = None) -> dict:
    """Image differencing for two dated images (requires overlapping footprints and a common CRS).

    The newer image is resampled onto the older image's grid; optional OpenCV ECC translation alignment
    reduces small misregistration. Changed regions (Otsu threshold on the absolute brightness difference,
    morphologically cleaned) are polygonised and stored as ``image_change`` records. Results are flagged
    as uncertain when pixel size exceeds 1 m.
    """
    import cv2
    import numpy as np
    import rasterio
    from rasterio.features import shapes
    from rasterio.warp import Resampling, reproject
    from shapely.geometry import shape

    p = {"min_area_m2": 20.0, "align": True, **(params or {})}
    with rasterio.open(old_raster_path) as a, rasterio.open(new_raster_path) as b:
        if a.crs is None or b.crs is None:
            raise ValueError("Both images must be georeferenced")
        img_a = a.read(out_dtype="float32").mean(axis=0)
        img_b = np.zeros_like(img_a)
        reproject(b.read(out_dtype="float32").mean(axis=0), img_b, src_transform=b.transform, src_crs=b.crs,
                  dst_transform=a.transform, dst_crs=a.crs, resampling=Resampling.bilinear)
        res = abs(a.res[0])
        transform, crs = a.transform, a.crs
    shift = (0.0, 0.0)
    if p["align"]:
        try:
            warp = np.eye(2, 3, dtype=np.float32)
            _, warp = cv2.findTransformECC(img_a, img_b, warp, cv2.MOTION_TRANSLATION,
                                           (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-5), None, 5)
            img_b = cv2.warpAffine(img_b, warp, (img_a.shape[1], img_a.shape[0]),
                                   flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
            shift = (float(warp[0, 2]), float(warp[1, 2]))
        except cv2.error:
            shift = (float("nan"), float("nan"))
    diff = np.abs(img_a - img_b)
    d8 = cv2.normalize(diff, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")
    _, mask = cv2.threshold(d8, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n = 0
    import pyproj
    from pyproj import CRS as PCRS
    from shapely.ops import transform as stransform

    to4326 = pyproj.Transformer.from_crs(PCRS.from_user_input(crs), 4326, always_xy=True).transform
    for geom, _val in shapes(mask.astype("uint8"), mask=mask.astype(bool), transform=transform):
        g = shape(geom)
        if g.area < p["min_area_m2"]:
            continue
        g4326 = stransform(to4326, g)
        rec = ChangeRecord(project_id=project_id, old_version_id=old_vid, new_version_id=new_vid,
                           change_type="image_change" if res <= 1.0 else "uncertain",
                           metrics={"area_m2": round(g.area, 2), "pixel_size_m": res, "alignment_shift_px": shift},
                           explanation=f"Brightness change region of {g.area:.0f} m² at {res:.2f} m/pixel"
                           + ("" if res <= 1.0 else " (coarse resolution: treat as uncertain)"))
        db.add(rec)
        db.flush()
        db.execute(text("UPDATE change_records SET geom=ST_GeomFromText(:w,4326) WHERE id=:i"),
                   {"w": g4326.wkt, "i": rec.id})
        n += 1
    db.flush()
    return {"image_change_regions": n, "pixel_size_m": res, "alignment_shift_px": shift}
