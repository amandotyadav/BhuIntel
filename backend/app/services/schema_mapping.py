"""Intelligent attribute (field) mapping.

Suggests correspondences between source fields and the canonical land-record schema using, in order:
1. deterministic built-in aliases and project-configured aliases,
2. mappings previously approved for the same field name in this project (reuse),
3. string similarity of the field name against canonical names and aliases (rapidfuzz),
4. data-type compatibility, and
5. value-profile evidence (date parse rate, numeric parse rate, uniqueness, vocabulary overlap).

An LLM provider is NOT used (no provider is configured in this codebase); see docs/ALGORITHMS.md.
Ambiguous suggestions are flagged and require explicit human confirmation.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
from rapidfuzz import fuzz

CANONICAL_FIELDS: dict[str, dict] = {
    "parcel_id": {
        "type": "string", "role": "id",
        "aliases": ["plot_no", "plot_number", "plotno", "plot_id", "parcel_no", "parcelid", "parcel_number",
                    "survey_no", "sy_no", "survey_number", "khasra_no", "khasra", "cts_no", "cs_no", "gat_no",
                    "dag_no", "plot"],
    },
    "ulpin": {"type": "string", "role": "id", "aliases": ["ulpin", "bhu_aadhaar", "unique_land_parcel_id"]},
    "property_id": {
        "type": "string", "role": "id",
        "aliases": ["property_number", "property_no", "prop_no", "prop_id", "propertyid", "pid",
                    "assessment_no", "assessment_number", "upic", "property_code"],
    },
    "building_id": {
        "type": "string", "role": "id",
        "aliases": ["bldg_id", "building_no", "building_number", "bldgid", "building_code", "structure_id"],
    },
    "land_use": {
        "type": "string", "role": "category",
        "aliases": ["landuse", "land_use_type", "lu", "lu_code", "usage", "use_type", "zone", "land_use_class",
                    "property_use"],
    },
    "area_sqm": {
        "type": "float", "role": "measure",
        "aliases": ["area", "area_m2", "area_sqm", "area_sq_m", "shape_area", "plot_area", "extent_sqm",
                    "area_sqmt", "recorded_area", "parcel_area"],
    },
    "surveyed_at": {
        "type": "date", "role": "date",
        "aliases": ["survey_date", "date_surveyed", "surv_dt", "survey_dt", "surveyed_on", "date_of_survey"],
    },
    "updated_at": {
        "type": "date", "role": "date",
        "aliases": ["last_updated", "modified", "upd_date", "update_date", "last_modified", "mutation_date"],
    },
    "owner_name": {
        "type": "string", "role": "personal",
        "aliases": ["owner", "owner_nm", "khatedar", "holder_name", "owner_names", "occupant", "owner_name"],
    },
    "ward": {"type": "string", "role": "admin", "aliases": ["ward_no", "ward_number", "ward_name", "ward_id"]},
    "village": {"type": "string", "role": "admin", "aliases": ["village_name", "mouza", "gaon", "revenue_village"]},
    "address": {"type": "string", "role": "text", "aliases": ["addr", "full_address", "address_line", "location"]},
    "building_levels": {
        "type": "integer", "role": "measure",
        "aliases": ["floors", "levels", "building_levels", "storeys", "no_of_floors", "num_floors"],
    },
    "height_m": {"type": "float", "role": "measure", "aliases": ["height", "bldg_height", "height_m"]},
}

LAND_USE_VOCAB = {
    "residential": ["res", "residential", "housing", "r", "resi"],
    "commercial": ["com", "comm", "commercial", "c", "business", "retail"],
    "industrial": ["ind", "industrial", "i", "industry"],
    "institutional": ["inst", "institutional", "public", "psp", "public_semi_public"],
    "mixed": ["mixed", "mix", "mixed_use", "res_com"],
    "agricultural": ["agri", "agricultural", "agriculture", "farm"],
    "open_space": ["open", "open_space", "park", "garden", "recreational"],
    "vacant": ["vacant", "empty", "unbuilt"],
    "transport": ["road", "transport", "transportation"],
}
_LU_LOOKUP = {alias: canon for canon, aliases in LAND_USE_VOCAB.items() for alias in aliases}

NAME_MIN_SCORE = 0.62
AMBIGUITY_MARGIN = 0.08


def norm_name(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower()).strip("_")
    return s


def normalize_identifier(value) -> str | None:
    """Canonical comparable form of an identifier, without destroying the original (kept separately).

    'Plot No. 012/A' -> '12A', 'P-0012' -> 'P12', ' sy 45 ' -> '45'.
    """
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if value.is_integer():
            value = int(value)
    s = str(value).strip().upper()
    if not s:
        return None
    s = re.sub(r"^(PLOT|PARCEL|SURVEY|SY|CTS|GAT|KHASRA|PROPERTY|PROP)\s*(NO|NUMBER|#)?\.?\s*", "", s)
    s = re.sub(r"[^0-9A-Z]", "", s)
    s = re.sub(r"(?<![0-9])0+(?=[0-9])", "", s)  # strip leading zeros of each digit run
    return s or None


def id_core(normalized: str | None) -> str | None:
    """Identifier without its leading alphabetic series prefix ('P12' -> '12', '12A' -> '12A')."""
    if not normalized:
        return None
    core = re.sub(r"^[A-Z]+(?=[0-9])", "", normalized)
    return core or normalized


def identifier_similarity(a: str | None, b: str | None) -> float | None:
    """Similarity of two *normalized* identifiers.

    1.0 exact; 0.95 same numeric core where one side has no series prefix ('12' vs 'P12', common when
    registers drop the prefix); 0.5 same core but *different* prefixes (likely different series);
    otherwise 0.9 x edit-distance similarity of the cores.
    """
    if not a or not b:
        return None
    if a == b:
        return 1.0
    ca, cb = id_core(a), id_core(b)
    if ca == cb:
        pa, pb = a[: len(a) - len(ca)], b[: len(b) - len(cb)]
        return 0.95 if (not pa or not pb) else 0.5
    return round(0.9 * fuzz.ratio(ca, cb) / 100.0, 4)


def normalize_land_use(value) -> str | None:
    if value is None:
        return None
    key = norm_name(str(value))
    return _LU_LOOKUP.get(key, key or None)


def coerce_value(target: str, value):
    """Coerce a raw value to the canonical type. Unparseable values become None (original is kept)."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    spec = CANONICAL_FIELDS.get(target)
    t = spec["type"] if spec else "string"
    try:
        if t == "float":
            return float(str(value).replace(",", ""))
        if t == "integer":
            return int(float(str(value).replace(",", "")))
        if t == "date":
            if isinstance(value, (date, datetime)):
                return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
            d = pd.to_datetime(str(value), errors="coerce", dayfirst=False)
            return None if pd.isna(d) else d.date().isoformat()
    except (ValueError, TypeError):
        return None
    if target == "land_use":
        return normalize_land_use(value)
    s = str(value).strip()
    return s or None


