"""Registry de adaptadores de canal.

Desacopla os consumidores (rotas, webhooks) das classes concretas: Instagram e
Messenger serão registrados aqui futuramente sem alterar quem consome
``get_adapter``.
"""
from __future__ import annotations

from app.meta.whatsapp import WhatsAppAdapter

CHANNEL_ADAPTERS = {
    "whatsapp": WhatsAppAdapter,
    # "instagram": InstagramAdapter,   # futuro
    # "messenger": MessengerAdapter,   # futuro
}


def get_adapter(channel: str):
    """Retorna a classe adaptadora do canal ou levanta ValueError."""
    try:
        return CHANNEL_ADAPTERS[channel]
    except KeyError:
        raise ValueError(f"Canal não suportado: {channel}") from None
