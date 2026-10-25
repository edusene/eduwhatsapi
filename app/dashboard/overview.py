"""Visão geral, logs e configurações da organização."""
from __future__ import annotations

import logging

from flask import flash, redirect, render_template, request, url_for

from app.dashboard import bp, page_arg, paginate
from app.extensions import limiter
from app.services import api_keys, audit, connections, organizations, outbound_webhooks, usage
from app.services.organizations import current_org_id, current_user_id, org_required, require_role
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)


@bp.get("/")
@org_required
def overview():
    org_id = current_org_id()
    conns = connections.list_connections(org_id, include_disconnected=False)
    keys = [k for k in api_keys.list_api_keys(org_id) if not k["revoked"]]
    endpoints = outbound_webhooks.list_endpoints(org_id)
    stats = usage.summary(org_id, days=30)
    daily = usage.daily_messages(org_id, days=14)
    sent_any = stats["messages_sent"] > 0
    checklist = [
        {"done": bool(conns), "title": "Conectar um número do WhatsApp",
         "text": "Use o cadastro oficial da Meta ou informe um token de System User.",
         "url": url_for("dashboard.channels"), "cta": "Conectar número"},
        {"done": bool(keys), "title": "Criar uma chave de API",
         "text": "Use a chave no n8n, Make ou no seu sistema para enviar mensagens.",
         "url": url_for("dashboard.api_keys_list"), "cta": "Criar chave"},
        {"done": bool(endpoints), "title": "Configurar um webhook",
         "text": "Receba mensagens e status de entrega no seu sistema em tempo real.",
         "url": url_for("dashboard.webhooks"), "cta": "Adicionar webhook"},
        {"done": sent_any, "title": "Enviar a primeira mensagem",
         "text": "Teste pelo painel do número ou chamando a API.",
         "url": url_for("dashboard.channels"), "cta": "Enviar teste"},
    ]
    recent = (
        get_service_client().table("webhook_events").select("id, event_type, status, received_at")
        .eq("organization_id", org_id).order("received_at", desc=True).limit(8).execute().data
    )
    return render_template(
        "dashboard/overview.html",
        connections=conns,
        stats=stats,
        daily=daily,
        daily_max=max([d["inbound"] + d["outbound"] for d in daily] + [1]),
        checklist=checklist,
        checklist_done=sum(1 for c in checklist if c["done"]),
        recent_events=recent,
        active_keys=len(keys),
    )


@bp.get("/logs")
@org_required
def logs():
    org_id = current_org_id()
    tab = request.args.get("tab", "api")
    status_filter = request.args.get("status", "")
    client = get_service_client()
    page = page_arg()
    api_logs, events, audits = [], [], []
    if tab == "events":
        events, has_next = paginate(
            client.table("webhook_events").select("id, received_at, event_type, status, payload")
            .eq("organization_id", org_id).order("received_at", desc=True), page)
    elif tab == "audit":
        audits, has_next = paginate(
            client.table("audit_logs").select("id, created_at, action, target_type, actor_user_id, metadata")
            .eq("organization_id", org_id).order("created_at", desc=True), page)
    else:
        tab = "api"
        query = (
            client.table("api_request_logs")
            .select("id, created_at, method, endpoint, status_code, latency_ms, error_code, request_id, api_key_id")
            .eq("organization_id", org_id)
        )
        if status_filter == "errors":
            query = query.gte("status_code", 400)
        api_logs, has_next = paginate(query.order("created_at", desc=True), page)
    member_emails = {m["user_id"]: m.get("email") for m in organizations.list_members(org_id)}
    return render_template("dashboard/logs.html", tab=tab, status_filter=status_filter, api_logs=api_logs,
                           events=events, audits=audits, member_emails=member_emails,
                           page=page, has_next=has_next)


@bp.get("/settings")
@org_required
def settings():
    org_id = current_org_id()
    return render_template(
        "dashboard/settings.html",
        org=organizations.get_organization(org_id) or {},
        members=organizations.list_members(org_id),
        invitations=organizations.list_invitations(org_id) if organizations.has_role("admin") else [],
        plans=organizations.PLANS,
        role_labels=organizations.ROLE_LABELS,
    )


@bp.post("/settings/organization")
@org_required
@require_role("owner")
def settings_update():
    try:
        retention = request.form.get("message_retention_days")
        organizations.update_organization(
            current_org_id(), current_user_id(),
            name=request.form.get("name"),
            plan=request.form.get("plan") or None,
            retention_days=int(retention) if retention else None,
        )
    except ValueError as exc:
        flash(str(exc) if "invalid literal" not in str(exc) else "Retenção inválida.", "error")
    else:
        flash("Configurações salvas.", "success")
    return redirect(url_for("dashboard.settings"))


@bp.post("/settings/invitations")
@org_required
@require_role("admin")
@limiter.limit("20 per hour")
def invitation_create():
    try:
        inv = organizations.create_invitation(current_org_id(), request.form.get("email", ""),
                                              request.form.get("role", "viewer"), current_user_id())
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard.settings"))
    link = url_for("auth.invite", token=inv["token"], _external=False)
    return render_template("dashboard/invite_created.html", inv=inv, link=link)


@bp.post("/settings/invitations/<invitation_id>/revoke")
@org_required
@require_role("admin")
def invitation_revoke(invitation_id: str):
    ok = organizations.revoke_invitation(current_org_id(), invitation_id, current_user_id())
    flash("Convite cancelado." if ok else "Convite não encontrado.", "success" if ok else "error")
    return redirect(url_for("dashboard.settings"))


def _actor() -> dict:
    from flask import g
    return {"user_id": g.user["id"], "role": g.membership["role"]}


@bp.post("/settings/members/<member_id>/role")
@org_required
@require_role("admin")
def member_role(member_id: str):
    try:
        organizations.update_member_role(current_org_id(), member_id, request.form.get("role", ""), _actor())
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("Papel atualizado.", "success")
    return redirect(url_for("dashboard.settings"))


@bp.post("/settings/members/<member_id>/remove")
@org_required
@require_role("admin")
def member_remove(member_id: str):
    try:
        organizations.remove_member(current_org_id(), member_id, _actor())
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("Membro removido.", "success")
    return redirect(url_for("dashboard.settings"))


@bp.post("/settings/purge-messages")
@org_required
@require_role("owner")
def purge_messages():
    """Apaga todo o conteúdo de mensagens e conversas da organização (LGPD)."""
    if request.form.get("confirm") != "APAGAR":
        flash("Digite APAGAR para confirmar.", "error")
        return redirect(url_for("dashboard.settings"))
    org_id = current_org_id()
    client = get_service_client()
    client.table("messages").delete().eq("organization_id", org_id).execute()
    client.table("conversations").delete().eq("organization_id", org_id).execute()
    client.table("contacts").delete().eq("organization_id", org_id).execute()
    audit.log_action(org_id, current_user_id(), "data.messages_purged", "organization", org_id, {})
    flash("Mensagens, conversas e contatos apagados.", "success")
    return redirect(url_for("dashboard.settings"))
