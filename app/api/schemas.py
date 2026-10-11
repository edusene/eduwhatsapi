"""Validação de payloads da API v1 (reexporta o validador compartilhado)."""
from app.services.message_validation import ValidationError, normalize_recipient, validate_send_payload

__all__ = ["ValidationError", "normalize_recipient", "validate_send_payload"]
