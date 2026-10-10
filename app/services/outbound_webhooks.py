"""Webhooks de saída: entrega de eventos da plataforma para URLs de clientes.

- URLs passam por validação anti-SSRF (HTTPS, sem IPs privados/reservados).
- Cada endpoint tem um segredo HMAC (``whsec_...``) criptografado no banco e
  exibido uma única vez (criação/rotação).
- A entrega é síncrona e leve: o POST é feito no momento do enfileiramento e
  falhas ficam como ``pending`` com ``next_retry_at`` (backoff exponencial),
  reprocessadas pelo comando ``flask deliver-webhooks``. Isso evita depender
  de um worker externo no MVP; uma fila assíncrona pode substituir a chamada
  síncrona depois sem mudar o schema.
- Nenhuma exceção de banco/rede propaga para o fluxo principal.
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

import click
import requests
from flask import Flask, current_app

from app.services import audit
from app.services.encryption import (
    DecryptionError,
    EncryptionNotConfigured,
    decrypt_token,
    encrypt_token,
)
from app.services.security import hmac_sha256_hex
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

VALID_EVENTS = (
    "message.received",
    "message.status.updated",
    "channel.connected",
    "channel.disconnected",
    "api.request.failed",
    "test",
)

USER_AGENT = "EduAuto-Webhooks/1.0"
_CLOUD_METADATA_IP = ipaddress.ip_address("169.254.169.254")


class WebhookURLBlocked(ValueError):
    """URL de webhook rejeitada pela validação anti-SSRF."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Validação de URL (anti-SSRF)
# ---------------------------------------------------------------------------
def validate_webhook_url(url: str) -> str:
    """Valida e normaliza a URL de destino. Levanta WebhookURLBlocked.

    Mensagens de erro são seguras: nunca ecoam o IP interno resolvido.
    """
    allow_insecure = bool(current_app.config.get("OUTBOUND_WEBHOOK_ALLOW_INSECURE", False))
    try:
        parsed = urlsplit((url or "").strip())
    except (ValueError, AttributeError) as exc:
        raise WebhookURLBlocked("URL inválida.") from exc

    scheme = parsed.scheme.lower()
    if scheme == "http" and not allow_insecure:
        raise WebhookURLBlocked("A URL do webhook precisa usar HTTPS.")
    if scheme not in ("https", "http"):
        raise WebhookURLBlocked("A URL do webhook precisa usar HTTPS.")
    if parsed.username or parsed.password:
        raise WebhookURLBlocked("A URL não pode conter usuário e senha.")

    host = parsed.hostname
    if not host:
        raise WebhookURLBlocked("A URL do webhook precisa ter um host válido.")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise WebhookURLBlocked("Porta inválida na URL.") from exc

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise WebhookURLBlocked("Não foi possível resolver o host da URL.") from exc
    if not infos:
        raise WebhookURLBlocked("Não foi possível resolver o host da URL.")

    for info in infos:
        raw_ip = info[4][0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise WebhookURLBlocked("Endereço resolvido inválido.") from exc
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        blocked = (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
            or ip == _CLOUD_METADATA_IP  # metadados de cloud (AWS/GCP/Azure)
        )
        if blocked:
            raise WebhookURLBlocked(
                "A URL aponta para um endereço de rede interno ou reservado."
            )
    return parsed.geturl()


# ---------------------------------------------------------------------------
# Views públicas
# ---------------------------------------------------------------------------
def _secret_hint(row: dict[str, Any]) -> str | None:
    """Últimos 4 caracteres do segredo (descriptografando sob demanda)."""
    try:
        secret = decrypt_token(row["secret_encrypted"])
    except (DecryptionError, EncryptionNotConfigured, KeyError, TypeError):
        return None
    return f"whsec_…{secret[-4:]}"


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "url": row["url"],
        "description": row.get("description"),
        "events": row.get("events") or [],
        "status": row.get("status"),
        "created_at": row.get("created_at"),
        "secret_hint": _secret_hint(row),
    }


