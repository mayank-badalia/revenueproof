"""Shared FastAPI dependencies: sessions, authentication and workspace authorization.

Object-level authorization is deliberately a dependency rather than a convention.
OWASP API1:2023 is about exactly this failure — a valid token being treated as
permission to read *any* object ID. `require_workspace` resolves the membership
every time, so a reviewer cannot reach another company's evidence by changing a
UUID in the URL.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_sessionmaker, set_workspace_scope
from app.models import User, Workspace, WorkspaceMember
from app.models.enums import UserRole

# Roles permitted to resolve review items and override material classifications.
RESOLVER_ROLES = {UserRole.OWNER, UserRole.ANALYST, UserRole.ADMIN}


async def get_db() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_db)]


def create_access_token(user_id: uuid.UUID, email: str) -> str:
    expires = datetime.now(UTC) + timedelta(minutes=settings.jwt_expiry_minutes)
    payload = {"sub": str(user_id), "email": email, "exp": expires, "iat": datetime.now(UTC)}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="token expired"
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        ) from exc


async def get_current_user(
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(authorization.split(" ", 1)[1].strip())
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="malformed token subject") from exc

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found or inactive")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


class WorkspaceContext:
    """A verified (user, workspace, role) triple for one request."""

    def __init__(self, workspace: Workspace, user: User, role: UserRole) -> None:
        self.workspace = workspace
        self.user = user
        self.role = role

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.workspace.id

    @property
    def can_resolve(self) -> bool:
        return self.role in RESOLVER_ROLES

    def require_resolver(self) -> None:
        """External reviewers may read and comment but not override (§17 RBAC)."""
        if not self.can_resolve:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{self.role}' cannot resolve or override decisions",
            )


async def require_workspace(
    workspace_id: uuid.UUID,
    session: DbSession,
    user: CurrentUser,
) -> WorkspaceContext:
    """Authorize the caller against this specific workspace, then scope the session."""
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    if user.is_platform_admin:
        role = UserRole.ADMIN
    else:
        membership = (
            await session.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            # 404 rather than 403: revealing that a workspace exists is itself
            # information a non-member should not receive.
            raise HTTPException(status_code=404, detail="workspace not found")
        role = UserRole(membership.role)

    # Bind row-level security for the remainder of this transaction.
    await set_workspace_scope(session, str(workspace_id))
    return WorkspaceContext(workspace, user, role)


Workspace_ = Annotated[WorkspaceContext, Depends(require_workspace)]
