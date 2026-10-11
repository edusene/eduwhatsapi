"""Contexto de usuário/organização (tenant), planos, membros e convites.

O organization_id NUNCA vem do navegador: é derivado das associações do
usuário autenticado (organization_members), consultadas no backend a cada
requisição. Quando o usuário pertence a várias organizações, a escolhida fica
na sessão e é revalidada contra as associações reais.
"""
from __future__ import annotations

import functools
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from flask import abort, flash, g, redirect, request, session, url_for

from app.services import audit
from app.services.security import hash_token, random_token
from app.services.supabase_client import get_service_client

ROLES = ("owner", "admin", "developer", "viewer")
ROLE_RANK = {"viewer": 1, "developer": 2, "admin": 3, "owner": 4}
ROLE_LABELS = {
    "owner": "Proprietário",
    "admin": "Administrador",
    "developer": "Desenvolvedor",
    "viewer": "Leitura",
}
PLANS = {
    "api": {
        "label": "API Oficial",
        "description": "Somente a API para integrar seus sistemas (n8n, Make, código próprio).",
    },
    "api_chat": {
        "label": "API + Chat",
        "description": "API completa e caixa de entrada para atender clientes pelo painel.",
    },
}
INVITE_TTL_DAYS = 7
_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_ORG_KEY = "org_id"


def slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug[:100] or "org"


def valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Contexto da requisição
# ---------------------------------------------------------------------------
def get_current_user() -> dict[str, Any] | None:
    """Usuário da sessão (validada no login pelo Supabase Auth)."""
    user = session.get("user")
    if not user or not user.get("id"):
        return None
    return user


def list_memberships(user_id: str) -> list[dict[str, Any]]:
    client = get_service_client()
    rows = (
        client.table("organization_members")
        .select("organization_id, role, created_at")
        .eq("user_id", user_id)
        .order("created_at")
        .execute()
        .data
    )
    if not rows:
        return []
    orgs = (
        client.table("organizations")
        .select("id, name, slug, plan")
        .in_("id", [r["organization_id"] for r in rows])
        .execute()
        .data
    )
    by_id = {o["id"]: o for o in orgs}
    out = []
    for row in rows:
        org = by_id.get(row["organization_id"])
        if not org:
            continue
        out.append({
            "organization_id": row["organization_id"],
            "role": row["role"],
            "org_name": org.get("name", ""),
            "org_slug": org.get("slug", ""),
            "plan": org.get("plan") or "api_chat",
        })
    return out


def load_current_context() -> None:
    """Popula g.user, g.memberships e g.membership (before_request)."""
    g.user = get_current_user()
    g.membership = None
    g.memberships = []
    if not g.user:
        return
    try:
        memberships = list_memberships(g.user["id"])
        if not memberships and request.blueprint == "dashboard" and not session.get("pending_invite"):
            # Usuário sem organização (ex.: criado antes do schema) entrando no
            # painel: cria a inicial. Não ocorre em rotas de convite, para que um
            # convidado não ganhe uma empresa vazia antes de aceitar.
            create_organization_for_user(g.user["id"], "Minha empresa", email=g.user.get("email"))
            memberships = list_memberships(g.user["id"])
    except Exception:
        # Tabelas ausentes (schema não executado) ou Supabase indisponível.
        return
    g.memberships = memberships
    preferred = session.get(SESSION_ORG_KEY)
    chosen = next((m for m in memberships if m["organization_id"] == preferred), None)
    g.membership = chosen or (memberships[0] if memberships else None)
    if g.membership:
        session[SESSION_ORG_KEY] = g.membership["organization_id"]


def current_org_id() -> str:
    return g.membership["organization_id"]


def current_user_id() -> str | None:
    return g.user["id"] if g.get("user") else None


def chat_enabled() -> bool:
    return bool(g.get("membership")) and g.membership.get("plan") == "api_chat"


def has_role(min_role: str) -> bool:
    membership = g.get("membership")
    return bool(membership) and ROLE_RANK.get(membership["role"], 0) >= ROLE_RANK[min_role]


# ---------------------------------------------------------------------------
# Decorators de autorização
# ---------------------------------------------------------------------------
def login_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapper


def org_required(view: Callable) -> Callable:
    """Exige usuário autenticado E associado a uma organização."""
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not g.get("user"):
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if not g.get("membership"):
            return redirect(url_for("auth.setup_problem"))
        return view(*args, **kwargs)

    return wrapper


