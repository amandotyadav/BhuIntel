# Algorithms

All thresholds below are defaults and are configurable per project, per run or per call. Source:
`backend/app/services/`.

## 1. CRS policy (`crs.py`, `etl.py`)

* A dataset's CRS comes from the file (e.g. `.prj`, GeoPackage SRS) or from an **explicit user
  confirmation with a written justification** (stored in the processing log and audit trail). CSVs
  never carry a CRS; for lon/lat-looking values the UI *suggests* EPSG:4326 but does not apply it.
* After reprojection the data extent is checked against the CRS's area of use; a mismatch produces a
  warning ("declared CRS may be incorrect").

## 2. Geometry repair policy (`geometry.py`)

Invalid geometries are repaired with `shapely.make_valid(method="structure")` (polygonal part kept),
and each repair is classified. The original geometry is always kept.

| class | rule | effect |
|---|---|---|
| `auto_safe` | same geometry family, same number of parts, area change ≤ 1e-6 (relative) and Hausdorff distance of boundaries ≤ 1 mm | applied automatically, recorded as an *applied* correction |
| `review_required` | any other change, non-cadastral datasets | proposed; computation uses the repaired geometry; integrated layer waits for approval |
| `legal_no_auto` | any other change to a **parcels** dataset | proposed; approval requires admin + explicit legal acknowledgement |

Regression tests cover bow-ties, self-touching rings (auto-safe) and zero-area spikes (not auto-safe:
area unchanged but the boundary moves 5 m).

## 3. Attribute mapping (`schema_mapping.py`)

For each source field and each canonical field (`parcel_id`, `ulpin`, `property_id`, `building_id`,
`land_use`, `area_sqm`, `surveyed_at`, `updated_at`, `owner_name`, `ward`, `village`, `address`,
`building_levels`, `height_m`):

* name score: 1.0 for built-in/project aliases and previously approved mappings of the same name;
  otherwise `max(ratio, token_sort_ratio)` (capped at 0.95 so fuzzy evidence never outranks a rule);
* type compatibility (numeric/date parse rates from the field profile);
* value profile (uniqueness and ID-likeness for identifiers, land-use vocabulary overlap, date and
  numeric parse rates);
* `score = 0.6·name + 0.25·type + 0.15·profile` (or `0.85 + 0.1·type + 0.05·profile` for exact rules).

Suggestions are **ambiguous** when the runner-up is within 0.08, or when two source fields compete for
one target. Exact alias matches are approved by rule; everything else needs a human decision, and
ambiguous ones need `confirm_ambiguous=true`. Two fields can never be silently mapped to one target.
An LLM provider is **not** implemented.

Identifiers are normalised without losing the original: upper-case, common prefixes such as
"Plot No." removed, separators dropped, leading zeros stripped per digit run (`"P-0012"→"P12"`,
`"Plot No. 012/A"→"12A"`). Identifier similarity: 1.0 exact; 0.95 same numeric core when one side
lacks the series prefix; 0.5 same core with *different* prefixes; otherwise 0.9 × edit-distance ratio.

## 4. Spatial matching (`matching.py`)

**Candidate generation**: `ST_DWithin(a.geom_work, b.geom_work, tol)` using GiST indexes, where
`tol = max(run tolerance, 2·√(acc_a² + acc_b²))` from the datasets' documented positional accuracies.
Record→parcel candidates are blocked by identifier core (exact, then fuzzy ≥ 80) and point proximity.

**Features** (stored per candidate): IoU, overlap of each side, max containment, area ratio, centroid
distance and closeness `1 − d/tol`, Hausdorff distance, identifier similarity, attribute similarity
(numeric: `1 − |a−b|/max`, text: token-set ratio), source quality (mean dataset reliability);
building→parcel adds **building coverage** (share of the building inside the parcel), parcel share and
centroid-inside; record→parcel adds point-inside and point distance.

**Score** = weighted mean over *available* evidence (missing evidence is listed and its weight is
redistributed). Default weights:

| type | weights |
|---|---|
| building↔building | IoU .45, centroid closeness .25, area ratio .15, attributes .10, id .05 |
| parcel↔parcel | IoU .35, id .25, centroid .15, area ratio .10, containment .10, attributes .05 |
| building→parcel | building coverage .70, centroid inside .20, attributes .10 (IoU is deliberately unused) |
| record→parcel | id .60, point inside .30, attributes .10 |

Classes: `match ≥ 0.75`, `possible ≥ 0.50`, otherwise `non_match` (candidates < 0.20 are not stored).
Scores are labelled `heuristic_match_score`, i.e. weighted evidence and not probabilities.

**Relationships**: over the graph of match/possible edges (for building→parcel: coverage ≥ 5 %), each
connected component is 1:1, 1:n, n:1 or n:m.

