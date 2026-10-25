"""Painel web (prefixo /dashboard). Rotas divididas por área."""
from flask import Blueprint, request

bp = Blueprint("dashboard", __name__)

PAGE_SIZE = 25


def page_arg() -> int:
    """Página pedida em ?page= (1 em diante); valor inválido vira 1."""
    raw = request.args.get("page", "1")
    return max(1, min(int(raw), 10_000)) if raw.isdigit() else 1


def paginate(query, page: int) -> tuple[list, bool]:
    """Executa a consulta na página pedida; devolve (linhas, há_próxima)."""
    start = (page - 1) * PAGE_SIZE
    rows = query.range(start, start + PAGE_SIZE).execute().data  # 1 a mais para saber se há próxima
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE

from app.dashboard import channels, developers, inbox, overview  # noqa: E402,F401
