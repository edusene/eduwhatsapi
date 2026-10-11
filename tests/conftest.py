"""Fixtures pytest: aplicação em modo de teste com Supabase falso (sem rede).

Duas organizações são semeadas para testar isolamento entre tenants:
- ORG A (TEST_ORG_ID): usuário TEST_USER_ID (owner), plano api_chat
- ORG B (OTHER_ORG_ID): usuário OTHER_USER_ID (owner), plano api
"""
from __future__ import annotations

import pytest

from app import create_app
from app.config import TestConfig
from app.services import api_keys
from app.services.encryption import encrypt_token
from tests.fakes import FakeSupabaseClient

TEST_USER_ID = "00000000-0000-4000-8000-000000000001"
OTHER_USER_ID = "00000000-0000-4000-8000-000000000002"
VIEWER_USER_ID = "00000000-0000-4000-8000-000000000003"
TEST_ORG_ID = "00000000-0000-4000-8000-0000000000aa"
OTHER_ORG_ID = "00000000-0000-4000-8000-0000000000bb"
CONN_A_ID = "00000000-0000-4000-8000-00000000c0a1"
CONN_B_ID = "00000000-0000-4000-8000-00000000c0b1"
PHONE_A = "111111111111111"
PHONE_B = "222222222222222"
WABA_A = "900000000000001"
CSRF = "test-csrf-token"
GRAPH = "https://graph.facebook.com/v26.0"


@pytest.fixture()
def fake_db() -> FakeSupabaseClient:
    return FakeSupabaseClient()


@pytest.fixture()
def app(fake_db):
    app = create_app(TestConfig)
    app.extensions["supabase_service_override"] = fake_db
    app.extensions["supabase_auth_override"] = fake_db
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def seed(app, fake_db):
    """Duas organizações, cada uma com um número ativo (token criptografado)."""
    db = fake_db
    db.insert_row("organizations", {"id": TEST_ORG_ID, "name": "Org A", "slug": "org-a",
                                    "owner_user_id": TEST_USER_ID, "plan": "api_chat"})
    db.insert_row("organizations", {"id": OTHER_ORG_ID, "name": "Org B", "slug": "org-b",
                                    "owner_user_id": OTHER_USER_ID, "plan": "api"})
    db.insert_row("organization_members", {"organization_id": TEST_ORG_ID, "user_id": TEST_USER_ID,
                                           "role": "owner", "email": "dono@a.com"})
    db.insert_row("organization_members", {"organization_id": OTHER_ORG_ID, "user_id": OTHER_USER_ID,
                                           "role": "owner", "email": "dono@b.com"})
    db.insert_row("organization_members", {"organization_id": TEST_ORG_ID, "user_id": VIEWER_USER_ID,
                                           "role": "viewer", "email": "leitor@a.com"})
    with app.app_context():
        token_a = encrypt_token("EAA-token-org-a")
        token_b = encrypt_token("EAA-token-org-b")
    db.insert_row("meta_connections", {
        "id": CONN_A_ID, "organization_id": TEST_ORG_ID, "waba_id": WABA_A, "phone_number_id": PHONE_A,
        "display_phone_number": "+55 11 4000-0001", "verified_name": "Loja A", "status": "active",
        "access_token_encrypted": token_a, "webhook_subscribed": True, "registered": True,
        "connected_at": "2026-10-01T12:00:00+00:00"})
    db.insert_row("meta_connections", {
        "id": CONN_B_ID, "organization_id": OTHER_ORG_ID, "waba_id": "900000000000002",
        "phone_number_id": PHONE_B, "display_phone_number": "+55 11 4000-0002", "verified_name": "Loja B",
        "status": "active", "access_token_encrypted": token_b, "webhook_subscribed": True,
        "connected_at": "2026-10-01T12:00:00+00:00"})
    return db


def _login(client, user_id: str, email: str):
    with client.session_transaction() as sess:
        sess["user"] = {"id": user_id, "email": email}
        sess["_csrf_token"] = CSRF
    return client


@pytest.fixture()
def owner(client, seed):
    return _login(client, TEST_USER_ID, "dono@a.com")


@pytest.fixture()
def viewer(client, seed):
    return _login(client, VIEWER_USER_ID, "leitor@a.com")


@pytest.fixture()
def make_key(app, seed):
    def _make(org_id: str = TEST_ORG_ID, environment: str = "live", scopes=None) -> str:
        with app.app_context():
            return api_keys.create_api_key(org_id, "Teste", environment=environment, scopes=scopes)["key"]
    return _make


def auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}