@dataclass
class Suggestion:
    source_field: str
    target_field: str | None
    score: float
    method: str
    ambiguous: bool
    evidence: dict


def _type_compat(canonical_type: str, prof: dict) -> float:
    t = prof.get("type", "string")
    if canonical_type == "string":
        return 1.0 if t in ("string", "integer") else 0.4
    if canonical_type in ("float", "integer"):
        if t in ("float", "integer"):
            return 1.0
        return float(prof.get("numeric_parse_rate", 0.0))
    if canonical_type == "date":
        if t == "datetime":
            return 1.0
        return float(prof.get("date_parse_rate", 0.0)) if t == "string" else 0.0
    return 0.5


def _profile_score(target: str, prof: dict) -> float:
    role = CANONICAL_FIELDS[target]["role"]
    if role == "id":
        return min(1.0, prof.get("unique_ratio", 0.0) * 0.7 + prof.get("alnum_id_like_rate", 0.5) * 0.3)
    if target == "land_use":
        samples = [norm_name(v) for v in prof.get("sample_values", [])]
        if not samples:
            return 0.0
        return sum(1 for v in samples if v in _LU_LOOKUP) / len(samples)
    if role == "date":
        return float(prof.get("date_parse_rate", 1.0 if prof.get("type") == "datetime" else 0.0))
    if role == "measure":
        return float(prof.get("numeric_parse_rate", 0.0))
    return 0.5


