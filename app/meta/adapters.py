"""Registry de adaptadores de canal.

Desacopla os consumidores (serviço de mensagens, webhooks, rotas) das classes
concretas. Instagram Messaging e Messenger Platform podem ser adicionados aqui
implementando ``ChannelAdapter`` — somente quando o app tiver as permissões
aprovadas pela Meta para esses produtos.
"""
from __future__ import annotations

from typing import Any, Protocol

from app.meta.graph_api import MetaGraphClient, get_client
from app.meta.whatsapp import WhatsAppAdapter


class ChannelAdapter(Protocol):
    channel: str

    def send_message(self, sender_id: str, token: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def mark_as_read(self, sender_id: str, token: str, message_id: str) -> dict[str, Any]: ...


CHANNEL_ADAPTERS: dict[str, type] = {
    "whatsapp": WhatsAppAdapter,
    # "instagram": InstagramAdapter,   # futuro (exige instagram_manage_messages aprovado)
    # "messenger": MessengerAdapter,   # futuro (exige pages_messaging aprovado)
}


def get_adapter(channel: str, client: MetaGraphClient | None = None):
    """Instância do adaptador do canal ou ValueError se não suportado."""
    try:
        cls = CHANNEL_ADAPTERS[channel]
    except KeyError:
        raise ValueError(f"Canal não suportado: {channel}") from None
    return cls(client or get_client())
