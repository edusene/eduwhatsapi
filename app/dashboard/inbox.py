"""Caixa de entrada (chat) do plano API + Chat.

A página é renderizada no servidor e atualizada por um script que consulta os
endpoints JSON abaixo (sessão + CSRF via header X-CSRF-Token). Leitura liberada
a todos os membros; envio e alterações exigem papel developer ou superior.
"""
from __future__ import annotations

import logging
import re

from flask import Response, abort, jsonify, render_template, request, stream_with_context

from app.dashboard import bp
from app.extensions import limiter
from app.meta.adapters import get_adapter
from app.meta.graph_api import MetaAPIError
from app.services import connections, messaging
from app.services.message_validation import ValidationError, validate_send_payload
from app.services.organizations import chat_enabled, chat_required, current_org_id, current_user_id, has_role, org_required
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"[0-9a-fA-F-]{32,36}")


def _json_error(message: str, status: int = 400, code: str = "error"):
    return jsonify({"ok": False, "code": code, "message": message}), status


def _require_sender():
    if not has_role("developer"):
        abort(403, description="Seu papel permite apenas visualizar conversas.")


def _conversation_or_404(conversation_id: str) -> dict:
    if not _UUID_RE.fullmatch(conversation_id):
        abort(404)
    conversation = messaging.get_conversation(current_org_id(), conversation_id)
    if not conversation:
        abort(404)
    return conversation


def _template_payload(to: str, data: dict) -> dict:
    tpl = data.get("template") or {}
    params = [str(p) for p in (tpl.get("params") or []) if str(p).strip()][:20]
    body = {"to": to, "type": "template", "template": {
        "name": tpl.get("name", ""), "language": {"code": tpl.get("language", "pt_BR")}}}
    if params:
        body["template"]["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": p[:1024]} for p in params]}]
    return validate_send_payload(body)


def _send(connection: dict, payload: dict):
    try:
        message = messaging.send(current_org_id(), connection, payload, source="dashboard",
                                 sent_by=current_user_id())
    except connections.ConnectionError_ as exc:
        return _json_error(str(exc), 409, "channel_unavailable")
    except MetaAPIError as exc:
        text = exc.message + (f" — {exc.details}" if exc.details else "")
        if exc.fb_code == 131047:
            text = "A janela de 24h está fechada: envie um modelo aprovado para reabrir a conversa."
        return _json_error(text, 502, "meta_error")
    message.pop("meta_response", None)
    return jsonify({"ok": True, "message": message})


@bp.get("/inbox")
@org_required
def inbox():
    if not chat_enabled():
        return render_template("dashboard/inbox_disabled.html")
    return render_template(
        "dashboard/inbox.html",
        channels=[c for c in connections.list_connections(current_org_id(), include_disconnected=False)
                  if c.get("status") == "active"],
        can_send=has_role("developer"),
        user_id=current_user_id(),
    )


@bp.get("/inbox/api/conversations")
@org_required
@chat_required
def inbox_conversations():
    mine = request.args.get("mine") == "1"
    connection_id = request.args.get("connection_id", "")
    rows = messaging.list_conversations(
        current_org_id(), status=request.args.get("status") or None,
        assigned_to=current_user_id() if mine else None, search=request.args.get("q"), limit=100,
        connection_id=connection_id if _UUID_RE.fullmatch(connection_id) else None)
    return jsonify({"ok": True, "conversations": rows})


@bp.get("/inbox/api/conversations/<conversation_id>/messages")
@org_required
@chat_required
def inbox_messages(conversation_id: str):
    conversation = _conversation_or_404(conversation_id)
    after = request.args.get("after") or None
    rows = messaging.conversation_messages(current_org_id(), conversation_id, after=after)
    return jsonify({"ok": True, "messages": rows, "window_open": messaging.window_open(conversation),
                    "status": conversation.get("status"), "assigned_to": conversation.get("assigned_to")})


@bp.post("/inbox/api/conversations/<conversation_id>/messages")
@org_required
@chat_required
@limiter.limit("60 per minute")
def inbox_send(conversation_id: str):
    _require_sender()
    conversation = _conversation_or_404(conversation_id)
    connection = connections.get_connection(current_org_id(), conversation["connection_id"])
    if not connection or connection.get("status") != "active":
        return _json_error("O número desta conversa não está ativo.", 409)
    contact = get_service_client().table("contacts").select("wa_id").eq("id", conversation["contact_id"]).eq(
        "organization_id", current_org_id()).limit(1).execute().data
    if not contact:
        abort(404)
    data = request.get_json(silent=True) or {}
    try:
        if data.get("type") == "template":
            payload = _template_payload(contact[0]["wa_id"], data)
        else:
            if not messaging.window_open(conversation):
                return _json_error("A janela de 24h está fechada: envie um modelo aprovado.", 409, "window_closed")
            body = {"to": contact[0]["wa_id"], "type": "text", "text": {"body": str(data.get("body") or "")}}
            if data.get("reply_to"):
                body["context"] = {"message_id": str(data["reply_to"])}
            payload = validate_send_payload(body)
    except ValidationError as exc:
        return _json_error(str(exc), 400, exc.code)
    return _send(connection, payload)


