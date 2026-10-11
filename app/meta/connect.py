"""Rotas de conexão de números (prefixo /connect/meta). Todas exigem sessão,
organização e papel admin+ (exceto onde indicado) e proteção CSRF.

- POST /embedded-signup      recebe {code, waba_id, phone_number_id, ...} do JS
                             do Embedded Signup (SDK oficial) e conclui no servidor
- POST /manual               conexão com token de System User
- POST /<id>/refresh         revalida na Meta
- POST /<id>/subscribe       inscreve o app nos webhooks da WABA
- POST /<id>/register        registra o número (PIN de 6 dígitos)
- POST /<id>/disconnect      remove token e desconecta
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, jsonify, redirect, request, url_for

from app.extensions import limiter
from app.services import connections
from app.services.organizations import current_org_id, current_user_id, org_required, require_role

logger = logging.getLogger(__name__)

bp = Blueprint("meta_connect", __name__)


def _back(connection_id: str | None = None):
    if connection_id:
        return redirect(url_for("dashboard.channel_detail", connection_id=connection_id))
    return redirect(url_for("dashboard.channels"))


@bp.post("/embedded-signup")
@org_required
@require_role("admin")
@limiter.limit("10 per minute")
def embedded_signup():
    cfg = current_app.config
    if not (cfg["META_APP_ID"] and cfg["META_APP_SECRET"] and cfg["META_ES_CONFIG_ID"]):
        return jsonify({"ok": False, "message": "Embedded Signup não configurado no servidor."}), 503
    data = request.get_json(silent=True) or {}
    try:
        record = connections.connect_embedded_signup(
            current_org_id(), current_user_id(),
            code=str(data.get("code") or ""),
            waba_id=str(data.get("waba_id") or ""),
            phone_number_id=str(data.get("phone_number_id") or ""),
            business_id=str(data.get("business_id") or "") or None,
            coexistence=bool(data.get("coexistence")),
            pin=str(data.get("pin") or "") or None,
        )
    except connections.ConnectionError_ as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    flash("Número conectado com sucesso pelo cadastro oficial da Meta.", "success")
    return jsonify({"ok": True, "redirect": url_for("dashboard.channel_detail", connection_id=record["id"])})


@bp.post("/manual")
@org_required
@require_role("admin")
@limiter.limit("10 per minute")
def manual():
    try:
        record = connections.connect_manual(
            current_org_id(), current_user_id(),
            token=request.form.get("access_token", ""),
            waba_id=request.form.get("waba_id", ""),
            phone_number_id=request.form.get("phone_number_id", ""),
            app_secret=request.form.get("app_secret", ""),
            subscribe=request.form.get("subscribe") == "on",
        )
    except connections.ConnectionError_ as exc:
        flash(str(exc), "error")
        return _back()
    flash("Número conectado e validado na Meta.", "success")
    return _back(record["id"])


def _action(connection_id: str, fn, success: str):
    try:
        fn()
    except connections.ConnectionError_ as exc:
        flash(str(exc), "error")
    else:
        flash(success, "success")
    return _back(connection_id)


@bp.post("/<connection_id>/refresh")
@org_required
@require_role("developer")
def refresh(connection_id: str):
    return _action(connection_id, lambda: connections.refresh(current_org_id(), connection_id),
                   "Dados do número atualizados com a Meta.")


@bp.post("/<connection_id>/subscribe")
@org_required
@require_role("admin")
def subscribe(connection_id: str):
    return _action(connection_id,
                   lambda: connections.subscribe_webhooks(current_org_id(), connection_id, current_user_id()),
                   "App inscrito nos webhooks desta conta do WhatsApp.")


@bp.post("/<connection_id>/register")
@org_required
@require_role("admin")
@limiter.limit("5 per minute")
def register(connection_id: str):
    pin = request.form.get("pin", "").strip()
    return _action(connection_id,
                   lambda: connections.register_number(current_org_id(), connection_id, pin, current_user_id()),
                   "Número registrado na Cloud API.")


@bp.post("/<connection_id>/disconnect")
@org_required
@require_role("admin")
def disconnect(connection_id: str):
    try:
        connections.disconnect(current_org_id(), connection_id, current_user_id())
    except connections.ConnectionError_ as exc:
        flash(str(exc), "error")
    else:
        flash("Número desconectado. O token foi apagado.", "success")
    return _back()
