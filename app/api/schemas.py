"""Validação de payloads da API v1."""
from __future__ import annotations

import re
from typing import Any

_RECIPIENT_RE = re.compile(r"\d{8,15}")


class ValidationError(ValueError):
    """Erro de validação de entrada da API (mapeado para HTTP 400)."""

    def __init__(self, message: str, code: str = "validation_error") -> None:
        super().__init__(message)
        self.code = code


def validate_send_message_payload(data: Any) -> dict[str, str]:
    """Valida o corpo de POST /whatsapp/<id>/messages. Retorna {to, body}."""
    if not isinstance(data, dict):
        raise ValidationError("Corpo JSON inválido.", "invalid_json")

    to = data.get("to")
    if not isinstance(to, str):
        raise ValidationError("Destinatário inválido.", "invalid_recipient")
    to = to.strip()
    if to.startswith("+"):
        to = to[1:]
    if not _RECIPIENT_RE.fullmatch(to):
        raise ValidationError(
            "Destinatário inválido: use apenas dígitos (8 a 15), incluindo "
            "código do país e DDD.",
            "invalid_recipient",
        )

    if data.get("type") != "text":
        raise ValidationError(
            "Tipo de mensagem não suportado: o MVP aceita apenas 'text'.",
            "unsupported_message_type",
        )

    text = data.get("text")
    body = text.get("body") if isinstance(text, dict) else None
    if not isinstance(body, str) or not (1 <= len(body) <= 4096):
        raise ValidationError(
            "O campo text.body é obrigatório (1 a 4096 caracteres).",
            "invalid_message_body",
        )
    return {"to": to, "body": body}
