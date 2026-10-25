"""Chaves internas de API (autenticação da API pública da plataforma).

- Formato: sk_test_<aleatório> ou sk_live_<aleatório>.
- O segredo completo é retornado UMA única vez, na criação/rotação.
- No banco fica apenas o hash HMAC-SHA256 (com pepper) — nunca o texto puro.
- Essas chaves são credenciais DESTA plataforma; não são tokens da Meta.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Any

from app.services import audit
from app.services.security import hash_api_key
from app.services.supabase_client import get_service_client

VALID_ENVIRONMENTS = ("test", "live")
SCOPES = {
    "messages:send": "Enviar mensagens e marcar como lidas",
    "messages:read": "Consultar mensagens e status de entrega",
    "channels:read": "Listar números conectados",
    "templates:read": "Listar modelos de mensagem",
    "templates:write": "Criar e apagar modelos de mensagem",
    "profile:write": "Editar o perfil comercial do número",
    "media:read": "Baixar mídias recebidas",
    "media:write": "Enviar arquivos de mídia",
    "conversations:read": "Ler conversas do chat (plano API + Chat)",
    "usage:read": "Consultar uso da API",
}
ALL_SCOPES = sorted(SCOPES)
DEFAULT_SCOPES = list(ALL_SCOPES)
PREFIX_LEN = 16
PHONE_NUMBER_ID_RE = re.compile(r"\d{5,25}")


def clean_phone_number_ids(values: list[str] | None) -> list[str]:
    """Lista de phone_number_id válidos, sem repetição. Vazia = todos os números."""
    ids = sorted({str(v).strip() for v in (values or []) if str(v).strip()})
    invalid = [v for v in ids if not PHONE_NUMBER_ID_RE.fullmatch(v)]
    if invalid:
        raise ValueError(f"phone_number_id inválido: {', '.join(invalid)}")
    return ids


def _generate_secret(environment: str) -> str:
    return f"sk_{environment}_{secrets.token_urlsafe(32)}"


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    """Representação segura de uma chave: sem hash nem segredo."""
    return {
        "id": row["id"],
        "name": row["name"],
        "environment": row["environment"],
        "scopes": row.get("scopes") or [],
        "phone_number_ids": row.get("phone_number_ids") or [],
        "key_hint": f"{row['key_prefix']}…{row.get('key_suffix', '')}",
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
        "expires_at": row.get("expires_at"),
        "revoked": row.get("revoked_at") is not None,
    }


def create_api_key(
    organization_id: str,
    name: str,
    environment: str = "test",
    scopes: list[str] | None = None,
    created_by: str | None = None,
    expires_at: str | None = None,
    phone_number_ids: list[str] | None = None,
) -> dict[str, Any]:
    if environment not in VALID_ENVIRONMENTS:
        raise ValueError("Ambiente inválido: use 'test' ou 'live'.")
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Nome da chave é obrigatório (máx. 80 caracteres).")
    scopes = sorted(set(scopes or DEFAULT_SCOPES))
    invalid = set(scopes) - set(ALL_SCOPES)
    if invalid:
        raise ValueError(f"Escopos inválidos: {sorted(invalid)}")

    secret = _generate_secret(environment)
    row = {
        "organization_id": organization_id,
        "name": name,
        "environment": environment,
        "key_prefix": secret[:PREFIX_LEN],
        "key_suffix": secret[-4:],
        "key_hash": hash_api_key(secret),
        "scopes": scopes,
        "phone_number_ids": clean_phone_number_ids(phone_number_ids),
        "created_by": created_by,
        "expires_at": expires_at,
    }
    result = get_service_client().table("api_keys").insert(row).execute()
    record = result.data[0]
    audit.log_action(organization_id, created_by, "api_key.created",
                     "api_key", record["id"],
                     {"name": name, "environment": environment})
    view = _public_view(record)
    view["key"] = secret  # exibido apenas nesta resposta
    return view


def verify_api_key(secret: str) -> dict[str, Any] | None:
    """Valida uma chave apresentada. Retorna o registro interno ou None.

    Inclui organization_id, id, environment e scopes. Revogação tem efeito
    imediato. Atualiza last_used_at de forma assíncrona-tolerante.
    """
    if not secret or not secret.startswith(("sk_test_", "sk_live_")):
        return None
    client = get_service_client()
    result = (
        client.table("api_keys")
        .select("id, organization_id, name, environment, scopes, expires_at, revoked_at,"
                " last_used_at, phone_number_ids")
        .eq("key_hash", hash_api_key(secret))
        .execute()
    )
    if not result.data:
        return None
    record = result.data[0]
    if record.get("revoked_at"):
        return None
    expires = record.get("expires_at")
    if expires and _parse_ts(expires) < datetime.now(timezone.utc):
        return None
    now = datetime.now(timezone.utc)
    last_used = record.get("last_used_at")
    # Grava no máximo 1x por minuto por chave para não gerar escrita a cada chamada.
    if not last_used or (now - _parse_ts(last_used)).total_seconds() > 60:
        try:
            client.table("api_keys").update({"last_used_at": now.isoformat()}).eq(
                "id", record["id"]).execute()
        except Exception:
            pass  # métrica de uso não deve bloquear a requisição
    return record


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def list_api_keys(organization_id: str) -> list[dict[str, Any]]:
    result = (
        get_service_client()
        .table("api_keys")
        .select("id, name, environment, scopes, phone_number_ids, key_prefix, key_suffix,"
                " created_at, last_used_at, expires_at, revoked_at")
        .eq("organization_id", organization_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [_public_view(r) for r in result.data]


def get_api_key(organization_id: str, key_id: str) -> dict[str, Any] | None:
    result = (
        get_service_client()
        .table("api_keys")
        .select("id, organization_id, name, environment, scopes, key_prefix,"
                " key_suffix, created_at, last_used_at, expires_at, revoked_at")
        .eq("id", key_id)
        .eq("organization_id", organization_id)
        .execute()
    )
    return result.data[0] if result.data else None


def revoke_api_key(organization_id: str, key_id: str,
                   revoked_by: str | None = None) -> bool:
    record = get_api_key(organization_id, key_id)
    if not record or record.get("revoked_at"):
        return False
    get_service_client().table("api_keys").update(
        {"revoked_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", key_id).eq("organization_id", organization_id).execute()
    audit.log_action(organization_id, revoked_by, "api_key.revoked",
                     "api_key", key_id, {"name": record.get("name")})
    return True


def rotate_api_key(organization_id: str, key_id: str,
                   rotated_by: str | None = None) -> dict[str, Any] | None:
    """Cria uma nova chave com os mesmos metadados e revoga a anterior."""
    record = get_api_key(organization_id, key_id)
    if not record or record.get("revoked_at"):
        return None
    new_key = create_api_key(
        organization_id=organization_id,
        name=record["name"],
        environment=record["environment"],
        scopes=record.get("scopes") or list(DEFAULT_SCOPES),
        created_by=rotated_by,
        expires_at=record.get("expires_at"),
    )
    revoke_api_key(organization_id, key_id, revoked_by=rotated_by)
    audit.log_action(organization_id, rotated_by, "api_key.rotated",
                     "api_key", key_id, {"new_key_id": new_key["id"]})
    return new_key
