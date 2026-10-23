# Datasets, acquisition, attribution and licensing

## What was actually checked (2026-10-23)

The build environment's egress policy allowed PyPI, npm and GitHub raw content, but **blocked every
geospatial data host**. Reachability results from the build environment:

| source | URL probed | result from build environment |
|---|---|---|
| Microsoft Global ML Building Footprints index | `bfppub.blob.core.windows.net/$web/2026-08-13/dataset-links.csv` (and previous `minedbuildings.z5.web.core.windows.net`) | blocked (proxy 403) |
| Microsoft footprints README | `raw.githubusercontent.com/microsoft/GlobalMLBuildingFootprints/main/README.md` | **reachable**; used to verify licence, index location and format |
| Geofabrik | `download.geofabrik.de/asia/india.html` | blocked |
| OpenStreetMap / Overpass | `overpass-api.de` | blocked |
| OSM tiles | `tile.openstreetmap.org` | blocked (UI tested with `VITE_TILE_URL=none`) |
| OpenBuildingMap | `www.openbuildingmap.org` | blocked; licence/access **not verified** |
| Copernicus Data Space | `dataspace.copernicus.eu` | blocked |
| NAKSHA (DoLR) | `www.nakshauat.dolr.gov.in/NakshaUat/index.html` | blocked |
| Survey of India | `surveyofindia.gov.in` | blocked |
| ISRO Bhuvan | `bhuvan.nrsc.gov.in` | blocked |
| NRSC Bhoonidhi | `bhoonidhi.nrsc.gov.in` | blocked |
| Hugging Face (model weights) | `huggingface.co` | blocked |

Consequently **no real dataset was downloaded** and none is claimed. The acquisition connectors were
tested against mocked HTTP responses (which test our parsing, tiling, size limits, resume and clipping
logic, not data availability), and a live probe test verifies that unreachable sources are reported
as `unavailable` without substituting anything.

## Source registry (`backend/app/services/acquisition/sources.py`)

| key | access | automated | licence (as recorded) | notes |
|---|---|---|---|---|
| `microsoft_buildings` | open | yes | CDLA-Permissive-2.0 (verified in README 2026-10-23) | zoom-9 quadkey partitions, `.csv.gz` containing GeoJSON lines, EPSG:4326; ML-derived, ~1 % false-positive rate per README; never a legal boundary |
| `osm_overpass` | open | yes | ODbL-1.0, "© OpenStreetMap contributors" | community data; area capped at 25 km² bbox to respect Overpass usage policy |
| `geofabrik` | open | no | ODbL-1.0 | regional extracts are large; download/filter manually and upload |
| `openbuildingmap` | unverified | no | unverified | verify terms before use |
| `copernicus` | credentials | catalogue search only | Copernicus Sentinel data terms (attribution) | 10 m Sentinel-2 is unsuitable for parcel/building delineation |
| `naksha` | authorisation required | no | issuing authority's terms | import authorised exports manually |
| `survey_of_india` | authorisation required | no | NGP 2022 / product terms | record control-point accuracy as `positional_accuracy_m` |
| `bhuvan` | authorisation required | no | Bhuvan terms of use | import authorised GeoTIFFs manually |
| `bhoonidhi` | authorisation required | no | NRSC data policy | import delivered GeoTIFF/COG manually |

Every acquisition attempt (including refusals) produces an `acquisition_manifests` row: request,
status (`completed`, `unavailable`, `no_coverage`, `too_large`, `unauthorized`, `manual`, `failed`),
reachability, message, and for downloads the URL, size, SHA-256, quadkey and declared size of each
file. Successful downloads become a dataset version with provider, licence, attribution, source URL
and retrieval date filled in.

### Microsoft footprints connector

1. Compute the zoom-9 Bing quadkeys covering the study-area bbox; refuse if more than
   `BHU_ACQUISITION_MAX_TILES` (default 8) are needed (prevents country-scale downloads).
2. Fetch `dataset-links.csv` (current URL first, previous host as fallback) and select rows with the
   requested `Location` (default `India`) and those quadkeys.
3. Download each tile with retries (exponential backoff), HTTP Range resume of `.part` files and a
   size cap (`BHU_ACQUISITION_MAX_DOWNLOAD_MB`).
4. Validate gzip + JSON-lines integrity, keep footprints intersecting the study area (no clipping of
   geometry), write a GeoPackage, and register it as a dataset version.

## Data required from authorities (to replace the synthetic fixture)

For cadastral/revenue integration in a real study area you need, from the competent authority and
under a data-sharing agreement:

* parcel polygons (GeoPackage or zipped Shapefile **with `.prj`**), with parcel identifiers (survey /
  plot / CTS number and, where issued, ULPIN), and documentation of CRS, survey date and accuracy;
* property/assessment register (CSV) with property id, plot reference, land use and recorded area
  (owner names only if the purpose and legal basis permit; they are redacted for non-reviewers and
  never exported);
* optionally orthophotos (GeoTIFF/COG, ≤ 0.5 m) with acquisition date and licence.

Set each dataset's provenance, licence, attribution, reliability and positional accuracy on
registration; these drive tolerances, conflict resolution proposals and exported metadata.

## Synthetic fixture (`fixtures/synthetic_pune/`)

Generated deterministically by `backend/scripts/generate_fixtures.py`. It is a regular grid placed near
Pune (UTM 43N) **only** to exercise real CRS handling. It is not real land data. Owner names are
placeholders ("Synthetic Owner 001"). Every record carries `data_class = "SYNTHETIC - not an official
record"`, and datasets created from it are registered with `provenance = synthetic`, which the UI
labels on every screen.

Deliberate inconsistencies and expected outputs are listed in `ground_truth.json` (bow-tie parcel,
overlap, gap, sliver, duplicate, wrong recorded area, swapped identifier, land-use conflict, displaced
geometry, missing parcels in each layer, a building crossing parcels, source-only buildings, building
v1→v2 changes, unknown/duplicate/mis-located property records, a dangling utility segment).

## Attribution in exports

Exports include an `export_metadata` table (GeoPackage) or `metadata` member (GeoJSON) listing every
active dataset's name, kind, provenance, provider, licence, attribution and source URL, plus the
disclaimer that matches are not proof of ownership or legal validity.
