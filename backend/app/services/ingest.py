"""File validation, safe archive extraction and dataset inspection (Extract stage of the ETL).

Supported inputs: GeoJSON, zipped Shapefile bundles, GeoPackage, CSV (with X/Y or WKT columns, or
attribute-only), GeoParquet, GeoTIFF/COG, and other GDAL/OGR vector formats (KML, FlatGeobuf, GML).
"""

import json
import math
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import CRS
from shapely import wkt as shapely_wkt

from app.config import get_settings

VECTOR_EXT = {
    ".geojson": "geojson",
    ".json": "geojson",
    ".gpkg": "gpkg",
    ".csv": "csv",
    ".parquet": "geoparquet",
    ".geoparquet": "geoparquet",
    ".kml": "ogr",
    ".fgb": "ogr",
    ".gml": "ogr",
}
RASTER_EXT = {".tif": "geotiff", ".tiff": "geotiff"}
SHAPEFILE_REQUIRED = {".shp", ".shx", ".dbf"}

X_COLUMNS = ["longitude", "lon", "lng", "long", "x", "easting", "x_coord", "xcoord"]
Y_COLUMNS = ["latitude", "lat", "y", "northing", "y_coord", "ycoord"]
WKT_COLUMNS = ["wkt", "geometry", "geom", "the_geom", "shape"]
NULL_TOKENS = {"", "na", "n/a", "null", "none", "-", "--", "nan", "nil"}


class IngestError(Exception):
    """Validation failure with an actionable, user-facing message."""

    def __init__(self, message: str, code: str = "invalid_file"):
        super().__init__(message)
        self.code = code


@dataclass
class Inspection:
    format: str
    kind: str  # vector | raster | table
    layer: str | None = None
    layers: list[str] = field(default_factory=list)
    crs: str | None = None
    crs_name: str | None = None
    crs_is_projected: bool | None = None
    crs_suggestion: str | None = None
    geometry_type: str | None = None
    feature_count: int = 0
    fields: dict[str, str] = field(default_factory=dict)
    field_profile: dict[str, dict] = field(default_factory=dict)
    extent_4326: list[float] | None = None
    csv_options: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raster: dict | None = None
    data_path: str | None = None  # path of the readable file (inside an extracted archive, if any)


def detect_format(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".zip":
        return "zip"
    if ext in VECTOR_EXT:
        return VECTOR_EXT[ext]
    if ext in RASTER_EXT:
        return RASTER_EXT[ext]
    raise IngestError(
        f"Unsupported file extension '{ext}'. Supported: .geojson/.json, .zip (Shapefile bundle), "
        ".gpkg, .csv, .parquet/.geoparquet, .tif/.tiff, .kml, .fgb, .gml",
        "unsupported_format",
    )


def safe_extract_zip(zip_path: Path, dest: Path) -> list[Path]:
    """Extract a ZIP archive defensively.

    Rejects: path traversal (``..``/absolute paths), symlinks, too many members, excessive total
    uncompressed size and suspicious compression ratios (zip bombs), encrypted members.
    """
    s = get_settings()
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise IngestError("The uploaded .zip file is corrupt or not a ZIP archive.", "bad_zip") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > s.max_zip_members:
            raise IngestError(f"Archive has {len(infos)} members; limit is {s.max_zip_members}.", "zip_limits")
        total = 0
        for info in infos:
            name = info.filename
            if info.flag_bits & 0x1:
                raise IngestError(f"Encrypted archive member '{name}' is not supported.", "zip_encrypted")
            if name.startswith(("/", "\\")) or ".." in Path(name.replace("\\", "/")).parts or ":" in name:
                raise IngestError(f"Unsafe path in archive: '{name}'.", "zip_unsafe_path")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise IngestError(f"Symbolic links are not allowed in archives: '{name}'.", "zip_unsafe_path")
            total += info.file_size
            if info.compress_size > 0 and info.file_size / info.compress_size > 200 and info.file_size > 10_000_000:
                raise IngestError(f"Suspicious compression ratio for '{name}' (possible zip bomb).", "zip_bomb")
        if total > s.max_zip_uncompressed_mb * 1024 * 1024:
            raise IngestError(
                f"Archive expands to {total / 1e6:.0f} MB; limit is {s.max_zip_uncompressed_mb} MB.", "zip_limits"
            )
        out: list[Path] = []
        for info in infos:
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest):
                raise IngestError(f"Unsafe path in archive: '{info.filename}'.", "zip_unsafe_path")
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with zf.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    written += len(chunk)
                    if written > info.file_size + 1024:  # header lied about size
                        raise IngestError("Archive member larger than declared size.", "zip_bomb")
                    dst.write(chunk)
            out.append(target)
        return out


