"""Contexto de usuário/organização (tenant) e autorização por papel.

O organization_id NUNCA vem do navegador: é derivado da associação do usuário
autenticado (organization_members), consultada no backend a cada requisição.
"""
from __future__ import annotations

import functools
import re
from typing import Any, Callable

from flask import abort, g, redirect, request, session, url_for

from app.services.supabase_client import get_service_client

ROLES = ("owner", "admin", "developer", "viewer")
ROLE_RANK = {"viewer": 1, "developer": 2, "admin": 3, "owner": 4}
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "org"


def get_current_user() -> dict[str, Any] | None:
    """Usuário da sessão (validada no login pelo Supabase Auth)."""
    user = session.get("user")
    if not user or not user.get("id"):
        return None
    return user


def get_membership(user_id: str) -> dict[str, Any] | None:
    """Associação do usuário à organização (MVP: primeira/única organização)."""
    result = (
        get_service_client()
        .table("organization_members")
        .select("organization_id, role, organizations(id, name, slug)")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    org = row.get("organizations") or {}
    return {
        "organization_id": row["organization_id"],
        "role": row["role"],
        "org_name": org.get("name", ""),
        "org_slug": org.get("slug", ""),
    }


def load_current_context() -> None:
    """Popula g.user e g.membership a cada requisição (before_request)."""
    g.user = get_current_user()
    g.membership = get_membership(g.user["id"]) if g.user else None


def create_organization_for_user(user_id: str, name: str) -> dict[str, Any]:
    """Cria a organização inicial do usuário e o associa como owner."""
    name = (name or "").strip()[:120] or "Minha Organização"
    client = get_service_client()
    org = client.table("organizations").insert(
        {"name": name, "slug": slugify(name), "owner_user_id": user_id}
    ).execute().data[0]
    client.table("organization_members").insert(
        {"organization_id": org["id"], "user_id": user_id, "role": "owner"}
    ).execute()
    return org


def login_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def org_required(view: Callable) -> Callable:
    """Exige usuário autenticado E associado a uma organização."""
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.path))
        if not g.get("membership"):
            abort(403, description="Usuário sem organização associada.")
        return view(*args, **kwargs)

    return wrapper


def require_role(*roles: str) -> Callable:
    """Restringe a rota a papéis mínimos (owner > admin > developer > viewer)."""
    min_rank = min(ROLE_RANK[r] for r in roles) if roles else ROLE_RANK["viewer"]

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            membership = g.get("membership")
            if not membership:
                abort(403, description="Usuário sem organização associada.")
            if ROLE_RANK.get(membership["role"], 0) < min_rank:
                abort(403, description="Permissão insuficiente para esta ação.")
            return view(*args, **kwargs)

        return wrapper

    return decorator