def _name_score(field: str, target: str, extra_aliases: dict[str, str]) -> tuple[float, str]:
    n = norm_name(field)
    if n == target or n in CANONICAL_FIELDS[target]["aliases"]:
        return 1.0, "alias"
    if extra_aliases.get(n) == target:
        return 1.0, "project_alias"
    names = [target, *CANONICAL_FIELDS[target]["aliases"]]
    best = max(
        max(fuzz.ratio(n, a), fuzz.token_sort_ratio(n.replace("_", " "), a.replace("_", " "))) for a in names
    )
    # Fuzzy evidence is capped below an exact alias so it can never outrank a deterministic rule.
    return min(best / 100.0, 0.95), "similarity"


def suggest_mappings(
    field_profile: dict[str, dict],
    project_aliases: dict[str, str] | None = None,
    approved_history: dict[str, str] | None = None,
) -> list[Suggestion]:
    """Return one suggestion per source field (target None = no confident correspondence)."""
    project_aliases = {norm_name(k): v for k, v in (project_aliases or {}).items()}
    approved_history = {norm_name(k): v for k, v in (approved_history or {}).items()}
    raw: list[Suggestion] = []
    for field, prof in field_profile.items():
        n = norm_name(field)
        scored = []
        for target, spec in CANONICAL_FIELDS.items():
            name_s, method = _name_score(field, target, project_aliases)
            if approved_history.get(n) == target:
                name_s, method = 1.0, "approved_reuse"
            type_s = _type_compat(spec["type"], prof)
            prof_s = _profile_score(target, prof)
            if name_s >= 1.0:
                total = 0.85 + 0.1 * type_s + 0.05 * prof_s
            else:
                total = 0.6 * name_s + 0.25 * type_s + 0.15 * prof_s
            scored.append((round(total, 4), target, method, {"name": round(name_s, 3), "type": round(type_s, 3),
                                                             "profile": round(prof_s, 3)}))
        scored.sort(reverse=True)
        best = scored[0]
        second = scored[1]
        evidence = {"components": best[3], "runner_up": {"target": second[1], "score": second[0]}}
        if best[3]["name"] < NAME_MIN_SCORE:
            raw.append(Suggestion(field, None, best[0], "none", False, evidence))
            continue
        ambiguous = (best[0] - second[0]) < AMBIGUITY_MARGIN and best[2] not in ("alias", "project_alias",
                                                                                   "approved_reuse")
        raw.append(Suggestion(field, best[1], best[0], best[2], ambiguous, evidence))

    # Two source fields competing for the same target -> both ambiguous (unless one is an exact alias).
    by_target: dict[str, list[Suggestion]] = {}
    for s in raw:
        if s.target_field:
            by_target.setdefault(s.target_field, []).append(s)
    for group in by_target.values():
        if len(group) > 1:
            exact = [g for g in group if g.method in ("alias", "project_alias", "approved_reuse")]
            for g in group:
                if len(exact) == 1 and g is exact[0]:
                    continue
                g.ambiguous = True
                g.evidence["competing_fields"] = [x.source_field for x in group if x is not g]
    return raw


def apply_mappings(attributes: dict, mappings: dict[str, str]) -> dict:
    """Build normalized attributes from approved mappings {source_field: target_field}."""
    out: dict = {}
    for src, target in mappings.items():
        if src in attributes:
            out[target] = coerce_value(target, attributes[src])
    return out
