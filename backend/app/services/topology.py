"""Topology validation (PostGIS + GEOS) for a processed dataset version.

Checks: invalid/self-intersecting geometry (from ETL validation), duplicate geometries, overlapping
parcels, unexpected interior gaps, sliver polygons, unexpected geometry types, and for utility line
networks dangling segment ends and disconnected components. Building/parcel crossings are detected by
the building_parcel match run (see conflicts.detect_from_run).

Nothing here modifies geometry. Overlaps, gaps and slivers in cadastral data are reported as
legally-sensitive conflicts that must not be auto-corrected.
"""

from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import Dataset, DatasetVersion
from app.services.conflicts import detect_dataset_issues, upsert_conflict
from app.services.etl import EXPECTED_GEOMETRY

DEFAULTS = {
    "min_overlap_m2": 0.5,
    "min_gap_m2": 0.5,
    "max_gap_m2": 500.0,  # larger enclosed voids are likely roads/open land, not digitising gaps
    "sliver_thinness": 0.12,  # Polsby-Popper 4*pi*A/P^2
    "sliver_max_area_m2": 100.0,
    "duplicate_hausdorff_m": 0.01,
    "snap_tolerance_m": 0.5,
}


def run_topology(db: Session, version_id: int, params: dict | None = None) -> dict:
    p = {**DEFAULTS, **(params or {})}
    v = db.get(DatasetVersion, version_id)
    ds = db.get(Dataset, v.dataset_id)
    pid = ds.project_id
    counts: dict[str, int] = defaultdict(int)
    for k, n in detect_dataset_issues(db, version_id).items():
        counts[k] += n

    base = "dataset_version_id=:v AND duplicate_of IS NULL AND geom_work IS NOT NULL"
    abase = "a.dataset_version_id=:v AND a.duplicate_of IS NULL AND a.geom_work IS NOT NULL"

    # duplicate geometries (non-identical rows sharing (nearly) the same geometry)
    for r in db.execute(text(
        f"""SELECT a.id AS a, b.id AS b, a.source_id_raw AS aid, b.source_id_raw AS bid FROM source_features a
            JOIN source_features b ON b.dataset_version_id=:v AND b.id > a.id AND b.duplicate_of IS NULL
              AND a.geom_work && b.geom_work AND ST_HausdorffDistance(a.geom_work, b.geom_work) <= :tol
            WHERE {abase}"""), {"v": version_id, "tol": p["duplicate_hausdorff_m"]}).mappings():
        upsert_conflict(db, pid, "duplicate_geometry", [r["a"], r["b"]], [version_id], 0.95,
                        f"Features '{r['aid']}' and '{r['bid']}' have identical geometry (Hausdorff <= "
                        f"{p['duplicate_hausdorff_m']} m) but different attributes.", {"hausdorff_max_m":
                                                                                    p["duplicate_hausdorff_m"]})
        counts["duplicate_geometry"] += 1

    expected = EXPECTED_GEOMETRY.get(ds.kind)
    if expected:
        for r in db.execute(text(f"SELECT id, geometry_type FROM source_features WHERE {base} AND NOT "
                                 "(geometry_type = ANY(:exp))"), {"v": version_id, "exp": list(expected)}).mappings():
            upsert_conflict(db, pid, "unexpected_geometry_type", [r["id"]], [version_id], 1.0,
                            f"Geometry type {r['geometry_type']} is not expected for a '{ds.kind}' dataset "
                            f"(expected {sorted(expected)}).", {"geometry_type": r["geometry_type"]})
            counts["unexpected_geometry_type"] += 1

    if ds.kind in ("parcels", "buildings", "predicted_buildings"):
        # slivers
        for r in db.execute(text(
            f"""SELECT id, source_id_raw, ST_Area(geom_work) AS a, ST_Perimeter(geom_work) AS per
                FROM source_features WHERE {base} AND geometry_type IN ('Polygon','MultiPolygon')
                AND ST_Area(geom_work) <= :maxa
                AND 4*pi()*ST_Area(geom_work)/NULLIF(ST_Perimeter(geom_work)^2,0) < :thin"""),
                {"v": version_id, "maxa": p["sliver_max_area_m2"], "thin": p["sliver_thinness"]}).mappings():
            thin = 4 * 3.141592653589793 * r["a"] / (r["per"] ** 2)
            upsert_conflict(db, pid, "sliver_polygon", [r["id"]], [version_id], min(1.0, 1 - thin / p["sliver_thinness"]
                                                                                   + 0.5),
                            f"Polygon '{r['source_id_raw']}' is a sliver: area {r['a']:.2f} m², compactness "
                            f"{thin:.3f} (< {p['sliver_thinness']}).", {"area_m2": r["a"], "compactness": thin})
            counts["sliver_polygon"] += 1

    if ds.kind == "parcels":
        # overlaps between parcels (excludes exact duplicates handled above)
        for r in db.execute(text(
            f"""SELECT a.id AS a, b.id AS b, a.source_id_raw AS aid, b.source_id_raw AS bid,
                       ST_Area(ST_Intersection(a.geom_work, b.geom_work)) AS ia
                FROM source_features a JOIN source_features b
                  ON b.dataset_version_id=:v AND b.id > a.id AND b.duplicate_of IS NULL AND b.geom_work IS NOT NULL
                 AND ST_Intersects(a.geom_work, b.geom_work) AND NOT ST_Touches(a.geom_work, b.geom_work)
                WHERE {abase}"""), {"v": version_id}).mappings():
            if r["ia"] < p["min_overlap_m2"]:
                continue
            upsert_conflict(
                db, pid, "parcel_overlap", [r["a"], r["b"]], [version_id], min(1.0, 0.6 + r["ia"] / 100.0),
                f"Parcels '{r['aid']}' and '{r['bid']}' overlap by {r['ia']:.2f} m².",
                {"overlap_area_m2": r["ia"]},
                geom_sql="SELECT ST_Transform(ST_CollectionExtract(ST_Intersection(a.geom_work, b.geom_work), 3), 4326) "
                         "FROM source_features a, source_features b WHERE a.id=:a AND b.id=:b",
                geom_params={"a": r["a"], "b": r["b"]})
            counts["parcel_overlap"] += 1

        # gaps: interior rings of the union of all parcels that are small enough to be digitising gaps
        srid = db.execute(text(f"SELECT ST_SRID(geom_work) FROM source_features WHERE {base} LIMIT 1"),
                          {"v": version_id}).scalar()
        gaps = db.execute(text(
            f"""WITH u AS (SELECT ST_Union(ST_SnapToGrid(geom_work, 0.001)) AS g FROM source_features WHERE {base}
                           AND geometry_type IN ('Polygon','MultiPolygon')),
                 polys AS (SELECT (ST_Dump(g)).geom AS p FROM u),
                 rings AS (SELECT ST_MakePolygon(ST_InteriorRingN(p, n)) AS hole
                           FROM polys, generate_series(1, ST_NumInteriorRings(p)) AS n)
                SELECT ST_AsText(hole) AS wkt, ST_Area(hole) AS a FROM rings
                WHERE ST_Area(hole) BETWEEN :mn AND :mx"""),
            {"v": version_id, "mn": p["min_gap_m2"], "mx": p["max_gap_m2"]}).mappings().all()
        for g in gaps:
            near = db.execute(text(
                f"SELECT id FROM source_features WHERE {base} AND ST_DWithin(geom_work, "
                "ST_GeomFromText(:w, :srid), 0.01) ORDER BY id"), {"v": version_id, "w": g["wkt"], "srid": srid}).scalars().all()
            upsert_conflict(
                db, pid, "parcel_gap", list(near), [version_id], 0.8,
                f"Enclosed gap of {g['a']:.2f} m² between {len(near)} parcels.",
                {"gap_area_m2": g["a"], "bounded_by_feature_ids": list(near)},
                geom_sql="SELECT ST_Transform(ST_GeomFromText(:w, :srid), 4326)",
                geom_params={"w": g["wkt"], "srid": srid}, key=g["wkt"][:64])
            counts["parcel_gap"] += 1

    if ds.kind == "utility_lines":
        tol = p["snap_tolerance_m"]
        for r in db.execute(text(
            f"""WITH ends AS (
                  SELECT id, source_id_raw, ST_StartPoint(ST_LineMerge(geom_work)) AS pt FROM source_features WHERE {base}
                  UNION ALL
                  SELECT id, source_id_raw, ST_EndPoint(ST_LineMerge(geom_work)) FROM source_features WHERE {base})
                SELECT e.id, e.source_id_raw, count(*) AS dangling_ends FROM ends e
                WHERE NOT EXISTS (SELECT 1 FROM source_features o WHERE o.dataset_version_id=:v AND o.id <> e.id
                                  AND ST_DWithin(o.geom_work, e.pt, :tol))
                GROUP BY e.id, e.source_id_raw"""), {"v": version_id, "tol": tol}).mappings():
            upsert_conflict(db, pid, "dangling_segment", [r["id"]], [version_id], 0.7,
                            f"Segment '{r['source_id_raw'] or r['id']}' has {r['dangling_ends']} unconnected end(s) "
                            f"(snap tolerance {tol} m).", {"dangling_ends": r["dangling_ends"]})
            counts["dangling_segment"] += 1
        comps = db.execute(text(
            f"""SELECT cid, array_agg(id) AS ids FROM (
                  SELECT id, ST_ClusterDBSCAN(geom_work, eps := :tol, minpoints := 1) OVER () AS cid
                  FROM source_features WHERE {base}) c GROUP BY cid ORDER BY count(*) DESC"""),
            {"v": version_id, "tol": tol}).mappings().all()
        for c in comps[1:]:
            upsert_conflict(db, pid, "disconnected_network", list(c["ids"]), [version_id], 0.6,
                            f"Network component of {len(c['ids'])} segment(s) is disconnected from the main network.",
                            {"component_size": len(c["ids"]), "components_total": len(comps)})
            counts["disconnected_network"] += 1
    db.flush()
    return dict(counts)
