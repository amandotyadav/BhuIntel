"""Score a project built from the SYNTHETIC fixture (scripts/seed.py --demo) against ground_truth.json.

    python scripts/evaluate_fixture.py --project 1

Prints precision/recall/F1 per algorithm, computed from the database. These are results on synthetic
data with deliberately introduced errors: they validate the implementation, not real-world accuracy.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.services.schema_mapping import id_core, normalize_identifier  # noqa: E402

FIX = Path(os.environ.get("BHU_FIXTURES_DIR", Path(__file__).resolve().parents[2] / "fixtures" / "synthetic_pune"))


def prf(pred: set, truth: set) -> dict:
    tp = len(pred & truth)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(truth) if truth else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "fp": len(pred - truth), "fn": len(truth - pred), "precision": round(p, 4), "recall": round(r, 4),
            "f1": round(f, 4)}


def core(x):
    return id_core(normalize_identifier(x))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", type=int, required=True)
    pid = ap.parse_args().project
    truth = json.loads((FIX / "ground_truth.json").read_text())
    out: dict = {}
    with SessionLocal() as db:
        def run(mtype):
            return db.execute(text("SELECT id FROM match_runs WHERE project_id=:p AND match_type=:t AND status='succeeded' "
                                   "ORDER BY id DESC LIMIT 1"), {"p": pid, "t": mtype}).scalar()

        def pairs(run_id, classes=("match",)):
            return {(r[0], r[1]) for r in db.execute(text(
                "SELECT l.source_id_raw, r.source_id_raw FROM match_candidates c JOIN source_features l ON "
                "l.id=c.left_feature_id JOIN source_features r ON r.id=c.right_feature_id WHERE c.run_id=:r AND "
                "c.match_class = ANY(:c)"), {"r": run_id, "c": list(classes)})}

        # parcel <-> parcel: a predicted match is correct when both sides denote the same grid parcel
        pp = pairs(run("parcel_parcel"))
        same_parcel_truth = {f"P-{n:03d}" for n in range(1, 31)} - {"P-022", "P-028", "P-030"}
        pred = {(a, core(b)) for a, b in pp}
        tr = {(p, core(p)) for p in same_parcel_truth}
        # P-015 is geometrically the same parcel but carries the wrong id "P-051" in the other source
        tr = {(p, c) if p != "P-015" else ("P-015", core("P-051")) for p, c in tr}
        out["parcel_matching (geometry-identical parcels, incl. swapped id)"] = prf(pred, tr)

        out["building_matching (source A <-> B)"] = prf(pairs(run("building_building")),
                                                         {tuple(p) for p in truth["buildings"]["pairs_a_b"]})

        rec = {}
        for a, b, s in db.execute(text(
                "SELECT l.source_id_raw, r.source_id_raw, c.score FROM match_candidates c JOIN source_features l ON "
                "l.id=c.left_feature_id JOIN source_features r ON r.id=c.right_feature_id WHERE c.run_id=:r AND "
                "c.match_class='match'"), {"r": run("record_feature")}):
            if a not in rec or s > rec[a][1]:
                rec[a] = (b, s)
        in_a = set(truth["parcels"]["a_ids"])
        rec_truth = {(k, v) for k, v in truth["records"]["links"].items() if v in in_a}
        out["record_linking"] = prf({(k, v[0]) for k, v in rec.items()}, rec_truth)
        out["record_linking"]["note"] = (f"{truth['records']['wrong_point']} has contradictory evidence (point in a "
                                         "neighbouring parcel) and is deliberately routed to review as 'possible'")

        # building -> parcel assignment (largest coverage)
        bp = {}
        for b, p_, s in db.execute(text(
                "SELECT l.source_id_raw, r.source_id_raw, (c.features->>'building_coverage')::float FROM match_candidates c "
                "JOIN source_features l ON l.id=c.left_feature_id JOIN source_features r ON r.id=c.right_feature_id "
                "WHERE c.run_id=:r"), {"r": run("building_parcel")}):
            if b not in bp or s > bp[b][1]:
                bp[b] = (p_, s)
        bp_truth = set()
        for parcel, v in truth["buildings"]["by_parcel"].items():
            # P-010's building deliberately straddles two parcels; P-030 is absent from the municipal layer
            if v["a"] and parcel != "P-010" and parcel in in_a:
                bp_truth.add((v["a"], parcel))
        out["building_to_parcel_assignment (excluding the deliberately straddling building)"] = prf(
            {(b, v[0]) for b, v in bp.items() if b != truth["buildings"]["crossing_building"]}, bp_truth)

        # conflict detection recall over the fixture's list of introduced issues
        rows = db.execute(text("SELECT category, feature_ids FROM conflicts WHERE project_id=:p"), {"p": pid}).all()
        raw = dict(db.execute(text("SELECT id, coalesce(source_id_raw, attributes->>'segment_id') FROM source_features")).all())
        found = []
        for exp in truth["conflicts"]:
            want = {exp[k] for k in ("parcel", "property", "segment") if k in exp} | set(exp.get("parcels", []))
            if exp["category"] == "parcel_id_mismatch":
                want = {exp["parcel"], exp["other_id"]}
            ok = any(cat == exp["category"] and {core(w) for w in want} <= {core(raw.get(i)) for i in fids}
                     for cat, fids in rows)
            found.append(ok)
        out["conflict_detection_recall"] = {"introduced": len(found), "detected": sum(found),
                                            "recall": round(sum(found) / len(found), 4),
                                            "total_conflicts_raised": len(rows)}

        # change detection (latest building version comparison)
        ch = {}
        for ctype, o, n in db.execute(text(
                "SELECT c.change_type, so.source_id_raw, sn.source_id_raw FROM change_records c LEFT JOIN source_features so "
                "ON so.id=c.old_feature_id LEFT JOIN source_features sn ON sn.id=c.new_feature_id WHERE c.project_id=:p "
                "AND c.change_type <> 'image_change'"), {"p": pid}):
            ch.setdefault(ctype, set()).add(n or o)
        ct = truth["changes"]
        out["change_detection"] = {k: prf(ch.get(k, set()), set(ct[k])) for k in
                                   ("added", "removed", "geometry_modified")}
        out["change_detection"]["within_uncertainty_not_flagged_as_modified"] = not (
            ch.get("geometry_modified", set()) & set(ct["within_uncertainty"]))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
