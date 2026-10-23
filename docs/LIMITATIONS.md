# Known limitations and production-readiness checklist

## Data and validity

* **No real data was used.** Every dataset in this repository and every reported metric is synthetic.
  No official cadastral, revenue, ownership, ULPIN or survey record was accessed; all candidate source
  hosts were unreachable from the build environment (see [DATASETS.md](DATASETS.md)).
* Algorithm metrics in [TESTING.md](TESTING.md) are measured on a synthetic grid with known,
  deliberately simple errors. Real accuracy is unknown until evaluated on authorised, labelled local
  data with survey-grade reference geometry.
* Match scores are heuristic (weighted evidence); RF probabilities are uncalibrated. Neither is a
  probability of correctness, and neither is evidence of ownership or legal validity.
* Inferred building footprints (ML or threshold) are never legal parcel boundaries; the integrated
  layer is a derived analytical product.

## Functional gaps

* **Building segmentation:** no pretrained weights are bundled or verified; the ONNX backend is
  implemented and runs, but has not been exercised with a real model in this build (it was not tested
  end-to-end, as no model file was available). The threshold baseline is not ML.
* **External acquisition** connectors were tested only against mocked HTTP; none completed a real
  download here. The Microsoft index URL and format were verified from the project's README only.
  Copernicus: catalogue search only; authenticated downloads are not implemented.
* **LLM-assisted mapping** is not implemented (deterministic + similarity + profile only).
* **OGC:** OGC API - Features Core/GeoJSON (read-only) is implemented in the API; WMS/WFS/vector tiles
  are not. Large layers are paged GeoJSON; there is no tile pyramid, so very large datasets render
  slowly in the browser (feature requests are capped at 5,000 per layer).
* **Rasters** are stored and processed whole-file (bounded by `BHU_MAX_RASTER_PIXELS`); no COG
  conversion or windowed streaming from object storage.
* **Object storage:** only the local-filesystem backend exists (S3-compatible backend not implemented).
* **CRS:** datum transformation grids are not downloaded (PROJ network disabled); sub-metre accuracy
  for legacy Indian datums (e.g. Everest 1830 based) requires the appropriate transformation pipeline
  and grids, which must be configured and validated by a geodesist.
* **Topology:** gap detection uses interior rings of the parcel union bounded by an area range; gaps
  open to the outside of the block are not detected. No automatic snapping/edge matching is proposed
  for parcels (by policy).
* **Change detection** for imagery assumes co-registered images with the same band semantics; there is
  no radiometric normalisation or cloud masking.
* **Auth:** local accounts only (no SSO/OIDC, no MFA, no password reset flow, no token revocation).
  Rate limiting is per process.
* **Scale:** loading uses per-row inserts (fine for 10⁴–10⁵ features; use COPY for millions); matching
  candidate SQL is not partitioned for city-scale runs; no performance benchmarks were run.
* **Frontend:** desktop/tablet layouts implemented; not tested on real tablets; no i18n
  (English only); no offline mode.
* **Docker image:** the `apt-get install libexpat1` line in `backend/Dockerfile` could not run in the
  build environment (Debian mirrors blocked). The containers that were verified had the same library
  side-loaded from Ubuntu 22.04's package. The Dockerfile line is standard but unexecuted here.
  Base-image pulls hit Docker Hub rate limits; use authenticated pulls or a mirror in CI.

## Production-readiness checklist

| item | status |
|---|---|
| Migrations, health/readiness, structured logs, request ids | done |
| Authentication, RBAC, project isolation, legal approval gating | done (local accounts) |
| Append-only, tamper-evident audit | done |
| Upload validation, archive safety, size/feature/pixel limits, job timeouts | done |
| Backups: documented and restore-tested | done (manual procedure; schedule it) |
| Automated tests (unit, integration, API, UI, browser E2E) | done; CI workflow written but not yet run on GitHub |
| Dependency audit | done; dev-tooling Tailwind 3 findings accepted (see SECURITY.md) |
| TLS termination, domain, firewall | **to do** (deployment-specific) |
| SSO/OIDC, MFA, password policy, account lockout | **to do** |
| Shared rate limiting / WAF | **to do** |
| Licence for this repository | **to do** (owner decision) |
| Data-sharing agreements, DPIA / privacy review for ownership data | **to do** (organisational) |
| Real-data evaluation and acceptance thresholds with domain experts | **to do** |
| Pretrained building model selection, licence review and local evaluation | **to do** |
| Basemap tile service for production | **to do** |
| Monitoring/alerting (metrics, log aggregation), job backlog alerts | **to do** |
| Load/performance testing at city scale | **to do** |
| Penetration test / security review | **to do** |
