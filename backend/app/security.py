"""Authentication (argon2 password hashing + JWT bearer tokens) and role-based access control."""

from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.errors import ApiError
from app.models import Project, ProjectMember, User

ROLES = ["viewer", "analyst", "reviewer", "admin"]
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

_hasher = PasswordHasher()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_access_token(user: User) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.access_token_minutes)).timestamp()),
    }
    return jwt.encode(payload, s.secret_key, algorithm="HS256")


def get_current_user(token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    if not token:
        raise ApiError(401, "not_authenticated", "Authentication required")
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "Invalid or expired token") from None
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise ApiError(401, "invalid_token", "User inactive or not found")
    return user


def require_role(min_role: str):
    def dep(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise ApiError(403, "forbidden", f"Requires role '{min_role}' or higher")
        return user

    return dep


def effective_project_role(db: Session, user: User, project: Project) -> str | None:
    """Global admins have full access; otherwise the lower of global role and project membership role."""
    if user.role == "admin":
        return "admin"
    if project.owner_id == user.id:
        return user.role
    m = db.execute(
        select(ProjectMember).where(ProjectMember.project_id == project.id, ProjectMember.user_id == user.id)
    ).scalar_one_or_none()
    if m is None:
        return None
    return min(user.role, m.role, key=lambda r: ROLE_RANK[r])


def authorize_project(db: Session, user: User, project_id: int, min_role: str = "viewer") -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise ApiError(404, "not_found", "Project not found")
    role = effective_project_role(db, user, project)
    if role is None:
        # Do not reveal existence of projects the user cannot access.
        raise ApiError(404, "not_found", "Project not found")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise ApiError(403, "forbidden", f"Requires project role '{min_role}' or higher")
    return project
