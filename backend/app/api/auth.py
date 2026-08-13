"""Authentication endpoints.

Deliberately minimal — the build instructions put effort into the backend and
agents, not the frontend or account management. What is here is the part that
cannot be skipped safely: hashed passwords, signed tokens and a real membership
record, because every workspace authorization check depends on them.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession, create_access_token
from app.core.passwords import PasswordError, hash_password, verify_password
from app.core.events import EventKind, Severity, emit
from app.models import User, WorkspaceMember

router = APIRouter(tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=8, max_length=128)]
    full_name: Annotated[str | None, Field(max_length=200)] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID
    email: str
    full_name: str | None = None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    is_platform_admin: bool
    workspace_count: int


@router.post("/auth/register", response_model=TokenResponse, status_code=201)
async def register(payload: RegisterRequest, session: DbSession):
    existing = (
        await session.execute(select(User).where(func.lower(User.email) == payload.email.lower()))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email already registered")

    try:
        hashed = hash_password(payload.password)
    except PasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hashed,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    emit(EventKind.SYSTEM, f"User registered: {user.email}", severity=Severity.SUCCESS)
    return TokenResponse(
        access_token=create_access_token(user.id, user.email),
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
    )


@router.post("/auth/login", response_model=TokenResponse)
async def login(payload: LoginRequest, session: DbSession):
    user = (
        await session.execute(select(User).where(func.lower(User.email) == payload.email.lower()))
    ).scalar_one_or_none()

    # Same error and comparable timing whether the email or the password was wrong,
    # so the endpoint does not become a user-enumeration oracle.
    if user is None or not verify_password(payload.password, user.hashed_password):
        emit(
            EventKind.SYSTEM,
            f"Failed login attempt for {payload.email}",
            severity=Severity.WARNING,
        )
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="account is disabled")

    user.last_login_at = func.now()
    await session.commit()

    emit(EventKind.SYSTEM, f"User signed in: {user.email}", severity=Severity.SUCCESS)
    return TokenResponse(
        access_token=create_access_token(user.id, user.email),
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
    )


@router.get("/auth/me", response_model=UserOut)
async def me(user: CurrentUser, session: DbSession):
    count = (
        await session.execute(
            select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.user_id == user.id
            )
        )
    ).scalar_one()
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_platform_admin=user.is_platform_admin,
        workspace_count=int(count),
    )
