"""Dataset acquisition: reachability checks, bounded/resumable downloads, coverage checks, clipping and
manifests. Never bypasses authentication or licence terms; unavailable sources are reported, never
substituted with fake data."""

import csv
import gzip
import hashlib
import io
import json
import math
import time
from pathlib import Path

import geopandas as gpd
import httpx
from shapely.geometry import shape

from app.config import get_settings
from app.services.acquisition.sources import SOURCES


class AcquisitionError(Exception):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    s = get_settings()
    return httpx.Client(timeout=s.acquisition_timeout_seconds, follow_redirects=True, transport=transport,
                        headers={"User-Agent": "BhuIntel/0.1 (land-record harmonization; contact: project admin)"})


def check_reachable(url: str, http: httpx.Client) -> tuple[bool, str]:
    try:
        r = http.head(url)
        if r.status_code == 405:
            r = http.get(url, headers={"Range": "bytes=0-0"})
        return r.status_code < 400, f"HTTP {r.status_code}"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"


def download(url: str, dest: Path, http: httpx.Client, max_bytes: int, retries: int = 4) -> dict:
    """Download with size cap, retries (exponential backoff) and HTTP Range resume of partial files."""
    part = dest.with_suffix(dest.suffix + ".part")
    attempt = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with http.stream("GET", url, headers=headers) as r:
                if r.status_code == 416:  # already complete
                    break
                if r.status_code not in (200, 206):
                    raise AcquisitionError(f"HTTP {r.status_code} for {url}", "failed")
                mode = "ab" if r.status_code == 206 else "wb"
                total = have if mode == "ab" else 0
                with open(part, mode) as fh:
                    for chunk in r.iter_bytes(1024 * 256):
                        total += len(chunk)
                        if total > max_bytes:
                            raise AcquisitionError(f"Download exceeds limit of {max_bytes // 1_000_000} MB; "
                                                   "narrow the study area.", "too_large")
                        fh.write(chunk)
            break
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            attempt += 1
            if attempt > retries:
                raise AcquisitionError(f"Download failed after {retries} retries: {exc}", "failed") from None
            time.sleep(min(2 ** attempt, 16))
    part.replace(dest)
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    return {"url": url, "path": str(dest), "size": dest.stat().st_size, "sha256": h}


# ---------------- Microsoft building footprints (zoom-9 quadkeys) ----------------

def tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    s = math.sin(math.radians(lat))
    y = int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def quadkey(x: int, y: int, z: int) -> str:
    out = []
    for i in range(z, 0, -1):
        d = 0
        mask = 1 << (i - 1)
        if x & mask:
            d += 1
        if y & mask:
            d += 2
        out.append(str(d))
    return "".join(out)