@bp.post("/inbox/api/conversations/new")
@org_required
@chat_required
@limiter.limit("20 per minute")
def inbox_new():
    _require_sender()
    data = request.get_json(silent=True) or {}
    connection = connections.get_connection(current_org_id(), str(data.get("connection_id") or ""))
    if not connection or connection.get("status") != "active":
        return _json_error("Escolha um número ativo.", 400)
    try:
        payload = _template_payload(str(data.get("to") or ""), data)
    except ValidationError as exc:
        return _json_error(str(exc), 400, exc.code)
    response = _send(connection, payload)
    if isinstance(response, tuple):
        return response
    body = response.get_json()
    body["conversation_id"] = body["message"].get("conversation_id")
    return jsonify(body)


@bp.post("/inbox/api/conversations/<conversation_id>")
@org_required
@chat_required
def inbox_update(conversation_id: str):
    _require_sender()
    _conversation_or_404(conversation_id)
    data = request.get_json(silent=True) or {}
    changes: dict = {}
    if data.get("status") in ("open", "resolved"):
        changes["status"] = data["status"]
    if "assign" in data:
        changes["assigned_to"] = current_user_id() if data["assign"] == "me" else None
    try:
        messaging.update_conversation(current_org_id(), conversation_id, changes)
    except ValueError as exc:
        return _json_error(str(exc))
    return jsonify({"ok": True})


@bp.post("/inbox/api/conversations/<conversation_id>/read")
@org_required
@chat_required
def inbox_mark_read(conversation_id: str):
    conversation = _conversation_or_404(conversation_id)
    messaging.update_conversation(current_org_id(), conversation_id, {"unread_count": 0})
    # Confirmação de leitura no WhatsApp (os "dois tracinhos azuis"): best-effort.
    if has_role("developer") and request.args.get("receipt") == "1":
        last = [m for m in messaging.conversation_messages(current_org_id(), conversation_id, limit=20)
                if m["direction"] == "inbound" and m.get("wamid")]
        connection = connections.get_connection(current_org_id(), conversation["connection_id"])
        if last and connection and connection.get("status") == "active":
            try:
                messaging.mark_as_read(connection, last[-1]["wamid"])
            except (MetaAPIError, connections.ConnectionError_):
                logger.info("Falha ao enviar confirmação de leitura (conversa %s).", conversation_id)
    return jsonify({"ok": True})


@bp.get("/inbox/api/templates")
@org_required
@chat_required
def inbox_templates():
    connection = connections.get_connection(current_org_id(), request.args.get("connection_id", ""))
    if not connection or connection.get("status") != "active":
        return _json_error("Número não encontrado.", 404)
    try:
        templates = get_adapter("whatsapp").list_templates(connection["waba_id"], connections.token_for(connection))
    except (MetaAPIError, connections.ConnectionError_) as exc:
        return _json_error(getattr(exc, "message", None) or str(exc), 502)
    out = []
    for t in templates:
        if t.get("status") != "APPROVED":
            continue
        body = next((c.get("text", "") for c in t.get("components") or [] if c.get("type") == "BODY"), "")
        out.append({"name": t.get("name"), "language": t.get("language"), "category": t.get("category"),
                    "body": body, "variables": len(set(re.findall(r"\{\{(\d+)\}\}", body)))})
    return jsonify({"ok": True, "templates": out})


@bp.get("/inbox/media/<message_id>")
@org_required
@chat_required
def inbox_media(message_id: str):
    if not _UUID_RE.fullmatch(message_id):
        abort(404)
    found = messaging.find_inbound_media(current_org_id(), message_id)
    if not found:
        abort(404)
    message, connection = found
    try:
        stream, content_type, size = get_adapter("whatsapp").download_media(
            message["content"]["id"], connections.token_for(connection), connection["phone_number_id"])
    except (MetaAPIError, connections.ConnectionError_):
        abort(404)
    headers = {"Cache-Control": "private, max-age=3600", "Content-Security-Policy": "default-src 'none'; sandbox"}
    if size:
        headers["Content-Length"] = str(size)
    if not content_type.startswith(("image/", "audio/", "video/")):
        headers["Content-Disposition"] = "attachment"
    return Response(stream_with_context(stream), mimetype=content_type, headers=headers)

