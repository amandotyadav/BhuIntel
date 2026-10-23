# API overview

The authoritative contract is the OpenAPI document generated from the FastAPI routes:
`GET /openapi.json`, interactive at `/docs`. All endpoints except `/health`, `/ready` and
`POST /api/v1/auth/token` require `Authorization: Bearer <token>`.

Errors are always `{"error": {"code", "message", "details", "request_id"}}`. Send `X-Request-ID` to
correlate logs; it is echoed back. Long-running work returns `202` with a job object; poll
`GET /api/v1/projects/{pid}/jobs/{job_id}`.

| area | endpoints |
|---|---|
| health | `GET /health`, `GET /ready` (DB, PostGIS, migration, storage) |
| auth & users | `POST /api/v1/auth/token` (OAuth2 password form), `GET /auth/me`, `GET/POST /users`, `PATCH /users/{id}`, `GET /users/directory` |
| projects | `GET/POST /projects`, `GET/PATCH /projects/{pid}`, `GET/PUT /members`, `GET/POST /study-areas`, `GET /suggest-crs`, `GET /canonical-fields`, `POST /aliases`, `GET /metrics` |
| datasets | `GET/POST /datasets`, `GET/PATCH/DELETE /datasets/{id}` (`?mode=archive|delete`), `GET/POST /datasets/{id}/versions` (multipart upload), `GET /versions/{vid}`, `GET /versions/{vid}/download`, `POST /versions/{vid}/crs`, `POST /versions/{vid}/csv-options`, `POST /versions/{vid}/process`, `GET /versions/{vid}/features?bbox=&limit=&offset=`, `GET /features/{fid}` |
| mapping | `GET /datasets/{id}/mappings`, `POST /datasets/{id}/mappings/suggest`, `PATCH /mappings/{mid}` |
| matching | `POST/GET /match-runs`, `GET /match-runs/{rid}`, `GET /match-runs/{rid}/candidates`, `POST /match-candidates/{cid}/decision`, `PUT /match-candidates/{cid}/label` |
| topology & change | `POST /topology`, `POST/GET /changes` |
| conflicts & review | `GET /conflicts?status=&category=&severity=&bbox=`, `GET /conflicts/{id}`, `POST /conflicts/{id}/decision`, `GET /corrections`, `POST /corrections/{id}/decision`, `GET /review/queue`, `GET /decisions`, `GET /audit`, `GET /audit/verify` |
| ML | `GET /ml/schema`, `POST /ml/train`, `GET /ml/models`, `POST /ml/models/{id}/activate`, `GET /ml/labels.csv`, `POST /ml/labels/import` |
| vision | `POST /vision/extract`, `GET /vision/predictions`, `POST /vision/predictions/{fid}/decision` |
| integration & export | `POST /integration/build`, `GET /integrated`, `GET /integrated/{id}/history`, `POST /exports`, `GET /exports/{job_id}/download` |
| acquisition | `GET /acquisition/sources`, `POST /acquisition`, `GET /acquisition/manifests` |
| jobs | `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/retry` |
| OGC API - Features | `GET /projects/{pid}/ogc`, `/ogc/conformance`, `/ogc/collections`, `/ogc/collections/{parcels|buildings}`, `/ogc/collections/{id}/items?bbox=&limit=&offset=` (Core + GeoJSON conformance classes; authentication required) |

(Paths in the table after the first rows are relative to `/api/v1/projects/{pid}`.)

## Decision payload

```json
{"decision": "approve|reject|defer|comment", "comment": "...", "acknowledge_legal": false, "expected_version": 3}
```

* `403 legal_approval_requires_admin`, `422 legal_acknowledgement_required`: legally sensitive item.
* `409 version_conflict`: the integrated feature changed since the correction was prepared.

## Example (curl)

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/token -d 'username=admin@example.org&password=...' | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" -F file=@parcels.gpkg \
  localhost:8000/api/v1/projects/1/datasets/2/versions | jq .version.status
curl -s -H "Authorization: Bearer $TOKEN" -X POST localhost:8000/api/v1/projects/1/versions/5/process -H 'Content-Type: application/json' -d '{}'
```
