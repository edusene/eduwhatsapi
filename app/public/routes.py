"""Páginas públicas: apresentação do produto, documentação e páginas legais."""
from __future__ import annotations

from flask import Blueprint, g, redirect, render_template, url_for

from app.services.api_keys import SCOPES
from app.services.outbound_webhooks import EVENTS

bp = Blueprint("public", __name__)


@bp.get("/")
def index():
    if g.get("user"):
        return redirect(url_for("dashboard.overview"))
    return render_template("public/landing.html")


@bp.get("/docs")
def docs():
    return render_template("public/docs.html", scopes=SCOPES, events=EVENTS)


@bp.get("/termos")
def termos():
    return render_template("legal/termos.html")


@bp.get("/privacidade")
def privacidade():
    return render_template("legal/privacidade.html")
