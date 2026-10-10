"""Primitivas de segurança: hashing de chaves de API, CSRF, HMAC."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from flask import abort, current_app, request, session

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FIELD = "csrf_token"


def hash_api_key(key: str) -> str:
    """Hash HMAC-SHA256 (com pepper do servidor) de uma chave interna de API.

    Determinístico para permitir busca no banco; o pepper impede ataques de
    dicionário caso o banco vaze sem as variáveis de ambiente.
    """
    pepper = current_app.config.get("API_KEY_PEPPER", "")
    if not pepper:
        raise RuntimeError("API_KEY_PEPPER não definida.")
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def hmac_sha256_hex(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def generate_csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf() -> None:
    """Compara o token enviado (campo de formulário ou header) com o da sessão."""
    expected = session.get(CSRF_SESSION_KEY)
    sent = request.form.get(CSRF_FIELD) or request.headers.get("X-CSRF-Token", "")
    if not expected or not sent or not constant_time_equals(expected, sent):
        abort(403, description="Token CSRF inválido ou ausente.")


def is_csrf_exempt() -> bool:
    """Rotas autenticadas por chave de API ou webhooks não usam sessão/CSRF."""
    path = request.path
    return path.startswith("/api/") or path.startswith("/webhooks/")
