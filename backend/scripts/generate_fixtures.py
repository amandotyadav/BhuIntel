"""Generate the reproducible SYNTHETIC test fixture (fixtures/synthetic_pune).

All geometries and records produced here are SYNTHETIC. They are laid out on a regular grid placed
near Pune, Maharashtra (UTM zone 43N) purely so that realistic CRS handling is exercised. They do not
represent real parcels, buildings, owners or government records.

Deliberately introduced issues (recorded in ground_truth.json):
  municipal_parcels (GeoPackage, EPSG:32643):
    P-007 self-intersecting (bow-tie) polygon, P-013/P-014 overlap, gap between P-020/P-021,
    sliver polygon P-031, exact duplicate row of P-025, recorded area wrong for P-004, P-030 missing.
  revenue_parcels (GeoJSON, EPSG:4326, different field names / id formats, ~1.5 m systematic shift):
    P-015 carries the wrong identifier (P-051), P-018 land use differs, P-022 geometry displaced 12 m,
    P-028 missing, P-030 present only here.
  buildings_source_a v1/v2 (GeoJSON 4326) and buildings_source_b (GeoPackage 32643):
    building straddling P-010/P-011, source-only buildings, a duplicate, and v1->v2 changes.
  property_records.csv: plot number formats vary, an unknown plot, a duplicate record, a land-use
    conflict, a point in the wrong parcel.
"""

import argparse
import csv
import json
import random
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely import affinity
from shapely.geometry import LineString, Polygon, box

SYN = "SYNTHETIC - not an official record"
CRS_WORK = "EPSG:32643"
E0, N0 = 374200.0, 2049300.0
PW, PH = 30.0, 40.0  # parcel width / height (m)
COLS, ROWS = 6, 5
ROAD = 10.0  # road between column 3 and 4
LAND_USES = ["residential", "residential", "commercial", "residential", "mixed", "institutional"]
LU_CODES = {"residential": "RES", "commercial": "COM", "mixed": "MIX", "institutional": "INST"}
VACANT = {3, 9, 26}  # parcels without buildings
ONLY_A = {5, 14, 27}  # buildings present only in source A
ONLY_B = {8, 19}  # buildings present only in source B


def pid(n: int) -> str:
    return f"P-{n:03d}"


def parcel_geom(n: int) -> Polygon:
    i = n - 1
    col, row = i % COLS, i // COLS
    x = E0 + col * PW + (ROAD if col >= 3 else 0.0)
    y = N0 + row * PH
    return box(x, y, x + PW, y + PH)


def jitter(poly: Polygon, rng: random.Random, amp: float) -> Polygon:
    coords = list(poly.exterior.coords)[:-1]
    moved = [(x + rng.uniform(-amp, amp), y + rng.uniform(-amp, amp)) for x, y in coords]
    return Polygon(moved)


def building_for(n: int, rng: random.Random) -> Polygon:
    p = parcel_geom(n)
    minx, miny, maxx, maxy = p.bounds
    w, h = rng.uniform(12, 18), rng.uniform(14, 22)
    x = minx + rng.uniform(4, PW - w - 4)
    y = miny + rng.uniform(5, PH - h - 5)
    return box(x, y, x + w, y + h)


