"""Adaptador da WhatsApp Business Cloud API sobre o cliente da Graph API.

Endpoints usados (documentação oficial da Cloud API / Embedded Signup):
- GET  /{phone-number-id}                      dados do número
- POST /{phone-number-id}/messages             envio / marcar como lida
- POST /{phone-number-id}/register             registro do número (PIN 2FA)
- POST /{phone-number-id}/media                upload de mídia
- GET  /{media-id}                             URL temporária da mídia
- GET  /{waba-id}/phone_numbers                números da WABA
- GET  /{waba-id}/message_templates            modelos de mensagem
- POST /{waba-id}/message_templates            criar modelo
- POST/DELETE /{waba-id}/subscribed_apps       inscrever o app nos webhooks da WABA
- POST /{phone-number-id}/smb_app_data         sincronização (coexistência)
"""
from __future__ import annotations

from typing import Any

from app.meta.graph_api import MetaGraphClient

PHONE_FIELDS = "display_phone_number,verified_name,quality_rating,name_status,code_verification_status,platform_type,throughput"
TEMPLATE_FIELDS = "id,name,status,language,category,components,rejected_reason"


class WhatsAppAdapter:
    channel = "whatsapp"

    def __init__(self, client: MetaGraphClient) -> None:
        self.client = client

    # -- números --------------------------------------------------------------
    def get_phone_number(self, phone_number_id: str, token: str) -> dict[str, Any]:
        return self.client.get(f"/{phone_number_id}", token, params={"fields": PHONE_FIELDS})

    def list_phone_numbers(self, waba_id: str, token: str) -> list[dict[str, Any]]:
        data = self.client.get(f"/{waba_id}/phone_numbers", token,
                               params={"fields": "id," + PHONE_FIELDS})
        return data.get("data", []) if isinstance(data, dict) else []

    def register_phone_number(self, phone_number_id: str, token: str, pin: str) -> dict[str, Any]:
        return self.client.post(f"/{phone_number_id}/register", token,
                                json={"messaging_product": "whatsapp", "pin": pin})

    def request_coexistence_sync(self, phone_number_id: str, token: str, sync_type: str) -> dict[str, Any]:
        return self.client.post(f"/{phone_number_id}/smb_app_data", token,
                                json={"messaging_product": "whatsapp", "sync_type": sync_type})

    # -- webhooks da WABA ------------------------------------------------------
    def subscribe_app(self, waba_id: str, token: str) -> dict[str, Any]:
        return self.client.post(f"/{waba_id}/subscribed_apps", token)

    def unsubscribe_app(self, waba_id: str, token: str) -> dict[str, Any]:
        return self.client.delete(f"/{waba_id}/subscribed_apps", token)

    # -- mensagens --------------------------------------------------------------
    def send_message(self, phone_number_id: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        """``payload`` já validado; aqui só se acrescentam os campos obrigatórios."""
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        return self.client.post(f"/{phone_number_id}/messages", token, json=body)

    def send_text_message(self, phone_number_id: str, token: str, to: str, body: str) -> dict[str, Any]:
        return self.send_message(phone_number_id, token, {
            "to": to, "type": "text", "text": {"preview_url": False, "body": body},
        })

    def mark_as_read(self, phone_number_id: str, token: str, message_id: str) -> dict[str, Any]:
        return self.client.post(f"/{phone_number_id}/messages", token, json={
            "messaging_product": "whatsapp", "status": "read", "message_id": message_id,
        })

    # -- modelos ---------------------------------------------------------------
    def list_templates(self, waba_id: str, token: str, limit: int = 100) -> list[dict[str, Any]]:
        data = self.client.get(f"/{waba_id}/message_templates", token,
                               params={"fields": TEMPLATE_FIELDS, "limit": limit})
        return data.get("data", []) if isinstance(data, dict) else []

    def create_template(self, waba_id: str, token: str, template: dict[str, Any]) -> dict[str, Any]:
        return self.client.post(f"/{waba_id}/message_templates", token, json=template)

    # -- mídia -----------------------------------------------------------------
    def upload_media(self, phone_number_id: str, token: str, filename: str,
                     content: bytes, mime_type: str) -> dict[str, Any]:
        return self.client.post_multipart(
            f"/{phone_number_id}/media", token,
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, content, mime_type)},
        )

    def get_media(self, media_id: str, token: str, phone_number_id: str | None = None) -> dict[str, Any]:
        params = {"phone_number_id": phone_number_id} if phone_number_id else None
        return self.client.get(f"/{media_id}", token, params=params)

    def download_media(self, media_id: str, token: str, phone_number_id: str | None = None):
        info = self.get_media(media_id, token, phone_number_id)
        stream, content_type, size = self.client.download(info.get("url", ""), token)
        return stream, info.get("mime_type") or content_type, size