def resolve_archive(zip_path: Path, extract_dir: Path) -> tuple[str, Path]:
    """Return (format, path_to_open) for a ZIP: shapefile bundle, or a single GeoPackage/GeoJSON inside."""
    files = [p for p in safe_extract_zip(zip_path, extract_dir) if not p.name.startswith(("._", "__MACOSX"))]
    files = [p for p in files if "__MACOSX" not in p.parts]
    shps = [p for p in files if p.suffix.lower() == ".shp"]
    if len(shps) > 1:
        raise IngestError(
            "Archive contains multiple shapefiles: "
            + ", ".join(p.name for p in shps)
            + ". Upload one shapefile bundle per dataset version.",
            "zip_multiple_layers",
        )
    if shps:
        shp = shps[0]
        stems = {p.suffix.lower() for p in files if p.stem == shp.stem and p.parent == shp.parent}
        missing = SHAPEFILE_REQUIRED - stems
        if missing:
            raise IngestError(
                f"Shapefile bundle '{shp.stem}' is missing required sidecar file(s): "
                + ", ".join(sorted(missing))
                + ". A shapefile needs .shp, .shx and .dbf (and .prj for the coordinate system).",
                "shapefile_incomplete",
            )
        return "shapefile", shp
    for p in files:
        ext = p.suffix.lower()
        if ext in (".gpkg", ".geojson", ".json", ".fgb", ".kml", ".csv"):
            return VECTOR_EXT.get(ext, "ogr"), p
        if ext in RASTER_EXT:
            return "geotiff", p
    raise IngestError("Archive contains no supported dataset (.shp bundle, .gpkg, .geojson, .tif).", "zip_empty")


def _crs_string(crs) -> tuple[str | None, str | None, bool | None]:
    if crs is None:
        return None, None, None
    c = CRS.from_user_input(crs)
    epsg = c.to_epsg(min_confidence=70)
    ident = f"EPSG:{epsg}" if epsg else c.to_wkt()
    return ident, c.name, c.is_projected


def normalize_null(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, str) and v.strip().lower() in NULL_TOKENS:
        return None
    return v


def _infer_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    return "string"


def profile_fields(df: pd.DataFrame, max_rows: int = 50_000) -> tuple[dict[str, str], dict[str, dict]]:
    """Per-field type and value profile used by attribute mapping (no values are logged)."""
    sample = df.head(max_rows)
    types: dict[str, str] = {}
    prof: dict[str, dict] = {}
    for col in sample.columns:
        if col == "geometry":
            continue
        s = sample[col].map(normalize_null)
        non_null = s.dropna()
        t = _infer_type(sample[col])
        info: dict = {
            "type": t,
            "null_count": int(s.isna().sum()),
            "non_null_count": int(non_null.shape[0]),
            "distinct_count": int(non_null.astype(str).nunique()),
        }
        info["unique_ratio"] = round(info["distinct_count"] / info["non_null_count"], 4) if len(non_null) else 0.0
        as_str = non_null.astype(str)
        if t == "string" and len(non_null):
            num = pd.to_numeric(as_str.str.replace(",", "", regex=False), errors="coerce")
            info["numeric_parse_rate"] = round(float(num.notna().mean()), 4)
            dt = pd.to_datetime(as_str, errors="coerce", format="mixed", dayfirst=False)
            info["date_parse_rate"] = round(float(dt.notna().mean()), 4)
            info["mean_length"] = round(float(as_str.str.len().mean()), 2)
            info["alnum_id_like_rate"] = round(float(as_str.str.fullmatch(r"[A-Za-z]{0,6}[-_/ ]?\d+[A-Za-z0-9/_-]*").mean()), 4)
        if t in ("integer", "float") and len(non_null):
            info["min"] = float(non_null.min())
            info["max"] = float(non_null.max())
            info["numeric_parse_rate"] = 1.0
        info["sample_values"] = [str(v)[:60] for v in as_str.drop_duplicates().head(5)]
        types[col] = t
        prof[col] = info
    return types, prof


