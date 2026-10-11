"""Canais (números do WhatsApp): lista, detalhe, modelos e envio de teste."""
from __future__ import annotations

import json
import logging
import re

from flask import current_app, flash, g, redirect, render_template, request, url_for

from app.dashboard import bp
from app.extensions import limiter
from app.meta.adapters import get_adapter
from app.meta.graph_api import MetaAPIError
from app.services import connections, messaging
from app.services.message_validation import ValidationError, validate_send_payload
from app.services.organizations import current_org_id, current_user_id, org_required, require_role

logger = logging.getLogger(__name__)

_TEMPLATE_NAME_RE = re.compile(r"[a-z0-9_]{1,512}")
_LANGUAGES = [("pt_BR", "Português (Brasil)"), ("en_US", "Inglês (EUA)"), ("es", "Espanhol")]


def _connection_or_404(connection_id: str) -> dict:
    connection = connections.get_connection(current_org_id(), connection_id)
    if not connection:
        from flask import abort
        abort(404)
    return connection


@bp.get("/channels")
@org_required
def channels():
    g.allow_facebook_sdk = True  # libera o SDK oficial da Meta na CSP desta página
    cfg = current_app.config
    return render_template(
        "dashboard/channels.html",
        channels=connections.list_connections(current_org_id()),
        es_enabled=bool(cfg["META_APP_ID"] and cfg["META_APP_SECRET"] and cfg["META_ES_CONFIG_ID"]),
        meta_app_id=cfg["META_APP_ID"],
        es_config_id=cfg["META_ES_CONFIG_ID"],
        graph_version=cfg["META_GRAPH_API_VERSION"],
        webhook_configured=bool(cfg["META_WEBHOOK_VERIFY_TOKEN"] and cfg["META_WEBHOOK_APP_SECRET"]),
    )


@bp.get("/channels/<connection_id>")
@org_required
def channel_detail(connection_id: str):
    connection = _connection_or_404(connection_id)
    templates, templates_error = [], None
    if connection.get("status") != "disconnected":
        try:
            templates = get_adapter("whatsapp").list_templates(
                connection["waba_id"], connections.token_for(connection))
        except (MetaAPIError, connections.ConnectionError_) as exc:
            templates_error = getattr(exc, "message", None) or str(exc)
    own_app = bool(connection.get("app_secret_encrypted"))
    return render_template(
        "dashboard/channel_detail.html",
        c=connections.public_view(connection),
        templates=templates,
        templates_error=templates_error,
        own_app=own_app,
        own_webhook_url=f"{current_app.config['APP_BASE_URL']}/webhooks/meta/{connection['id']}",
        own_verify_token=connections.webhook_verify_token(connection) if own_app else None,
        languages=_LANGUAGES,
        recent=messaging.list_messages(current_org_id(), phone_number_id=connection.get("phone_number_id"),
                                       limit=10) if connection.get("phone_number_id") else [],
    )


@bp.post("/channels/<connection_id>/test-message")
@org_required
@require_role("developer")
@limiter.limit("20 per minute")
def channel_test_message(connection_id: str):
    connection = _connection_or_404(connection_id)
    if connection.get("status") != "active":
        flash("O número precisa estar ativo para enviar mensagens.", "error")
        return redirect(url_for("dashboard.channel_detail", connection_id=connection_id))
    kind = request.form.get("kind", "text")
    try:
        if kind == "template":
            params = [p.strip() for p in request.form.get("params", "").split("|") if p.strip()]
            data = {"to": request.form.get("to", ""), "type": "template", "template": {
                "name": request.form.get("template_name", ""),
                "language": {"code": request.form.get("template_language", "pt_BR")},
            }}
            if params:
                data["template"]["components"] = [
                    {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
        else:
            data = {"to": request.form.get("to", ""), "type": "text", "text": {"body": request.form.get("body", "")}}
        payload = validate_send_payload(data)
        result = messaging.send(current_org_id(), connection, payload, source="dashboard", sent_by=current_user_id())
    except ValidationError as exc:
        flash(str(exc), "error")
    except connections.ConnectionError_ as exc:
        flash(str(exc), "error")
    except MetaAPIError as exc:
        # 131047: mensagem livre fora da janela de atendimento de 24h.
        hint = " Fora da janela de 24h, use um modelo aprovado." if exc.fb_code == 131047 else ""
        flash(f"A Meta recusou o envio: {exc.message}{hint}", "error")
    else:
        flash(f"Mensagem aceita pela Meta (id {result.get('wamid')}). Acompanhe o status abaixo.", "success")
    return redirect(url_for("dashboard.channel_detail", connection_id=connection_id))


@bp.post("/channels/<connection_id>/templates")
@org_required
@require_role("admin")
@limiter.limit("10 per minute")
def channel_create_template(connection_id: str):
    connection = _connection_or_404(connection_id)
    name = request.form.get("name", "").strip().lower()
    language = request.form.get("language", "pt_BR")
    category = request.form.get("category", "UTILITY")
    body = request.form.get("body", "").strip()
    header = request.form.get("header", "").strip()
    footer = request.form.get("footer", "").strip()
    examples = [e.strip() for e in request.form.get("examples", "").split("|") if e.strip()]

    error = None
    variables = re.findall(r"\{\{(\d+)\}\}", body)
    if not _TEMPLATE_NAME_RE.fullmatch(name):
        error = "Nome: use apenas letras minúsculas, números e _ (ex.: confirmacao_pedido)."
    elif language not in dict(_LANGUAGES):
        error = "Idioma inválido."
    elif category not in ("UTILITY", "MARKETING"):
        error = "Categoria inválida."
    elif not body or len(body) > 1024:
        error = "O corpo é obrigatório (até 1024 caracteres)."
    elif len(header) > 60 or len(footer) > 60:
        error = "Cabeçalho e rodapé têm até 60 caracteres."
    elif variables and len(examples) != len(set(variables)):
        error = f"Informe {len(set(variables))} exemplo(s) separados por | para as variáveis do corpo."
    if error:
        flash(error, "error")
        return redirect(url_for("dashboard.channel_detail", connection_id=connection_id) + "#modelos")

    body_component: dict = {"type": "BODY", "text": body}
    if variables:
        body_component["example"] = {"body_text": [examples]}
    components = [body_component]
    if header:
        components.insert(0, {"type": "HEADER", "format": "TEXT", "text": header})
    if footer:
        components.append({"type": "FOOTER", "text": footer})
    try:
        result = get_adapter("whatsapp").create_template(
            connection["waba_id"], connections.token_for(connection),
            {"name": name, "language": language, "category": category, "components": components})
    except (MetaAPIError, connections.ConnectionError_) as exc:
        message = getattr(exc, "message", None) or str(exc)
        details = getattr(exc, "details", None)
        flash(f"A Meta recusou o modelo: {message}{' — ' + details if details else ''}", "error")
    else:
        flash(f"Modelo enviado para análise da Meta (status: {result.get('status', 'PENDING')}).", "success")
    return redirect(url_for("dashboard.channel_detail", connection_id=connection_id) + "#modelos")


@bp.app_template_filter("tojson_pretty")
def _tojson_pretty(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
