# Testing and observed results

Everything below was **executed** on 2026-10-23 in the build environment (Linux, Python 3.12.13,
Node 22.22, PostgreSQL 16.15 + PostGIS 3.4.2, GDAL 3.12 / GEOS 3.14 / PROJ 9.8 via wheels).
Nothing is extrapolated. All data is the synthetic fixture.

## Summary

| suite | command | result |
|---|---|---|
| Backend unit + integration + API (real PostGIS) | `cd backend && pytest --cov=app` | **96 passed**, 0 failed; line coverage **86 %** (`app/`) |
| Backend lint | `ruff check app scripts tests` | clean |
| Frontend component tests | `cd frontend && npm test` | **9 passed** (Vitest 4.1.12 + Testing Library) |
| Frontend typecheck / build | `npm run typecheck && npm run build` | clean / built |
| Browser E2E against local dev stack | `cd e2e && npx playwright test` | **1 passed** (full workflow, ~20 s) |
| Browser E2E against Docker Compose stack (nginx + API + worker + PostGIS) | same, `E2E_BASE_URL=http://127.0.0.1:8080` | **1 passed** |
| Demo seed through the real pipeline (local and in Docker) | `python scripts/seed.py --demo` | 17/17 jobs succeeded, 32 conflicts |
| Persistence across `docker compose down && up` | metrics + audit verify | identical counts; audit chain verified |
| Backup/restore | `pg_dump -Fc` → `pg_restore` into new DB | row counts and migration version matched |
| `pip-audit` (backend env) | | no known vulnerabilities |
| `npm audit --omit=dev` | | 0 vulnerabilities (after upgrades); dev tooling findings in [SECURITY.md](SECURITY.md) |

Not executed: the GitHub Actions workflow (`.github/workflows/ci.yml`) has not run on GitHub yet; the
Dockerfile line `apt-get install libexpat1` could not run here because Debian mirrors were blocked
(the verified container used the same library side-loaded from Ubuntu 22.04's package; see
[LIMITATIONS.md](LIMITATIONS.md)); live downloads from any external dataset (all hosts blocked).

## What the backend tests cover

* `test_unit_core.py`: geometry repair classification (bow-tie, self-touching ring, zero-area spike,
  overlapping multipolygon), sliver compactness, working-CRS validation, UTM suggestions for Indian
  cities, area-of-use check, identifier normalisation/similarity, mapping aliases/ambiguity/project
  aliases/reuse/type compatibility, value coercion, tolerance widening, missing-evidence
  redistribution, building→parcel not relying on IoU, relationship cardinalities, ML feature
  validation, Bing quadkey reference value, CORS env parsing.
* `test_ingest.py`: path traversal, absolute paths, symlinks, zip bomb, member limits, corrupt zip,
  shapefile missing `.shx`, missing `.prj` (CRS not guessed), multiple shapefiles, malformed and empty
  GeoJSON, unsupported extension, CSV CRS suggestion, non-UTF-8 CSV, raster inspection.
* `test_api.py`: health/readiness, request-id propagation, 401 (no/invalid token, wrong password),
  403 (roles, membership caps), 404 for other users' projects, structured 422s, 413/415 upload limits,
  missing-CRS workflow with mandatory justification, ambiguous mapping confirmation and target clash,
  dependency-safe delete vs archive, match-run validation, OGC API Features, DB-derived metrics, job
  failure → retry, study area/CRS suggestion, personal-data redaction.
* `test_end_to_end.py`: the 12-step acceptance scenario via HTTP (create project; upload two
  inconsistent parcel layers + CSV; inspect schema/CRS; refuse CSV processing without CRS; preprocess;
  match; detect all fixture conflicts; explanations; conflict detail with map geometry; role- and
  legal-gated approval; audit chain; append-only enforcement; GeoPackage and GeoJSON export reloaded
  with GDAL and validated; idempotent re-upload/re-process/re-detection; building and record metrics;
  building on no parcel).