## 5. Conflict engine (`conflicts.py`, `topology.py`)

| category | detection |
|---|---|
| `parcel_id_mismatch` | best match with IoU ≥ 0.8 but identifier similarity < 0.9 (legal) |
| `attribute_conflict` | matched pair disagrees on land use / area (>10 %) / ward / village / levels; record land use vs parcel; record point outside its referenced parcel |
| `source_disagreement` | same identifier but IoU < 0.5 or Hausdorff > 2·tol (legal) |
| `unmatched_feature` / `potentially_outdated` | no match/possible candidate; "outdated" when the other building source is newer |
| `unmatched_record`, `ambiguous_record_link` | record links to no parcel / several strong parcels |
| `duplicate_record` | identical geometry+attributes (row hash), or one identifier used by several features |
| `area_discrepancy` | recorded `area_sqm` differs from computed area by > 10 % (legal) |
| `building_crosses_parcel` | building has ≥ 5 % coverage in ≥ 2 parcels |
| `invalid_geometry` | invalid source geometry with a non-auto-safe repair |
| `parcel_overlap` | parcel intersection area ≥ 0.5 m² (legal; never auto-corrected) |
| `parcel_gap` | interior ring of the parcel union with 0.5 m² ≤ area ≤ 500 m² (legal) |
| `sliver_polygon` | area ≤ 100 m² and Polsby–Popper compactness < 0.12 (legal for parcels) |
| `duplicate_geometry`, `unexpected_geometry_type` | Hausdorff ≤ 1 cm; type not expected for dataset kind |
| `dangling_segment`, `disconnected_network` | line end not within snap tolerance of another line; DBSCAN components beyond the largest |

Each conflict stores evidence (scores, measures, values, involved sources with reliability and date),
a proposed action and a heuristic **confidence** that the conflict is real. Priority:

```
priority = 100 · severity_weight · (0.5 + 0.5 · confidence)  (+10 if legally sensitive, max 100)
severity_weight: low .25, medium .5, high .75, critical 1.0
```

Conflicts are fingerprinted (`category + sorted feature ids + key`), so re-running detection updates
evidence without duplicating rows and never resets a reviewer's decision.

**Attribute resolution proposal**: prefer the value from the source with higher reliability, then the
newer source date. It is applied only when a reviewer approves the conflict.

## 6. Change detection (`change_detection.py`)

Pairs features by unique identifier, then by greedy best IoU (≥ 0.3). With positional uncertainty `u`
(dataset accuracy × √2, or the project tolerance):

* `geometry_modified` if (IoU < 0.8 or area change > 20 %) **and** Hausdorff > 2u;
* `uncertain` if the difference is of the order of `u` (could be registration error), or if an
  unpaired feature lies outside the other version's coverage extent;
* `attribute_changed` if geometry is equivalent but attributes differ;
* `added` / `removed` for unpaired features inside the other version's coverage.

Image differencing (dated, georeferenced images): resample the newer image to the older grid, optional
OpenCV ECC translation alignment, absolute brightness difference, Otsu threshold, morphological
opening, polygonisation (≥ 20 m²). Results at pixel sizes > 1 m are labelled uncertain.

## 7. Random Forest matcher (`ml.py`)

* Features: the `FEATURE_SCHEMA` of the match type (validated at inference; missing → NaN, explicit).
* Labels: reviewer accept/reject, imported CSV, or `synthetic_fixture` (tests only), with the label
  source recorded.
* Minimum: 40 labels with ≥ 10 per class, otherwise an `insufficient_data` model version is recorded
  and deterministic matching stays in use.
* Split: `GroupShuffleSplit` on 100 m spatial grid cells (leakage control); a stratified split is used
  only if grouping cannot produce both classes, and is flagged.
* Pipeline: median imputation with missing-indicators → `RandomForestClassifier(300 trees,
  min_samples_leaf=2, class_weight=balanced, random_state=42)`.
* Reported: precision, recall, F1, confusion matrix, class distributions, feature importances, and
  the deterministic baseline evaluated on the same held-out rows. Probabilities are **not calibrated**.
* Artefacts: joblib file with SHA-256 recorded in the DB and verified before loading; activation is an
  explicit admin action.

## 8. Integration (`integration.py`)

Primary parcel layer → integrated parcels (attributes filled from the matched secondary parcel when
missing; disagreements kept as `lineage.alternatives`), linked property ids from the record run,
QA flags (pending repairs, open conflict counts). Buildings matched across sources are merged (geometry
from the more reliable source) with parcel assignment and `crosses_parcels`. Only *accepted* predicted
buildings are included. Re-builds are idempotent and version-aware; approved corrections are re-applied.
