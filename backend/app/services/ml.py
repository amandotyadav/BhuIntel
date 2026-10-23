"""Supervised match classifier (Random Forest) with an honest training protocol.

Training data schema (one row per labelled match candidate):
    features: the ordered list in matching.FEATURE_SCHEMA[match_type] (floats; missing allowed)
    label:    1 = same real-world feature, 0 = different
    group:    spatial block id (grid cell of the left feature's centroid) used for splitting
    label_source: reviewer | imported | synthetic_fixture

Protocol
--------
* Refuse to train below ``MIN_LABELS`` total or ``MIN_PER_CLASS`` per class; record an
  ``insufficient_data`` model version with the counts instead of inventing results.
* Split by spatial groups (GroupShuffleSplit) so near-duplicate neighbouring pairs do not leak between
  train and test. Falls back to a stratified split only if grouping cannot produce both classes in the
  test set, and records that leakage risk.
* Pipeline: SimpleImputer(median, add_indicator=True) -> RandomForestClassifier(class_weight=balanced),
  fixed random_state for reproducibility.
* Report precision/recall/F1/confusion matrix/class distribution on the held-out set, and the
  deterministic baseline (score >= match threshold) evaluated on exactly the same rows.
* Probabilities are NOT calibrated; they are labelled ``model_probability_uncalibrated``.
"""

import hashlib
import io
import math
from dataclasses import dataclass

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.pipeline import Pipeline
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import MatchCandidate, MatchRun, ModelVersion
from app.services.matching import DEFAULT_THRESHOLDS, FEATURE_SCHEMA
from app.storage import get_storage

MIN_LABELS = 40
MIN_PER_CLASS = 10
GROUP_CELL_M = 100.0
RANDOM_STATE = 42


class FeatureValidationError(ValueError):
    pass


def feature_vector(match_type: str, feats: dict) -> list[float]:
    """Validate and order features for inference. Unknown types fail; missing values -> NaN (explicit)."""
    out = []
    for name in FEATURE_SCHEMA[match_type]:
        v = feats.get(name)
        if v is None:
            out.append(math.nan)
            continue
        if isinstance(v, bool):
            v = float(v)
        if not isinstance(v, (int, float)):
            raise FeatureValidationError(f"Feature '{name}' must be numeric, got {type(v).__name__}")
        if not math.isfinite(float(v)):
            raise FeatureValidationError(f"Feature '{name}' is not finite")
        out.append(float(v))
    return out


def labelled_examples(db: Session, project_id: int, match_type: str) -> list[dict]:
    rows = db.execute(text(
        """SELECT c.id, c.left_feature_id, c.right_feature_id, c.features, c.label, c.label_source, c.score,
                  ST_X(ST_Centroid(f.geom_work)) AS x, ST_Y(ST_Centroid(f.geom_work)) AS y
           FROM match_candidates c JOIN match_runs r ON r.id=c.run_id
           JOIN source_features f ON f.id=c.left_feature_id
           WHERE r.project_id=:p AND r.match_type=:t AND c.label IS NOT NULL"""),
        {"p": project_id, "t": match_type}).mappings().all()
    out = []
    seen = set()
    for r in rows:
        key = (r["left_feature_id"], r["right_feature_id"])
        if key in seen:  # the same pair labelled in repeated runs must not be double-counted
            continue
        seen.add(key)
        gx = math.floor((r["x"] or 0) / GROUP_CELL_M)
        gy = math.floor((r["y"] or 0) / GROUP_CELL_M)
        out.append({"id": r["id"], "features": r["features"], "label": int(r["label"]),
                    "label_source": r["label_source"], "baseline_score": (r["features"] or {}).get("baseline_score",
                                                                                                   r["score"]),
                    "group": f"{gx}:{gy}"})
    return out


def _metrics(y_true, y_pred) -> dict:
    p, r, f, _ = precision_recall_fscore_support(y_true, y_pred, average="binary", pos_label=1, zero_division=0)
    cm = confusion_matrix(y_true, y_pred, labels=[0, 1])
    return {"precision": round(float(p), 4), "recall": round(float(r), 4), "f1": round(float(f), 4),
            "confusion_matrix": {"labels": [0, 1], "matrix": cm.tolist()}, "support": int(len(y_true))}


