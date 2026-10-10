"""Rotas da API pública v1 (prefixo /api/v1)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import g, jsonify, request, send_file

from app.api import bp, error_response, schemas
from app.api.auth import require_api_key
from app.meta.adapters import get_adapter
from app.meta.graph_api import get_client
from app.services.encryption import (
    DecryptionError,
    EncryptionNotConfigured,
    decrypt_token,
)
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

_PHONE_NUMBER_ID_RE = re.compile(r"\d{5,25}")
_IDEMPOTENCY_ENDPOINT = "/whatsapp/messages"

# Campos seguros expostos por GET /channels (nunca tokens nem metadados internos).
_CHANNEL_PUBLIC_FIELDS = (
    "id", "channel", "phone_number_id", "display_phone_number",
    "verified_name", "status", "connected_at",
)


@bp.get("/me")
@require_api_key()
def me():
    key = g.api_key
    return jsonify({
        "organization_id": key["organization_id"],
        "key_id": key["id"],
        "environment": key["environment"],
        "scopes": key.get("scopes") or [],
        "request_id": g.request_id,
    })


@bp.get("/channels")
@require_api_key("channels:read")
def channels():
    result = (
        get_service_client()
        .table("meta_connections")
        .select("id, channel, phone_number_id, display_phone_number,"
                " verified_name, status, connected_at")
        .eq("organization_id", g.organization_id)
        .order("created_at", desc=True)
        .execute()
    )
    # Projeção também em Python: garante que nenhum campo sensível sai, mesmo
    # se a camada de dados ignorar a lista de colunas.
    data = [{k: row.get(k) for k in _CHANNEL_PUBLIC_FIELDS} for row in result.data]
    return jsonify({"data": data})


@bp.post("/whatsapp/<phone_number_id>/messages")
@require_api_key("messages:send")
def send_whatsapp_message(phone_number_id: str):
    if not _PHONE_NUMBER_ID_RE.fullmatch(phone_number_id):
        return error_response(
            "validation_error", "phone_number_id inválido (5 a 25 dígitos).", 400
        )

    client = get_service_client()
    org_id = g.organization_id

    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is not None:
        if not (8 <= len(idempotency_key) <= 128):
            return error_response(
                "invalid_idempotency_key",
                "Idempotency-Key deve ter entre 8 e 128 caracteres.",
                400,
            )
        existing = (
            client.table("idempotency_keys")
            .select("response")
            .eq("organization_id", org_id)
            .eq("key", idempotency_key)
            .eq("endpoint", _IDEMPOTENCY_ENDPOINT)
            .limit(1)
            .execute()
        )
        if existing.data:
            replay = jsonify(existing.data[0]["response"])
            replay.headers["X-Idempotent-Replay"] = "true"
            return replay, 200

    connection_result = (
        client.table("meta_connections")
        .select("id, access_token_encrypted")
        .eq("organization_id", org_id)
        .eq("phone_number_id", phone_number_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )
    if not connection_result.data:
        # Mensagem genérica: não confirma existência do número em outra org.
        return error_response(
            "channel_not_found",
            "Nenhum canal ativo encontrado para este número.",
            404,
        )
    connection = connection_result.data[0]

    data = request.get_json(silent=True)
    if data is None:
        return error_response(
            "invalid_json", "Corpo da requisição deve ser JSON válido.", 400
        )
    payload = schemas.validate_send_message_payload(data)  # ValidationError -> 400

    try:
        token = decrypt_token(connection["access_token_encrypted"])
    except (DecryptionError, EncryptionNotConfigured) as exc:
        logger.error(
            "Token indisponível na conexão %s (%s).",
            connection["id"], type(exc).__name__,
        )
        return error_response(
            "token_unavailable",
            "Credencial do canal indisponível. Revalide a conexão no painel.",
            500,
        )

    adapter = get_adapter("whatsapp")(get_client())
    # MetaAPIError propaga para o handler do blueprint (502/504).
    meta_response = adapter.send_text_message(
        phone_number_id, token, payload["to"], payload["body"]
    )

    body = {"data": meta_response, "request_id": g.request_id}
    if idempotency_key is not None:
        try:
            client.table("idempotency_keys").insert({
                "organization_id": org_id,
                "key": idempotency_key,
                "endpoint": _IDEMPOTENCY_ENDPOINT,
                "response": body,
            }).execute()
        except Exception:
            logger.exception("Falha ao gravar chave de idempotência.")
    return jsonify(body), 200


@bp.get("/usage")
@require_api_key("usage:read")
def usage():
    """Métricas da org a partir das 1000 requisições mais recentes logadas."""
    result = (
        get_service_client()
        .table("api_request_logs")
        .select("endpoint, status_code, created_at")
        .eq("organization_id", g.organization_id)
        .order("created_at", desc=True)
        .limit(1000)
        .execute()
    )
    rows = result.data
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    by_endpoint: dict[str, int] = {}
    last_24h = 0
    for row in rows:
        endpoint = row.get("endpoint") or "unknown"
        by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + 1
        if (row.get("created_at") or "") >= cutoff:
            last_24h += 1
    return jsonify({"data": {
        "total_requests": len(rows),
        "last_24h": last_24h,
        "by_endpoint": by_endpoint,
    }})


@bp.get("/openapi.yaml")
def openapi_spec():
    """Especificação OpenAPI pública (sem autenticação)."""
    # Âncora no arquivo (app/api/routes.py -> raiz do projeto), independente do root_path.
    path = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"
    return send_file(path, mimetype="application/yaml")