def _extent_4326(gdf: gpd.GeoDataFrame) -> list[float] | None:
    if gdf.crs is None or gdf.empty:
        return None
    g = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    if g.empty:
        return None
    b = g.to_crs(4326).total_bounds
    if not np.all(np.isfinite(b)):
        return None
    return [float(x) for x in b]


def detect_csv_geometry(columns: list[str]) -> dict:
    lower = {c.lower().strip(): c for c in columns}
    for w in WKT_COLUMNS:
        if w in lower:
            return {"wkt_column": lower[w]}
    x = next((lower[c] for c in X_COLUMNS if c in lower), None)
    y = next((lower[c] for c in Y_COLUMNS if c in lower), None)
    if x and y:
        return {"x_column": x, "y_column": y}
    return {}


def read_csv(path: Path, options: dict, crs: str | None) -> gpd.GeoDataFrame | pd.DataFrame:
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding_errors="strict")
    except UnicodeDecodeError:
        raise IngestError("CSV is not valid UTF-8. Re-save the file as UTF-8 and upload again.", "bad_encoding") from None
    except pd.errors.ParserError as exc:
        raise IngestError(f"CSV could not be parsed: {str(exc)[:200]}", "malformed_csv") from None
    if df.empty and not len(df.columns):
        raise IngestError("CSV file is empty.", "empty_file")
    df = df.map(normalize_null)
    if options.get("wkt_column"):
        col = options["wkt_column"]
        if col not in df.columns:
            raise IngestError(f"WKT column '{col}' not found in CSV.", "csv_geometry")

        def parse(v):
            if v is None:
                return None
            try:
                return shapely_wkt.loads(v)
            except Exception:
                return None

        geoms = df[col].map(parse)
        bad = int(df[col].notna().sum() - geoms.notna().sum())
        gdf = gpd.GeoDataFrame(df.drop(columns=[col]), geometry=list(geoms), crs=crs)
        gdf.attrs["unparseable_geometries"] = bad
        return gdf
    if options.get("x_column") and options.get("y_column"):
        xc, yc = options["x_column"], options["y_column"]
        for c in (xc, yc):
            if c not in df.columns:
                raise IngestError(f"Coordinate column '{c}' not found in CSV.", "csv_geometry")
        x = pd.to_numeric(df[xc], errors="coerce")
        y = pd.to_numeric(df[yc], errors="coerce")
        bad = int(((x.isna() | y.isna()) & (df[xc].notna() | df[yc].notna())).sum())
        pts = gpd.points_from_xy(x, y)
        geoms = [None if (pd.isna(a) or pd.isna(b)) else p for a, b, p in zip(x, y, pts, strict=True)]
        gdf = gpd.GeoDataFrame(df, geometry=geoms, crs=crs)
        gdf.attrs["unparseable_geometries"] = bad
        return gdf
    return df  # attribute-only table


