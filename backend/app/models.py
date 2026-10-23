"""SQLAlchemy / GeoAlchemy2 ORM models for the BhuIntel PostGIS schema.

Geometry conventions
--------------------
* ``geom``       canonical geometry in EPSG:4326 (for display, export and interchange).
* ``geom_work``  same geometry transformed to the project's projected working CRS
                 (metres) - all distance/area/overlap computations use this column.
* ``geom_source`` geometry exactly as read from the source file, in the source CRS
                 (never modified; preserves original data).
"""

from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


GEOM_4326 = Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False)
GEOM_ANY = Geometry(geometry_type="GEOMETRY", srid=-1, spatial_index=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    # viewer < analyst < reviewer < admin
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # Projected working CRS (EPSG code) used for metric computations, e.g. 32643 (UTM 43N).
    working_crs: Mapped[int] = mapped_column(Integer)
    default_tolerance_m: Mapped[float] = mapped_column(Float, default=5.0)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    members: Mapped[list["ProjectMember"]] = relationship(back_populates="project", cascade="all, delete-orphan")


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # Project-level role caps the user's global role inside this project.
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    project: Mapped[Project] = relationship(back_populates="members")


class StudyArea(Base):
    __tablename__ = "study_areas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    geom = mapped_column(GEOM_4326)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Dataset(Base):
    """A logical dataset (e.g. "Municipal parcels"); files are uploaded as versions."""

    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # parcels | buildings | predicted_buildings | property_records | utility_lines | imagery | other
    kind: Mapped[str] = mapped_column(String(64))
    # real | synthetic | derived  (provenance class - synthetic data must never be labelled official)
    provenance: Mapped[str] = mapped_column(String(32), default="real")
    provider: Mapped[str] = mapped_column(String(255), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    license: Mapped[str] = mapped_column(String(255), default="")
    attribution: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    # Relative reliability 0..1 used by conflict resolution (configurable per dataset).
    reliability: Mapped[float] = mapped_column(Float, default=0.5)
    # Documented horizontal accuracy of the source (metres), if known.
    positional_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | archived
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["DatasetVersion"]] = relationship(
        back_populates="dataset", order_by="DatasetVersion.version_no"
    )


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version_no"),
        # Idempotency: the same file uploaded twice to the same dataset maps to one version.
        UniqueConstraint("dataset_id", "file_sha256", name="uq_version_file_hash"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    file_sha256: Mapped[str] = mapped_column(String(64))
    original_filename: Mapped[str] = mapped_column(String(512))
    storage_key: Mapped[str] = mapped_column(String(1024))
    format: Mapped[str] = mapped_column(String(64))  # geojson | shapefile | gpkg | csv | geotiff | geoparquet ...
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    layer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_crs: Mapped[str | None] = mapped_column(String(255), nullable=True)  # e.g. "EPSG:32643" or WKT
    crs_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    crs_confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    geometry_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extent = mapped_column(GEOM_4326, nullable=True)
    schema: Mapped[dict] = mapped_column(JSONB, default=dict)  # field name -> type
    field_profile: Mapped[dict] = mapped_column(JSONB, default=dict)  # per-field stats
    csv_options: Mapped[dict] = mapped_column(JSONB, default=dict)  # x/y/wkt columns for CSV
    # uploaded | inspected | needs_crs | queued | processing | processed | failed | partial
    status: Mapped[str] = mapped_column(String(32), default="uploaded")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_log: Mapped[list] = mapped_column(JSONB, default=list)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    dataset: Mapped[Dataset] = relationship(back_populates="versions")


class SourceFeature(Base):
    __tablename__ = "source_features"
    __table_args__ = (UniqueConstraint("dataset_version_id", "source_fid"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"), index=True
    )
    source_fid: Mapped[str] = mapped_column(String(255))  # row index / FID in the source file
    source_id_raw: Mapped[str | None] = mapped_column(String(255), nullable=True)  # original identifier value
    normalized_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)  # untouched original attributes
    normalized_attributes: Mapped[dict] = mapped_column(JSONB, default=dict)  # after approved mapping
    geom_source = mapped_column(GEOM_ANY, nullable=True)
    geom = mapped_column(GEOM_4326, nullable=True)
    geom_work = mapped_column(GEOM_ANY, nullable=True)
    geometry_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_valid_source: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    validity_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    repair_status: Mapped[str] = mapped_column(String(32), default="none")  # none | auto_repaired | needs_review
    row_hash: Mapped[str] = mapped_column(String(64), index=True)
    duplicate_of: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # For predicted buildings: pending | accepted | rejected (NULL for ordinary features)
    review_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RasterAsset(Base):
    __tablename__ = "raster_assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_version_id: Mapped[int] = mapped_column(
        ForeignKey("dataset_versions.id", ondelete="CASCADE"), unique=True
    )
    storage_key: Mapped[str] = mapped_column(String(1024))
    crs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    band_count: Mapped[int] = mapped_column(Integer)
    dtype: Mapped[str] = mapped_column(String(32))
    nodata: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution_x: Mapped[float] = mapped_column(Float)
    resolution_y: Mapped[float] = mapped_column(Float)
    resolution_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_cog: Mapped[bool] = mapped_column(Boolean, default=False)
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    footprint = mapped_column(GEOM_4326, nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)


class FieldMapping(Base):
    __tablename__ = "field_mappings"
    __table_args__ = (UniqueConstraint("dataset_id", "source_field"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    source_field: Mapped[str] = mapped_column(String(255))
    target_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    method: Mapped[str] = mapped_column(String(64))  # alias | similarity | profile | manual | llm
    score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    ambiguous: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="suggested")  # suggested | approved | rejected
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FieldAlias(Base):
    """Project-configurable alias -> canonical field mapping (in addition to built-in aliases)."""

    __tablename__ = "field_aliases"
    __table_args__ = (UniqueConstraint("project_id", "alias"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    alias: Mapped[str] = mapped_column(String(255))
    target_field: Mapped[str] = mapped_column(String(255))


class MatchRun(Base):
    __tablename__ = "match_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    left_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"))
    right_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"))
    # building_building | building_parcel | parcel_parcel | record_feature
    match_type: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(64), default="deterministic")  # deterministic | random_forest
    model_version_id: Mapped[int | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatchCandidate(Base):
    __tablename__ = "match_candidates"
    __table_args__ = (UniqueConstraint("run_id", "left_feature_id", "right_feature_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("match_runs.id", ondelete="CASCADE"), index=True)
    left_feature_id: Mapped[int] = mapped_column(ForeignKey("source_features.id", ondelete="CASCADE"), index=True)
    right_feature_id: Mapped[int] = mapped_column(ForeignKey("source_features.id", ondelete="CASCADE"), index=True)
    features: Mapped[dict] = mapped_column(JSONB, default=dict)  # computed matching features
    method: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column(Float)
    score_kind: Mapped[str] = mapped_column(String(64), default="heuristic_match_score")
    match_class: Mapped[str] = mapped_column(String(32))  # match | possible | non_match
    relationship: Mapped[str | None] = mapped_column(String(32), nullable=True)  # 1:1, 1:n, n:1, n:m
    explanation: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|accepted|rejected|deferred
    # Training label: 1 match, 0 non-match. label_source: reviewer | imported | synthetic_fixture
    label: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Conflict(Base):
    __tablename__ = "conflicts"
    __table_args__ = (UniqueConstraint("project_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("match_runs.id", ondelete="SET NULL"), nullable=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))  # low | medium | high | critical
    priority: Mapped[float] = mapped_column(Float)  # 0..100, higher = review sooner
    confidence: Mapped[float] = mapped_column(Float)  # confidence that the conflict is real (heuristic)
    feature_ids: Mapped[list] = mapped_column(ARRAY(BigInteger), default=list)
    dataset_version_ids: Mapped[list] = mapped_column(ARRAY(Integer), default=list)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    explanation: Mapped[str] = mapped_column(Text, default="")
    proposed_action: Mapped[str] = mapped_column(Text, default="")
    legal_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|approved|rejected|deferred
    fingerprint: Mapped[str] = mapped_column(String(64))  # stable hash -> idempotent re-detection
    geom = mapped_column(GEOM_4326, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Correction(Base):
    __tablename__ = "corrections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    conflict_id: Mapped[int | None] = mapped_column(ForeignKey("conflicts.id", ondelete="SET NULL"), nullable=True)
    source_feature_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_features.id", ondelete="CASCADE"), nullable=True
    )
    integrated_feature_id: Mapped[int | None] = mapped_column(
        ForeignKey("integrated_features.id", ondelete="SET NULL"), nullable=True
    )
    # Version of the integrated feature this correction was computed against (optimistic concurrency).
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(64))  # geometry_repair | attribute_update | assign_id | ...
    safety: Mapped[str] = mapped_column(String(32))  # auto_safe | review_required | legal_no_auto
    original_geom = mapped_column(GEOM_4326, nullable=True)
    proposed_geom = mapped_column(GEOM_4326, nullable=True)
    original_attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    proposed_attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(128))
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="proposed")  # proposed|approved|rejected|deferred|applied
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReviewDecision(Base):
    """Append-only reviewer decisions (protected by a DB trigger against UPDATE/DELETE)."""

    __tablename__ = "review_decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    subject_type: Mapped[str] = mapped_column(String(32))  # conflict | correction | match | prediction
    subject_id: Mapped[int] = mapped_column(BigInteger)
    decision: Mapped[str] = mapped_column(String(32))  # approve | reject | defer | comment
    comment: Mapped[str] = mapped_column(Text, default="")
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntegratedFeature(Base):
    __tablename__ = "integrated_features"
    __table_args__ = (UniqueConstraint("project_id", "feature_class", "integrated_key"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    feature_class: Mapped[str] = mapped_column(String(32))  # parcel | building
    integrated_key: Mapped[str] = mapped_column(String(255))
    attributes: Mapped[dict] = mapped_column(JSONB, default=dict)
    geom = mapped_column(GEOM_4326)
    source_feature_ids: Mapped[list] = mapped_column(ARRAY(BigInteger), default=list)
    lineage: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | retired
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntegratedFeatureHistory(Base):
    """Append-only history of every version of every integrated feature."""

    __tablename__ = "integrated_feature_history"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    integrated_feature_id: Mapped[int] = mapped_column(
        ForeignKey("integrated_features.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    attributes: Mapped[dict] = mapped_column(JSONB)
    geom = mapped_column(GEOM_4326)
    status: Mapped[str] = mapped_column(String(32))
    change_reason: Mapped[str] = mapped_column(Text)
    correction_id: Mapped[int | None] = mapped_column(ForeignKey("corrections.id"), nullable=True)
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChangeRecord(Base):
    __tablename__ = "change_records"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    old_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"))
    new_version_id: Mapped[int] = mapped_column(ForeignKey("dataset_versions.id", ondelete="CASCADE"))
    # added | removed | geometry_modified | attribute_changed | uncertain | unchanged
    change_type: Mapped[str] = mapped_column(String(32), index=True)
    old_feature_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    new_feature_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    explanation: Mapped[str] = mapped_column(Text, default="")
    geom = mapped_column(GEOM_4326, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    job_type: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    # queued | running | succeeded | failed | partial | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1, reported by the job itself
    message: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("project_id", "name", "version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))  # e.g. match_rf_building_building
    version: Mapped[int] = mapped_column(Integer)
    algorithm: Mapped[str] = mapped_column(String(64))
    match_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feature_schema: Mapped[dict] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    training_info: Mapped[dict] = mapped_column(JSONB, default=dict)
    artifact_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32))  # trained | insufficient_data | failed
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AcquisitionManifest(Base):
    __tablename__ = "acquisition_manifests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    source_key: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(32))  # unavailable | unauthorized | no_coverage | completed | failed
    reachable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    files: Mapped[list] = mapped_column(JSONB, default=list)  # [{name, sha256, size, crs, extent, ...}]
    source_meta: Mapped[dict] = mapped_column(JSONB, default=dict)  # provider, license, attribution, url
    dataset_version_id: Mapped[int | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    """Append-only, hash-chained audit log (UPDATE/DELETE blocked by a DB trigger)."""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(128))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64))
