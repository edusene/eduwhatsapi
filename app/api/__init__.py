"""API pública v1 da plataforma (prefixo /api/v1, aplicado pela factory).

- Autenticação por chave interna (sk_test_/sk_live_) — ver app.api.auth.
- Erros seguem o envelope {"error": {code, message, request_id}}.
- Cada requisição é registrada em api_request_logs de forma sanitizada
  (sem Authorization, tokens, cookies ou corpo). Falha no log nunca quebra
  a resposta.
"""
from __future__ import annotations

import logging
import time

from flask import Blueprint, current_app, g, jsonify, request

from app.meta.graph_api import MetaAPIError
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

bp = Blueprint("api_v1", __name__)


def error_response(code: str, message: str, status: int, **extra):
    """Envelope padrão de erro da API v1."""
    payload = {"error": {"code": code, "message": message,
                         "request_id": g.get("request_id", "")}}
    payload["error"].update(extra)
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# Log sanitizado de requisições
# ---------------------------------------------------------------------------
@bp.before_request
def _start_timer() -> None:
    g._api_request_started = time.perf_counter()


@bp.after_request
def _log_request(response):
    try:
        started = g.get("_api_request_started")
        latency_ms = (
            int((time.perf_counter() - started) * 1000) if started is not None else None
        )
        error_code = None
        if response.status_code >= 400:
            body = response.get_json(silent=True)
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                error_code = body["error"].get("code")
        api_key = g.get("api_key") or {}
        endpoint = request.url_rule.rule if request.url_rule else request.path
        get_service_client().table("api_request_logs").insert({
            "organization_id": api_key.get("organization_id"),
            "api_key_id": api_key.get("id"),
            "request_id": g.get("request_id", ""),
            "method": request.method,
            "endpoint": endpoint,
            "status_code": response.status_code,
            "latency_ms": latency_ms,
            "error_code": error_code,
        }).execute()
    except Exception:
        logger.exception("Falha ao registrar log de requisição da API.")
    return response


# ---------------------------------------------------------------------------
# Handlers de erro do blueprint
# ---------------------------------------------------------------------------
@bp.errorhandler(MetaAPIError)
def _handle_meta_error(exc: MetaAPIError):
    logger.warning(
        "Erro da Meta Graph API (http=%s fb_code=%s fb_subcode=%s timeout=%s).",
        exc.http_status, exc.fb_code, exc.fb_subcode, exc.is_timeout,
    )
    if exc.is_timeout:
        return error_response(
            "gateway_timeout",
            "A Meta demorou para responder. Tente novamente.",
            504,
        )
    return error_response(
        "meta_api_error",
        "A Meta recusou a operação. Verifique o canal e tente novamente.",
        502,
        fb_code=exc.fb_code,
    )


# Registrado após import de schemas (evita dependência circular no topo).
from app.api.schemas import ValidationError  # noqa: E402


@bp.errorhandler(ValidationError)
def _handle_validation_error(exc: ValidationError):
    return error_response(exc.code, str(exc), 400)


from app.api import routes  # noqa: E402,F401
