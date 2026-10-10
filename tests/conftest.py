"""Fixtures pytest: aplicação em modo de teste com Supabase falso (sem rede)."""
from __future__ import annotations

import pytest

from app import create_app
from app.config import TestConfig
from tests.fakes import FakeSupabaseClient

TEST_USER_ID = "00000000-0000-0000-0000-000000000001"
TEST_ORG_ID = "00000000-0000-0000-0000-0000000000aa"
OTHER_ORG_ID = "00000000-0000-0000-0000-0000000000bb"


@pytest.fixture()
def fake_db() -> FakeSupabaseClient:
    return FakeSupabaseClient()


@pytest.fixture()
def app(fake_db):
    app = create_app(TestConfig)
    app.extensions["supabase_service_override"] = fake_db
    app.extensions["supabase_auth_override"] = fake_db
    # Desabilita CSRF nos testes para simplificar POSTs de formulários.
    import app.services.security as sec
    app._orig_validate_csrf = sec.validate_csrf
    sec.validate_csrf = lambda: None
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seed_membership(fake_db):
    """Semeia usuário + organização + associação owner padrão."""
    fake_db.table("organizations").insert({
        "id": TEST_ORG_ID, "name": "Org Teste", "slug": "org-teste",
        "owner_user_id": TEST_USER_ID,
    }).execute()
    fake_db.table("organization_members").insert({
        "organization_id": TEST_ORG_ID, "user_id": TEST_USER_ID, "role": "owner",
    }).execute()
    fake_db.table("organizations").insert({
        "id": OTHER_ORG_ID, "name": "Outra Org", "slug": "outra-org",
        "owner_user_id": "00000000-0000-0000-0000-0000000000ff",
    }).execute()
    return fake_db


@pytest.fixture()
def logged_in_client(client):
    """Cliente de teste com sessão autenticada (sem Supabase Auth real)."""
    with client.session_transaction() as sess:
        sess["user"] = {"id": TEST_USER_ID, "email": "teste@example.com"}
        sess["access_token"] = "fake-access-token"
    return client