def main(out: Path) -> None:
    rng = random.Random(20240521)
    out.mkdir(parents=True, exist_ok=True)
    truth: dict = {"_note": SYN, "parcels": {}, "conflicts": [], "buildings": {}, "records": {}, "changes": {}}

    # ---------------- Municipal parcels (A) ----------------
    rows_a = []
    for n in range(1, 31):
        if n == 30:
            continue  # missing in A
        g = parcel_geom(n)
        if n == 7:  # bow-tie self-intersection (swap two vertices)
            minx, miny, maxx, maxy = g.bounds
            g = Polygon([(minx, miny), (maxx, maxy), (maxx, miny), (minx, maxy)])
        if n == 13:  # extend 2 m into P-014 (overlap)
            minx, miny, maxx, maxy = g.bounds
            g = box(minx, miny, maxx + 2.0, maxy)
        if n == 20:  # shrink right edge 1.5 m -> gap with P-021
            minx, miny, maxx, maxy = g.bounds
            g = box(minx, miny, maxx - 1.5, maxy)
        lu = LAND_USES[n % len(LAND_USES)]
        area = round(PW * PH, 1)
        if n == 4:
            area = 1800.0  # wrong recorded area (actual 1200)
        rows_a.append({"plot_no": pid(n), "landuse": LU_CODES[lu], "area": area, "survey_date": "2019-03-15",
                       "ward_no": f"W{1 + (n - 1) // 10}", "data_class": SYN, "geometry": g})
    # sliver polygon squeezed between rows 1 and 2 of column 1
    rows_a.append({"plot_no": "P-031", "landuse": "RES", "area": 9.0, "survey_date": "2019-03-15", "ward_no": "W1",
                   "data_class": SYN, "geometry": box(E0, N0 + PH - 0.3, E0 + PW, N0 + PH)})
    dup = dict(next(r for r in rows_a if r["plot_no"] == "P-025"))
    rows_a.append(dup)  # exact duplicate
    gdf_a = gpd.GeoDataFrame(rows_a, crs=CRS_WORK)
    gdf_a.to_file(out / "municipal_parcels.gpkg", layer="parcels", driver="GPKG")

    # ---------------- Revenue parcels (B) ----------------
    rows_b = []
    shift = (1.5, 0.0)
    for n in range(1, 31):
        if n == 28:
            continue  # missing in B
        g = jitter(parcel_geom(n), rng, 0.3)
        g = affinity.translate(g, *shift)
        if n == 22:
            g = affinity.translate(g, 12.0, 0.0)
        ident = pid(n)
        if n == 15:
            ident = "P-051"
        fmt = rng.choice(["P{n}", "P-{n:03d}", "p {n}", "P{n:04d}"])
        ident_fmt = ident if n == 15 else fmt.format(n=n)
        lu = LAND_USES[n % len(LAND_USES)]
        if n == 18:
            lu = "commercial" if lu != "commercial" else "industrial"
        rows_b.append({"parcel_id": ident_fmt, "land_use_type": lu.title(), "recorded_area": round(g.area, 1),
                       "surv_dt": "2021-11-02", "village": "Synthetic Village", "data_class": SYN, "geometry": g})
    gdf_b = gpd.GeoDataFrame(rows_b, crs=CRS_WORK).to_crs(4326)
    gdf_b.to_file(out / "revenue_parcels.geojson", driver="GeoJSON")

    truth["parcels"] = {
        "a_ids": [r["plot_no"] for r in rows_a],
        "matches_by_geometry": [pid(n) for n in range(1, 31) if n not in (28, 30, 22)],
        "only_in_a": [pid(28), "P-031"],
        "only_in_b": [pid(30)],
        "systematic_shift_m": list(shift),
    }
    truth["conflicts"] = [
        {"category": "invalid_geometry", "parcel": "P-007"},
        {"category": "parcel_overlap", "parcels": ["P-013", "P-014"]},
        {"category": "parcel_gap", "parcels": ["P-020", "P-021"]},
        {"category": "sliver_polygon", "parcel": "P-031"},
        {"category": "duplicate_record", "parcel": "P-025"},
        {"category": "area_discrepancy", "parcel": "P-004"},
        {"category": "parcel_id_mismatch", "parcel": "P-015", "other_id": "P-051"},
        {"category": "attribute_conflict", "parcel": "P-018", "field": "land_use"},
        {"category": "source_disagreement", "parcel": "P-022"},
        {"category": "unmatched_feature", "parcel": "P-028", "dataset": "municipal_parcels"},
        {"category": "unmatched_feature", "parcel": "P-030", "dataset": "revenue_parcels"},
        {"category": "building_crosses_parcel", "parcels": ["P-010", "P-011"]},
        {"category": "unmatched_record", "property": "PROP-9001"},
        {"category": "duplicate_record", "property": "PROP-1016"},
        {"category": "attribute_conflict", "property": "PROP-1016", "field": "land_use"},
        {"category": "dangling_segment", "segment": "S5", "dataset": "utility_lines"},
        {"category": "disconnected_network", "segment": "S4", "dataset": "utility_lines"},
    ]

    # ---------------- Buildings ----------------
    a_rows, b_rows, pairs = [], [], []
    brng = random.Random(7)
    building_truth = {}
    for n in range(1, 31):
        if n in VACANT:
            continue
        g = building_for(n, brng)
        if n == 10:  # straddles P-010 / P-011 boundary
            p = parcel_geom(n)
            g = box(p.bounds[2] - 8, p.bounds[1] + 10, p.bounds[2] + 7, p.bounds[1] + 24)
        bid_a, bid_b = f"MSB-{n:03d}", f"osm-{1000 + n}"
        building_truth[pid(n)] = {"a": bid_a if n not in ONLY_B else None, "b": bid_b if n not in ONLY_A else None,
                                  "parcel": pid(n) if n != 10 else [pid(10), pid(11)]}
        if n not in ONLY_B:
            a_rows.append({"bldg_id": bid_a, "confidence": round(brng.uniform(0.8, 0.99), 3), "data_class": SYN,
                           "geometry": g})
        if n not in ONLY_A:
            gb = affinity.translate(jitter(g, brng, 0.4), 1.0, -0.5)
            b_rows.append({"osm_id": bid_b, "building": "yes", "building:levels": brng.choice([1, 2, 3, 4]),
                           "data_class": SYN, "geometry": gb})
            if n not in ONLY_B:
                pairs.append([bid_a, bid_b])
    a_rows.append(dict(a_rows[3]))  # duplicate footprint in source A
    truth["buildings"] = {"pairs_a_b": pairs, "only_a": [f"MSB-{n:03d}" for n in sorted(ONLY_A)],
                          "only_b": [f"osm-{1000 + n}" for n in sorted(ONLY_B)], "duplicate_in_a": a_rows[3]["bldg_id"],
                          "by_parcel": building_truth, "crossing_building": "MSB-010"}
    gpd.GeoDataFrame(a_rows, crs=CRS_WORK).to_crs(4326).to_file(out / "buildings_source_a_v1.geojson", driver="GeoJSON")
    gpd.GeoDataFrame(b_rows, crs=CRS_WORK).to_file(out / "buildings_source_b.gpkg", layer="buildings", driver="GPKG")

    # v2 of source A: changes
    v2 = [dict(r) for r in a_rows[:-1]]  # drop the duplicate
    removed = {"MSB-002", "MSB-017"}
    v2 = [r for r in v2 if r["bldg_id"] not in removed]
    for r in v2:
        if r["bldg_id"] == "MSB-021":  # enlarged ~40%
            r["geometry"] = affinity.scale(r["geometry"], 1.4, 1.0, origin="center")
        if r["bldg_id"] == "MSB-024":  # small shift within positional uncertainty
            r["geometry"] = affinity.translate(r["geometry"], 0.8, 0.0)
        if r["bldg_id"] == "MSB-013":
            r["confidence"] = 0.5
    for n, tag in ((3, "MSB-N01"), (9, "MSB-N02")):  # new buildings on previously vacant parcels
        v2.append({"bldg_id": tag, "confidence": 0.9, "data_class": SYN, "geometry": building_for(n, brng)})
    gpd.GeoDataFrame(v2, crs=CRS_WORK).to_crs(4326).to_file(out / "buildings_source_a_v2.geojson", driver="GeoJSON")
    truth["changes"] = {"added": ["MSB-N01", "MSB-N02"], "removed": sorted(removed),
                        "geometry_modified": ["MSB-021"], "within_uncertainty": ["MSB-024"],
                        "attribute_changed": ["MSB-013"]}

    # ---------------- Property records (CSV) ----------------
    centroids = gpd.GeoSeries([parcel_geom(n).centroid for n in range(1, 31)], crs=CRS_WORK).to_crs(4326)
    recs = []
    for n in range(1, 31):
        if n in (6,):
            continue
        fmt = ["Plot No. {n}", "P-{n:03d}", "p{n}", "P {n:02d}"][n % 4]
        lu = LAND_USES[n % len(LAND_USES)]
        if n == 16:
            lu = "industrial"
        pt = centroids.iloc[n - 1]
        lon, lat = pt.x, pt.y
        if n == 23:  # point placed in the neighbouring parcel P-024
            q = centroids.iloc[23]
            lon, lat = q.x, q.y
        recs.append({"property_number": f"PROP-{1000 + n}", "plot_no": fmt.format(n=n),
                     "owner_name": f"Synthetic Owner {n:03d}", "land_use": lu, "area_sqm": PW * PH,
                     "latitude": f"{lat:.7f}", "longitude": f"{lon:.7f}", "data_class": SYN})
    recs.append({"property_number": "PROP-9001", "plot_no": "P-099", "owner_name": "Synthetic Owner 099",
                 "land_use": "residential", "area_sqm": 1200, "latitude": "", "longitude": "", "data_class": SYN})
    recs.append(dict(next(r for r in recs if r["property_number"] == "PROP-1016")))
    with open(out / "property_records.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)
    truth["records"] = {"links": {f"PROP-{1000 + n}": pid(n) for n in range(1, 31) if n not in (6,)},
                        "unmatched": ["PROP-9001"], "duplicate": "PROP-1016", "wrong_point": "PROP-1023"}

    # ---------------- Utility lines (with a dangling segment) ----------------
    lines = [
        LineString([(E0, N0 - 5), (E0 + 100, N0 - 5)]),
        LineString([(E0 + 100, N0 - 5), (E0 + 190, N0 - 5)]),
        LineString([(E0 + 100, N0 - 5), (E0 + 100, N0 - 40)]),
        LineString([(E0 + 50, N0 - 20), (E0 + 70, N0 - 30)]),  # disconnected component
        LineString([(E0 + 150, N0 - 30), (E0 + 150, N0 - 6.2)]),  # undershoot: stops 1.2 m short of S2
    ]
    gpd.GeoDataFrame({"segment_id": ["S1", "S2", "S3", "S4", "S5"], "data_class": [SYN] * 5}, geometry=lines,
                     crs=CRS_WORK).to_file(out / "utility_lines.gpkg", layer="lines", driver="GPKG")

    # ---------------- Study area ----------------
    sa = box(E0 - 50, N0 - 60, E0 + COLS * PW + ROAD + 50, N0 + ROWS * PH + 50)
    gpd.GeoDataFrame({"name": ["Synthetic study area"]}, geometry=[sa], crs=CRS_WORK).to_crs(4326).to_file(
        out / "study_area.geojson", driver="GeoJSON")

    # ---------------- Small synthetic orthophoto (0.5 m) for the vision pipeline ----------------
    write_raster(out, a_rows[:-1])

    # Shapefile bundle variants (complete + incomplete) for upload-validation tests
    import zipfile

    shp_dir = out / "_shp"
    shp_dir.mkdir(exist_ok=True)
    gdf_a.iloc[:5].to_file(shp_dir / "parcels_subset.shp")
    with zipfile.ZipFile(out / "parcels_subset_shapefile.zip", "w") as z:
        for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            p = shp_dir / f"parcels_subset{ext}"
            if p.exists():
                z.write(p, p.name)
    with zipfile.ZipFile(out / "parcels_missing_shx.zip", "w") as z:
        for ext in (".shp", ".dbf", ".prj"):
            z.write(shp_dir / f"parcels_subset{ext}", f"parcels_subset{ext}")
    with zipfile.ZipFile(out / "parcels_no_prj.zip", "w") as z:
        for ext in (".shp", ".shx", ".dbf"):
            z.write(shp_dir / f"parcels_subset{ext}", f"parcels_subset{ext}")
    for p in shp_dir.iterdir():
        p.unlink()
    shp_dir.rmdir()

    (out / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    (out / "README.md").write_text(README)
    print(f"fixtures written to {out}")


def write_raster(out: Path, buildings: list[dict]) -> None:
    import rasterio
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    res = 0.5
    minx, miny = E0 - 10, N0 - 10
    maxx, maxy = E0 + COLS * PW + ROAD + 10, N0 + ROWS * PH + 10
    w, h = int((maxx - minx) / res), int((maxy - miny) / res)
    transform = from_origin(minx, maxy, res, res)
    rng = np.random.default_rng(3)
    base = rng.normal(90, 12, (h, w)).clip(0, 255)
    mask = rasterize([(b["geometry"], 1) for b in buildings], out_shape=(h, w), transform=transform)
    roof = np.where(mask == 1, rng.normal(215, 10, (h, w)), base).clip(0, 255).astype("uint8")
    bands = np.stack([roof, (roof * 0.95).astype("uint8"), (roof * 0.9).astype("uint8")])
    profile = {"driver": "GTiff", "width": w, "height": h, "count": 3, "dtype": "uint8", "crs": CRS_WORK,
               "transform": transform, "compress": "deflate", "tiled": True, "blockxsize": 256, "blockysize": 256}
    with rasterio.open(out / "synthetic_orthophoto.tif", "w", **profile) as dst:
        dst.write(bands)
        dst.update_tags(SOURCE=SYN, DESCRIPTION="Synthetic 0.5 m image: bright rectangles = building roofs")


README = """# Synthetic fixture: `synthetic_pune`

**Every file in this directory is SYNTHETIC.** Geometries are a regular grid placed near Pune
(UTM 43N, EPSG:32643) only to exercise real CRS handling. They are not real parcels, buildings,
owners or government records and must never be presented as such. Owner names are placeholders.

Regenerate with `python backend/scripts/generate_fixtures.py` (deterministic, seeded).
`ground_truth.json` lists every deliberately introduced inconsistency and the expected matches,
and is used by the automated tests to score the algorithms.

| File | Format / CRS | Purpose |
|---|---|---|
| municipal_parcels.gpkg | GeoPackage, EPSG:32643 | parcel layer A (`plot_no`, `landuse`, `area`, ...) |
| revenue_parcels.geojson | GeoJSON, EPSG:4326 | parcel layer B (`parcel_id`, `land_use_type`, ...), ~1.5 m shift |
| buildings_source_a_v1/v2.geojson | GeoJSON, EPSG:4326 | footprints "A" (two versions for change detection) |
| buildings_source_b.gpkg | GeoPackage, EPSG:32643 | footprints "B" (OSM-like tags) |
| property_records.csv | CSV with lat/lon (CRS must be confirmed) | property register |
| utility_lines.gpkg | GeoPackage | small network with a dangling segment |
| study_area.geojson | GeoJSON | study-area boundary |
| synthetic_orthophoto.tif | GeoTIFF 0.5 m | image for the building-extraction pipeline test |
| parcels_*.zip | Shapefile bundles | complete, missing `.shx`, missing `.prj` |
"""

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "fixtures" / "synthetic_pune"))
    main(Path(ap.parse_args().out))
