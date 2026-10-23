# BhuIntel: Geospatial Land-Record Harmonization Platform

BhuIntel ingests heterogeneous land-related geospatial datasets (parcel/cadastral layers, building
footprints, property registers, utility networks, imagery), standardises their coordinate systems and
attributes, detects spatial inconsistencies, matches related features, proposes corrections with
explainable scores, routes every uncertain or legally sensitive decision to a human reviewer, keeps
an append-only audit trail, and exports a validated integrated dataset.

> **Scope and honesty statement.** This is a working, tested system for technical demonstration
> that is designed to evolve into authorised land-administration workflows. It is **not**
> production-certified and **not** legally authoritative. Match scores are heuristic evidence
> scores (or uncalibrated model probabilities, labelled as such). They are never proof of
> ownership. All bundled data is **synthetic**: no official cadastral, revenue or ownership record
> was available to this build. What was implemented, tested and verified is listed in
> [docs/TESTING.md](docs/TESTING.md); known gaps are in [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## What it does

| Module | Implemented |
|---|---|
| Projects & datasets | Projects with study areas and a projected working CRS; dataset registration with provenance (real/synthetic/derived), licence, attribution, reliability, positional accuracy; versioned uploads (idempotent on file hash); archive/delete with dependency checks |
| Ingestion / ETL | GeoJSON, zipped Shapefile, GeoPackage, CSV (X/Y or WKT, or attribute-only), GeoParquet, GeoTIFF/COG, KML/FGB/GML; safe ZIP extraction; CRS never guessed (explicit, justified confirmation); reprojection; documented geometry-repair policy; dedup; PostGIS load with GiST indexes; id-stable re-processing |
| Attribute mapping | Alias rules, project aliases, reuse of approved mappings, string similarity, type compatibility and value profiles; ambiguous suggestions require explicit confirmation; audited |
| Spatial matching | PostGIS candidate generation; building↔building, parcel↔parcel, building→parcel (coverage-based, not IoU-only), record→parcel; 1:1 / 1:n / n:1 / n:m relationships; configurable tolerances widened by source accuracy; explanation for every candidate |
| ML | Random Forest with spatially grouped splits, refuses to train on insufficient labels, compares against the deterministic baseline on the same held-out rows, versioned and checksummed artefacts, validated inference |
| Computer vision | Tiled segmentation → georeferenced polygons, seam-safe merging, review workflow. ONNX backend for user-supplied pretrained models; a clearly labelled non-ML threshold baseline for pipeline testing (no model weights are bundled; see [docs/VISION.md](docs/VISION.md)) |
| Topology | Invalid/self-intersecting geometry, duplicates, parcel overlaps, enclosed gaps, slivers, unexpected geometry types, buildings across parcels, dangling/disconnected utility segments |
| Change detection | Version-to-version vector comparison with positional uncertainty ("uncertain" class), coverage-aware add/remove, attribute changes; image differencing for co-registered dated imagery |
| Conflicts & scoring | 18 conflict categories with evidence, source reliability/dates, proposed action, heuristic confidence, priority and legal-sensitivity flag; idempotent re-detection |
| Review & audit | Review queue, before/after geometry, source comparison, approve/reject/defer/comment; legal items require admin + explicit acknowledgement; append-only decisions and hash-chained audit log (DB triggers block UPDATE/DELETE) |
| Integration & sync | Derived integrated parcels/buildings with lineage, version history, optimistic concurrency (409 on stale corrections), retire-not-delete |
| APIs | 81 documented REST operations on 70 paths (OpenAPI at `/docs`), OGC API - Features (Core + GeoJSON) for the integrated layer, GeoJSON/GeoPackage export reloaded and validated after writing |
| Dataset acquisition | Microsoft Global ML Building Footprints (quadkey-tiled, bounded, resumable, clipped), OSM via Overpass (area-capped), Copernicus catalogue search; official Indian sources recorded as authorisation-required (never accessed automatically); manifests for every attempt |
| Web-GIS UI | React + Leaflet: projects, map with layer controls and distinct styles, dataset inspection, mapping, matching, review queue, change & imagery, ML models, integration/export, acquisition, jobs, audit |

## Screenshots (synthetic data)

| Map with layers and conflicts | Review with legal gating | Explainable match candidates |
|---|---|---|
| ![map](docs/images/map.png) | ![review](docs/images/review.png) | ![matching](docs/images/matching.png) |

## Quick start (Docker)

Requirements: Docker with Compose v2.

```bash
cp .env.example .env              # then set POSTGRES_PASSWORD, BHU_SECRET_KEY, BHU_BOOTSTRAP_ADMIN_PASSWORD
docker compose up -d --build      # db + api (runs migrations) + worker + web
docker compose exec api python scripts/seed.py                 # create the bootstrap admin
# optional synthetic demo project (fixtures are mounted read-only):
docker compose run --rm -v "$PWD/fixtures:/fixtures:ro" -e BHU_FIXTURES_DIR=/fixtures/synthetic_pune \
  api python scripts/seed.py --demo
```

Open http://localhost:8080 and sign in as `BHU_BOOTSTRAP_ADMIN_EMAIL` with your bootstrap password.
The demo also creates `analyst@`, `reviewer@` and `viewer@example.org` with the same password.
API docs: http://localhost:8000/docs. Readiness: http://localhost:8000/ready.

Basemap: the UI defaults to the public OpenStreetMap tile server, which is for light interactive use
under the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/). Configure
your own tile server with `VITE_TILE_URL` (build arg), or `none` to disable.

