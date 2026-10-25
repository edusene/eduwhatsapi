"""Webhooks de saída: entrega de eventos da plataforma para URLs de clientes.

- URLs passam por validação anti-SSRF (HTTPS, sem IPs privados/reservados) no
  cadastro E a cada entrega (o DNS pode mudar depois do cadastro).
- Cada endpoint tem um segredo HMAC (``whsec_...``) criptografado no banco e
  exibido uma única vez (criação/rotação).
- Enfileiramento: a linha é criada como ``pending`` (travada por 2 min) e a
  entrega roda numa thread (app.services.tasks), sem atrasar quem gerou o
  evento. Falhas recebem ``next_retry_at`` com backoff exponencial e são
  reprocessadas pelo worker/cron via ``claim_due_webhook_deliveries``.
- Sem redirecionamentos, timeout explícito, tentativas limitadas.

Formato entregue ao cliente::

    POST <url>
    X-Webhook-Id: <delivery id>
    X-Webhook-Timestamp: <unix seconds>
    X-Webhook-Signature: sha256=HMAC_SHA256(secret, "<timestamp>.<corpo>")
    {"id": "...", "event": "message.received", "created_at": "...", "data": {...}}
"""
from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import requests
from flask import current_app

from app.services import audit, tasks
from app.services.api_keys import clean_phone_number_ids
from app.services.encryption import DecryptionError, EncryptionNotConfigured, decrypt_token, encrypt_token
from app.services.security import hmac_sha256_hex
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

EVENTS = {
    "message.received": "Mensagem recebida de um contato",
    "message.status.updated": "Status de mensagem enviada (enviada, entregue, lida, falhou)",
    "message.sent": "Mensagem enviada pela API ou pelo painel",
    "channel.connected": "Número conectado",
    "channel.disconnected": "Número desconectado",
    "template.status.updated": "Modelo de mensagem aprovado/rejeitado pela Meta",
    "api.request.failed": "Requisição à API recusada pela Meta",
}
VALID_EVENTS = tuple(EVENTS) + ("test",)
USER_AGENT = "AutoZap-Webhooks/2.0"
LOCK_MINUTES = 2
_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data"}


