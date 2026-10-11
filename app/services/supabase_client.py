"""Clientes Supabase.

- `get_service_client()`: cliente com a service role key. Ignora RLS, portanto
  TODA consulta deve filtrar explicitamente por organization_id validado no
  backend. Nunca expor esta chave ao navegador.
- `get_auth_client()`: cliente com a anon key para fluxos de autenticação.
- `create_user_client(access_token)`: cliente com anon key + JWT do usuário,
  sujeito às políticas RLS.

Testes podem injetar implementações falsas sem rede definindo, na factory ou
conftest, `app.extensions["supabase_service_override"]` e/ou
`app.extensions["supabase_auth_override"]` com o objeto fake.
"""
from __future__ import annotations

from flask import current_app, g

from supabase import Client, create_client


class SupabaseNotConfigured(RuntimeError):
    pass


def get_service_client() -> Client:
    override = current_app.extensions.get("supabase_service_override")
    if override is not None:
        return override
    if "supabase_service" not in g:
        cfg = current_app.config
        if not (cfg["SUPABASE_URL"] and cfg["SUPABASE_SERVICE_ROLE_KEY"]):
            raise SupabaseNotConfigured(
                "Supabase não configurado: defina SUPABASE_URL e "
                "SUPABASE_SERVICE_ROLE_KEY."
            )
        g.supabase_service = create_client(
            cfg["SUPABASE_URL"], cfg["SUPABASE_SERVICE_ROLE_KEY"]
        )
    return g.supabase_service


def get_auth_client() -> Client:
    """Cliente com anon key para operações de autenticação (login, cadastro)."""
    override = current_app.extensions.get("supabase_auth_override")
    if override is not None:
        return override
    if "supabase_auth" not in g:
        cfg = current_app.config
        if not (cfg["SUPABASE_URL"] and cfg["SUPABASE_ANON_KEY"]):
            raise SupabaseNotConfigured(
                "Supabase não configurado: defina SUPABASE_URL e SUPABASE_ANON_KEY."
            )
        g.supabase_auth = create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_ANON_KEY"])
    return g.supabase_auth


def create_user_client(access_token: str) -> Client:
    cfg = current_app.config
    if not (cfg["SUPABASE_URL"] and cfg["SUPABASE_ANON_KEY"]):
        raise SupabaseNotConfigured(
            "Supabase não configurado: defina SUPABASE_URL e SUPABASE_ANON_KEY."
        )
    client = create_client(cfg["SUPABASE_URL"], cfg["SUPABASE_ANON_KEY"])
    client.postgrest.auth(access_token)
    return client
