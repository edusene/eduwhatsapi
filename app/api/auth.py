"""Autenticação da API pública por chave interna (sk_test_/sk_live_).

Rate limit por chave (60 req/min): o decorador do Flask-Limiter é aplicado
DIRETAMENTE na view e a verificação de autenticação acontece no wrapper
externo, que só então chama a view limitada. Assim o key_func já encontra
``g.api_key`` preenchido; sem chave válida, cai para o IP como chave. Essa
ordem (auth primeiro, limite depois) é o que permite limitar por chave com o
Flask-Limiter 4 sem decoradores aninhados nas rotas.
"""
from __future__ import annotations

import functools
from typing import Any, Callable

from flask import g, request
from flask_limiter.util import get_remote_address

from app.api import error_response
from app.extensions import limiter
from app.services.api_keys import verify_api_key

RATE_LIMIT_PER_KEY = "60 per minute"


def _rate_limit_key() -> str:
    api_key = g.get("api_key")
    return api_key["id"] if api_key else get_remote_address()


def require_api_key(*required_scopes: str) -> Callable:
    """Exige ``Authorization: Bearer sk_...`` válido e os escopos informados.

    Define ``g.api_key`` (registro da chave) e ``g.organization_id`` — a
    organização da requisição vem SOMENTE da chave validada.
    """
    def decorator(view: Callable) -> Callable:
        limited_view = limiter.limit(RATE_LIMIT_PER_KEY, key_func=_rate_limit_key)(view)

        @functools.wraps(view)
        def wrapper(*args: Any, **kwargs: Any):
            header = request.headers.get("Authorization", "")
            parts = header.split()
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return error_response(
                    "missing_api_key",
                    "Envie o header Authorization: Bearer <sua_chave>.",
                    401,
                )
            record = verify_api_key(parts[1])
            if record is None:
                return error_response(
                    "invalid_api_key", "Chave de API inválida ou revogada.", 401
                )
            g.api_key = record
            g.organization_id = record["organization_id"]

            granted = record.get("scopes") or []
            missing = [s for s in required_scopes if s not in granted]
            if missing:
                return error_response(
                    "insufficient_scope",
                    "A chave não tem o escopo necessário para esta operação.",
                    403,
                )
            return limited_view(*args, **kwargs)

        return wrapper

    return decorator
