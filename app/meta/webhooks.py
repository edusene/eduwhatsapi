"""Webhook público da Meta (entrada): verificação e eventos do WhatsApp.

- ``GET /webhooks/meta``: handshake oficial (hub.mode/hub.verify_token/challenge).
- ``POST /webhooks/meta``: valida X-Hub-Signature-256 (HMAC do corpo bruto),
  persiste eventos deduplicados em webhook_events e dispara os webhooks de
  saída dos clientes. Processamento é inline e leve (apenas insert + entregas);
  responde sempre 200 ao final para a Meta não reenviar.

Payloads armazenados são mínimos: IDs, remetente, timestamps e tipo — NUNCA o
corpo do texto das mensagens.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from app.services import outbound_webhooks
from app.services.security import constant_time_equals, hmac_sha256_hex
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

bp = Blueprint("meta_webhooks", __name__)


def _text(message: str, status: int) -> Response:
    return Response(message, status=status, mimetype="text/plain")


@bp.get("/webhooks/meta")
def verify():
    verify_token = current_app.config.get("META_WEBHOOK_VERIFY_TOKEN", "")
    if not verify_token:
        return _text("Webhook da Meta não configurado no servidor.", 503)
    if (
        request.args.get("hub.mode") == "subscribe"
        and constant_time_equals(request.args.get("hub.verify_token", ""), verify_token)
    ):
        return _text(request.args.get("hub.challenge", ""), 200)
    return _text("Verificação falhou.", 403)


@bp.post("/webhooks/meta")
def receive():
    app_secret = current_app.config.get("META_WEBHOOK_APP_SECRET", "")
    if not app_secret:
        return _text("Webhook da Meta não configurado no servidor.", 503)

    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac_sha256_hex(app_secret, request.get_data())
    if not signature.startswith("sha256=") or not constant_time_equals(signature, expected):
        return _text("Assinatura inválida.", 403)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _text("Payload JSON inválido.", 400)
    if data.get("object") != "whatsapp_business_account":
        return jsonify({"status": "ok"}), 200  # objeto não relacionado: ignora

    for entry in data.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if isinstance(change, dict):
                _process_change(change)
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Processamento de eventos
# ---------------------------------------------------------------------------
def _resolve_organization(phone_number_id: Any) -> str | None:
    if not phone_number_id:
        return None
    result = (
        get_service_client()
        .table("meta_connections")
        .select("organization_id")
        .eq("phone_number_id", str(phone_number_id))
        .limit(1)
        .execute()
    )
    return result.data[0]["organization_id"] if result.data else None


def _store_event(org_id: str | None, event_type: str, dedupe_key: str, payload: dict) -> None:
    try:
        get_service_client().table("webhook_events").insert({
            "dedupe_key": dedupe_key,
            "organization_id": org_id,
            "channel": "whatsapp",
            "event_type": event_type,
            "payload": payload,
            "status": "processed",
        }).execute()
    except Exception as exc:
        message = str(exc)
        if "duplicate" in message or "23505" in message:
            return  # evento duplicado (reenvio da Meta): ignora silenciosamente
        logger.exception("Falha ao persistir webhook_event %s", event_type)
        return
    if org_id:
        outbound_webhooks.enqueue_event(org_id, event_type, payload)


def _minimal(source: dict, keys: tuple[str, ...]) -> dict:
    return {k: source[k] for k in keys if source.get(k) is not None}


def _process_change(change: dict) -> None:
    value = change.get("value") or {}
    if not isinstance(value, dict):
        return
    org_id = _resolve_organization((value.get("metadata") or {}).get("phone_number_id"))

    for message in value.get("messages") or []:
        if not isinstance(message, dict) or not message.get("id"):
            continue
        payload = _minimal(message, ("id", "from", "timestamp", "type"))
        _store_event(org_id, "message.received", message["id"], payload)

    for status in value.get("statuses") or []:
        if not isinstance(status, dict) or not status.get("id") or not status.get("status"):
            continue
        dedupe_key = f"{status['id']}:{status['status']}"
        payload = _minimal(status, ("id", "status", "timestamp", "recipient_id"))
        _store_event(org_id, "message.status.updated", dedupe_key, payload)