* `test_analysis.py`: vector change detection vs truth; image change detection vs truth (two rendered
  dated images); ML insufficient-data refusal, training on synthetic labels, baseline comparison,
  admin-only activation, RF match run; model checksum tamper detection; building-extraction pipeline
  and review; tile-seam deduplication; utility network topology; optimistic-concurrency conflict;
  correction reject/defer/stale-version via API; acquisition connectors with mocked HTTP (tile
  filtering + clipping, unavailable, no coverage, size refusal, Range resume), restricted sources
  reported not faked, live probe reports unreachable honestly.

## Algorithm evaluation on the synthetic fixture

`python scripts/evaluate_fixture.py --project <demo project id>`. Raw output:
[fixture_evaluation.json](fixture_evaluation.json).

| algorithm | precision | recall | F1 | notes |
|---|---|---|---|---|
| parcel ↔ parcel matching (`match` class) | 1.000 | 0.963 (26/27) | 0.981 | the miss is P-007, the deliberately invalid bow-tie: its repaired geometry covers half the parcel (IoU 0.43) so it is scored `possible` (0.69) and routed to review alongside `invalid_geometry` and `source_disagreement` conflicts |
| building ↔ building matching | 1.000 | 1.000 (22/22) | 1.000 | 1.5 m-shifted, jittered second source |
| property record → parcel linking (`match` class) | 1.000 | 0.964 (27/28) | 0.982 | the miss is PROP-1023, whose point lies 15 m outside its referenced parcel: scored `possible` and raised as a location conflict, by design |
| building → parcel assignment | 1.000 | 1.000 (23/23) | 1.000 | straddling building evaluated separately (raised as `building_crosses_parcel`, 53 % / 47 %) |
| conflict detection (introduced issues) | — | 1.000 (17/17) | — | 32 conflicts raised in total; every one was inspected and traces to an introduced issue or its direct consequence (e.g. the bow-tie also yields an area discrepancy and a source disagreement) |
| vector change detection: added / removed / modified | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 | | the 0.8 m shift within ~1 m positional uncertainty is **not** reported as modified |
| image change detection | — | all 4 added/removed buildings covered by a change region | — | unchanged building not flagged; synthetic 0.5 m images |

**Random Forest (synthetic labels)**, from `test_ml_refuses_without_labels_then_trains_on_synthetic_labels`
(`pytest -s` prints these): 146 labelled candidate pairs from a wide-tolerance building run (22 match,
124 non-match), split by 100 m spatial cells into 75 training / 71 held-out pairs. The fixture area
spans only **4** such cells, so the grouped split is coarse. Held-out confusion matrix `[[60, 0], [0, 11]]`:
precision 1.0, recall 1.0, F1 1.0. The deterministic baseline on the same 71 rows scores identically.
The synthetic fixture is too easy to separate the two; this validates the training/evaluation
machinery, not real-world performance.

These numbers are measured on synthetic data with deliberately simple geometry (rectangular parcels,
known shifts). They are **not** estimates of accuracy on real Indian cadastral data, which needs
labelled real records and survey-grade reference data.

## Defects found by the tests and fixed during the build

* reprocessing changed feature ids (stale conflicts) → id-stable upsert;
* `ST_Union` failing on invalid source geometry in conflict display → `ST_Collect`;
* project owners without reviewer role could not manage members;
* `LabeledInput` dropped react-hook-form refs (forms silently submitted empty values) → `forwardRef`;
* labels not associated with controls (accessibility) → automatic `Field` id wiring;
* the UI hid the legal acknowledgement for `legal_no_auto` corrections under non-legal conflicts;
* comma-separated `BHU_CORS_ORIGINS` crashed startup in containers;
* rasterio needs system `libexpat1` in slim images;
* seed script race with a concurrent worker; a recursive call introduced by an edit;
* misleading "changes area by 100 %" for zero-area bow-ties; "0.0 m away" for distant record points;
* every network terminal flagged as dangling → near-miss (undershoot/overshoot) semantics;
* buildings on no parcel raised no conflict → new review item.