def read_vector(path: Path, fmt: str, layer: str | None = None, crs_override: str | None = None,
                csv_options: dict | None = None) -> gpd.GeoDataFrame | pd.DataFrame:
    """Read a dataset into a (Geo)DataFrame. ``crs_override`` is only applied when the user confirmed it."""
    if fmt == "csv":
        return read_csv(path, csv_options or {}, crs_override)
    try:
        if fmt == "geoparquet":
            gdf = gpd.read_parquet(path)
        else:
            gdf = pyogrio.read_dataframe(path, layer=layer, use_arrow=False)
    except Exception as exc:  # pyogrio/GDAL raise various DataSourceError subclasses
        raise IngestError(f"GDAL could not read the dataset: {str(exc)[:300]}", "unreadable") from None
    if crs_override:
        gdf = gdf.set_crs(crs_override, allow_override=True)
    return gdf


def suggest_csv_crs(df: pd.DataFrame, options: dict) -> str | None:
    """Suggest (never assign) EPSG:4326 if coordinate values are plausibly lon/lat."""
    if not options.get("x_column"):
        return None
    x = pd.to_numeric(df[options["x_column"]], errors="coerce").dropna()
    y = pd.to_numeric(df[options["y_column"]], errors="coerce").dropna()
    if len(x) and x.between(-180, 180).all() and y.between(-90, 90).all():
        return "EPSG:4326"
    return None


def inspect_raster(path: Path) -> Inspection:
    import rasterio
    from rasterio.warp import transform_bounds

    s = get_settings()
    try:
        ds = rasterio.open(path)
    except Exception as exc:
        raise IngestError(f"Raster could not be opened: {str(exc)[:200]}", "unreadable") from None
    with ds:
        if ds.width * ds.height > s.max_raster_pixels:
            raise IngestError(
                f"Raster has {ds.width * ds.height:,} pixels; limit is {s.max_raster_pixels:,}.", "raster_too_large"
            )
        ins = Inspection(format="geotiff", kind="raster")
        ins.crs, ins.crs_name, ins.crs_is_projected = _crs_string(ds.crs) if ds.crs else (None, None, None)
        overviews = ds.overviews(1) if ds.count else []
        is_tiled = ds.profile.get("tiled", False) or (ds.block_shapes and ds.block_shapes[0][0] < ds.height)
        res_m = None
        if ds.crs:
            c = CRS.from_user_input(ds.crs)
            if c.is_projected and c.axis_info and c.axis_info[0].unit_name in ("metre", "meter"):
                res_m = float(abs(ds.res[0]))
            ins.extent_4326 = list(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds))
        else:
            ins.warnings.append("Raster has no CRS; georeferencing must be supplied before use.")
        ins.raster = {
            "width": ds.width,
            "height": ds.height,
            "band_count": ds.count,
            "dtype": ds.dtypes[0] if ds.count else "unknown",
            "nodata": ds.nodata,
            "resolution": [float(ds.res[0]), float(ds.res[1])],
            "resolution_m": res_m,
            "is_cog": bool(is_tiled and overviews),
            "overviews": overviews,
            "tags": {k: str(v)[:200] for k, v in ds.tags().items()},
        }
        if res_m and res_m > 2.0:
            ins.warnings.append(
                f"Resolution {res_m:.1f} m/pixel is too coarse to delineate individual urban buildings or "
                "cadastral boundaries reliably."
            )
        ins.data_path = str(path)
        return ins


