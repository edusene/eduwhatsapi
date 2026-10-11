"""Endpoints para agendadores externos (/internal/cron/*).

Protegidos por ``Authorization: Bearer <CRON_SECRET>``. Se CRON_SECRET não
estiver definido, respondem 404 (desativados).
"""
from __future__ import annotations

from flask import Blueprint, abort, current_app, jsonify, request

from app.extensions import limiter
from app.services.security import constant_time_equals

bp = Blueprint("internal", __name__)


def _authorize() -> None:
    secret = current_app.config.get("CRON_SECRET", "")
    if not secret:
        abort(404)
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or not constant_time_equals(header[7:], secret):
        abort(403)


@bp.post("/cron/deliver-webhooks")
@limiter.limit("30 per minute")
def cron_deliver():
    _authorize()
    from app.services.outbound_webhooks import process_pending_retries

    return jsonify({"processed": process_pending_retries(limit=100)})


@bp.post("/cron/purge")
@limiter.limit("5 per minute")
def cron_purge():
    _authorize()
    from app.services.maintenance import purge_expired

    return jsonify({"removed": purge_expired()})
