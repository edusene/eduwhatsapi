"""Área do desenvolvedor: chaves de API e webhooks de saída."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import flash, redirect, render_template, request, url_for

from app.dashboard import PAGE_SIZE, bp, page_arg
from app.extensions import limiter
from app.services import api_keys, connections, outbound_webhooks
from app.services.encryption import EncryptionNotConfigured
from app.services.organizations import current_org_id, current_user_id, org_required, require_role

_EXPIRY_OPTIONS = {"": None, "30": 30, "90": 90, "365": 365}


@bp.get("/api-keys")
@org_required
def api_keys_list():
    return render_template("dashboard/api_keys.html", keys=api_keys.list_api_keys(current_org_id()),
                           scopes=api_keys.SCOPES, default_scopes=api_keys.DEFAULT_SCOPES,
                           channels=_channels())


def _channels() -> list[dict]:
    return connections.list_connections(current_org_id(), include_disconnected=False)


@bp.post("/api-keys")
@org_required
@require_role("admin")
@limiter.limit("20 per hour")
def api_keys_create():
    days = _EXPIRY_OPTIONS.get(request.form.get("expires_in", ""))
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat() if days else None
    try:
        record = api_keys.create_api_key(
            current_org_id(), request.form.get("name", ""),
            environment=request.form.get("environment", "test"),
            scopes=request.form.getlist("scopes") or None,
            created_by=current_user_id(),
            expires_at=expires_at,
            phone_number_ids=request.form.getlist("phone_number_ids"),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard.api_keys_list"))
    return render_template("dashboard/secret_created.html", kind="api_key", item=record, secret=record["key"])


@bp.post("/api-keys/<key_id>/revoke")
@org_required
@require_role("admin")
def api_keys_revoke(key_id: str):
    ok = api_keys.revoke_api_key(current_org_id(), key_id, revoked_by=current_user_id())
    flash("Chave revogada. Ela para de funcionar imediatamente." if ok else "Chave não encontrada.",
          "success" if ok else "error")
    return redirect(url_for("dashboard.api_keys_list"))


@bp.post("/api-keys/<key_id>/rotate")
@org_required
@require_role("admin")
@limiter.limit("20 per hour")
def api_keys_rotate(key_id: str):
    record = api_keys.rotate_api_key(current_org_id(), key_id, rotated_by=current_user_id())
    if not record:
        flash("Chave não encontrada ou já revogada.", "error")
        return redirect(url_for("dashboard.api_keys_list"))
    return render_template("dashboard/secret_created.html", kind="api_key", item=record, secret=record["key"],
                           rotated=True)


@bp.get("/webhooks")
@org_required
def webhooks():
    org_id = current_org_id()
    endpoints = outbound_webhooks.list_endpoints(org_id)
    by_id = {e["id"]: e for e in endpoints}
    page = page_arg()
    deliveries = outbound_webhooks.list_deliveries(org_id, limit=PAGE_SIZE + 1, offset=(page - 1) * PAGE_SIZE)
    return render_template("dashboard/webhooks.html", endpoints=endpoints, endpoints_by_id=by_id,
                           deliveries=deliveries[:PAGE_SIZE], page=page, has_next=len(deliveries) > PAGE_SIZE,
                           events=outbound_webhooks.EVENTS, channels=_channels())


@bp.post("/webhooks")
@org_required
@require_role("admin")
@limiter.limit("20 per hour")
def webhooks_create():
    try:
        record = outbound_webhooks.create_endpoint(
            current_org_id(), url=request.form.get("url", ""), events=request.form.getlist("events"),
            description=request.form.get("description"), created_by=current_user_id(),
            phone_number_ids=request.form.getlist("phone_number_ids"))
    except outbound_webhooks.WebhookURLBlocked as exc:
        flash(f"URL recusada: {exc}", "error")
        return redirect(url_for("dashboard.webhooks"))
    except EncryptionNotConfigured:
        flash("TOKEN_ENCRYPTION_KEY não configurada no servidor.", "error")
        return redirect(url_for("dashboard.webhooks"))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("dashboard.webhooks"))
    return render_template("dashboard/secret_created.html", kind="webhook", item=record, secret=record["secret"])


@bp.post("/webhooks/<endpoint_id>/update")
@org_required
@require_role("admin")
def webhooks_update(endpoint_id: str):
    try:
        ok = outbound_webhooks.update_endpoint(
            current_org_id(), endpoint_id,
            events=request.form.getlist("events") if "events_present" in request.form else None,
            status=request.form.get("status") or None, user_id=current_user_id())
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("Webhook atualizado." if ok else "Webhook não encontrado.", "success" if ok else "error")
    return redirect(url_for("dashboard.webhooks"))


@bp.post("/webhooks/<endpoint_id>/delete")
@org_required
@require_role("admin")
def webhooks_delete(endpoint_id: str):
    ok = outbound_webhooks.delete_endpoint(current_org_id(), endpoint_id, user_id=current_user_id())
    flash("Webhook removido." if ok else "Webhook não encontrado.", "success" if ok else "error")
    return redirect(url_for("dashboard.webhooks"))


@bp.post("/webhooks/<endpoint_id>/rotate-secret")
@org_required
@require_role("admin")
def webhooks_rotate_secret(endpoint_id: str):
    record = outbound_webhooks.rotate_secret(current_org_id(), endpoint_id, user_id=current_user_id())
    if not record:
        flash("Webhook não encontrado.", "error")
        return redirect(url_for("dashboard.webhooks"))
    return render_template("dashboard/secret_created.html", kind="webhook", item=record, secret=record["secret"],
                           rotated=True)


@bp.post("/webhooks/<endpoint_id>/test")
@org_required
@require_role("developer")
@limiter.limit("10 per minute")
def webhooks_test(endpoint_id: str):
    ok = outbound_webhooks.send_test_event(current_org_id(), endpoint_id)
    flash("Evento de teste enfileirado. Veja o resultado no histórico abaixo em alguns segundos."
          if ok else "Webhook não encontrado ou desativado.", "success" if ok else "error")
    return redirect(url_for("dashboard.webhooks"))


@bp.post("/webhooks/deliveries/<delivery_id>/redeliver")
@org_required
@require_role("developer")
@limiter.limit("20 per minute")
def webhooks_redeliver(delivery_id: str):
    ok = outbound_webhooks.redeliver(current_org_id(), delivery_id)
    flash("Reenvio solicitado." if ok else "Entrega não encontrada.", "success" if ok else "error")
    return redirect(url_for("dashboard.webhooks"))
