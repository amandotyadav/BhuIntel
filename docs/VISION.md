# Building extraction models

## Status

**No pretrained model weights are bundled.** Model hubs (e.g. Hugging Face) were unreachable from the
build environment, and no building-segmentation model could be verified as (a) licensed for this use,
(b) suitable for Indian urban imagery at the available resolution, and (c) downloadable. Nothing in
this repository claims ML building extraction results.

What is implemented and tested:

* tiled inference with configurable tile size and overlap; per-pixel probabilities are averaged across
  overlapping tiles and polygons are produced once from the merged mask (tested: identical polygon
  counts for 96 px tiles and a single tile; no seam duplicates);
* georeferenced polygonisation (`rasterio.features.shapes`), minimum-area filter, simplification at
  0.75 px, validity repair;
* resolution checks (warns when the image is coarser than 2 m or outside the model's declared range);
* predictions stored as a separate `predicted_buildings` dataset (`provenance = derived`, reliability
  0.3) with model name/version, imagery version, tiles and warnings in the processing log, shown with
  their own map style, and excluded from the integrated layer until a reviewer accepts them.

## Backends

| backend | what it is |
|---|---|
| `threshold_baseline` | **Not machine learning.** Otsu brightness threshold. Useful only for testing the pipeline or for imagery where roofs are much brighter than surroundings (true for the synthetic fixture image). |
| `onnx` | Runs any binary building-segmentation model exported to ONNX (CPU, `onnxruntime`, optional extra `pip install .[vision]`). |

## ONNX model contract

Place `model.onnx` and `model.json` on the server (paths are passed to `POST /vision/extract`):

```json
{
  "name": "my-building-seg",
  "version": "1.2.0",
  "license": "licence of the weights, e.g. Apache-2.0",
  "input_size": 512,
  "bands": [1, 2, 3],
  "mean": [0.485, 0.456, 0.406],
  "std": [0.229, 0.224, 0.225],
  "scale": 255.0,
  "output": "logits",
  "expected_resolution_m": [0.3, 1.0]
}
```

Input: `float32 [1, C, input_size, input_size]`, normalised as `(x/scale - mean)/std`. Output: either
`[1, K, H, W]` (last channel = building) or `[1, H, W]`; `"output": "logits"` applies a sigmoid.

Before using a model, check its training geography and resolution, its licence (weights often carry a
different licence than code), and evaluate it on labelled local tiles. Training a deep network from
scratch is out of scope without a labelled local dataset and a compute budget.
