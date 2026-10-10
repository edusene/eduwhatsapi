"""Rotas do painel administrativo (protegidas por sessão + org)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from app.extensions import limiter
from app.services import api_keys, audit, organizations
from app.services.supabase_client import get_service_client

bp = Blueprint("dashboard", __name__)


def _org_id() -> str:
    m = g.get("membership")
    if not m:
        flash(
            "Organização não encontrada. Execute o schema SQL no Supabase e faça login novamente.",
            "error",
        )
        return redirect(url_for("auth.logout"))
    return m["organization_id"]


@bp.get("/")
@organizations.login_required
@organizations.org_required
def overview():
    org_id = _org_id()
    client = get_service_client()
    try:
        conns = (
            client.table("meta_connections")
            .select("id, status")
            .eq("organization_id", org_id)
            .execute()
        )
        active_conns = sum(1 for c in conns.data if c.get("status") == "active")
        keys = (
            client.table("api_keys")
            .select("id, revoked_at")
            .eq("organization_id", org_id)
            .execute()
        )
        active_keys = sum(1 for k in keys.data if not k.get("revoked_at"))
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        logs = (
            client.table("api_request_logs")
            .select("id, created_at")
            .eq("organization_id", org_id)
            .execute()
        )
        logs_24h = sum(1 for r in logs.data if r.get("created_at", "") >= since)
        events = (
            client.table("webhook_events")
            .select("id, event_type, received_at, status")
            .eq("organization_id", org_id)
            .order("received_at", desc=True)
            .limit(10)
            .execute()
        )
        recent_events = events.data
    except Exception as exc:
        flash(
            "Erro ao carregar dados. Verifique se o schema SQL foi executado no Supabase.",
            "error",
        )
        active_conns = 0
        active_keys = 0
        logs_24h = 0
        recent_events = []
    return render_template(
        "dashboard/overview.html",
        active_conns=active_conns,
        active_keys=active_keys,
        logs_24h=logs_24h,
        recent_events=recent_events,
    )


@bp.get("/channels")
@organizations.login_required
@organizations.org_required
def channels():
    org_id = _org_id()
    rows = (
        get_service_client()
        .table("meta_connections")
        .select("id, channel, phone_number_id, display_phone_number, "
                "verified_name, status, connected_at, waba_id")
        .eq("organization_id", org_id)
        .order("created_at", desc=True)
        .execute()
    )
    return render_template("dashboard/channels.html", channels=rows.data)


@bp.get("/api-keys")
@organizations.login_required
@organizations.org_required
def api_keys_list():
    org_id = _org_id()
    rows = api_keys.list_api_keys(org_id)
    return render_template("dashboard/api_keys.html", keys=rows)


@bp.post("/api-keys/create")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner", "admin")
@limiter.limit("20 per minute")
def api_keys_create():
    org_id = _org_id()
    name = request.form.get("name", "").strip()
    env = request.form.get("environment", "test")
    try:
        record = api_keys.create_api_key(
            org_id, name, environment=env, created_by=g.user["id"] if g.user else None
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard.api_keys_list"))
    return render_template("dashboard/api_key_created.html", key=record)


@bp.post("/api-keys/<key_id>/revoke")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner", "admin")
def api_keys_revoke(key_id: str):
    org_id = _org_id()
    if api_keys.revoke_api_key(org_id, key_id, revoked_by=g.user["id"] if g.user else None):
        flash("Chave revogada.", "success")
    else:
        flash("Chave não encontrada.", "error")
    return redirect(url_for("dashboard.api_keys_list"))


@bp.post("/api-keys/<key_id>/rotate")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner", "admin")
def api_keys_rotate(key_id: str):
    org_id = _org_id()
    record = api_keys.rotate_api_key(org_id, key_id, rotated_by=g.user["id"] if g.user else None)
    if not record:
        flash("Chave não encontrada.", "error")
        return redirect(url_for("dashboard.api_keys_list"))
    return render_template("dashboard/api_key_created.html", key=record)


@bp.get("/webhooks")
@organizations.login_required
@organizations.org_required
def webhooks():
    org_id = _org_id()
    from app.services import outbound_webhooks
    endpoints = outbound_webhooks.list_endpoints(org_id)
    deliveries = (
        get_service_client()
        .table("webhook_deliveries")
        .select("id, event_type, status, attempts, response_status, created_at")
        .eq("organization_id", org_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return render_template(
        "dashboard/webhooks.html",
        endpoints=endpoints,
        deliveries=deliveries.data,
    )


@bp.post("/webhooks/create")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner", "admin")
@limiter.limit("20 per minute")
def webhooks_create():
    org_id = _org_id()
    from app.services import outbound_webhooks
    url = request.form.get("url", "").strip()
    description = request.form.get("description", "").strip()
    events = request.form.getlist("events")
    try:
        record = outbound_webhooks.create_endpoint(
            org_id,
            url=url,
            events=events,
            description=description or None,
            created_by=g.user["id"] if g.user else None,
        )
    except outbound_webhooks.WebhookURLBlocked as exc:
        flash(f"URL rejeitada: {exc}", "error")
        return redirect(url_for("dashboard.webhooks"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard.webhooks"))
    return render_template("dashboard/webhook_created.html", endpoint=record)


@bp.post("/webhooks/<endpoint_id>/delete")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner", "admin")
def webhooks_delete(endpoint_id: str):
    org_id = _org_id()
    from app.services import outbound_webhooks
    if outbound_webhooks.delete_endpoint(
        org_id, endpoint_id, user_id=g.user["id"] if g.user else None
    ):
        flash("Webhook removido.", "success")
    else:
        flash("Webhook não encontrado.", "error")
    return redirect(url_for("dashboard.webhooks"))


@bp.post("/webhooks/<endpoint_id>/rotate-secret")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner", "admin")
def webhooks_rotate_secret(endpoint_id: str):
    org_id = _org_id()
    from app.services import outbound_webhooks
    record = outbound_webhooks.rotate_secret(
        org_id, endpoint_id, user_id=g.user["id"] if g.user else None
    )
    if not record:
        flash("Webhook não encontrado.", "error")
        return redirect(url_for("dashboard.webhooks"))
    return render_template("dashboard/webhook_created.html", endpoint=record)


@bp.post("/webhooks/<endpoint_id>/test")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner", "admin")
def webhooks_test(endpoint_id: str):
    org_id = _org_id()
    from app.services import outbound_webhooks
    ok = outbound_webhooks.send_test_event(
        org_id, endpoint_id, user_id=g.user["id"] if g.user else None
    )
    flash("Evento de teste enviado." if ok else "Falha ao enviar evento de teste.",
          "success" if ok else "error")
    return redirect(url_for("dashboard.webhooks"))


@bp.get("/logs")
@organizations.login_required
@organizations.org_required
def logs():
    org_id = _org_id()
    api_logs = (
        get_service_client()
        .table("api_request_logs")
        .select("id, created_at, method, endpoint, status_code, latency_ms, error_code, request_id")
        .eq("organization_id", org_id)
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    wh_events = (
        get_service_client()
        .table("webhook_events")
        .select("id, received_at, event_type, status")
        .eq("organization_id", org_id)
        .order("received_at", desc=True)
        .limit(50)
        .execute()
    )
    return render_template(
        "dashboard/logs.html",
        api_logs=api_logs.data,
        webhook_events=wh_events.data,
    )


@bp.get("/settings")
@organizations.login_required
@organizations.org_required
def settings():
    org_id = _org_id()
    members = (
        get_service_client()
        .table("organization_members")
        .select("user_id, role")
        .eq("organization_id", org_id)
        .execute()
    )
    org = (
        get_service_client()
        .table("organizations")
        .select("id, name, slug")
        .eq("id", org_id)
        .limit(1)
        .execute()
    )
    org_data = org.data[0] if org.data else {"name": "", "slug": ""}
    return render_template(
        "dashboard/settings.html",
        org=org_data,
        members=members.data,
        current_role=g.membership["role"],
    )


@bp.post("/settings/rename")
@organizations.login_required
@organizations.org_required
@organizations.require_role("owner")
def settings_rename():
    org_id = _org_id()
    name = request.form.get("name", "").strip()
    if not name or len(name) > 120:
        flash("Nome inválido.", "error")
        return redirect(url_for("dashboard.settings"))
    get_service_client().table("organizations").update({"name": name}).eq(
        "id", org_id
    ).execute()
    audit.log_action(
        org_id, g.user["id"] if g.user else None, "organization.renamed",
        "organization", org_id, {"new_name": name}
    )
    flash("Nome da organização atualizado.", "success")
    return redirect(url_for("dashboard.settings"))
