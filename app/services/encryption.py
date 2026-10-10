"""Criptografia simétrica (Fernet/AES) para tokens da Meta persistidos.

Tokens que precisam ser recuperados (access tokens da Meta) NÃO devem usar hash;
usa-se criptografia com chave dedicada (TOKEN_ENCRYPTION_KEY).

Rotação de chave: gere nova chave, re-criptografe os registros (descriptografando
com a chave antiga) e atualize a variável de ambiente. Um script de rotação pode
ser adicionado sem mudar esta interface.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app


class EncryptionNotConfigured(RuntimeError):
    pass


class DecryptionError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = current_app.config.get("TOKEN_ENCRYPTION_KEY", "")
    if not key:
        raise EncryptionNotConfigured(
            "TOKEN_ENCRYPTION_KEY não definida. Gere uma com: python -c "
            "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfigured(
            "TOKEN_ENCRYPTION_KEY inválida: esperada chave Fernet em base64 (44 chars)."
        ) from exc


def encrypt_token(plaintext: str) -> str:
    if not plaintext:
        raise ValueError("Token vazio não pode ser criptografado.")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise DecryptionError(
            "Falha ao descriptografar token (chave incorreta ou dado corrompido)."
        ) from exc