class WebhookURLBlocked(ValueError):
    """URL de webhook rejeitada pela validação anti-SSRF."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Validação de URL (anti-SSRF)
# ---------------------------------------------------------------------------
def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified or not ip.is_global
    )


def validate_webhook_url(url: str) -> str:
    """Valida e normaliza a URL de destino. Levanta WebhookURLBlocked.

    Mensagens de erro nunca ecoam o IP interno resolvido.
    """
    allow_insecure = bool(current_app.config.get("OUTBOUND_WEBHOOK_ALLOW_INSECURE", False))
    url = (url or "").strip()
    if len(url) > 2048:
        raise WebhookURLBlocked("URL longa demais (máx. 2048 caracteres).")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (ValueError, AttributeError) as exc:
        raise WebhookURLBlocked("URL inválida.") from exc

    scheme = parsed.scheme.lower()
    if scheme not in ("https", "http") or (scheme == "http" and not allow_insecure):
        raise WebhookURLBlocked("A URL do webhook precisa usar HTTPS.")
    if parsed.username or parsed.password:
        raise WebhookURLBlocked("A URL não pode conter usuário e senha.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise WebhookURLBlocked("A URL do webhook precisa ter um host válido.")
    if host in _METADATA_HOSTS or host.endswith(".internal") or host == "localhost":
        if not allow_insecure:
            raise WebhookURLBlocked("A URL aponta para um endereço de rede interno ou reservado.")
    if allow_insecure:
        return parsed.geturl()  # somente desenvolvimento local

    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise WebhookURLBlocked("Não foi possível resolver o host da URL.") from exc
    if not infos:
        raise WebhookURLBlocked("Não foi possível resolver o host da URL.")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError as exc:
            raise WebhookURLBlocked("Endereço resolvido inválido.") from exc
        if _ip_blocked(ip):
            raise WebhookURLBlocked("A URL aponta para um endereço de rede interno ou reservado.")
    return parsed.geturl()


# ---------------------------------------------------------------------------
# CRUD de endpoints
# ---------------------------------------------------------------------------
def _new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "description": row.get("description"),
        "events": row.get("events") or [],
        "phone_number_ids": row.get("phone_number_ids") or [],
        "status": row.get("status"),
        "created_at": row.get("created_at"),
    }


def _validate_events(events: list[str]) -> list[str]:
    events = sorted(set(events or []))
    invalid = [e for e in events if e not in EVENTS]
    if invalid:
        raise ValueError(f"Eventos inválidos: {', '.join(invalid)}")
    if not events:
        raise ValueError("Selecione ao menos um evento.")
    return events


def create_endpoint(org_id: str, url: str, events: list[str], description: str | None = None,
                    created_by: str | None = None,
                    phone_number_ids: list[str] | None = None) -> dict[str, Any]:
    url = validate_webhook_url(url)
    events = _validate_events(events)
    phone_number_ids = clean_phone_number_ids(phone_number_ids)
    description = (description or "").strip()[:200] or None
    secret = _new_secret()
    record = get_service_client().table("webhook_endpoints").insert({
        "organization_id": org_id,
        "url": url,
        "description": description,
        "events": events,
        "phone_number_ids": phone_number_ids,
        "secret_encrypted": encrypt_token(secret),
        "status": "active",
        "created_by": created_by,
    }).execute().data[0]
    audit.log_action(org_id, created_by, "webhook_endpoint.created", "webhook_endpoint",
                     record["id"], {"url": url, "events": events, "phone_number_ids": phone_number_ids})
    view = _public_view(record)
    view["secret"] = secret  # exibido apenas nesta resposta
    return view


def update_endpoint(org_id: str, endpoint_id: str, *, events: list[str] | None = None,
                    status: str | None = None, user_id: str | None = None,
                    phone_number_ids: list[str] | None = None) -> bool:
    changes: dict[str, Any] = {}
    if events is not None:
        changes["events"] = _validate_events(events)
    if phone_number_ids is not None:
        changes["phone_number_ids"] = clean_phone_number_ids(phone_number_ids)
    if status is not None:
        if status not in ("active", "disabled"):
            raise ValueError("Status inválido.")
        changes["status"] = status
    if not changes:
        return False
    result = (
        get_service_client().table("webhook_endpoints").update(changes)
        .eq("id", endpoint_id).eq("organization_id", org_id).execute()
    )
    if result.data:
        audit.log_action(org_id, user_id, "webhook_endpoint.updated", "webhook_endpoint",
                         endpoint_id, changes)
    return bool(result.data)


def list_endpoints(org_id: str) -> list[dict[str, Any]]:
    rows = (
        get_service_client().table("webhook_endpoints")
        .select("id, url, description, events, phone_number_ids, status, created_at")
        .eq("organization_id", org_id).order("created_at", desc=True).execute().data
    )
    return [_public_view(r) for r in rows]


def delete_endpoint(org_id: str, endpoint_id: str, user_id: str | None = None) -> bool:
    result = (
        get_service_client().table("webhook_endpoints").delete()
        .eq("id", endpoint_id).eq("organization_id", org_id).execute()
    )
    if not result.data:
        return False
    audit.log_action(org_id, user_id, "webhook_endpoint.deleted", "webhook_endpoint",
                     endpoint_id, {"url": result.data[0].get("url")})
    return True


def rotate_secret(org_id: str, endpoint_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    secret = _new_secret()
    result = (
        get_service_client().table("webhook_endpoints")
        .update({"secret_encrypted": encrypt_token(secret)})
        .eq("id", endpoint_id).eq("organization_id", org_id).execute()
    )
    if not result.data:
        return None
    audit.log_action(org_id, user_id, "webhook_endpoint.secret_rotated", "webhook_endpoint",
                     endpoint_id, {})
    view = _public_view(result.data[0])
    view["secret"] = secret  # exibido apenas nesta resposta
    return view


def list_deliveries(org_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return (
        get_service_client().table("webhook_deliveries")
        .select("id, endpoint_id, event_type, status, attempts, max_attempts, response_status,"
                " last_error, next_retry_at, delivered_at, created_at")
        .eq("organization_id", org_id).order("created_at", desc=True)
        .range(offset, offset + limit - 1).execute().data
    )


# ---------------------------------------------------------------------------
# Assinatura e entrega
# ---------------------------------------------------------------------------
def sign_delivery(secret: str, timestamp: int, body: bytes) -> str:
    """Assinatura HMAC-SHA256 de ``"<timestamp>." + corpo`` (anti-replay)."""
    return "sha256=" + hmac_sha256_hex(secret, f"{timestamp}.".encode() + body)


def _delivery_body(row: dict[str, Any]) -> bytes:
    envelope = {
        "id": row["id"],
        "event": row["event_type"],
        "created_at": row.get("created_at"),
        "data": row.get("payload") or {},
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _backoff_minutes(attempts: int) -> int:
    return min(2 ** attempts, 120)  # 2, 4, 8, 16, 32, 64, 120...


def deliver(row: dict[str, Any], endpoint_url: str, secret: str) -> str:
    """Executa um POST e atualiza a linha. Retorna o novo status."""
    cfg = current_app.config
    max_attempts = int(row.get("max_attempts") or cfg.get("OUTBOUND_WEBHOOK_MAX_ATTEMPTS", 6))
    attempts = int(row.get("attempts") or 0) + 1
    body = _delivery_body(row)
    timestamp = int(time.time())
    update: dict[str, Any] = {"attempts": attempts, "locked_until": None}
    try:
        validate_webhook_url(endpoint_url)  # revalida DNS (anti DNS-rebinding)
        resp = requests.post(
            endpoint_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": sign_delivery(secret, timestamp, body),
                "X-Webhook-Timestamp": str(timestamp),
                "X-Webhook-Id": str(row["id"]),
                "X-Webhook-Event": row["event_type"],
                "User-Agent": USER_AGENT,
            },
            timeout=int(cfg.get("OUTBOUND_WEBHOOK_TIMEOUT", 10)),
            allow_redirects=False,
        )
        update["response_status"] = resp.status_code
        update["response_body"] = (resp.text or "")[:500]
        if 200 <= resp.status_code < 300:
            update.update(status="delivered", delivered_at=_utcnow().isoformat(),
                          last_error=None, next_retry_at=None)
        else:
            update["last_error"] = f"Endpoint respondeu HTTP {resp.status_code}."
    except WebhookURLBlocked as exc:
        update["last_error"] = f"URL bloqueada: {exc}"
        attempts = max_attempts  # não adianta tentar de novo
    except requests.Timeout:
        update["last_error"] = "Tempo esgotado ao chamar o endpoint."
    except requests.RequestException as exc:
        update["last_error"] = f"Falha de conexão ({type(exc).__name__})."

    if update.get("status") != "delivered":
        if attempts < max_attempts:
            update["status"] = "pending"
            update["next_retry_at"] = (_utcnow() + timedelta(minutes=_backoff_minutes(attempts))).isoformat()
        else:
            update["status"] = "failed"
            update["next_retry_at"] = None
    try:
        get_service_client().table("webhook_deliveries").update(update).eq("id", row["id"]).execute()
    except Exception:
        logger.exception("Falha ao atualizar entrega de webhook %s", row.get("id"))
    return update["status"]


def _deliver_by_id(delivery_id: str) -> None:
    client = get_service_client()
    rows = (
        client.table("webhook_deliveries")
        .select("id, organization_id, endpoint_id, event_type, payload, attempts, max_attempts, created_at")
        .eq("id", delivery_id).limit(1).execute().data
    )
    if not rows:
        return
    _deliver_row(rows[0])


def _deliver_row(row: dict[str, Any]) -> None:
    client = get_service_client()
    endpoints = (
        client.table("webhook_endpoints").select("id, url, secret_encrypted, status")
        .eq("id", row["endpoint_id"]).eq("organization_id", row["organization_id"])
        .limit(1).execute().data
    )
    if not endpoints or endpoints[0].get("status") != "active":
        client.table("webhook_deliveries").update({
            "status": "failed", "locked_until": None, "next_retry_at": None,
            "last_error": "Endpoint removido ou desativado.",
        }).eq("id", row["id"]).execute()
        return
    endpoint = endpoints[0]
    try:
        secret = decrypt_token(endpoint["secret_encrypted"])
    except (DecryptionError, EncryptionNotConfigured):
        logger.error("Segredo indisponível para o endpoint %s", endpoint["id"])
        client.table("webhook_deliveries").update({
            "status": "failed", "locked_until": None, "next_retry_at": None,
            "last_error": "Segredo do endpoint indisponível (chave de criptografia alterada?).",
        }).eq("id", row["id"]).execute()
        return
    deliver(row, endpoint["url"], secret)


# ---------------------------------------------------------------------------
# Enfileiramento e reprocessamento
# ---------------------------------------------------------------------------
def enqueue_event(org_id: str, event_type: str, payload: dict[str, Any],
                  only_endpoint_id: str | None = None) -> int:
    """Cria uma entrega por endpoint ativo inscrito no evento e agenda o envio.

    Erros nunca propagam: webhook de saída não pode derrubar o fluxo principal.
    """
    created = 0
    try:
        client = get_service_client()
        query = (
            client.table("webhook_endpoints").select("id, events, phone_number_ids")
            .eq("organization_id", org_id).eq("status", "active")
        )
        if only_endpoint_id:
            query = query.eq("id", only_endpoint_id)
        endpoints = query.execute().data
        now = _utcnow()
        event_phones = _event_phone_numbers(client, org_id, payload)
        for endpoint in endpoints:
            if event_type != "test" and event_type not in (endpoint.get("events") or []):
                continue
            allowed = endpoint.get("phone_number_ids") or []
            # Endpoint restrito a números: só recebe eventos desses números.
            if allowed and event_type != "test" and not event_phones & set(allowed):
                continue
            inserted = client.table("webhook_deliveries").insert({
                "organization_id": org_id,
                "endpoint_id": endpoint["id"],
                "event_type": event_type,
                "payload": payload,
                "attempts": 0,
                "max_attempts": int(current_app.config.get("OUTBOUND_WEBHOOK_MAX_ATTEMPTS", 6)),
                "status": "pending",
                # Já "vencida", mas travada: se o processo cair antes da entrega,
                # o worker a recupera quando a trava expirar.
                "next_retry_at": now.isoformat(),
                "locked_until": (now + timedelta(minutes=LOCK_MINUTES)).isoformat(),
            }).execute().data[0]
            created += 1
            tasks.submit(_deliver_by_id, inserted["id"])
    except Exception:
        logger.exception("Falha ao enfileirar evento %s para org %s", event_type, org_id)
    return created


def _event_phone_numbers(client: Any, org_id: str, payload: dict[str, Any]) -> set[str]:
    """Números a que o evento se refere (direto, ou todos os números da WABA)."""
    if payload.get("phone_number_id"):
        return {str(payload["phone_number_id"])}
    if payload.get("waba_id"):
        rows = (client.table("meta_connections").select("phone_number_id")
                .eq("organization_id", org_id).eq("waba_id", str(payload["waba_id"])).execute().data)
        return {str(r["phone_number_id"]) for r in rows}
    return set()


def send_test_event(org_id: str, endpoint_id: str) -> bool:
    return enqueue_event(org_id, "test", {
        "message": "Evento de teste. Se você recebeu isto, seu endpoint está funcionando.",
    }, only_endpoint_id=endpoint_id) > 0


def redeliver(org_id: str, delivery_id: str) -> bool:
    """Reenvia manualmente uma entrega (nova tentativa imediata)."""
    client = get_service_client()
    rows = (
        client.table("webhook_deliveries")
        .select("id, organization_id, endpoint_id, event_type, payload, attempts, max_attempts, created_at")
        .eq("id", delivery_id).eq("organization_id", org_id).limit(1).execute().data
    )
    if not rows:
        return False
    row = rows[0]
    row["max_attempts"] = max(int(row.get("max_attempts") or 1), int(row.get("attempts") or 0) + 1)
    client.table("webhook_deliveries").update({
        "status": "pending", "max_attempts": row["max_attempts"],
        "locked_until": (_utcnow() + timedelta(minutes=LOCK_MINUTES)).isoformat(),
    }).eq("id", delivery_id).execute()
    tasks.submit(_deliver_row, row)
    return True


def process_pending_retries(limit: int = 25) -> int:
    """Reivindica entregas vencidas (SKIP LOCKED) e tenta de novo."""
    rows = get_service_client().rpc("claim_due_webhook_deliveries", {"p_limit": limit}).execute().data or []
    for row in rows:
        _deliver_row(row)
    return len(rows)
