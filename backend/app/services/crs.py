"""Coordinate reference system helpers (PROJ via pyproj)."""

from pyproj import CRS
from pyproj.exceptions import CRSError


class CrsError(ValueError):
    pass


def parse_crs(value: str | int) -> CRS:
    try:
        return CRS.from_user_input(value)
    except CRSError as exc:
        raise CrsError(f"Unrecognised coordinate reference system '{value}': {exc}") from None


def crs_identifier(crs: CRS) -> str:
    epsg = crs.to_epsg(min_confidence=70)
    return f"EPSG:{epsg}" if epsg else crs.to_wkt()


def validate_working_crs(epsg: int) -> CRS:
    """The working CRS must be projected with metre units so distances/areas are meaningful."""
    crs = parse_crs(epsg)
    if not crs.is_projected:
        raise CrsError(f"EPSG:{epsg} ({crs.name}) is geographic; choose a projected CRS in metres (e.g. UTM).")
    units = {a.unit_name for a in crs.axis_info}
    if not units <= {"metre", "meter"}:
        raise CrsError(f"EPSG:{epsg} uses units {units}; a metre-based projected CRS is required.")
    return crs


def suggest_utm_epsg(lon: float, lat: float) -> int:
    """WGS84 / UTM zone for a location (India spans zones 42N-47N)."""
    if not (-180 <= lon <= 180 and -80 <= lat <= 84):
        raise CrsError("Coordinates outside the UTM domain")
    zone = int((lon + 180) // 6) + 1
    return (32600 if lat >= 0 else 32700) + zone


def area_of_use_check(crs: CRS, bounds_4326: list[float] | None) -> str | None:
    """Warn when data lies far outside the CRS's documented area of use (often a wrong CRS label)."""
    if bounds_4326 is None or crs.area_of_use is None:
        return None
    a = crs.area_of_use
    minx, miny, maxx, maxy = bounds_4326
    margin = 5.0
    if maxx < a.west - margin or minx > a.east + margin or maxy < a.south - margin or miny > a.north + margin:
        return (
            f"Data extent {[round(v, 4) for v in bounds_4326]} lies outside the area of use of {crs.name} "
            f"({a.west}, {a.south}, {a.east}, {a.north}). The declared CRS may be incorrect."
        )
    return None
