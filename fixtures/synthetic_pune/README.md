# Synthetic fixture: `synthetic_pune`

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
