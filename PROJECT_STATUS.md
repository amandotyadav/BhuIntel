# Project status

_Last updated: 2026-10-23 (branch `claude/hopeful-thompson-s3xcmg`)._

## Completed

* **Phase 1, inspection:** empty repository; Python 3.12, Node 22, PostgreSQL 16 (PostGIS 3.4 installed),
  Docker available; PyPI/npm reachable; all geospatial data hosts and model hubs blocked by egress policy.
* **Phase 2, architecture:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/ALGORITHMS.md](docs/ALGORITHMS.md),
  [docs/DATASETS.md](docs/DATASETS.md), [docs/API.md](docs/API.md).
* **Phase 3, backend foundation:** config, DB, Alembic schema (GiST indexes, append-only triggers),
  auth/RBAC, storage, uploads, inspection, DB-backed job queue + worker.
* **Phase 4, GIS pipeline:** CRS policy, reprojection, repair policy, attribute mapping, normalisation,
  dedup, deterministic matching (4 match types), topology checks.
* **Phase 5, conflicts & AI:** conflict engine (18 categories), scoring/priority, review & corrections,
  integration with versioned history, Random Forest training/inference, vision pipeline (ONNX +
  non-ML baseline), vector and image change detection, acquisition connectors and manifests.
* **Phase 6, frontend:** React/Leaflet UI for every module, wired to the real API.
* **Phase 7, end-to-end:** API-level acceptance scenario, Playwright browser workflow (local and Docker),
  synthetic-fixture evaluation script.
* **Phase 8, security & deployment:** Dockerfiles, compose, seed script, backup/restore (tested),
  dependency audits, docs.

## Test results (see [docs/TESTING.md](docs/TESTING.md))

Backend 96 passed (86 % coverage) · frontend 9 passed · Playwright E2E passed (dev and Docker stacks) ·
ruff/tsc clean · demo seed 17/17 jobs · conflict recall 17/17 on the fixture.

## Next tasks (suggested order)

1. Obtain authorised data for a pilot study area; register it with licence/accuracy metadata; run the
   pipeline; build a labelled evaluation set with domain experts; set acceptance thresholds.
2. Select and license-review a pretrained building segmentation model; export to ONNX; evaluate on local tiles.
3. Run the CI workflow on GitHub; add authenticated Docker Hub pulls or a mirror.
4. SSO/OIDC + MFA; TLS reverse proxy; shared rate limiting; monitoring/alerting.
5. Performance: COPY-based loading, vector tiles for large layers, partitioned matching.
6. Tailwind 4 migration (clears remaining dev-tooling audit findings).
7. Choose a licence for the repository.

## Outstanding issues

See [docs/LIMITATIONS.md](docs/LIMITATIONS.md). Nothing is known to be broken; the main risks are the
absence of real-data validation and the unexercised ONNX/real-acquisition paths.