# ---------------------------------------------------------------------------
# CRUD de endpoints
# ---------------------------------------------------------------------------
def create_endpoint(
    org_id: str,
    url: str,
    events: list[str],
    description: str | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    url = validate_webhook_url(url)
    events = list(events or [])
    invalid = sorted(set(events) - set(VALID_EVENTS))
    if invalid:
        raise ValueError(f"Eventos inválidos: {', '.join(invalid)}")
    if not events:
        raise ValueError("Selecione ao menos um evento.")
    if description is not None:
        description = description.strip()[:200] or None

    secret = "whsec_" + secrets.token_urlsafe(32)
    row = {
        "organization_id": org_id,
        "url": url,
        "description": description,
        "events": events,
        "secret_encrypted": encrypt_token(secret),
        "status": "active",
        "created_by": created_by,
    }
    record = get_service_client().table("webhook_endpoints").insert(row).execute().data[0]
    audit.log_action(org_id, created_by, "webhook_endpoint.created",
                     "webhook_endpoint", record["id"],
                     {"url": url, "events": events})
    view = _public_view(record)
    view["secret"] = secret  # exibido apenas nesta resposta
    return view


def list_endpoints(org_id: str) -> list[dict[str, Any]]:
    result = (
        get_service_client()
        .table("webhook_endpoints")
        .select("id, url, description, events, status, secret_encrypted, created_at")
        .eq("organization_id", org_id)
        .order("created_at", desc=True)
        .execute()
    )
    return [_public_view(r) for r in result.data]


def get_endpoint(org_id: str, endpoint_id: str) -> dict[str, Any] | None:
    result = (
        get_service_client()
        .table("webhook_endpoints")
        .select("id, url, description, events, status, secret_encrypted, created_at")
        .eq("id", endpoint_id)
        .eq("organization_id", org_id)
        .limit(1)
        .execute()
    )
    return _public_view(result.data[0]) if result.data else None


def delete_endpoint(org_id: str, endpoint_id: str, user_id: str | None = None) -> bool:
    result = (
        get_service_client()
        .table("webhook_endpoints")
        .delete()
        .eq("id", endpoint_id)
        .eq("organization_id", org_id)
        .execute()
    )
    if not result.data:
        return False
    audit.log_action(org_id, user_id, "webhook_endpoint.deleted",
                     "webhook_endpoint", endpoint_id,
                     {"url": result.data[0].get("url")})
    return True


def rotate_secret(org_id: str, endpoint_id: str, user_id: str | None = None) -> dict[str, Any] | None:
    client = get_service_client()
    result = (
        client.table("webhook_endpoints")
        .select("id")
        .eq("id", endpoint_id)
        .eq("organization_id", org_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    secret = "whsec_" + secrets.token_urlsafe(32)
    client.table("webhook_endpoints").update(
        {"secret_encrypted": encrypt_token(secret)}
    ).eq("id", endpoint_id).eq("organization_id", org_id).execute()
    audit.log_action(org_id, user_id, "webhook_endpoint.secret_rotated",
                     "webhook_endpoint", endpoint_id, {})
    return {
        "id": endpoint_id,
        "secret": secret,  # exibido apenas nesta resposta
        "secret_hint": f"whsec_…{secret[-4:]}",
    }


# ---------------------------------------------------------------------------
# Assinatura e entrega
# ---------------------------------------------------------------------------
def sign_delivery(secret: str, timestamp: int, body: bytes) -> str:
    """Assinatura HMAC-SHA256 do corpo, com timestamp para anti-replay."""
    return "sha256=" + hmac_sha256_hex(secret, f"{timestamp}.".encode() + body)


def _sanitize_error(exc: BaseException, url: str) -> str:
    """Mensagem de erro sem a URL (que poderia conter credenciais embutidas)."""
    message = str(exc).replace(url, "<endpoint>")
    return message[:300]


def _delivery_body(delivery_row: dict[str, Any]) -> bytes:
    envelope = {
        "id": delivery_row["id"],
        "event": delivery_row["event_type"],
        "created_at": delivery_row.get("created_at"),
        "data": delivery_row.get("payload") or {},
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def deliver(delivery_row: dict[str, Any], endpoint_url: str, secret: str) -> None:
    """Executa o POST de uma entrega e atualiza a linha webhook_deliveries.

    Sucesso = resposta 2xx. Falhas agendam retry com backoff exponencial em
    minutos (2**attempts, máx. 60) até atingir max_attempts.
    """
    cfg = current_app.config
    timeout = int(cfg.get("OUTBOUND_WEBHOOK_TIMEOUT", 10))
    max_attempts = int(
        delivery_row.get("max_attempts") or cfg.get("OUTBOUND_WEBHOOK_MAX_ATTEMPTS", 5)
    )
    attempts = int(delivery_row.get("attempts") or 0) + 1
    body = _delivery_body(delivery_row)
    timestamp = int(time.time())

    update: dict[str, Any] = {"attempts": attempts}
    try:
        resp = requests.post(
            endpoint_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": sign_delivery(secret, timestamp, body),
                "X-Webhook-Timestamp": str(timestamp),
                "X-Webhook-Id": str(delivery_row["id"]),
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
            allow_redirects=False,
        )
        update["response_status"] = resp.status_code
        update["response_body"] = (resp.text or "")[:500]
        if 200 <= resp.status_code < 300:
            update["status"] = "delivered"
            update["delivered_at"] = _utcnow().isoformat()
            update["last_error"] = None
            update["next_retry_at"] = None
        else:
            update["last_error"] = f"Endpoint respondeu HTTP {resp.status_code}."
    except requests.Timeout:
        update["last_error"] = "Tempo esgotado ao chamar o endpoint."
    except requests.RequestException as exc:
        update["last_error"] = _sanitize_error(exc, endpoint_url)

    if update.get("status") != "delivered":
        if attempts < max_attempts:
            delay_minutes = min(2 ** attempts, 60)
            update["status"] = "pending"
            update["next_retry_at"] = (
                _utcnow() + timedelta(minutes=delay_minutes)
            ).isoformat()
        else:
            update["status"] = "failed"
            update["next_retry_at"] = None

    try:
        get_service_client().table("webhook_deliveries").update(update).eq(
            "id", delivery_row["id"]
        ).execute()
    except Exception:
        logger.exception("Falha ao atualizar entrega de webhook %s", delivery_row.get("id"))


# ---------------------------------------------------------------------------
# Enfileiramento e reprocessamento
# ---------------------------------------------------------------------------
def enqueue_event(org_id: str, event_type: str, payload: dict) -> int:
    """Cria uma entrega por endpoint ativo inscrito no evento e a dispara.

    A entrega inicial é síncrona (ver docstring do módulo). Erros de banco são
    logados e NÃO propagados: webhook de saída não pode derrubar o fluxo
    principal. Retorna o número de entregas criadas.
    """
    created = 0
    try:
        client = get_service_client()
        endpoints = (
            client.table("webhook_endpoints")
            .select("id, url, events, secret_encrypted")
            .eq("organization_id", org_id)
            .eq("status", "active")
            .execute()
            .data
        )
        max_attempts = int(current_app.config.get("OUTBOUND_WEBHOOK_MAX_ATTEMPTS", 5))
        for endpoint in endpoints:
            if event_type not in (endpoint.get("events") or []):
                continue
            row = {
                "organization_id": org_id,
                "endpoint_id": endpoint["id"],
                "event_type": event_type,
                "payload": payload,
                "attempts": 0,
                "max_attempts": max_attempts,
                "status": "pending",
            }
            inserted = client.table("webhook_deliveries").insert(row).execute().data[0]
            created += 1
            try:
                secret = decrypt_token(endpoint["secret_encrypted"])
            except (DecryptionError, EncryptionNotConfigured):
                logger.exception(
                    "Segredo indisponível para o endpoint %s", endpoint["id"]
                )
                client.table("webhook_deliveries").update(
                    {"status": "failed", "last_error": "Segredo do endpoint indisponível."}
                ).eq("id", inserted["id"]).execute()
                continue
            deliver(inserted, endpoint["url"], secret)
    except Exception:
        logger.exception(
            "Falha ao enfileirar evento de saída %s para org %s", event_type, org_id
        )
    return created


def send_test_event(org_id: str, endpoint_id: str, user_id: str | None = None) -> bool:
    """Dispara um evento ``test`` apenas para o endpoint informado."""
    try:
        client = get_service_client()
        result = (
            client.table("webhook_endpoints")
            .select("id, url, secret_encrypted")
            .eq("id", endpoint_id)
            .eq("organization_id", org_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            return False
        endpoint = result.data[0]
        row = {
            "organization_id": org_id,
            "endpoint_id": endpoint["id"],
            "event_type": "test",
            "payload": {"message": "Evento de teste da plataforma"},
            "attempts": 0,
            "max_attempts": int(current_app.config.get("OUTBOUND_WEBHOOK_MAX_ATTEMPTS", 5)),
            "status": "pending",
        }
        inserted = client.table("webhook_deliveries").insert(row).execute().data[0]
        secret = decrypt_token(endpoint["secret_encrypted"])
        deliver(inserted, endpoint["url"], secret)
        return True
    except Exception:
        logger.exception("Falha ao enviar evento de teste para endpoint %s", endpoint_id)
        return False


def process_pending_retries(limit: int = 50) -> int:
    """Re-entrega deliveries ``pending`` cujo ``next_retry_at`` já venceu.

    O filtro de data é feito em Python (ISO strings são comparáveis) para
    compatibilidade com o cliente fake dos testes. Linhas sem next_retry_at
    são ignoradas: ou acabaram de ser criadas (entrega síncrona em andamento)
    ou não têm retry agendado.
    """
    now_iso = _utcnow().isoformat()
    client = get_service_client()
    rows = (
        client.table("webhook_deliveries")
        .select("id, endpoint_id, event_type, payload, attempts, max_attempts,"
                " next_retry_at, created_at")
        .eq("status", "pending")
        .order("created_at")
        .limit(limit)
        .execute()
        .data
    )
    due = [r for r in rows if r.get("next_retry_at") and r["next_retry_at"] <= now_iso]
    processed = 0
    for delivery in due:
        result = (
            client.table("webhook_endpoints")
            .select("id, url, secret_encrypted, status")
            .eq("id", delivery["endpoint_id"])
            .limit(1)
            .execute()
        )
        if not result.data or result.data[0].get("status") != "active":
            client.table("webhook_deliveries").update(
                {"status": "failed", "last_error": "Endpoint removido ou desativado."}
            ).eq("id", delivery["id"]).execute()
            continue
        endpoint = result.data[0]
        try:
            secret = decrypt_token(endpoint["secret_encrypted"])
        except (DecryptionError, EncryptionNotConfigured):
            logger.exception("Segredo indisponível para o endpoint %s", endpoint["id"])
            client.table("webhook_deliveries").update(
                {"status": "failed", "last_error": "Segredo do endpoint indisponível."}
            ).eq("id", delivery["id"]).execute()
            continue
        deliver(delivery, endpoint["url"], secret)
        processed += 1
    return processed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def register_cli(app: Flask) -> None:
    @app.cli.command("deliver-webhooks")
    def deliver_webhooks() -> None:
        """Reprocessa entregas de webhook pendentes (retries vencidos)."""
        total = process_pending_retries()
        click.echo(f"Entregas reprocessadas: {total}")
