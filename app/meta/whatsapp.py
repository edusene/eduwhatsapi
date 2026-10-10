"""Adaptador da WhatsApp Business Cloud API sobre o cliente da Graph API."""
from __future__ import annotations

from typing import Any

from app.meta.graph_api import MetaGraphClient


class WhatsAppAdapter:
    """Operações oficiais de um número WhatsApp Business (phone_number_id)."""

    def __init__(self, client: MetaGraphClient) -> None:
        self.client = client

    def get_phone_number(self, phone_number_id: str, token: str) -> dict[str, Any]:
        """Dados públicos do número (valida token + phone_number_id na Meta)."""
        return self.client.get(
            f"/{phone_number_id}",
            token,
            params={"fields": "display_phone_number,verified_name,quality_rating"},
        )

    def list_phone_numbers(self, waba_id: str, token: str) -> list[dict[str, Any]]:
        """Números de uma WABA (WhatsApp Business Account)."""
        data = self.client.get(f"/{waba_id}/phone_numbers", token)
        return data.get("data", []) if isinstance(data, dict) else []

    def send_text_message(
        self, phone_number_id: str, token: str, to: str, body: str
    ) -> dict[str, Any]:
        """Envia mensagem de texto. Retorna o JSON da Meta (inclui messages[].id)."""
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        return self.client.post(f"/{phone_number_id}/messages", token, json=payload)
