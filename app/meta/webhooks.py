"""Webhooks públicos da Meta (entrada).

Duas URLs:

- ``/webhooks/meta`` — app Meta da PLATAFORMA (Embedded Signup e números
  inscritos no seu app). Verify token: META_WEBHOOK_VERIFY_TOKEN; assinatura
  com META_WEBHOOK_APP_SECRET (App Secret do seu app).
- ``/webhooks/meta/<connection_id>`` — app Meta PRÓPRIO do cliente (conexão
  manual com App Secret informado). Verify token e App Secret da conexão.

Fluxo do POST: assinatura X-Hub-Signature-256 validada sobre o corpo BRUTO
antes de qualquer processamento → payload validado → cada evento deduplicado
em ``webhook_events`` (dedupe_key único) → processamento leve no banco →
webhooks de saída despachados em segundo plano. Responde 200 rapidamente.
Payloads gravados em webhook_events são mínimos (ids, tipo, horário) — nunca
o texto das mensagens.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, abort, current_app, jsonify, request

from app.services import connections, messaging, outbound_webhooks
from app.services.security import constant_time_equals, verify_meta_signature
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

bp = Blueprint("meta_webhooks", __name__)

_UUID_LEN = 36


def _text(message: str, status: int) -> Response:
    return Response(message, status=status, mimetype="text/plain")


def _handshake(expected_token: str | None) -> Response:
    if not expected_token:
        return _text("Webhook não configurado.", 503)
    if request.args.get("hub.mode") == "subscribe" and constant_time_equals(
            request.args.get("hub.verify_token", ""), expected_token):
        return _text(request.args.get("hub.challenge", ""), 200)
    return _text("Verificação falhou.", 403)


def _connection_or_404(connection_id: str) -> dict[str, Any]:
    if len(connection_id) != _UUID_LEN:
        abort(404)
    rows = (
        get_service_client().table("meta_connections").select("*")
        .eq("id", connection_id).neq("status", "disconnected").limit(1).execute().data
    )
    if not rows or not rows[0].get("app_secret_encrypted"):
        abort(404)
    return rows[0]


@bp.get("/webhooks/meta")
def verify():
    return _handshake(current_app.config.get("META_WEBHOOK_VERIFY_TOKEN"))


@bp.get("/webhooks/meta/<connection_id>")
def verify_connection(connection_id: str):
    return _handshake(connections.webhook_verify_token(_connection_or_404(connection_id)))


@bp.post("/webhooks/meta")
def receive():
    secret = current_app.config.get("META_WEBHOOK_APP_SECRET", "")
    if not secret:
        return _text("Webhook não configurado.", 503)
    if not verify_meta_signature(secret, request.get_data(cache=True),
                                 request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Webhook da Meta com assinatura inválida (request_id=%s).", request.headers.get("X-Request-Id"))
        return _text("Assinatura inválida.", 403)
    return _process_body(only_connection=None)


@bp.post("/webhooks/meta/<connection_id>")
def receive_connection(connection_id: str):
    connection = _connection_or_404(connection_id)
    secret = connections.app_secret_for(connection)
    if not secret or not verify_meta_signature(secret, request.get_data(cache=True),
                                               request.headers.get("X-Hub-Signature-256", "")):
        return _text("Assinatura inválida.", 403)
    return _process_body(only_connection=connection)


# ---------------------------------------------------------------------------
# Processamento
# ---------------------------------------------------------------------------
def _process_body(only_connection: dict[str, Any] | None):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _text("Payload JSON inválido.", 400)
    if data.get("object") != "whatsapp_business_account":
        return jsonify({"status": "ignored"}), 200

    for entry in data.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        waba_id = str(entry.get("id") or "")
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or not isinstance(change.get("value"), dict):
                continue
            try:
                _process_change(change.get("field") or "", change["value"], waba_id, only_connection)
            except Exception:
                # Um evento com erro não deve impedir os demais nem gerar reenvio
                # infinito da Meta; o erro fica registrado nos logs do servidor.
                logger.exception("Falha ao processar evento de webhook (field=%s).", change.get("field"))
    return jsonify({"status": "ok"}), 200


def _claim(dedupe_key: str, org_id: str | None, event_type: str, payload: dict[str, Any],
           status: str = "processed") -> bool:
    """Registra o evento; False se já foi processado (reenvio da Meta)."""
    try:
        get_service_client().table("webhook_events").insert({
            "dedupe_key": dedupe_key[:300],
            "organization_id": org_id,
            "channel": "whatsapp",
            "event_type": event_type,
            "payload": payload,
            "status": status,
        }).execute()
        return True
    except Exception as exc:
        text = str(exc).lower()
        if "duplicate" in text or "23505" in text:
            return False
        raise


def _resolve(phone_number_id: Any, only_connection: dict[str, Any] | None) -> dict[str, Any] | None:
    if not phone_number_id:
        return None
    if only_connection is not None:
        # URL exclusiva: só aceita eventos do número daquela conexão.
        return only_connection if str(only_connection.get("phone_number_id")) == str(phone_number_id) else None
    return connections.find_live_by_phone(str(phone_number_id))


def _process_change(field: str, value: dict[str, Any], waba_id: str,
                    only_connection: dict[str, Any] | None) -> None:
    if field in ("messages", "smb_message_echoes", ""):
        phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
        connection = _resolve(phone_number_id, only_connection)
        org_id = connection["organization_id"] if connection else None

        for message in value.get("messages") or []:
            if not isinstance(message, dict) or not message.get("id"):
                continue
            minimal = {k: message[k] for k in ("id", "timestamp", "type") if k in message}
            if not _claim(f"msg:{message['id']}", org_id, "message.received", minimal,
                          "processed" if connection else "ignored"):
                continue
            if connection:
                messaging.handle_inbound_message(connection, message, value.get("contacts") or [])

        for echo in value.get("message_echoes") or []:
            if not isinstance(echo, dict) or not echo.get("id"):
                continue
            minimal = {k: echo[k] for k in ("id", "timestamp", "type") if k in echo}
            if not _claim(f"echo:{echo['id']}", org_id, "message.echo", minimal,
                          "processed" if connection else "ignored"):
                continue
            if connection:
                messaging.handle_inbound_message(connection, echo, [], echo=True)

        for status in value.get("statuses") or []:
            if not isinstance(status, dict) or not status.get("id") or not status.get("status"):
                continue
            minimal = {k: status[k] for k in ("id", "status", "timestamp") if k in status}
            if not _claim(f"st:{status['id']}:{status['status']}", org_id, "message.status.updated",
                          minimal, "processed" if connection else "ignored"):
                continue
            if connection:
                messaging.handle_status(connection, status)
        return

    if field == "message_template_status_update":
        _fan_out_by_waba(waba_id, only_connection, "template.status.updated", {
            k: value.get(k) for k in ("event", "message_template_id", "message_template_name",
                                      "message_template_language", "reason") if value.get(k) is not None
        }, dedupe=f"tpl:{value.get('message_template_id')}:{value.get('event')}")
        return

    if field == "account_update" and value.get("event") in ("PARTNER_REMOVED", "ACCOUNT_DELETED"):
        for connection in _connections_by_waba(waba_id, only_connection):
            if _claim(f"acct:{connection['id']}:{value.get('event')}", connection["organization_id"],
                      "account.update", {"event": value.get("event")}):
                connections.mark_offboarded(connection, f"A Meta informou: {value.get('event')}. Reconecte o número.")
        return

    # Campos não tratados (ex.: history, smb_app_state_sync, phone_number_quality_update)
    # são aceitos e ignorados com 200, como recomenda a Meta.
    logger.info("Webhook da Meta com campo não tratado: %s", field or "<vazio>")


def _connections_by_waba(waba_id: str, only_connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    if only_connection is not None:
        return [only_connection] if str(only_connection.get("waba_id")) == waba_id else []
    if not waba_id:
        return []
    return (
        get_service_client().table("meta_connections").select("*")
        .eq("waba_id", waba_id).neq("status", "disconnected").execute().data
    )


def _fan_out_by_waba(waba_id: str, only_connection: dict[str, Any] | None, event_type: str,
                     payload: dict[str, Any], dedupe: str) -> None:
    seen: set[str] = set()
    for connection in _connections_by_waba(waba_id, only_connection):
        org_id = connection["organization_id"]
        if org_id in seen:
            continue
        seen.add(org_id)
        if _claim(f"{dedupe}:{org_id}", org_id, event_type, payload):
            outbound_webhooks.enqueue_event(org_id, event_type, {**payload, "waba_id": waba_id})
