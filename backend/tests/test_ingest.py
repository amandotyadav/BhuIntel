"""Upload validation: unsafe/malformed archives, missing sidecars, missing CRS, CSV handling."""

import stat
import zipfile

import pytest

from app.services.ingest import IngestError, detect_format, inspect_file, safe_extract_zip
from tests.conftest import FIXTURES


def _zip(path, members: dict[str, bytes], symlink: str | None = None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
    return path


def test_rejects_path_traversal(tmp_path):
    z = _zip(tmp_path / "evil.zip", {"../../evil.txt": b"x"})
    with pytest.raises(IngestError) as e:
        safe_extract_zip(z, tmp_path / "out")
    assert e.value.code == "zip_unsafe_path"
    assert not (tmp_path / "evil.txt").exists()


def test_rejects_absolute_paths(tmp_path):
    z = _zip(tmp_path / "abs.zip", {"/tmp/abs.txt": b"x"})
    with pytest.raises(IngestError):
        safe_extract_zip(z, tmp_path / "out")


def test_rejects_symlinks(tmp_path):
    z = _zip(tmp_path / "link.zip", {"a.txt": b"x"}, symlink="link")
    with pytest.raises(IngestError) as e:
        safe_extract_zip(z, tmp_path / "out")
    assert "Symbolic links" in str(e.value)


def test_rejects_zip_bomb_ratio(tmp_path):
    z = _zip(tmp_path / "bomb.zip", {"big.bin": b"\0" * 30_000_000})
    with pytest.raises(IngestError) as e:
        safe_extract_zip(z, tmp_path / "out")
    assert e.value.code == "zip_bomb"


def test_rejects_too_many_members(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_zip_members", 3)
    z = _zip(tmp_path / "many.zip", {f"f{i}.txt": b"x" for i in range(5)})
    with pytest.raises(IngestError) as e:
        safe_extract_zip(z, tmp_path / "out")
    assert e.value.code == "zip_limits"


def test_rejects_corrupt_zip(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"PK\x03\x04garbage")
    with pytest.raises(IngestError) as e:
        safe_extract_zip(p, tmp_path / "out")
    assert e.value.code == "bad_zip"


def test_shapefile_missing_shx(tmp_path):
    with pytest.raises(IngestError) as e:
        inspect_file(FIXTURES / "parcels_missing_shx.zip", "zip", tmp_path)
    assert e.value.code == "shapefile_incomplete" and ".shx" in str(e.value)


def test_shapefile_without_prj_has_no_guessed_crs(tmp_path):
    ins = inspect_file(FIXTURES / "parcels_no_prj.zip", "zip", tmp_path)
    assert ins.crs is None and ins.feature_count == 5
    assert any("will NOT be guessed" in w for w in ins.warnings)


def test_complete_shapefile_bundle(tmp_path):
    ins = inspect_file(FIXTURES / "parcels_subset_shapefile.zip", "zip", tmp_path)
    assert ins.format == "shapefile" and ins.crs == "EPSG:32643" and ins.geometry_type == "Polygon"


def test_multiple_shapefiles_rejected(tmp_path):
    src = zipfile.ZipFile(FIXTURES / "parcels_subset_shapefile.zip")
    members = {n: src.read(n) for n in src.namelist()}
    members.update({n.replace("parcels_subset", "other"): d for n, d in list(members.items())})
    z = _zip(tmp_path / "two.zip", members)
    with pytest.raises(IngestError) as e:
        inspect_file(z, "zip", tmp_path / "w")
    assert e.value.code == "zip_multiple_layers"


def test_malformed_geojson(tmp_path):
    p = tmp_path / "broken.geojson"
    p.write_text('{"type": "FeatureCollection", "features": [ {"type": "Feature", ')
    with pytest.raises(IngestError) as e:
        inspect_file(p, "geojson", tmp_path / "w")
    assert e.value.code == "unreadable"


def test_empty_feature_collection(tmp_path):
    p = tmp_path / "empty.geojson"
    p.write_text('{"type": "FeatureCollection", "features": []}')
    with pytest.raises(IngestError):
        inspect_file(p, "geojson", tmp_path / "w")


def test_unsupported_extension():
    with pytest.raises(IngestError) as e:
        detect_format("parcels.dwg")
    assert e.value.code == "unsupported_format"


def test_csv_coordinates_need_crs_confirmation(tmp_path):
    ins = inspect_file(FIXTURES / "property_records.csv", "csv", tmp_path)
    assert ins.crs is None and ins.crs_suggestion == "EPSG:4326"
    assert ins.csv_options == {"x_column": "longitude", "y_column": "latitude"}
    assert ins.field_profile["property_number"]["unique_ratio"] < 1.0  # contains a duplicate record


def test_csv_non_utf8_rejected(tmp_path):
    p = tmp_path / "latin.csv"
    p.write_bytes("plot_no,owner\nP1,Jos\xe9\n".encode("latin-1"))
    with pytest.raises(IngestError) as e:
        inspect_file(p, "csv", tmp_path / "w")
    assert e.value.code == "bad_encoding"


def test_raster_inspection(tmp_path):
    ins = inspect_file(FIXTURES / "synthetic_orthophoto.tif", "geotiff", tmp_path)
    assert ins.kind == "raster" and ins.crs == "EPSG:32643"
    assert ins.raster["resolution_m"] == pytest.approx(0.5) and ins.raster["band_count"] == 3
