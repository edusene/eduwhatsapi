"""Núcleo de mensagens: envio, recebimento, status e conversas.

Compartilhado pela API própria (/api/v1) e pela caixa de entrada do painel.

Privacidade (LGPD — minimização):
- Plano ``api``: guardamos só metadados (ids, tipo, status) para permitir
  consultar o estado da entrega. O conteúdo é repassado ao webhook do cliente
  e não fica armazenado em ``messages``.
- Plano ``api_chat``: o conteúdo é armazenado para exibir o histórico no chat,
  sujeito a ``organizations.message_retention_days``.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from app.meta.adapters import get_adapter
from app.meta.graph_api import MetaAPIError
from app.services import connections, outbound_webhooks
from app.services.message_validation import preview_text
from app.services.organizations import get_plan
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

STATUS_ORDER = {"accepted": 1, "sent": 2, "delivered": 3, "read": 4}
SERVICE_WINDOW = timedelta(hours=24)
MESSAGE_PUBLIC_FIELDS = (
    "id, connection_id, conversation_id, direction, wamid, contact_wa_id, type, content,"
    " status, error, source, created_at, updated_at"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ts_from_unix(value: Any) -> str:
    try:
        return _iso(datetime.fromtimestamp(int(value), tz=timezone.utc))
    except (TypeError, ValueError, OverflowError):
        return _iso(_now())


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_duplicate(exc: Exception) -> bool:
    text = str(exc).lower()
    return "duplicate" in text or "23505" in text


# ---------------------------------------------------------------------------
# Contatos e conversas (plano api_chat)
# ---------------------------------------------------------------------------
def get_or_create_conversation(org_id: str, connection_id: str, wa_id: str,
                               profile_name: str | None = None) -> dict[str, Any]:
    client = get_service_client()
    contact_row = {"organization_id": org_id, "wa_id": wa_id}
    if profile_name:
        contact_row["profile_name"] = profile_name[:256]
    contact = client.table("contacts").upsert(
        contact_row, on_conflict="organization_id,wa_id").execute().data[0]

    def _find() -> list[dict[str, Any]]:
        return (
            client.table("conversations").select("*")
            .eq("connection_id", connection_id).eq("contact_id", contact["id"]).limit(1).execute().data
        )

    rows = _find()
    if rows:
        return rows[0]
    try:
        return client.table("conversations").insert({
            "organization_id": org_id,
            "connection_id": connection_id,
            "contact_id": contact["id"],
            "status": "open",
        }).execute().data[0]
    except Exception as exc:
        if _is_duplicate(exc):
            return _find()[0]
        raise


def _bump(conversation_id: str, preview: str, inbound: bool, at: str) -> None:
    get_service_client().rpc("bump_conversation", {
        "p_conversation_id": conversation_id,
        "p_preview": preview,
        "p_inbound": inbound,
        "p_at": at,
    }).execute()


def window_open(conversation: dict[str, Any]) -> bool:
    last_inbound = _parse(conversation.get("last_inbound_at"))
    return bool(last_inbound) and _now() - last_inbound < SERVICE_WINDOW


# ---------------------------------------------------------------------------
# Envio
# ---------------------------------------------------------------------------
def _content_for_storage(payload: dict[str, Any]) -> dict[str, Any]:
    mtype = payload["type"]
    content = dict(payload.get(mtype) or {})
    if payload.get("context"):
        content["context"] = payload["context"]
    return content


def send(org_id: str, connection: dict[str, Any], payload: dict[str, Any], *, source: str,
         api_key_id: str | None = None, sent_by: str | None = None,
         test_mode: bool = False) -> dict[str, Any]:
    """Envia uma mensagem validada. Retorna o registro público da mensagem.

    ``test_mode`` (chaves sk_test_): NÃO chama a Meta; registra a mensagem com
    status ``test`` e um id fictício ``wamid.TEST...`` claramente identificado.
    MetaAPIError é registrada (status failed) e propagada ao chamador.
    """
    client = get_service_client()
    chat = get_plan(org_id) == "api_chat"
    mtype = payload["type"]
    content = _content_for_storage(payload)
    preview = preview_text(mtype, content)

    conversation = None
    if chat and mtype != "reaction":
        conversation = get_or_create_conversation(org_id, connection["id"], payload["to"])

    base_row = {
        "organization_id": org_id,
        "connection_id": connection["id"],
        "conversation_id": conversation["id"] if conversation else None,
        "direction": "outbound",
        "contact_wa_id": payload["to"],
        "type": mtype,
        "content": content if chat else None,
        "source": source,
        "api_key_id": api_key_id,
        "sent_by": sent_by,
    }

    if test_mode:
        wamid = "wamid.TEST." + secrets.token_hex(12)
        meta_response: dict[str, Any] = {"test_mode": True, "messages": [{"id": wamid}]}
        status = "test"
    else:
        try:
            token = connections.token_for(connection)
            meta_response = get_adapter("whatsapp").send_message(connection["phone_number_id"], token, payload)
        except MetaAPIError as exc:
            client.table("messages").insert({**base_row, "status": "failed", "error": exc.as_dict()}).execute()
            outbound_webhooks.enqueue_event(org_id, "api.request.failed", {
                "phone_number_id": connection["phone_number_id"],
                "to": payload["to"],
                "type": mtype,
                "error": exc.as_dict(),
            })
            raise
        messages = meta_response.get("messages") if isinstance(meta_response, dict) else None
        wamid = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
        status = "accepted"

    record = client.table("messages").insert({**base_row, "wamid": wamid, "status": status}).execute().data[0]
    if conversation:
        _bump(conversation["id"], preview, False, record.get("created_at") or _iso(_now()))
    outbound_webhooks.enqueue_event(org_id, "message.sent", {
        "message_id": record["id"],
        "wamid": wamid,
        "phone_number_id": connection["phone_number_id"],
        "to": payload["to"],
        "type": mtype,
        "status": status,
        "source": source,
    })
    out = public_message(record)
    out["meta_response"] = meta_response
    return out


def mark_as_read(connection: dict[str, Any], wamid: str, test_mode: bool = False) -> dict[str, Any]:
    if test_mode:
        return {"success": True, "test_mode": True}
    token = connections.token_for(connection)
    return get_adapter("whatsapp").mark_as_read(connection["phone_number_id"], token, wamid)


def public_message(row: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "connection_id", "conversation_id", "direction", "wamid", "contact_wa_id",
            "type", "content", "status", "error", "source", "created_at", "updated_at")
    return {k: row.get(k) for k in keys}


# ---------------------------------------------------------------------------
# Recebimento (webhooks da Meta)
# ---------------------------------------------------------------------------
def _inbound_content(message: dict[str, Any]) -> dict[str, Any]:
    mtype = message.get("type") or "unknown"
    raw = message.get(mtype) if isinstance(message.get(mtype), dict) else {}
    if mtype == "text":
        content = {"body": raw.get("body", "")}
    elif mtype in ("image", "video", "audio", "document", "sticker"):
        content = {k: raw[k] for k in ("id", "mime_type", "sha256", "caption", "filename", "voice", "animated")
                   if k in raw}
    elif mtype == "location":
        content = {k: raw[k] for k in ("latitude", "longitude", "name", "address", "url") if k in raw}
    elif mtype == "interactive":
        reply = raw.get("button_reply") or raw.get("list_reply") or raw.get("nfm_reply") or {}
        content = {"type": raw.get("type"), "id": reply.get("id"), "title": reply.get("title"),
                   "description": reply.get("description")}
        content = {k: v for k, v in content.items() if v is not None}
    elif mtype == "button":
        content = {"text": raw.get("text"), "payload": raw.get("payload"), "title": raw.get("text")}
    elif mtype == "reaction":
        content = {"message_id": raw.get("message_id"), "emoji": raw.get("emoji", "")}
    elif mtype == "contacts":
        content = {"contacts": message.get("contacts") or []}
    elif mtype == "order":
        content = {"catalog_id": raw.get("catalog_id"), "items": len(raw.get("product_items") or [])}
    else:
        content = {}
    if isinstance(message.get("context"), dict):
        content["context"] = {k: message["context"][k] for k in ("id", "from") if k in message["context"]}
    if message.get("errors"):
        content["errors"] = message["errors"]
    return content


def handle_inbound_message(connection: dict[str, Any], message: dict[str, Any],
                           contacts: list[dict[str, Any]], *, echo: bool = False) -> None:
    """Processa uma mensagem recebida (ou eco de coexistência) já deduplicada."""
    org_id = connection["organization_id"]
    chat = get_plan(org_id) == "api_chat"
    mtype = message.get("type") or "unknown"
    content = _inbound_content(message)
    wa_id = str(message.get("to") if echo else message.get("from") or "")
    if not wa_id.isdigit():
        logger.info("Mensagem sem remetente válido ignorada.")
        return
    profile_name = None
    for contact in contacts or []:
        if isinstance(contact, dict) and str(contact.get("wa_id")) == wa_id:
            profile_name = (contact.get("profile") or {}).get("name")
    at = _ts_from_unix(message.get("timestamp"))

    conversation = None
    if chat and mtype != "reaction":
        conversation = get_or_create_conversation(org_id, connection["id"], wa_id, profile_name)

    client = get_service_client()
    try:
        record = client.table("messages").insert({
            "organization_id": org_id,
            "connection_id": connection["id"],
            "conversation_id": conversation["id"] if conversation else None,
            "direction": "outbound" if echo else "inbound",
            "wamid": message.get("id"),
            "contact_wa_id": wa_id,
            "type": mtype,
            "content": content if chat else None,
            "status": "sent" if echo else "received",
            "source": "webhook",
            "created_at": at,
        }).execute().data[0]
    except Exception as exc:
        if _is_duplicate(exc):
            return
        raise
    if conversation:
        _bump(conversation["id"], preview_text(mtype, content), not echo, at)

    event_message = {"id": message.get("id"), "timestamp": message.get("timestamp"), "type": mtype,
                     mtype: content}
    if echo:
        event_message.update({"to": wa_id, "echo": True})
    else:
        event_message["from"] = wa_id
    outbound_webhooks.enqueue_event(org_id, "message.sent" if echo else "message.received", {
        "message_id": record["id"],
        "phone_number_id": connection["phone_number_id"],
        "display_phone_number": connection.get("display_phone_number"),
        "contact": {"wa_id": wa_id, "name": profile_name},
        "conversation_id": conversation["id"] if conversation else None,
        "message": event_message,
    })


def handle_status(connection: dict[str, Any], status: dict[str, Any]) -> None:
    org_id = connection["organization_id"]
    new_status = status.get("status")
    wamid = status.get("id")
    client = get_service_client()
    rows = (
        client.table("messages").select("id, status, conversation_id")
        .eq("connection_id", connection["id"]).eq("wamid", wamid).limit(1).execute().data
    )
    if rows:
        current = rows[0]["status"]
        update: dict[str, Any] = {}
        if new_status == "failed":
            update = {"status": "failed", "error": (status.get("errors") or [{}])[0]}
        elif STATUS_ORDER.get(new_status, 0) > STATUS_ORDER.get(current, 0) and current != "failed":
            update = {"status": new_status}
        if update:
            client.table("messages").update(update).eq("id", rows[0]["id"]).execute()

    payload = {k: status[k] for k in ("id", "status", "timestamp", "recipient_id",
                                      "biz_opaque_callback_data") if k in status}
    if isinstance(status.get("conversation"), dict):
        payload["conversation"] = status["conversation"]
    if isinstance(status.get("pricing"), dict):
        payload["pricing"] = status["pricing"]
    if status.get("errors"):
        payload["errors"] = status["errors"]
    payload["phone_number_id"] = connection["phone_number_id"]
    payload["message_id"] = rows[0]["id"] if rows else None
    outbound_webhooks.enqueue_event(org_id, "message.status.updated", payload)


# ---------------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------------
def list_messages(org_id: str, *, phone_number_id: str | None = None, direction: str | None = None,
                  contact: str | None = None, limit: int = 50, before: str | None = None) -> list[dict[str, Any]]:
    query = get_service_client().table("messages").select(MESSAGE_PUBLIC_FIELDS).eq("organization_id", org_id)
    if phone_number_id:
        conn_ids = [c["id"] for c in connections.list_connections(org_id)
                    if c.get("phone_number_id") == phone_number_id]
        if not conn_ids:
            return []
        query = query.in_("connection_id", conn_ids)
    if direction in ("inbound", "outbound"):
        query = query.eq("direction", direction)
    if contact:
        query = query.eq("contact_wa_id", contact)
    if before:
        query = query.lt("created_at", before)
    rows = query.order("created_at", desc=True).limit(max(1, min(limit, 100))).execute().data
    return [public_message(r) for r in rows]


def get_message(org_id: str, message_id: str) -> dict[str, Any] | None:
    client = get_service_client()
    column = "wamid" if message_id.startswith("wamid.") else "id"
    rows = (
        client.table("messages").select(MESSAGE_PUBLIC_FIELDS)
        .eq("organization_id", org_id).eq(column, message_id).limit(1).execute().data
    )
    return public_message(rows[0]) if rows else None


def list_conversations(org_id: str, *, status: str | None = None, assigned_to: str | None = None,
                       search: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    client = get_service_client()
    query = client.table("conversations").select("*").eq("organization_id", org_id)
    if status in ("open", "resolved"):
        query = query.eq("status", status)
    if assigned_to:
        query = query.eq("assigned_to", assigned_to)
    rows = query.order("last_message_at", desc=True).limit(max(1, min(limit, 200))).execute().data
    if not rows:
        return []
    contacts = {
        c["id"]: c for c in client.table("contacts").select("id, wa_id, profile_name")
        .in_("id", list({r["contact_id"] for r in rows})).execute().data
    }
    conns = {c["id"]: c for c in connections.list_connections(org_id)}
    out = []
    term = (search or "").strip().lower()
    for row in rows:
        contact = contacts.get(row["contact_id"], {})
        conn = conns.get(row["connection_id"], {})
        item = {
            "id": row["id"],
            "status": row.get("status"),
            "assigned_to": row.get("assigned_to"),
            "unread_count": row.get("unread_count") or 0,
            "last_message_at": row.get("last_message_at"),
            "last_inbound_at": row.get("last_inbound_at"),
            "last_message_preview": row.get("last_message_preview"),
            "window_open": window_open(row),
            "contact": {"id": contact.get("id"), "wa_id": contact.get("wa_id"),
                        "name": contact.get("profile_name")},
            "channel": {"id": conn.get("id"), "phone_number_id": conn.get("phone_number_id"),
                        "display_phone_number": conn.get("display_phone_number"),
                        "verified_name": conn.get("verified_name")},
        }
        if term and term not in (item["contact"]["name"] or "").lower() and term not in (item["contact"]["wa_id"] or ""):
            continue
        out.append(item)
    return out


def get_conversation(org_id: str, conversation_id: str) -> dict[str, Any] | None:
    rows = (
        get_service_client().table("conversations").select("*")
        .eq("id", conversation_id).eq("organization_id", org_id).limit(1).execute().data
    )
    return rows[0] if rows else None


def conversation_messages(org_id: str, conversation_id: str, *, after: str | None = None,
                          limit: int = 100) -> list[dict[str, Any]]:
    query = (
        get_service_client().table("messages").select(MESSAGE_PUBLIC_FIELDS)
        .eq("organization_id", org_id).eq("conversation_id", conversation_id)
    )
    if after:
        query = query.gt("updated_at", after)
        rows = query.order("updated_at").limit(limit).execute().data
        return [public_message(r) for r in rows]
    rows = query.order("created_at", desc=True).limit(limit).execute().data
    return [public_message(r) for r in reversed(rows)]


def update_conversation(org_id: str, conversation_id: str, changes: dict[str, Any]) -> bool:
    allowed = {k: v for k, v in changes.items() if k in ("status", "assigned_to", "unread_count")}
    if "status" in allowed and allowed["status"] not in ("open", "resolved"):
        raise ValueError("Status inválido.")
    if not allowed:
        return False
    result = (
        get_service_client().table("conversations").update(allowed)
        .eq("id", conversation_id).eq("organization_id", org_id).execute()
    )
    return bool(result.data)


def find_inbound_media(org_id: str, message_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Mensagem com mídia + conexão, para o proxy de download do painel."""
    rows = (
        get_service_client().table("messages").select("id, connection_id, type, content")
        .eq("id", message_id).eq("organization_id", org_id).limit(1).execute().data
    )
    if not rows or not (rows[0].get("content") or {}).get("id"):
        return None
    connection = connections.get_connection(org_id, rows[0]["connection_id"])
    if not connection or connection.get("status") == "disconnected":
        return None
    return rows[0], connection
