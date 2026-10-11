"""Idempotency-Key para operações que não podem ser duplicadas (envio).

Fluxo: ``begin`` RESERVA a chave (insert com unique) antes de chamar a Meta.
- Reserva concluída anteriormente -> devolve a resposta salva (replay).
- Reserva em andamento por outra requisição -> 409 (não envia de novo).
- Falha no envio -> ``release`` apaga a reserva para permitir nova tentativa.
Diferente do "consulta, envia, grava", não há janela para envio duplicado.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import Response

from app.services.supabase_client import get_service_client


@dataclass
class IdempotencyState:
    replay: dict[str, Any] | None = None
    status_code: int = 200
    error: tuple[Response, int] | None = None


def _error(code: str, message: str, status: int):
    from app.api import error_response
    return error_response(code, message, status)


def begin(org_id: str, key: str, endpoint: str) -> IdempotencyState:
    if not (8 <= len(key) <= 128) or not key.isprintable():
        return IdempotencyState(error=_error("invalid_idempotency_key",
                                             "Idempotency-Key deve ter entre 8 e 128 caracteres.", 400))
    client = get_service_client()
    try:
        client.table("idempotency_keys").insert({
            "organization_id": org_id, "key": key, "endpoint": endpoint, "status": "processing",
        }).execute()
        return IdempotencyState()
    except Exception as exc:
        text = str(exc).lower()
        if "duplicate" not in text and "23505" not in text:
            raise
    rows = (
        client.table("idempotency_keys").select("status, response, status_code")
        .eq("organization_id", org_id).eq("key", key).eq("endpoint", endpoint).limit(1).execute().data
    )
    if rows and rows[0].get("status") == "completed" and rows[0].get("response") is not None:
        return IdempotencyState(replay=rows[0]["response"], status_code=rows[0].get("status_code") or 200)
    return IdempotencyState(error=_error("idempotency_conflict",
                                         "Já existe uma requisição em andamento com esta Idempotency-Key.", 409))


def complete(org_id: str, key: str, endpoint: str, response: dict[str, Any], status_code: int) -> None:
    get_service_client().table("idempotency_keys").update({
        "status": "completed", "response": response, "status_code": status_code,
    }).eq("organization_id", org_id).eq("key", key).eq("endpoint", endpoint).execute()


def release(org_id: str, key: str, endpoint: str) -> None:
    get_service_client().table("idempotency_keys").delete().eq("organization_id", org_id).eq(
        "key", key).eq("endpoint", endpoint).eq("status", "processing").execute()
