"""Rotas da API pública v1 (prefixo /api/v1).

Autenticação: ``Authorization: Bearer sk_live_...`` (ou sk_test_ para o modo
de teste, que valida tudo mas NÃO chama a Meta). A organização vem sempre da
chave validada; IDs de outra organização respondem 404.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from flask import Response, g, jsonify, request, send_file, stream_with_context

from app.api import bp, error_response, schemas
from app.api.auth import require_api_key
from app.meta.adapters import get_adapter
from app.services import connections, idempotency, messaging, usage
from app.services.organizations import get_organization

logger = logging.getLogger(__name__)

_PHONE_NUMBER_ID_RE = re.compile(r"\d{5,25}")
_MEDIA_ID_RE = re.compile(r"\d{5,30}")
_UUID_RE = re.compile(r"[0-9a-fA-F-]{32,36}")
_ALLOWED_UPLOAD_TYPES = {
    "image/jpeg", "image/png", "image/webp", "video/mp4", "video/3gpp", "audio/aac", "audio/mp4",
    "audio/mpeg", "audio/amr", "audio/ogg", "application/pdf", "text/plain",
    "application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
_CHANNEL_PUBLIC_FIELDS = (
    "id", "channel", "connection_type", "waba_id", "phone_number_id", "display_phone_number",
    "verified_name", "quality_rating", "status", "webhook_subscribed", "connected_at",
)


def _channel_view(row: dict) -> dict:
    return {k: row.get(k) for k in _CHANNEL_PUBLIC_FIELDS}


def _connection_or_error(phone_number_id: str):
    """Conexão ativa da org da chave, ou resposta de erro (sem vazar outras orgs)."""
    if not _PHONE_NUMBER_ID_RE.fullmatch(phone_number_id):
        return None, error_response("validation_error", "phone_number_id inválido.", 400)
    connection = connections.get_active_by_phone(g.organization_id, phone_number_id)
    if not connection:
        return None, error_response("channel_not_found", "Nenhum número ativo com este phone_number_id.", 404)
    return connection, None


def _int_arg(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(request.args.get(name, default))))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Conta e canais
# ---------------------------------------------------------------------------
@bp.get("/me")
@require_api_key()
def me():
    key = g.api_key
    org = get_organization(key["organization_id"]) or {}
    return jsonify({
        "organization": {"id": key["organization_id"], "name": org.get("name"), "plan": org.get("plan")},
        "key": {"id": key["id"], "name": key.get("name"), "environment": key["environment"],
                "scopes": key.get("scopes") or []},
        "request_id": g.request_id,
    })


@bp.get("/channels")
@require_api_key("channels:read")
def channels_list():
    rows = connections.list_connections(g.organization_id, include_disconnected=False)
    return jsonify({"data": [_channel_view(r) for r in rows]})


@bp.get("/channels/<phone_number_id>")
@require_api_key("channels:read")
def channel_get(phone_number_id: str):
    connection, error = _connection_or_error(phone_number_id)
    if error:
        return error
    return jsonify({"data": _channel_view(connection)})


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------
@bp.post("/whatsapp/<phone_number_id>/messages")
@require_api_key("messages:send")
def send_message(phone_number_id: str):
    connection, error = _connection_or_error(phone_number_id)
    if error:
        return error
    data = request.get_json(silent=True)
    if data is None:
        return error_response("invalid_json", "Envie um corpo JSON válido (Content-Type: application/json).", 400)
    payload = schemas.validate_send_payload(data)  # ValidationError -> 400

    idem_key = request.headers.get("Idempotency-Key")
    endpoint = f"POST /whatsapp/{phone_number_id}/messages"
    if idem_key is not None:
        state = idempotency.begin(g.organization_id, idem_key, endpoint)
        if state.replay is not None:
            resp = jsonify(state.replay)
            resp.headers["Idempotent-Replayed"] = "true"
            return resp, state.status_code
        if state.error:
            return state.error

    try:
        message = messaging.send(g.organization_id, connection, payload, source="api",
                                 api_key_id=g.api_key["id"], test_mode=g.test_mode)
    except Exception:
        if idem_key is not None:
            idempotency.release(g.organization_id, idem_key, endpoint)
        raise
    body = {"data": message, "test_mode": g.test_mode, "request_id": g.request_id}
    if idem_key is not None:
        idempotency.complete(g.organization_id, idem_key, endpoint, body, 201)
    return jsonify(body), 201


@bp.post("/whatsapp/<phone_number_id>/messages/read")
@require_api_key("messages:send")
def mark_read(phone_number_id: str):
    connection, error = _connection_or_error(phone_number_id)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    wamid = data.get("message_id")
    if not isinstance(wamid, str) or not wamid.startswith("wamid.") or len(wamid) > 256:
        raise schemas.ValidationError("Informe message_id (wamid.*) da mensagem recebida.", "invalid_field", "message_id")
    result = messaging.mark_as_read(connection, wamid, test_mode=g.test_mode)
    return jsonify({"data": result, "request_id": g.request_id})


@bp.get("/messages")
@require_api_key("messages:read")
def messages_list():
    direction = request.args.get("direction")
    contact = request.args.get("contact")
    if contact:
        contact = schemas.normalize_recipient(contact)
    phone = request.args.get("phone_number_id")
    if phone and not _PHONE_NUMBER_ID_RE.fullmatch(phone):
        raise schemas.ValidationError("phone_number_id inválido.", "invalid_field", "phone_number_id")
    before = request.args.get("before")
    if before:
        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise schemas.ValidationError("before deve ser uma data ISO 8601.", "invalid_field", "before") from exc
    rows = messaging.list_messages(g.organization_id, phone_number_id=phone, direction=direction,
                                   contact=contact, limit=_int_arg("limit", 50, 1, 100), before=before)
    next_before = rows[-1]["created_at"] if len(rows) == _int_arg("limit", 50, 1, 100) else None
    return jsonify({"data": rows, "next_before": next_before})


@bp.get("/messages/<message_id>")
@require_api_key("messages:read")
def message_get(message_id: str):
    if not (_UUID_RE.fullmatch(message_id) or (message_id.startswith("wamid.") and len(message_id) <= 256)):
        return error_response("validation_error", "Use o id interno (UUID) ou o wamid da mensagem.", 400)
    message = messaging.get_message(g.organization_id, message_id)
    if not message:
        return error_response("message_not_found", "Mensagem não encontrada.", 404)
    return jsonify({"data": message})


# ---------------------------------------------------------------------------
# Modelos e mídia
# ---------------------------------------------------------------------------
@bp.get("/whatsapp/<phone_number_id>/templates")
@require_api_key("templates:read")
def templates_list(phone_number_id: str):
    connection, error = _connection_or_error(phone_number_id)
    if error:
        return error
    templates = get_adapter("whatsapp").list_templates(connection["waba_id"], connections.token_for(connection))
    status = request.args.get("status")
    if status:
        templates = [t for t in templates if t.get("status") == status.upper()]
    return jsonify({"data": templates})


@bp.post("/whatsapp/<phone_number_id>/media")
@require_api_key("media:write")
def media_upload(phone_number_id: str):
    connection, error = _connection_or_error(phone_number_id)
    if error:
        return error
    upload = request.files.get("file")
    if not upload or not upload.filename:
        raise schemas.ValidationError("Envie o arquivo no campo multipart 'file'.", "missing_field", "file")
    mime = (upload.mimetype or "").lower()
    if mime not in _ALLOWED_UPLOAD_TYPES:
        raise schemas.ValidationError("Tipo de arquivo não suportado pela Cloud API.", "unsupported_media_type", "file")
    content = upload.read()
    if not content:
        raise schemas.ValidationError("Arquivo vazio.", "invalid_field", "file")
    if g.test_mode:
        return jsonify({"data": {"id": "0" * 15, "test_mode": True}, "test_mode": True}), 201
    result = get_adapter("whatsapp").upload_media(
        connection["phone_number_id"], connections.token_for(connection),
        upload.filename[:200], content, mime)
    return jsonify({"data": {"id": result.get("id")}, "test_mode": False}), 201


@bp.get("/whatsapp/<phone_number_id>/media/<media_id>")
@require_api_key("media:read")
def media_download(phone_number_id: str, media_id: str):
    connection, error = _connection_or_error(phone_number_id)
    if error:
        return error
    if not _MEDIA_ID_RE.fullmatch(media_id):
        return error_response("validation_error", "media_id inválido.", 400)
    stream, content_type, size = get_adapter("whatsapp").download_media(
        media_id, connections.token_for(connection), connection["phone_number_id"])
    headers = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff",
               "Content-Security-Policy": "default-src 'none'; sandbox"}
    if size:
        headers["Content-Length"] = str(size)
    return Response(stream_with_context(stream), mimetype=content_type, headers=headers)


# ---------------------------------------------------------------------------
# Conversas (plano API + Chat)
# ---------------------------------------------------------------------------
def _chat_plan_or_error():
    org = get_organization(g.organization_id) or {}
    if org.get("plan") != "api_chat":
        return error_response("plan_required", "Conversas exigem o plano API + Chat.", 403)
    return None


@bp.get("/conversations")
@require_api_key("conversations:read")
def conversations_list():
    error = _chat_plan_or_error()
    if error:
        return error
    rows = messaging.list_conversations(g.organization_id, status=request.args.get("status"),
                                        limit=_int_arg("limit", 50, 1, 200))
    return jsonify({"data": rows})


@bp.get("/conversations/<conversation_id>/messages")
@require_api_key("conversations:read")
def conversation_messages(conversation_id: str):
    error = _chat_plan_or_error()
    if error:
        return error
    if not _UUID_RE.fullmatch(conversation_id) or not messaging.get_conversation(g.organization_id, conversation_id):
        return error_response("conversation_not_found", "Conversa não encontrada.", 404)
    rows = messaging.conversation_messages(g.organization_id, conversation_id,
                                           limit=_int_arg("limit", 100, 1, 200))
    return jsonify({"data": rows})


# ---------------------------------------------------------------------------
# Uso e especificação
# ---------------------------------------------------------------------------
@bp.get("/usage")
@require_api_key("usage:read")
def usage_summary():
    days = _int_arg("days", 30, 1, 90)
    return jsonify({"data": usage.summary(g.organization_id, days=days)})


@bp.get("/openapi.yaml")
def openapi_spec():
    """Especificação OpenAPI pública (sem autenticação)."""
    path = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"
    return send_file(path, mimetype="application/yaml")