def quadkeys_for_bbox(bbox: list[float], z: int = 9) -> list[str]:
    minx, miny, maxx, maxy = bbox
    x0, y1 = tile_xy(minx, miny, z)
    x1, y0 = tile_xy(maxx, maxy, z)
    return [quadkey(x, y, z) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def acquire_microsoft(study_area, location: str, work: Path, http: httpx.Client) -> dict:
    s = get_settings()
    src = SOURCES["microsoft_buildings"]
    bbox = list(study_area.bounds)
    qks = quadkeys_for_bbox(bbox, 9)
    if len(qks) > s.acquisition_max_tiles:
        raise AcquisitionError(f"Study area needs {len(qks)} tiles (> limit {s.acquisition_max_tiles}).", "too_large")
    index_text, index_url, reach = None, None, []
    for u in src["index_urls"]:
        ok, msg = check_reachable(u, http)
        reach.append({"url": u, "reachable": ok, "detail": msg})
        if ok:
            r = http.get(u)
            if r.status_code == 200:
                index_text, index_url = r.text, u
                break
    if index_text is None:
        raise AcquisitionError("Microsoft footprint index unreachable: " + json.dumps(reach), "unavailable")
    rows = [r for r in csv.DictReader(io.StringIO(index_text)) if r.get("QuadKey") in qks
            and (not location or r.get("Location") == location)]
    if not rows:
        raise AcquisitionError(f"No Microsoft footprint tiles for quadkeys {qks} (location={location!r}).",
                               "no_coverage")
    files, feats = [], []
    for r in rows:
        dest = work / f"ms_{r['Location']}_{r['QuadKey']}.csv.gz"
        info = download(r["Url"], dest, http, s.acquisition_max_download_mb * 1024 * 1024)
        try:
            with gzip.open(dest, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    g = shape(obj["geometry"])
                    if g.intersects(study_area):
                        feats.append({**(obj.get("properties") or {}), "ms_quadkey": r["QuadKey"], "geometry": g})
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            raise AcquisitionError(f"Tile {r['QuadKey']} failed integrity/format validation: {exc}", "failed") from None
        files.append({**info, "quadkey": r["QuadKey"], "location": r["Location"], "declared_size": r.get("Size")})
    if not feats:
        raise AcquisitionError("Tiles downloaded but contain no footprints inside the study area.", "no_coverage")
    gdf = gpd.GeoDataFrame(feats, crs=4326)
    out = work / "microsoft_buildings_clipped.gpkg"
    gdf.to_file(out, layer="buildings", driver="GPKG")
    return {"output": str(out), "feature_count": len(gdf), "files": files, "index_url": index_url,
            "quadkeys": qks, "extent": [float(v) for v in gdf.total_bounds], "crs": "EPSG:4326",
            "filter": "ST_Intersects with study area (geometries not clipped)"}


# ---------------- OpenStreetMap via Overpass ----------------

def acquire_osm(study_area, work: Path, http: httpx.Client) -> dict:
    src = SOURCES["osm_overpass"]
    minx, miny, maxx, maxy = study_area.bounds
    km2 = (maxx - minx) * 111.32 * math.cos(math.radians((miny + maxy) / 2)) * (maxy - miny) * 110.57
    if km2 > src["max_area_km2"]:
        raise AcquisitionError(f"Study area bbox is {km2:.1f} km² (> {src['max_area_km2']} km² Overpass limit "
                               "configured to respect the usage policy). Use a Geofabrik extract instead.", "too_large")
    q = (f"[out:json][timeout:120];(way[\"building\"]({miny},{minx},{maxy},{maxx}););out body geom;")
    try:
        r = http.post(src["endpoint"], data={"data": q})
    except httpx.HTTPError as exc:
        raise AcquisitionError(f"Overpass unreachable: {exc}", "unavailable") from None
    if r.status_code != 200:
        raise AcquisitionError(f"Overpass returned HTTP {r.status_code}", "unavailable")
    data = r.json()
    from shapely.geometry import Polygon

    feats = []
    for el in data.get("elements", []):
        geom = el.get("geometry")
        if el.get("type") != "way" or not geom or len(geom) < 4:
            continue
        poly = Polygon([(p["lon"], p["lat"]) for p in geom])
        if not poly.is_valid or not poly.intersects(study_area):
            continue
        tags = el.get("tags", {})
        feats.append({"osm_id": f"way/{el['id']}", "building": tags.get("building"),
                      "building:levels": tags.get("building:levels"), "geometry": poly})
    if not feats:
        raise AcquisitionError("Overpass returned no buildings inside the study area.", "no_coverage")
    gdf = gpd.GeoDataFrame(feats, crs=4326)
    out = work / "osm_buildings.gpkg"
    gdf.to_file(out, layer="buildings", driver="GPKG")
    raw = work / "overpass_response.json"
    raw.write_text(r.text)
    return {"output": str(out), "feature_count": len(gdf), "crs": "EPSG:4326",
            "files": [{"path": str(raw), "size": raw.stat().st_size,
                       "sha256": hashlib.sha256(raw.read_bytes()).hexdigest(), "url": src["endpoint"]}],
            "extent": [float(v) for v in gdf.total_bounds], "query": q}


# ---------------- Copernicus catalogue search ----------------

def search_copernicus(study_area, start: str, end: str, max_cloud: float, http: httpx.Client) -> dict:
    src = SOURCES["copernicus"]
    wkt = study_area.envelope.wkt
    flt = (f"Collection/Name eq 'SENTINEL-2' and OData.CSC.Intersects(area=geography'SRID=4326;{wkt}') and "
           f"ContentDate/Start gt {start}T00:00:00.000Z and ContentDate/Start lt {end}T00:00:00.000Z and "
           f"Attributes/OData.CSC.DoubleAttribute/any(att:att/Name eq 'cloudCover' and "
           f"att/OData.CSC.DoubleAttribute/Value le {max_cloud})")
    try:
        r = http.get(src["catalogue"], params={"$filter": flt, "$top": 20, "$orderby": "ContentDate/Start desc"})
    except httpx.HTTPError as exc:
        raise AcquisitionError(f"Copernicus catalogue unreachable: {exc}", "unavailable") from None
    if r.status_code != 200:
        raise AcquisitionError(f"Copernicus catalogue returned HTTP {r.status_code}", "unavailable")
    items = [{"id": p.get("Id"), "name": p.get("Name"), "date": (p.get("ContentDate") or {}).get("Start"),
              "online": p.get("Online")} for p in r.json().get("value", [])]
    return {"products": items, "resolution_note": "Sentinel-2 10 m bands: unsuitable for parcel-level delineation."}
