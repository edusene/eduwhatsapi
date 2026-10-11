"""Primitivas de segurança: hashing de chaves, tokens, CSRF e HMAC."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from flask import abort, current_app, request, session

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FIELD = "csrf_token"
_CSRF_EXEMPT_PREFIXES = ("/api/", "/webhooks/", "/internal/")


def hash_api_key(key: str) -> str:
    """HMAC-SHA256 (com pepper do servidor) de uma chave interna de API.

    Determinístico para permitir busca no banco; o pepper impede ataques de
    dicionário caso o banco vaze sem as variáveis de ambiente.
    """
    pepper = current_app.config.get("API_KEY_PEPPER", "")
    if not pepper:
        raise RuntimeError("API_KEY_PEPPER não definida.")
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def hash_token(token: str) -> str:
    """Hash de tokens de uso único (convites). Mesmo pepper das chaves."""
    return hash_api_key("tok:" + token)


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def hmac_sha256_hex(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_meta_signature(app_secret: str, body: bytes, header: str) -> bool:
    """Valida X-Hub-Signature-256 (HMAC-SHA256 do corpo bruto com o App Secret)."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac_sha256_hex(app_secret, body)
    return constant_time_equals(header, expected)


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
        abort(403, description="Sessão expirada ou token CSRF inválido. Recarregue a página.")


def is_csrf_exempt() -> bool:
    """Rotas autenticadas por chave de API, assinatura ou segredo de cron."""
    return request.path.startswith(_CSRF_EXEMPT_PREFIXES)
