"""Pydantic request/response schemas (validated at the API boundary)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["viewer", "analyst", "reviewer", "admin"]
DatasetKind = Literal["parcels", "buildings", "predicted_buildings", "property_records", "utility_lines", "imagery",
                      "other"]
Provenance = Literal["real", "synthetic", "derived"]
MatchType = Literal["building_building", "parcel_parcel", "building_parcel", "record_feature"]
Decision = Literal["approve", "reject", "defer", "comment"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    user: "UserOut"


EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class UserCreate(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=255)
    full_name: str = Field(default="", max_length=255)
    password: str = Field(min_length=10, max_length=256)
    role: Role = "viewer"


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


class UserOut(ORM):
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    working_crs: int | None = Field(default=None, description="Projected EPSG code in metres; suggested from the "
                                                               "study area if omitted")
    default_tolerance_m: float = Field(default=5.0, gt=0, le=1000)
    study_area_geojson: dict | None = None
    settings: dict = {}


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    default_tolerance_m: float | None = Field(default=None, gt=0, le=1000)
    status: Literal["active", "archived"] | None = None
    settings: dict | None = None


class ProjectOut(ORM):
    id: int
    name: str
    description: str
    owner_id: int
    working_crs: int
    default_tolerance_m: float
    settings: dict
    status: str
    created_at: datetime


class MemberIn(BaseModel):
    user_id: int
    role: Role


class StudyAreaIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    geojson: dict

    @field_validator("geojson")
    @classmethod
    def _poly(cls, v):
        t = v.get("type")
        if t == "Feature":
            v = v.get("geometry") or {}
            t = v.get("type")
        if t == "FeatureCollection":
            feats = v.get("features") or []
            if len(feats) != 1:
                raise ValueError("FeatureCollection must contain exactly one feature")
            v = feats[0].get("geometry") or {}
            t = v.get("type")
        if t not in ("Polygon", "MultiPolygon"):
            raise ValueError("study area must be a Polygon or MultiPolygon in EPSG:4326")
        return v


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: DatasetKind
    provenance: Provenance = "real"
    provider: str = ""
    source_url: str = ""
    license: str = ""
    attribution: str = ""
    description: str = ""
    reliability: float = Field(default=0.5, ge=0, le=1)
    positional_accuracy_m: float | None = Field(default=None, ge=0, le=1000)
    source_date: datetime | None = None


class DatasetUpdate(BaseModel):
    name: str | None = None
    provider: str | None = None
    source_url: str | None = None
    license: str | None = None
    attribution: str | None = None
    description: str | None = None
    reliability: float | None = Field(default=None, ge=0, le=1)
    positional_accuracy_m: float | None = Field(default=None, ge=0, le=1000)
    source_date: datetime | None = None


class DatasetOut(ORM):
    id: int
    project_id: int
    name: str
    kind: str
    provenance: str
    provider: str
    source_url: str
    license: str
    attribution: str
    description: str
    reliability: float
    positional_accuracy_m: float | None
    source_date: datetime | None
    status: str
    created_at: datetime


class VersionOut(ORM):
    id: int
    dataset_id: int
    version_no: int
    file_sha256: str
    original_filename: str
    format: str
    size_bytes: int
    layer: str | None
    source_crs: str | None
    crs_confirmed: bool
    geometry_type: str | None
    feature_count: int | None
    schema_: dict = Field(validation_alias="schema", serialization_alias="schema")
    field_profile: dict
    csv_options: dict
    status: str
    error: str | None
    processing_log: list
    stats: dict
    created_at: datetime
    processed_at: datetime | None


class CrsConfirm(BaseModel):
    crs: str = Field(min_length=3, max_length=5000, description="e.g. 'EPSG:32643' or WKT")
    justification: str = Field(min_length=3, max_length=1000, description="Evidence for the CRS (metadata, control)")


class CsvOptions(BaseModel):
    x_column: str | None = None
    y_column: str | None = None
    wkt_column: str | None = None


class ProcessIn(BaseModel):
    study_area_filter: bool = False


class MappingOut(ORM):
    id: int
    dataset_id: int
    source_field: str
    target_field: str | None
    method: str
    score: float
    evidence: dict
    ambiguous: bool
    status: str


class MappingDecision(BaseModel):
    status: Literal["approved", "rejected"]
    target_field: str | None = None
    confirm_ambiguous: bool = False


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=255)
    target_field: str


class MatchRunCreate(BaseModel):
    left_version_id: int
    right_version_id: int
    match_type: MatchType
    method: Literal["deterministic", "random_forest"] = "deterministic"
    tolerance_m: float | None = Field(default=None, gt=0, le=1000)
    weights: dict[str, float] | None = None
    thresholds: dict[str, float] | None = None
    min_building_share: float = Field(default=0.05, ge=0, le=1)


class MatchRunOut(ORM):
    id: int
    project_id: int
    left_version_id: int
    right_version_id: int
    match_type: str
    method: str
    model_version_id: int | None
    params: dict
    stats: dict
    status: str
    created_at: datetime


class DecisionIn(BaseModel):
    decision: Decision
    comment: str = Field(default="", max_length=4000)
    acknowledge_legal: bool = False
    expected_version: int | None = None


class TopologyIn(BaseModel):
    version_id: int
    params: dict[str, float] | None = None


class ChangeIn(BaseModel):
    old_version_id: int
    new_version_id: int
    params: dict[str, Any] | None = None


class TrainIn(BaseModel):
    match_type: MatchType
    activate: bool = False


class LabelIn(BaseModel):
    label: Literal[0, 1]


class IntegrationIn(BaseModel):
    primary_parcel_version_id: int
    parcel_match_run_id: int | None = None
    record_match_run_id: int | None = None
    building_match_run_id: int | None = None
    building_parcel_run_id: int | None = None
    building_version_ids: list[int] = []


class ExportIn(BaseModel):
    format: Literal["gpkg", "geojson"] = "gpkg"
    include_retired: bool = False


class ExtractIn(BaseModel):
    raster_version_id: int
    backend: Literal["threshold_baseline", "onnx"] = "threshold_baseline"
    model_path: str | None = None
    model_meta_path: str | None = None
    threshold: float = Field(default=0.5, gt=0, lt=1)
    overlap: int = Field(default=64, ge=0, le=512)
    min_area_m2: float = Field(default=10.0, ge=0)
    dataset_name: str | None = None


class AcquireIn(BaseModel):
    source_key: str
    location: str = "India"
    dataset_name: str | None = None
    start: str | None = None
    end: str | None = None
    max_cloud: float | None = None


class JobOut(ORM):
    id: int
    project_id: int | None
    job_type: str
    params: dict
    status: str
    progress: float
    message: str
    result: dict
    error: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


TokenOut.model_rebuild()
