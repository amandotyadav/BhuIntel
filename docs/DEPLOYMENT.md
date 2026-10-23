# Deployment, operations and troubleshooting

## Docker Compose (single host)

```bash
cp .env.example .env     # set POSTGRES_PASSWORD, BHU_SECRET_KEY (>= 32 chars), BHU_CORS_ORIGINS, BHU_BOOTSTRAP_ADMIN_PASSWORD
docker compose up -d --build
docker compose exec api python scripts/seed.py      # bootstrap admin (first run only)
```

Services: `db` (postgis/postgis:16-3.4, volume `pgdata`), `api` (runs `alembic upgrade head` on start,
healthcheck `/health`), `worker` (`python -m app.worker`), `web` (nginx serving the SPA, proxying
`/api`, `/health`, `/ready`, `/docs`). Data persists in volumes `pgdata` and `storage`. Ports bind to
`127.0.0.1` by default. Put a TLS-terminating reverse proxy in front for remote access.

Scaling: run more workers with `docker compose up -d --scale worker=3` (jobs are claimed with
`SKIP LOCKED`). The API's in-memory rate limiter is per process; with several API replicas, enforce
rate limits at the reverse proxy as well.

### Production configuration checklist

* `BHU_ENV=production` (startup fails if the default secret is still set).
* Strong `BHU_SECRET_KEY` and `POSTGRES_PASSWORD`; keep `.env` out of version control.
* `BHU_CORS_ORIGINS` = the exact UI origin(s).
* Own basemap tile service (`VITE_TILE_URL` build arg); the public OSM server is not for production.
* Size limits: `BHU_MAX_UPLOAD_MB` (also nginx `client_max_body_size`), `BHU_MAX_FEATURES_PER_DATASET`,
  `BHU_MAX_RASTER_PIXELS`, `BHU_JOB_TIMEOUT_SECONDS`; container memory/CPU limits in compose.
* Backups (below) scheduled and restore-tested.

### Building behind a TLS-intercepting proxy

Both Dockerfiles accept an optional BuildKit secret `ca` (PEM bundle) used only during dependency
installation, and base images can be overridden for registry mirrors:

```bash
docker build --secret id=ca,src=/path/to/corporate-ca.pem \
  --build-arg PYTHON_IMAGE=registry.example.org/library/python:3.12-slim-bookworm backend
```

## Backup and restore

Verified on this build (dump restored into a fresh database; row counts and migration matched).

```bash
# database (custom format)
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > bhuintel.dump
# uploaded files, models and exports
docker run --rm -v bhuintel_storage:/data:ro -v "$PWD":/backup alpine:3 tar czf /backup/storage.tgz -C /data .

# restore into an empty database / volume
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" bhuintel_restored && pg_restore -U "$POSTGRES_USER" -d bhuintel_restored --no-owner' < bhuintel.dump
docker run --rm -v bhuintel_storage:/data -v "$PWD":/backup alpine:3 tar xzf /backup/storage.tgz -C /data
```

Back up the database and the storage volume together: dataset versions and model versions reference
files in storage by key, and model artefacts are verified by SHA-256 before loading. Restores insert
rows, so the append-only triggers do not interfere.

## Migrations

`alembic upgrade head` (the API container runs it when `BHU_RUN_MIGRATIONS=1`). Create new revisions
with `alembic revision --autogenerate -m "..."` and review them, especially spatial indexes and
triggers.

## Observability

* JSON logs to stdout with `request_id`, method, path, status and latency; job start/finish logs from
  the worker. Request bodies, tokens, passwords and owner names are never logged.
* `/health` (liveness) and `/ready` (DB + PostGIS + migration + writable storage, 503 otherwise).
* Job state, progress, errors and attempts are visible in the UI (Jobs) and `GET /jobs`.

## Troubleshooting

| symptom | cause / fix |
|---|---|
| `ImportError: libexpat.so.1` when processing rasters | the rasterio wheel needs system `libexpat1` (installed by the Dockerfile; install it on bare hosts) |
| API container exits with `error parsing value for field "cors_origins"` | fixed: `BHU_CORS_ORIGINS` is comma-separated (pydantic `NoDecode`); upgrade to this version |
| upload rejected `shapefile_incomplete` | zip must contain `.shp`, `.shx`, `.dbf` (and `.prj` for CRS) for exactly one layer |
| version stuck in `needs_crs` | the file has no CRS (missing `.prj`, CSV); confirm it with evidence in the Datasets page |
| match run `409 not_processed` | process both versions first |
| job `failed` | read `error` in Jobs; fix the input and use Retry; stale `running` jobs are re-queued automatically when a worker restarts |
| `429 Too Many Requests` from Docker Hub during build | anonymous pull limit; retry later, `docker login`, or use a mirror via the `*_IMAGE` build args |
| map has no basemap | `VITE_TILE_URL=none`, or the tile host is unreachable/blocked; overlays still work |
| `legal_acknowledgement_required` | approve legally sensitive items as admin and tick the acknowledgement |