## Local development

Requirements: Python 3.12, Node 22, PostgreSQL 16 + PostGIS 3.4 (or `docker compose up -d db`).

```bash
# database
createdb bhuintel && createdb bhuintel_test         # role with CREATE EXTENSION rights (PostGIS)

# backend
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"                             # GDAL/PROJ/GEOS come bundled in the wheels
export BHU_DATABASE_URL=postgresql+psycopg://bhuintel:bhuintel@localhost:5432/bhuintel
alembic upgrade head
python scripts/seed.py --demo --password 'choose-a-password'
uvicorn app.main:app --reload --port 8000           # terminal 1
python -m app.worker                                # terminal 2 (or BHU_INLINE_WORKER=true for one process)

# frontend
cd frontend && npm ci && npm run dev                # http://localhost:5173 (proxies /api to :8000)
```

## Tests

```bash
cd backend && pytest                    # 96 tests, real PostGIS (BHU_TEST_DATABASE_URL, default bhuintel_test)
cd backend && pytest --cov=app          # coverage
cd frontend && npm test                 # Vitest + Testing Library
cd frontend && npm run typecheck && npm run build
cd e2e && npm ci && npx playwright install chromium && npx playwright test   # needs running API, worker, UI and a seeded admin
```

Results observed for this build are recorded in [docs/TESTING.md](docs/TESTING.md).

## Repository layout

```
backend/            FastAPI app, services, Alembic migrations, worker, tests, scripts
  app/api/          REST routers (auth, projects, datasets, analysis, reviews, OGC)
  app/services/     ingest, etl, crs, geometry, schema_mapping, matching, conflicts, topology,
                    change_detection, ml, vision, integration, review, export, audit, acquisition/
  scripts/          generate_fixtures.py (synthetic fixture), seed.py (users + demo)
frontend/           React + TypeScript + Vite + Tailwind + React-Leaflet + TanStack Query
e2e/                Playwright end-to-end workflow test
fixtures/           Small SYNTHETIC fixture with ground truth (no real records)
docs/               Architecture, algorithms, datasets, API, deployment, security, testing, limitations
```

## Documentation

* [Architecture & database schema](docs/ARCHITECTURE.md)
* [Matching, scoring, conflict and topology algorithms](docs/ALGORITHMS.md)
* [Datasets, acquisition, attribution and licensing](docs/DATASETS.md)
* [API overview](docs/API.md) (generated OpenAPI at `/docs`)
* [Deployment, backup/restore, troubleshooting](docs/DEPLOYMENT.md)
* [Security](docs/SECURITY.md)
* [Testing and observed results](docs/TESTING.md)
* [Building extraction models](docs/VISION.md)
* [Known limitations & production-readiness checklist](docs/LIMITATIONS.md)
* [Project status / hand-over notes](PROJECT_STATUS.md)

## Licensing

No licence file has been chosen for this repository yet; add one before distributing. Third-party
datasets keep their own licences (recorded per dataset; see [docs/DATASETS.md](docs/DATASETS.md)).
