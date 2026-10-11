"""Rotinas de manutenção: retries de webhooks e retenção de dados (LGPD).

Execute periodicamente por um destes meios:
- CLI:  ``flask --app wsgi deliver-webhooks`` / ``flask --app wsgi purge-data``
- HTTP: ``POST /internal/cron/deliver-webhooks`` e ``/internal/cron/purge``
  com ``Authorization: Bearer <CRON_SECRET>`` (ex.: Render Cron Job ou outro
  agendador externo).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import click
from flask import Flask, current_app

from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)


def _ago(**kwargs: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def purge_expired() -> dict[str, Any]:
    client = get_service_client()
    log_days = int(current_app.config.get("LOG_RETENTION_DAYS", 90))
    removed: dict[str, Any] = {}
    removed["api_request_logs"] = len(
        client.table("api_request_logs").delete().lt("created_at", _ago(days=log_days)).execute().data or [])
    removed["webhook_events"] = len(
        client.table("webhook_events").delete().lt("received_at", _ago(days=30)).execute().data or [])
    removed["webhook_deliveries"] = len(
        client.table("webhook_deliveries").delete().neq("status", "pending")
        .lt("created_at", _ago(days=30)).execute().data or [])
    removed["idempotency_keys"] = len(
        client.table("idempotency_keys").delete().lt("created_at", _ago(days=1)).execute().data or [])

    messages = 0
    for org in client.table("organizations").select("id, message_retention_days").execute().data:
        days = int(org.get("message_retention_days") or 90)
        messages += len(client.table("messages").delete().eq("organization_id", org["id"])
                        .lt("created_at", _ago(days=days)).execute().data or [])
    removed["messages"] = messages
    logger.info("Retenção aplicada: %s", removed)
    return removed


def register_cli(app: Flask) -> None:
    @app.cli.command("deliver-webhooks")
    def deliver_webhooks() -> None:
        """Reprocessa entregas de webhook pendentes (retries vencidos)."""
        from app.services.outbound_webhooks import process_pending_retries

        click.echo(f"Entregas reprocessadas: {process_pending_retries(limit=100)}")

    @app.cli.command("purge-data")
    def purge_data() -> None:
        """Apaga logs, eventos e mensagens fora do prazo de retenção."""
        click.echo(f"Registros removidos: {purge_expired()}")