def train(db: Session, project_id: int, match_type: str, actor_id: int | None, activate: bool = False) -> ModelVersion:
    examples = labelled_examples(db, project_id, match_type)
    y = np.array([e["label"] for e in examples], dtype=int)
    n_pos, n_neg = int((y == 1).sum()), int((y == 0).sum())
    next_ver = (db.execute(select(func.max(ModelVersion.version)).where(
        ModelVersion.project_id == project_id, ModelVersion.name == f"match_rf_{match_type}")).scalar() or 0) + 1
    label_sources = sorted({e["label_source"] for e in examples})
    info = {"n_labels": len(examples), "class_distribution": {"match": n_pos, "non_match": n_neg},
            "label_sources": label_sources, "min_labels": MIN_LABELS, "min_per_class": MIN_PER_CLASS,
            "sklearn_version": sklearn.__version__}
    mv = ModelVersion(project_id=project_id, name=f"match_rf_{match_type}", version=next_ver,
                      algorithm="RandomForestClassifier", match_type=match_type,
                      feature_schema={"features": FEATURE_SCHEMA[match_type], "missing": "median imputation + indicator"},
                      created_by=actor_id, training_info=info, metrics={})
    if len(examples) < MIN_LABELS or n_pos < MIN_PER_CLASS or n_neg < MIN_PER_CLASS:
        mv.status = "insufficient_data"
        mv.training_info = {**info, "message": f"Need >= {MIN_LABELS} labels with >= {MIN_PER_CLASS} per class; "
                            "deterministic matching remains in use."}
        db.add(mv)
        db.flush()
        return mv

    X = np.array([feature_vector(match_type, e["features"]) for e in examples], dtype=float)
    groups = np.array([e["group"] for e in examples])
    split_method = None
    for seed in range(RANDOM_STATE, RANDOM_STATE + 20):
        gss = GroupShuffleSplit(n_splits=1, test_size=0.3, random_state=seed)
        tr, te = next(gss.split(X, y, groups))
        if len(set(y[te])) == 2 and len(set(y[tr])) == 2:
            split_method = f"GroupShuffleSplit(spatial {GROUP_CELL_M:.0f} m cells, seed={seed})"
            break
    if split_method is None:
        tr, te = train_test_split(np.arange(len(y)), test_size=0.3, stratify=y, random_state=RANDOM_STATE)
        split_method = "stratified random split (WARNING: spatial grouping failed; leakage risk)"

    pipe = Pipeline([
        ("impute", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
        ("rf", RandomForestClassifier(n_estimators=300, min_samples_leaf=2, class_weight="balanced",
                                      random_state=RANDOM_STATE, n_jobs=1)),
    ])
    pipe.fit(X[tr], y[tr])
    pred = pipe.predict(X[te])
    thr = DEFAULT_THRESHOLDS["match"]
    base_pred = np.array([1 if (examples[i]["baseline_score"] or 0) >= thr else 0 for i in te])
    importances = pipe.named_steps["rf"].feature_importances_[: len(FEATURE_SCHEMA[match_type])]
    mv.metrics = {
        "model": _metrics(y[te], pred),
        "deterministic_baseline": {**_metrics(y[te], base_pred), "rule": f"baseline_score >= {thr}"},
        "test_class_distribution": {"match": int((y[te] == 1).sum()), "non_match": int((y[te] == 0).sum())},
        "train_class_distribution": {"match": int((y[tr] == 1).sum()), "non_match": int((y[tr] == 0).sum())},
        "feature_importance": {n: round(float(v), 4) for n, v in zip(FEATURE_SCHEMA[match_type], importances,
                                                                    strict=False)},
        "evaluated_on": "held-out labelled examples from: " + ", ".join(label_sources),
        "probability_calibration": "none (uncalibrated)",
    }
    mv.training_info = {**info, "split": split_method, "n_train": int(len(tr)), "n_test": int(len(te)),
                        "groups": int(len(set(groups))), "random_state": RANDOM_STATE,
                        "hyperparameters": {"n_estimators": 300, "min_samples_leaf": 2, "class_weight": "balanced"}}
    buf = io.BytesIO()
    joblib.dump({"pipeline": pipe, "features": FEATURE_SCHEMA[match_type], "match_type": match_type}, buf)
    data = buf.getvalue()
    mv.artifact_key = get_storage().put_bytes(f"models/{project_id}", f"match_rf_{match_type}_v{next_ver}.joblib", data)
    mv.artifact_sha256 = hashlib.sha256(data).hexdigest()
    mv.status = "trained"
    db.add(mv)
    db.flush()
    if activate:
        set_active(db, mv)
    return mv


def set_active(db: Session, mv: ModelVersion) -> None:
    if mv.status != "trained":
        raise ValueError("Only trained models can be activated")
    db.execute(text("UPDATE model_versions SET is_active=false WHERE project_id=:p AND name=:n"),
               {"p": mv.project_id, "n": mv.name})
    mv.is_active = True
    db.flush()


@dataclass
class LoadedModel:
    version: int
    match_type: str
    pipeline: object
    features: list[str]
    importances: dict

    def predict_one(self, feats: dict) -> tuple[float, list[str]]:
        x = np.array([feature_vector(self.match_type, feats)], dtype=float)
        prob = float(self.pipeline.predict_proba(x)[0][list(self.pipeline.classes_).index(1)])
        top = sorted(self.importances.items(), key=lambda kv: -kv[1])[:3]
        return prob, [f"Random Forest v{self.version} probability {prob:.3f} (uncalibrated); most important "
                      "features: " + ", ".join(f"{k} ({v:.2f})" for k, v in top)]


def load_model(db: Session, mv: ModelVersion) -> LoadedModel:
    path = get_storage().local_path(mv.artifact_key)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != mv.artifact_sha256:
        raise ValueError("Model artifact checksum mismatch; refusing to load")
    obj = joblib.load(io.BytesIO(data))  # artifact produced by this service; integrity verified above
    if obj["features"] != FEATURE_SCHEMA[mv.match_type]:
        raise FeatureValidationError("Model feature schema does not match the current matching feature schema")
    return LoadedModel(mv.version, mv.match_type, obj["pipeline"], obj["features"],
                       mv.metrics.get("feature_importance", {}))


def active_model(db: Session, project_id: int, match_type: str) -> ModelVersion | None:
    return db.execute(select(ModelVersion).where(
        ModelVersion.project_id == project_id, ModelVersion.match_type == match_type,
        ModelVersion.is_active.is_(True), ModelVersion.status == "trained")).scalar_one_or_none()


def label_from_ground_truth(db: Session, run: MatchRun, truth_pairs: set[tuple[str, str]]) -> int:
    """Label candidates of a run from a known truth table of (left_raw_id, right_raw_id) pairs.

    Used only with the synthetic fixture; labels are tagged ``synthetic_fixture`` so that any
    resulting metrics are clearly reported as synthetic-data results.
    """
    rows = db.execute(text(
        """SELECT c.id, l.source_id_raw AS l, r.source_id_raw AS r FROM match_candidates c
           JOIN source_features l ON l.id=c.left_feature_id JOIN source_features r ON r.id=c.right_feature_id
           WHERE c.run_id=:run"""), {"run": run.id}).mappings().all()
    n = 0
    for row in rows:
        lab = 1 if (row["l"], row["r"]) in truth_pairs else 0
        db.execute(text("UPDATE match_candidates SET label=:lab, label_source='synthetic_fixture' WHERE id=:i"),
                   {"lab": lab, "i": row["id"]})
        n += 1
    db.flush()
    return n


def candidate_ids_for_run(db: Session, run_id: int) -> list[int]:
    return list(db.execute(select(MatchCandidate.id).where(MatchCandidate.run_id == run_id)).scalars())
