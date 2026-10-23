# syntax=docker/dockerfile:1.7
# Overridable for registry mirrors, e.g. --build-arg PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.12-slim-bookworm
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE} AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# GDAL/PROJ/GEOS come bundled in the pyogrio/rasterio/shapely/pyproj wheels; no system GDAL needed.
# The rasterio wheel links against the system libexpat, which slim images do not include.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
# Optional BuildKit secret "ca" lets builds run behind a TLS-intercepting proxy (see docs/DEPLOYMENT.md).
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --upgrade pip && \
    python -c "import tomllib;d=tomllib.load(open('pyproject.toml','rb'));print('\n'.join(d['project']['dependencies']))" > /tmp/req.txt && \
    pip install -r /tmp/req.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN useradd --system --uid 10001 --home /app bhu && mkdir -p /data/storage && chown -R bhu /data /app \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
USER bhu
ENV BHU_STORAGE_ROOT=/data/storage
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
