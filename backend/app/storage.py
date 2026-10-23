"""Object storage abstraction. ``LocalStorage`` is the development implementation; an S3-compatible
backend can implement the same interface (put/open/path/delete/exists) without touching callers."""

import hashlib
import os
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Protocol

from app.config import get_settings


class Storage(Protocol):
    def put_stream(self, prefix: str, filename: str, stream: BinaryIO, max_bytes: int) -> tuple[str, str, int]: ...
    def put_bytes(self, prefix: str, filename: str, data: bytes) -> str: ...
    def local_path(self, key: str) -> Path: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class UploadTooLarge(Exception):
    pass


def _safe_name(filename: str) -> str:
    base = os.path.basename(filename.replace("\\", "/"))
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in base)
    return keep[:200] or "upload.bin"


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if not p.is_relative_to(self.root):
            raise ValueError("storage key escapes storage root")
        return p

    def put_stream(self, prefix: str, filename: str, stream: BinaryIO, max_bytes: int) -> tuple[str, str, int]:
        """Stream to disk in chunks, enforcing ``max_bytes``. Returns (key, sha256, size)."""
        key = f"{prefix}/{uuid.uuid4().hex}/{_safe_name(filename)}"
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        size = 0
        try:
            with open(dest, "wb") as out:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise UploadTooLarge()
                    h.update(chunk)
                    out.write(chunk)
        except BaseException:
            shutil.rmtree(dest.parent, ignore_errors=True)
            raise
        return key, h.hexdigest(), size

    def put_bytes(self, prefix: str, filename: str, data: bytes) -> str:
        key = f"{prefix}/{uuid.uuid4().hex}/{_safe_name(filename)}"
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return key

    def local_path(self, key: str) -> Path:
        return self._resolve(key)

    def delete(self, key: str) -> None:
        p = self._resolve(key)
        if p.exists():
            shutil.rmtree(p.parent, ignore_errors=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()


_storage: LocalStorage | None = None


def get_storage() -> LocalStorage:
    global _storage
    if _storage is None or _storage.root != get_settings().storage_root.resolve():
        _storage = LocalStorage(get_settings().storage_root)
    return _storage
