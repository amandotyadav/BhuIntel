"""Registry of candidate data sources, with honest access status.

Facts recorded here were checked on 2026-10-23 where noted. Sources needing authorisation are never
accessed automatically; for them the platform provides an import interface plus documentation of the
files and permissions required (docs/DATASETS.md).
"""

SOURCES: dict[str, dict] = {
    "microsoft_buildings": {
        "name": "Microsoft Global ML Building Footprints",
        "provider": "Microsoft",
        "kind": "buildings",
        "access": "open",
        "automated": True,
        "license": "CDLA-Permissive-2.0",
        "license_url": "https://cdla.dev/permissive-2-0/",
        "attribution": "Building footprints (c) Microsoft, licensed under CDLA Permissive 2.0",
        "homepage": "https://github.com/microsoft/GlobalMLBuildingFootprints",
        # README (checked 2026-10-23) lists the index at this URL; previous host
        # minedbuildings.z5.web.core.windows.net is kept as a fallback.
        "index_urls": [
            "https://bfppub.blob.core.windows.net/%24web/2026-08-13/dataset-links.csv",
            "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv",
        ],
        "format": "Zoom-9 quadkey partitions, gzipped GeoJSON-lines (.csv.gz), EPSG:4326",
        "notes": "Machine-learning derived footprints (~1% false positive rate per README). Not authoritative; "
                 "never a legal boundary.",
    },
    "osm_overpass": {
        "name": "OpenStreetMap buildings via Overpass API",
        "provider": "OpenStreetMap contributors",
        "kind": "buildings",
        "access": "open",
        "automated": True,
        "license": "ODbL-1.0",
        "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution": "(c) OpenStreetMap contributors, ODbL 1.0",
        "homepage": "https://www.openstreetmap.org/",
        "endpoint": "https://overpass-api.de/api/interpreter",
        "format": "Overpass JSON -> polygons, EPSG:4326",
        "notes": "Community data; completeness varies. Respect the Overpass usage policy (small areas, low frequency).",
        "max_area_km2": 25.0,
    },
    "geofabrik": {
        "name": "Geofabrik OpenStreetMap extracts (India)",
        "provider": "Geofabrik GmbH / OpenStreetMap contributors",
        "kind": "buildings",
        "access": "open",
        "automated": False,
        "license": "ODbL-1.0",
        "attribution": "(c) OpenStreetMap contributors, ODbL 1.0",
        "homepage": "https://download.geofabrik.de/asia/india.html",
        "notes": "Regional extracts are large; download the relevant zone manually, filter buildings with "
                 "ogr2ogr/osmium, then upload the GeoPackage. Not automated to avoid whole-region downloads.",
    },
    "openbuildingmap": {
        "name": "OpenBuildingMap",
        "provider": "OpenBuildingMap",
        "kind": "buildings",
        "access": "unverified",
        "automated": False,
        "license": "unverified - check https://www.openbuildingmap.org/ before use",
        "homepage": "https://www.openbuildingmap.org/",
        "notes": "Access method and licence could not be verified from this environment (host unreachable). "
                 "Import manually after verifying terms.",
    },
    "copernicus": {
        "name": "Copernicus Data Space Ecosystem (Sentinel-2 etc.)",
        "provider": "European Union / ESA",
        "kind": "imagery",
        "access": "credentials",
        "automated": True,
        "license": "Copernicus Sentinel data terms (free, full and open; attribution required)",
        "attribution": "Contains modified Copernicus Sentinel data [year]",
        "homepage": "https://dataspace.copernicus.eu/",
        "catalogue": "https://catalogue.dataspace.copernicus.eu/odata/v1/Products",
        "notes": "Catalogue search is implemented; downloads require BHU_COPERNICUS_CLIENT_ID/SECRET. Sentinel-2 "
                 "(10 m) is far too coarse to delineate urban parcels or individual buildings.",
    },
    "naksha": {
        "name": "NAKSHA programme (DoLR) urban land survey",
        "provider": "Department of Land Resources, Government of India",
        "kind": "parcels",
        "access": "authorization_required",
        "automated": False,
        "license": "Government data - terms set by the issuing authority",
        "homepage": "https://www.nakshauat.dolr.gov.in/NakshaUat/index.html",
        "notes": "Cadastral/ownership records are not openly downloadable. Obtain authorised exports "
                 "(GeoPackage/Shapefile with documented CRS and parcel IDs/ULPIN) and import them as a parcels dataset.",
    },
    "survey_of_india": {
        "name": "Survey of India",
        "provider": "Survey of India",
        "kind": "parcels",
        "access": "authorization_required",
        "automated": False,
        "license": "National Geospatial Policy 2022 / product-specific terms",
        "homepage": "https://surveyofindia.gov.in/",
        "notes": "Large-scale maps, orthophotos and control points require registration/approval. Import "
                 "authorised files manually; record control-point accuracy as positional_accuracy_m.",
    },
    "bhuvan": {
        "name": "ISRO Bhuvan",
        "provider": "NRSC / ISRO",
        "kind": "imagery",
        "access": "authorization_required",
        "automated": False,
        "license": "Bhuvan terms of use",
        "homepage": "https://bhuvan.nrsc.gov.in/",
        "notes": "Thematic layers and some imagery are viewable via OGC services subject to terms; bulk "
                 "download generally requires registration. Import authorised GeoTIFFs manually.",
    },
    "bhoonidhi": {
        "name": "NRSC Bhoonidhi",
        "provider": "NRSC / ISRO",
        "kind": "imagery",
        "access": "authorization_required",
        "automated": False,
        "license": "NRSC data policy",
        "homepage": "https://bhoonidhi.nrsc.gov.in/",
        "notes": "Satellite data ordering requires a registered account; high-resolution products may be "
                 "restricted. Import delivered GeoTIFF/COG files manually.",
    },
}
