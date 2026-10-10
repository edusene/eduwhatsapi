"""Registro de auditoria de ações relevantes (criação/revogação de chaves,
conexão de canais, alterações de webhook). Nunca registre segredos aqui."""
from __future__ import annotations

import logging
from typing import Any

from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)


def log_action(
    organization_id: str | None,
    actor_user_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    row = {
        "organization_id": organization_id,
        "actor_user_id": actor_user_id,
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id) if target_id else None,
        "metadata": metadata or {},
    }
    try:
        get_service_client().table("audit_logs").insert(row).execute()
    except Exception:  # auditoria não deve derrubar a operação principal
        logger.exception("Falha ao registrar audit log: %s", action)
