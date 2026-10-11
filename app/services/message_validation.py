"""Validação do corpo de envio de mensagens (API própria e painel).

O formato aceito espelha o objeto de mensagem da WhatsApp Cloud API (sem
``messaging_product``/``recipient_type``, que o adaptador acrescenta). Assim o
cliente pode seguir a documentação oficial da Meta para cada tipo, e nós
validamos o essencial antes de gastar uma chamada à Meta.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

RECIPIENT_RE = re.compile(r"\d{8,15}")
TEMPLATE_NAME_RE = re.compile(r"[a-z0-9_]{1,512}")
LANG_RE = re.compile(r"[a-z]{2,3}(_[A-Z]{2})?")
MEDIA_TYPES = ("image", "video", "audio", "document", "sticker")
SUPPORTED_TYPES = ("text", "template", *MEDIA_TYPES, "location", "interactive", "reaction", "contacts")
MAX_STRUCT_BYTES = 24 * 1024


class ValidationError(ValueError):
    """Erro de validação de entrada (mapeado para HTTP 400)."""

    def __init__(self, message: str, code: str = "validation_error", field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def normalize_recipient(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("Destinatário inválido.", "invalid_recipient", "to")
    digits = re.sub(r"[\s().-]", "", value.strip())
    if digits.startswith("+"):
        digits = digits[1:]
    if not RECIPIENT_RE.fullmatch(digits):
        raise ValidationError(
            "Destinatário inválido: use o número com código do país e DDD, só dígitos "
            "(8 a 15). Ex.: 5511999998888.", "invalid_recipient", "to")
    return digits


def _str(obj: dict, key: str, field: str, *, required: bool = False, max_len: int = 1024,
         min_len: int = 1) -> str | None:
    value = obj.get(key)
    if value is None:
        if required:
            raise ValidationError(f"O campo {field} é obrigatório.", "missing_field", field)
        return None
    if not isinstance(value, str) or not (min_len <= len(value) <= max_len):
        raise ValidationError(f"O campo {field} deve ter entre {min_len} e {max_len} caracteres.",
                              "invalid_field", field)
    return value


def _https_url(value: str, field: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ValidationError(f"{field} deve ser uma URL HTTPS pública.", "invalid_field", field)
    return value


def _struct(value: Any, field: str, kind: type) -> Any:
    if not isinstance(value, kind):
        raise ValidationError(f"O campo {field} tem formato inválido.", "invalid_field", field)
    if len(json.dumps(value, ensure_ascii=False)) > MAX_STRUCT_BYTES:
        raise ValidationError(f"O campo {field} é grande demais.", "invalid_field", field)
    return value


def _media(obj: Any, mtype: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise ValidationError(f"Informe o objeto '{mtype}' com 'id' ou 'link'.", "invalid_field", mtype)
    out: dict[str, Any] = {}
    media_id = _str(obj, "id", f"{mtype}.id", max_len=64)
    link = _str(obj, "link", f"{mtype}.link", max_len=2048)
    if bool(media_id) == bool(link):
        raise ValidationError(f"Informe exatamente um entre {mtype}.id e {mtype}.link.", "invalid_field", mtype)
    if media_id:
        if not media_id.isdigit():
            raise ValidationError(f"{mtype}.id deve ser o ID numérico da mídia.", "invalid_field", f"{mtype}.id")
        out["id"] = media_id
    else:
        out["link"] = _https_url(link, f"{mtype}.link")
    if mtype in ("image", "video", "document"):
        caption = _str(obj, "caption", f"{mtype}.caption", max_len=1024)
        if caption:
            out["caption"] = caption
    if mtype == "document":
        filename = _str(obj, "filename", "document.filename", max_len=240)
        if filename:
            out["filename"] = filename
    return out


def validate_send_payload(data: Any) -> dict[str, Any]:
    """Valida e devolve o payload no formato oficial (sem messaging_product)."""
    if not isinstance(data, dict):
        raise ValidationError("Corpo JSON inválido.", "invalid_json")
    to = normalize_recipient(data.get("to"))
    mtype = data.get("type", "text")
    if mtype not in SUPPORTED_TYPES:
        raise ValidationError(
            f"Tipo de mensagem não suportado. Use: {', '.join(SUPPORTED_TYPES)}.",
            "unsupported_message_type", "type")

    payload: dict[str, Any] = {"to": to, "type": mtype}
    if mtype == "text":
        text = data.get("text")
        if not isinstance(text, dict):
            raise ValidationError("Informe text.body.", "invalid_message_body", "text.body")
        body = _str(text, "body", "text.body", required=True, max_len=4096)
        if not body.strip():
            raise ValidationError("text.body não pode ser vazio.", "invalid_message_body", "text.body")
        payload["text"] = {"body": body, "preview_url": bool(text.get("preview_url", False))}
    elif mtype in MEDIA_TYPES:
        payload[mtype] = _media(data.get(mtype), mtype)
    elif mtype == "location":
        loc = data.get("location")
        if not isinstance(loc, dict):
            raise ValidationError("Informe o objeto location.", "invalid_field", "location")
        try:
            lat, lng = float(loc.get("latitude")), float(loc.get("longitude"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("latitude/longitude inválidas.", "invalid_field", "location") from exc
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValidationError("latitude/longitude fora do intervalo.", "invalid_field", "location")
        payload["location"] = {"latitude": lat, "longitude": lng}
        for key in ("name", "address"):
            value = _str(loc, key, f"location.{key}", max_len=1000)
            if value:
                payload["location"][key] = value
    elif mtype == "template":
        tpl = data.get("template")
        if not isinstance(tpl, dict):
            raise ValidationError("Informe o objeto template.", "invalid_field", "template")
        name = _str(tpl, "name", "template.name", required=True, max_len=512)
        if not TEMPLATE_NAME_RE.fullmatch(name):
            raise ValidationError("template.name: use letras minúsculas, números e _.", "invalid_field", "template.name")
        language = tpl.get("language")
        code = language.get("code") if isinstance(language, dict) else language
        if not isinstance(code, str) or not LANG_RE.fullmatch(code):
            raise ValidationError("template.language.code inválido (ex.: pt_BR).", "invalid_field", "template.language")
        payload["template"] = {"name": name, "language": {"code": code}}
        if tpl.get("components") is not None:
            payload["template"]["components"] = _struct(tpl["components"], "template.components", list)
    elif mtype == "interactive":
        interactive = _struct(data.get("interactive"), "interactive", dict)
        if not isinstance(interactive.get("type"), str) or not isinstance(interactive.get("action"), dict):
            raise ValidationError("interactive precisa de 'type' e 'action'.", "invalid_field", "interactive")
        payload["interactive"] = interactive
    elif mtype == "reaction":
        reaction = data.get("reaction")
        if not isinstance(reaction, dict):
            raise ValidationError("Informe o objeto reaction.", "invalid_field", "reaction")
        payload["reaction"] = {
            "message_id": _str(reaction, "message_id", "reaction.message_id", required=True, max_len=256),
            "emoji": _str(reaction, "emoji", "reaction.emoji", required=True, max_len=16, min_len=0),
        }
    elif mtype == "contacts":
        payload["contacts"] = _struct(data.get("contacts"), "contacts", list)

    context = data.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise ValidationError("context inválido.", "invalid_field", "context")
        payload["context"] = {"message_id": _str(context, "message_id", "context.message_id",
                                                 required=True, max_len=256)}
    callback = _str(data, "biz_opaque_callback_data", "biz_opaque_callback_data", max_len=512)
    if callback:
        payload["biz_opaque_callback_data"] = callback
    return payload


def preview_text(mtype: str, content: dict[str, Any] | None) -> str:
    """Texto curto para listas de conversa (sem dados além do necessário)."""
    content = content or {}
    labels = {
        "image": "📷 Imagem", "video": "🎬 Vídeo", "audio": "🎤 Áudio", "document": "📄 Documento",
        "sticker": "Figurinha", "location": "📍 Localização", "contacts": "👤 Contato",
        "reaction": "Reação", "interactive": "Mensagem interativa", "button": "Resposta de botão",
        "order": "🛒 Pedido", "unsupported": "Mensagem não suportada",
    }
    if mtype == "text":
        return (content.get("body") or "")[:160]
    if mtype == "template":
        return f"Modelo: {content.get('name', '')}"[:160]
    if mtype in ("image", "video", "document") and content.get("caption"):
        return f"{labels[mtype]} · {content['caption']}"[:160]
    if mtype == "reaction":
        return f"Reação {content.get('emoji', '')}"[:160]
    if mtype in ("interactive", "button") and content.get("title"):
        return str(content["title"])[:160]
    return labels.get(mtype, mtype)[:160]