def require_role(min_role: str) -> Callable:
    """Restringe a rota a um papel mínimo (owner > admin > developer > viewer)."""
    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            if not g.get("membership"):
                return redirect(url_for("auth.setup_problem"))
            if not has_role(min_role):
                abort(403, description="Seu papel nesta organização não permite esta ação.")
            return view(*args, **kwargs)

        return wrapper

    return decorator


def chat_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        if not chat_enabled():
            if "/api/" in request.path:
                abort(403, description="O chat não está ativo no plano desta organização.")
            flash("O chat está disponível no plano API + Chat. Ative em Configurações.", "info")
            return redirect(url_for("dashboard.inbox"))
        return view(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# Organizações
# ---------------------------------------------------------------------------
def create_organization_for_user(user_id: str, name: str, plan: str = "api_chat",
                                 email: str | None = None) -> dict[str, Any]:
    """Cria a organização do usuário e o associa como owner."""
    name = (name or "").strip()[:120] or "Minha empresa"
    plan = plan if plan in PLANS else "api_chat"
    client = get_service_client()
    slug = f"{slugify(name)}-{random_token(4).lower().replace('_', '').replace('-', '')[:6]}"
    org = client.table("organizations").insert(
        {"name": name, "slug": slug, "owner_user_id": user_id, "plan": plan}
    ).execute().data[0]
    client.table("organization_members").insert(
        {"organization_id": org["id"], "user_id": user_id, "role": "owner", "email": email}
    ).execute()
    audit.log_action(org["id"], user_id, "organization.created", "organization", org["id"],
                     {"plan": plan})
    return org


def get_organization(org_id: str) -> dict[str, Any] | None:
    rows = (
        get_service_client().table("organizations")
        .select("id, name, slug, plan, message_retention_days, created_at")
        .eq("id", org_id).limit(1).execute().data
    )
    return rows[0] if rows else None


def get_plan(org_id: str) -> str:
    org = get_organization(org_id)
    return (org or {}).get("plan") or "api_chat"


def update_organization(org_id: str, user_id: str | None, *, name: str | None = None,
                        plan: str | None = None, retention_days: int | None = None) -> None:
    changes: dict[str, Any] = {}
    if name is not None:
        name = name.strip()
        if not name or len(name) > 120:
            raise ValueError("Informe um nome com até 120 caracteres.")
        changes["name"] = name
    if plan is not None:
        if plan not in PLANS:
            raise ValueError("Plano inválido.")
        changes["plan"] = plan
    if retention_days is not None:
        if not 1 <= retention_days <= 3650:
            raise ValueError("A retenção deve ficar entre 1 e 3650 dias.")
        changes["message_retention_days"] = retention_days
    if not changes:
        return
    get_service_client().table("organizations").update(changes).eq("id", org_id).execute()
    audit.log_action(org_id, user_id, "organization.updated", "organization", org_id, changes)


# ---------------------------------------------------------------------------
# Membros
# ---------------------------------------------------------------------------
def list_members(org_id: str) -> list[dict[str, Any]]:
    return (
        get_service_client().table("organization_members")
        .select("id, user_id, email, role, created_at")
        .eq("organization_id", org_id).order("created_at").execute().data
    )


def update_member_role(org_id: str, member_id: str, role: str, actor: dict[str, Any]) -> None:
    if role not in ROLES or role == "owner":
        raise ValueError("Papel inválido.")
    member = _get_member(org_id, member_id)
    if member["role"] == "owner":
        raise ValueError("O papel do proprietário não pode ser alterado.")
    if member["user_id"] == actor["user_id"]:
        raise ValueError("Você não pode alterar o próprio papel.")
    if ROLE_RANK[actor["role"]] < ROLE_RANK["owner"] and ROLE_RANK[member["role"]] >= ROLE_RANK["admin"]:
        raise ValueError("Somente o proprietário pode alterar administradores.")
    get_service_client().table("organization_members").update({"role": role}).eq(
        "id", member_id).eq("organization_id", org_id).execute()
    audit.log_action(org_id, actor["user_id"], "member.role_changed", "member", member_id,
                     {"from": member["role"], "to": role})


def remove_member(org_id: str, member_id: str, actor: dict[str, Any]) -> None:
    member = _get_member(org_id, member_id)
    if member["role"] == "owner":
        raise ValueError("O proprietário não pode ser removido.")
    if ROLE_RANK[actor["role"]] < ROLE_RANK["owner"] and ROLE_RANK[member["role"]] >= ROLE_RANK["admin"]:
        raise ValueError("Somente o proprietário pode remover administradores.")
    get_service_client().table("organization_members").delete().eq(
        "id", member_id).eq("organization_id", org_id).execute()
    audit.log_action(org_id, actor["user_id"], "member.removed", "member", member_id,
                     {"role": member["role"]})


def _get_member(org_id: str, member_id: str) -> dict[str, Any]:
    rows = (
        get_service_client().table("organization_members")
        .select("id, user_id, role").eq("id", member_id).eq("organization_id", org_id)
        .limit(1).execute().data
    )
    if not rows:
        raise ValueError("Membro não encontrado.")
    return rows[0]


# ---------------------------------------------------------------------------
# Convites
# ---------------------------------------------------------------------------
def create_invitation(org_id: str, email: str, role: str, invited_by: str | None) -> dict[str, Any]:
    email = (email or "").strip().lower()
    if not valid_email(email):
        raise ValueError("Informe um e-mail válido.")
    if role not in ("admin", "developer", "viewer"):
        raise ValueError("Papel inválido para convite.")
    token = random_token(24)
    row = get_service_client().table("organization_invitations").insert({
        "organization_id": org_id,
        "email": email,
        "role": role,
        "token_hash": hash_token(token),
        "invited_by": invited_by,
        "expires_at": (_now() + timedelta(days=INVITE_TTL_DAYS)).isoformat(),
    }).execute().data[0]
    audit.log_action(org_id, invited_by, "invitation.created", "invitation", row["id"],
                     {"role": role})
    return {"id": row["id"], "email": email, "role": role, "token": token}


def list_invitations(org_id: str) -> list[dict[str, Any]]:
    rows = (
        get_service_client().table("organization_invitations")
        .select("id, email, role, expires_at, accepted_at, revoked_at, created_at")
        .eq("organization_id", org_id).order("created_at", desc=True).limit(50).execute().data
    )
    now = _now().isoformat()
    return [r for r in rows if not r.get("accepted_at") and not r.get("revoked_at")
            and (r.get("expires_at") or "") > now]


def revoke_invitation(org_id: str, invitation_id: str, user_id: str | None) -> bool:
    result = (
        get_service_client().table("organization_invitations")
        .update({"revoked_at": _now().isoformat()})
        .eq("id", invitation_id).eq("organization_id", org_id).execute()
    )
    if result.data:
        audit.log_action(org_id, user_id, "invitation.revoked", "invitation", invitation_id, {})
    return bool(result.data)


def find_invitation(token: str) -> dict[str, Any] | None:
    if not token or len(token) > 200:
        return None
    rows = (
        get_service_client().table("organization_invitations")
        .select("id, organization_id, email, role, expires_at, accepted_at, revoked_at")
        .eq("token_hash", hash_token(token)).limit(1).execute().data
    )
    if not rows:
        return None
    inv = rows[0]
    if inv.get("accepted_at") or inv.get("revoked_at") or (inv.get("expires_at") or "") <= _now().isoformat():
        return None
    org = get_organization(inv["organization_id"])
    inv["org_name"] = (org or {}).get("name", "")
    return inv


def accept_invitation(token: str, user: dict[str, Any]) -> dict[str, Any]:
    inv = find_invitation(token)
    if not inv:
        raise ValueError("Convite inválido, expirado ou já utilizado.")
    if (user.get("email") or "").lower() != inv["email"]:
        raise ValueError("Este convite foi enviado para outro e-mail. Entre com a conta convidada.")
    client = get_service_client()
    existing = (
        client.table("organization_members").select("id")
        .eq("organization_id", inv["organization_id"]).eq("user_id", user["id"]).limit(1).execute().data
    )
    if not existing:
        client.table("organization_members").insert({
            "organization_id": inv["organization_id"],
            "user_id": user["id"],
            "email": inv["email"],
            "role": inv["role"],
        }).execute()
    client.table("organization_invitations").update({"accepted_at": _now().isoformat()}).eq(
        "id", inv["id"]).execute()
    audit.log_action(inv["organization_id"], user["id"], "invitation.accepted", "invitation",
                     inv["id"], {"role": inv["role"]})
    return inv
