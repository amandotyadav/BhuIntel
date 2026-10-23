"""Application configuration loaded from environment variables (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BHU_", env_file=".env", extra="ignore")

    env: str = "development"  # development | test | production
    database_url: str = "postgresql+psycopg://bhuintel:bhuintel@localhost:5432/bhuintel"

    # Security
    secret_key: str = Field(default="dev-insecure-change-me-0123456789abcdef", min_length=32)
    access_token_minutes: int = 60 * 8
    cors_origins: list[str] = ["http://localhost:5173"]
    rate_limit_per_minute: int = 300
    login_rate_limit_per_minute: int = 10

    # Storage
    storage_backend: str = "local"  # local (filesystem). Other backends plug into app.storage.Storage.
    storage_root: Path = Path("./storage")

    # Upload / processing resource limits
    max_upload_mb: int = 512
    max_zip_members: int = 2000
    max_zip_uncompressed_mb: int = 2048
    max_features_per_dataset: int = 2_000_000
    max_raster_pixels: int = 400_000_000
    job_timeout_seconds: int = 3600
    worker_poll_seconds: float = 2.0
    # When true the API also runs queued jobs in a background thread (single-process dev mode).
    inline_worker: bool = False

    # Matching defaults (projected units = metres)
    default_tolerance_m: float = 5.0

    # Dataset acquisition
    acquisition_max_tiles: int = 8
    acquisition_max_download_mb: int = 500
    acquisition_timeout_seconds: float = 60.0
    copernicus_client_id: str | None = None
    copernicus_client_secret: str | None = None

    # Bootstrap admin (created by scripts/seed.py when no users exist)
    bootstrap_admin_email: str = "admin@example.org"
    bootstrap_admin_password: str | None = None

    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_production and s.secret_key.startswith("dev-insecure"):
        raise RuntimeError("BHU_SECRET_KEY must be set to a strong secret in production")
    return s
