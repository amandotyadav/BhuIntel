from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import ApiError
from app.middleware import rate_limit_login
from app.models import User
from app.schemas import TokenOut, UserCreate, UserOut, UserUpdate
from app.security import create_access_token, get_current_user, hash_password, require_role, verify_password
from app.services import audit

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.post("/auth/token", response_model=TokenOut, summary="Obtain a bearer token (OAuth2 password flow)")
def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    rate_limit_login(request)
    user = db.execute(select(User).where(func.lower(User.email) == form.username.lower())).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(form.password, user.password_hash):
        audit.record(db, "auth.login_failed", "user", None, details={})
        db.commit()
        raise ApiError(401, "invalid_credentials", "Incorrect email or password")
    audit.record(db, "auth.login", "user", user.id, actor_id=user.id)
    db.commit()
    return TokenOut(access_token=create_access_token(user), user=UserOut.model_validate(user))


@router.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require_role("admin"))])
def list_users(db: Session = Depends(get_db)):
    return db.execute(select(User).order_by(User.id)).scalars().all()


@router.get("/users/directory", response_model=list[UserOut], summary="Active users (for adding project members)")
def directory(db: Session = Depends(get_db), user: User = Depends(require_role("reviewer"))):
    return db.execute(select(User).where(User.is_active.is_(True)).order_by(User.email)).scalars().all()


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, db: Session = Depends(get_db), admin: User = Depends(require_role("admin"))):
    if db.execute(select(User).where(func.lower(User.email) == body.email.lower())).scalar_one_or_none():
        raise ApiError(409, "conflict", "A user with this email already exists")
    u = User(email=body.email.lower(), full_name=body.full_name, role=body.role,
             password_hash=hash_password(body.password))
    db.add(u)
    db.flush()
    audit.record(db, "user.created", "user", u.id, actor_id=admin.id, details={"role": u.role})
    db.commit()
    return u


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, db: Session = Depends(get_db),
                admin: User = Depends(require_role("admin"))):
    u = db.get(User, user_id)
    if u is None:
        raise ApiError(404, "not_found", "User not found")
    changes = body.model_dump(exclude_none=True)
    if "password" in changes:
        u.password_hash = hash_password(changes.pop("password"))
        changes["password"] = "changed"  # noqa: S105 - audit marker, not a secret
    for k, v in changes.items():
        if k != "password":
            setattr(u, k, v)
    audit.record(db, "user.updated", "user", u.id, actor_id=admin.id, details={"fields": sorted(changes)})
    db.commit()
    return u