def inspect_file(path: Path, fmt: str, work_dir: Path, layer: str | None = None,
                 csv_options: dict | None = None) -> Inspection:
    """Inspect a stored upload: format, layers, CRS, geometry type, counts, schema, profile, extent."""
    if fmt == "zip":
        fmt, path = resolve_archive(path, work_dir / "extracted")
    if fmt == "geotiff":
        return inspect_raster(path)

    if fmt == "csv":
        raw = pd.read_csv(path, dtype=str, keep_default_na=False, nrows=5, encoding_errors="replace")
        opts = csv_options or detect_csv_geometry(list(raw.columns))
        df = read_vector(path, "csv", csv_options=opts)
        ins = Inspection(format="csv", kind="table" if not opts else "vector", csv_options=opts)
        ins.feature_count = len(df)
        ins.fields, ins.field_profile = profile_fields(df.drop(columns=["geometry"], errors="ignore"))
        if opts:
            ins.geometry_type = "Point" if "x_column" in opts else _geom_type(df)
            ins.crs_suggestion = suggest_csv_crs(df, opts)
            ins.warnings.append(
                "CSV files do not carry a coordinate reference system. Confirm the CRS of the coordinate "
                "columns before processing" + (f" (suggested: {ins.crs_suggestion})." if ins.crs_suggestion else ".")
            )
            bad = df.attrs.get("unparseable_geometries", 0)
            if bad:
                ins.warnings.append(f"{bad} row(s) have unparseable coordinates and will be loaded without geometry.")
        ins.data_path = str(path)
        return ins

    layers: list[str] = []
    if fmt != "geoparquet":
        try:
            layers = [str(row[0]) for row in pyogrio.list_layers(path)]
        except Exception as exc:
            raise IngestError(f"GDAL could not open the file: {str(exc)[:300]}", "unreadable") from None
        if not layers:
            raise IngestError("The file contains no vector layers.", "empty_file")
        if layer is None:
            if len(layers) > 1 and fmt == "gpkg":
                layer = layers[0]
            layer = layer or layers[0]
        elif layer not in layers:
            raise IngestError(f"Layer '{layer}' not found. Available layers: {', '.join(layers)}", "layer_not_found")

    gdf = read_vector(path, fmt, layer)
    if not isinstance(gdf, gpd.GeoDataFrame):
        raise IngestError("Dataset has no geometry column.", "no_geometry")
    ins = Inspection(format=fmt, kind="vector", layer=layer, layers=layers)
    ins.feature_count = len(gdf)
    if ins.feature_count == 0:
        raise IngestError("The dataset contains no features.", "empty_file")
    if ins.feature_count > get_settings().max_features_per_dataset:
        raise IngestError(
            f"Dataset has {ins.feature_count:,} features; limit is {get_settings().max_features_per_dataset:,}.",
            "too_many_features",
        )
    ins.crs, ins.crs_name, ins.crs_is_projected = _crs_string(gdf.crs)
    if ins.crs is None:
        ins.warnings.append(
            "No coordinate reference system found (e.g. missing .prj). The CRS will NOT be guessed: "
            "confirm it from the data supplier's documentation or survey control information."
        )
    ins.geometry_type = _geom_type(gdf)
    ins.fields, ins.field_profile = profile_fields(pd.DataFrame(gdf.drop(columns="geometry")))
    try:
        ins.extent_4326 = _extent_4326(gdf)
    except Exception:
        ins.warnings.append("Could not compute WGS84 extent from the declared CRS.")
    if ins.extent_4326 and not _plausible_lonlat(ins.extent_4326):
        ins.warnings.append(
            "Transformed extent falls outside valid longitude/latitude bounds: the declared CRS is likely wrong."
        )
    null_geoms = int(gdf.geometry.isna().sum() + gdf.geometry.is_empty.sum())
    if null_geoms:
        ins.warnings.append(f"{null_geoms} feature(s) have empty or missing geometry.")
    ins.data_path = str(path)
    return ins


def _plausible_lonlat(b: list[float]) -> bool:
    return -180 <= b[0] <= 180 and -180 <= b[2] <= 180 and -90 <= b[1] <= 90 and -90 <= b[3] <= 90


def _geom_type(gdf) -> str | None:
    if not isinstance(gdf, gpd.GeoDataFrame):
        return None
    types = sorted({t for t in gdf.geometry.geom_type.dropna().unique()})
    if not types:
        return None
    families = {t.replace("Multi", "") for t in types}
    if len(families) == 1:
        fam = families.pop()
        return f"Multi{fam}" if any(t.startswith("Multi") for t in types) else fam
    return "Mixed:" + ",".join(types)


def inspection_to_dict(ins: Inspection) -> dict:
    return json.loads(json.dumps(ins.__dict__, default=str))
