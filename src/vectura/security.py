"""API-key authentication -> principal (user, role); permissions come from the role_permissions table."""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_session
from .models import RolePermission


@dataclass(frozen=True)
class Principal:
    user: str
    role: str


def get_principal(x_api_key: Optional[str] = Header(default=None), cfg: Settings = Depends(get_settings)) -> Principal:
    if not cfg.auth_enabled:
        return Principal("local-dev", cfg.dev_role)
    for key, who in cfg.api_keys.items():
        if x_api_key and hmac.compare_digest(x_api_key, key):
            return Principal(who["user"], who["role"])
    raise HTTPException(status_code=401, detail="Invalid or missing API key")


def has_permission(session: Session, role: str, permission: str) -> bool:
    return session.get(RolePermission, (role, permission)) is not None


def require_permission(permission: str) -> Callable[..., Principal]:
    def dep(p: Principal = Depends(get_principal), s: Session = Depends(get_session)) -> Principal:
        if not has_permission(s, p.role, permission):
            raise HTTPException(status_code=403, detail=f"Role '{p.role}' lacks permission '{permission}'")
        return p
    return dep
