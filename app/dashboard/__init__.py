"""Painel web (prefixo /dashboard). Rotas divididas por área."""
from flask import Blueprint

bp = Blueprint("dashboard", __name__)

from app.dashboard import channels, developers, inbox, overview  # noqa: E402,F401
