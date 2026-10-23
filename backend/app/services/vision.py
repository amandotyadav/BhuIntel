"""Building extraction from imagery: tiled segmentation -> georeferenced, cleaned polygons.

Backends
--------
* ``onnx``  - a user-supplied pretrained segmentation model exported to ONNX, described by a JSON
              sidecar (see docs/VISION.md): input size, band order, normalisation, expected resolution,
              licence, version. No model weights are bundled with this repository: we could not verify a
              suitably licensed pretrained model for Indian urban imagery that is downloadable from this
              environment (model hubs are blocked here), so none is claimed.
* ``threshold_baseline`` - a NON-MACHINE-LEARNING brightness threshold (Otsu) used to test the
              tiling/polygonisation pipeline and for bright-roof synthetic imagery. Outputs are labelled
              with this method name and must never be presented as AI predictions.

Predictions are written as a new ``predicted_buildings`` dataset version with review_status='pending';
they stay separate from verified buildings until a reviewer accepts them.
"""

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import shapely
from rasterio.features import shapes
from rasterio.windows import Window
from shapely.geometry import shape


@dataclass
class ModelInfo:
    name: str
    version: str
    license: str
    expected_resolution_m: tuple[float, float] | None
    bands: list[int]
    tile_size: int
    is_ml: bool


class ThresholdBaseline:
    def __init__(self):
        self.info = ModelInfo("threshold_baseline", "1.0", "n/a (deterministic algorithm, Apache-2.0 code)", None,
                              [1, 2, 3], 512, is_ml=False)
        self._threshold: float | None = None

    def fit_global(self, sample: np.ndarray) -> None:
        import cv2

        g = sample.astype("float32").mean(axis=0)
        g8 = np.clip(g, 0, 255).astype("uint8")
        t, _ = cv2.threshold(g8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        self._threshold = float(t)

    def predict(self, tile: np.ndarray) -> np.ndarray:
        g = tile.astype("float32").mean(axis=0)
        return (g > (self._threshold or 128.0)).astype("float32")


class OnnxSegmenter:
    def __init__(self, model_path: Path, meta_path: Path):
        import onnxruntime as ort

        meta = json.loads(meta_path.read_text())
        for k in ("name", "version", "license", "input_size", "bands", "mean", "std"):
            if k not in meta:
                raise ValueError(f"Model metadata missing '{k}'")
        self.meta = meta
        self.info = ModelInfo(meta["name"], str(meta["version"]), meta["license"],
                              tuple(meta["expected_resolution_m"]) if meta.get("expected_resolution_m") else None,
                              meta["bands"], int(meta["input_size"]), is_ml=True)
        self.session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name

    def fit_global(self, sample: np.ndarray) -> None:
        return None

    def predict(self, tile: np.ndarray) -> np.ndarray:
        size = self.info.tile_size
        c, h, w = tile.shape
        x = np.zeros((c, size, size), dtype="float32")
        x[:, :h, :w] = tile.astype("float32")
        mean = np.array(self.meta["mean"], dtype="float32")[:, None, None]
        std = np.array(self.meta["std"], dtype="float32")[:, None, None]
        x = (x / float(self.meta.get("scale", 255.0)) - mean) / std
        out = self.session.run(None, {self.input_name: x[None]})[0]
        prob = out[0, -1] if out.ndim == 4 else out[0]
        if self.meta.get("output") == "logits":
            prob = 1 / (1 + np.exp(-prob))
        return prob[:h, :w]


def extract_buildings(raster_path: Path, backend, tile_size: int | None = None, overlap: int = 64,
                      threshold: float = 0.5, min_area_m2: float = 10.0, max_pixels: int = 400_000_000) -> dict:
    """Tiled inference with overlap; per-pixel probabilities are averaged across overlapping tiles so
    polygons are produced once from the merged mask (no duplicates along tile seams)."""
    with rasterio.open(raster_path) as src:
        if src.crs is None:
            raise ValueError("Imagery has no CRS; cannot georeference predictions")
        if src.width * src.height > max_pixels:
            raise ValueError("Raster exceeds the configured pixel limit for inference")
        res = abs(src.res[0])
        crs = src.crs
        warnings = []
        if not crs.is_projected:
            warnings.append("Imagery CRS is geographic; area filters use approximate metres.")
        exp = backend.info.expected_resolution_m
        if exp and not (exp[0] <= res <= exp[1]):
            warnings.append(f"Image resolution {res:.2f} m is outside the model's expected range {exp}; "
                            "predictions may be unreliable.")
        if res > 2.0:
            warnings.append(f"Resolution {res:.1f} m is too coarse for reliable building delineation.")
        bands = [b for b in backend.info.bands if b <= src.count] or [1]
        ts = tile_size or backend.info.tile_size
        step = max(1, ts - overlap)
        overview = src.read(bands, out_shape=(len(bands), min(src.height, 1024), min(src.width, 1024)))
        backend.fit_global(overview)
        acc = np.zeros((src.height, src.width), dtype="float32")
        cnt = np.zeros((src.height, src.width), dtype="uint8")
        tiles = 0
        for row in range(0, src.height, step):
            for col in range(0, src.width, step):
                win = Window(col, row, min(ts, src.width - col), min(ts, src.height - row))
                tile = src.read(bands, window=win)
                if src.nodata is not None and np.all(tile == src.nodata):
                    continue
                prob = backend.predict(tile)
                acc[row:row + win.height, col:col + win.width] += prob
                cnt[row:row + win.height, col:col + win.width] += 1
                tiles += 1
        transform = src.transform
    prob = np.divide(acc, np.maximum(cnt, 1))
    mask = (prob >= threshold).astype("uint8")
    import cv2

    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    polys = []
    px_area = res * res
    for geom, _ in shapes(mask, mask=mask.astype(bool), transform=transform):
        g = shape(geom)
        if g.area < min_area_m2:
            continue
        g = g.simplify(res * 0.75, preserve_topology=True)
        if not g.is_valid:
            g = shapely.make_valid(g)
        if g.is_empty or g.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        rows_px = int(round(g.area / px_area))
        polys.append({"geometry": g, "area_m2": round(g.area, 2), "pixels": rows_px})
    return {"polygons": polys, "crs": crs.to_string(), "resolution_m": res, "tiles": tiles, "warnings": warnings,
            "model": backend.info.__dict__, "mean_probability": float(prob[mask == 1].mean()) if mask.any() else math.nan}
