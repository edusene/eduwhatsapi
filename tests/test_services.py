"""Serviços: conexões com a Meta (mocks HTTP), chaves de API, configuração,
endpoints de cron e retenção de dados."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import responses

from app import create_app
from app.config import Config, TestConfig
from app.services import api_keys, connections, maintenance
from app.services.encryption import decrypt_token
from tests.conftest import CONN_A_ID, GRAPH, OTHER_ORG_ID, PHONE_A, TEST_ORG_ID, TEST_USER_ID, WABA_A

NEW_WABA = "800000000000009"
NEW_PHONE = "700000000000009"


def _mock_phone(phone=NEW_PHONE, waba=NEW_WABA):
    responses.add(responses.GET, f"{GRAPH}/{waba}/phone_numbers", json={"data": [{"id": phone}]})
    responses.add(responses.GET, f"{GRAPH}/{phone}", json={
        "id": phone, "display_phone_number": "+55 11 4000-0009", "verified_name": "Nova Loja",
        "quality_rating": "GREEN"})


class TestManualConnection:
    @responses.activate
    def test_connect_validates_on_meta_and_encrypts(self, app, seed, fake_db):
        _mock_phone()
        responses.add(responses.POST, f"{GRAPH}/{NEW_WABA}/subscribed_apps", json={"success": True})
        with app.app_context():
            record = connections.connect_manual(TEST_ORG_ID, TEST_USER_ID, token="EAA-novo",
                                                waba_id=NEW_WABA, phone_number_id=NEW_PHONE)
            row = connections.get_connection(TEST_ORG_ID, record["id"])
            assert row["status"] == "active" and row["verified_name"] == "Nova Loja"
            assert row["access_token_encrypted"] != "EAA-novo"
            assert decrypt_token(row["access_token_encrypted"]) == "EAA-novo"
            assert row["webhook_subscribed"] is True
        assert any(a["action"] == "channel.connected" for a in fake_db.rows("audit_logs"))

    @responses.activate
    def test_phone_must_belong_to_waba(self, app, seed):
        responses.add(responses.GET, f"{GRAPH}/{NEW_WABA}/phone_numbers", json={"data": [{"id": "123456"}]})
        responses.add(responses.GET, f"{GRAPH}/{NEW_PHONE}", json={"id": NEW_PHONE})
        with app.app_context(), pytest.raises(connections.ConnectionError_, match="não pertence"):
            connections.connect_manual(TEST_ORG_ID, TEST_USER_ID, token="EAA", waba_id=NEW_WABA,
                                       phone_number_id=NEW_PHONE)

    @responses.activate
    def test_meta_rejects_token(self, app, seed, fake_db):
        responses.add(responses.GET, f"{GRAPH}/{NEW_WABA}/phone_numbers", status=401,
                      json={"error": {"message": "Invalid OAuth access token.", "code": 190}})
        before = len(fake_db.rows("meta_connections"))
        with app.app_context(), pytest.raises(connections.ConnectionError_, match="Invalid OAuth"):
            connections.connect_manual(TEST_ORG_ID, TEST_USER_ID, token="EAA-ruim", waba_id=NEW_WABA,
                                       phone_number_id=NEW_PHONE)
        assert len(fake_db.rows("meta_connections")) == before

    def test_same_number_cannot_join_two_orgs(self, app, seed):
        with app.app_context(), pytest.raises(connections.ConnectionError_, match="outra conta"):
            connections.connect_manual(OTHER_ORG_ID, None, token="EAA", waba_id=WABA_A, phone_number_id=PHONE_A)


class TestEmbeddedSignup:
    @pytest.fixture()
    def es_app(self, app):
        app.config.update(META_APP_ID="1234567890", META_APP_SECRET="s" * 32, META_ES_CONFIG_ID="999")
        return app

    @responses.activate
    def test_exchange_and_onboard(self, es_app, seed, fake_db):
        responses.add(responses.GET, f"{GRAPH}/oauth/access_token", json={"access_token": "EAA-business"})
        responses.add(responses.GET, f"{GRAPH}/debug_token", json={"data": {
            "is_valid": True, "app_id": "1234567890",
            "scopes": ["whatsapp_business_management", "whatsapp_business_messaging"],
            "granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": [NEW_WABA]}]}})
        _mock_phone()
        responses.add(responses.POST, f"{GRAPH}/{NEW_WABA}/subscribed_apps", json={"success": True})
        responses.add(responses.POST, f"{GRAPH}/{NEW_PHONE}/register", json={"success": True})
        with es_app.app_context():
            record = connections.connect_embedded_signup(
                TEST_ORG_ID, TEST_USER_ID, code="CODE123", waba_id=NEW_WABA, phone_number_id=NEW_PHONE,
                business_id="555555", pin="123456")
            row = connections.get_connection(TEST_ORG_ID, record["id"])
        assert row["connection_type"] == "embedded_signup"
        assert row["registered"] is True and row["webhook_subscribed"] is True
        exchange = responses.calls[0].request
        assert "code=CODE123" in exchange.url and "client_secret=" in exchange.url
        register_call = [c for c in responses.calls if c.request.url.endswith("/register")][0]
        assert b'"pin": "123456"' in register_call.request.body

    @responses.activate
    def test_rejects_waba_not_granted(self, es_app, seed):
        responses.add(responses.GET, f"{GRAPH}/oauth/access_token", json={"access_token": "EAA-business"})
        responses.add(responses.GET, f"{GRAPH}/debug_token", json={"data": {
            "is_valid": True, "app_id": "1234567890",
            "granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": ["111"]}]}})
        with es_app.app_context(), pytest.raises(connections.ConnectionError_, match="não inclui"):
            connections.connect_embedded_signup(TEST_ORG_ID, TEST_USER_ID, code="C", waba_id=NEW_WABA,
                                                phone_number_id=NEW_PHONE, business_id=None)

    def test_route_disabled_without_config(self, owner):
        r = owner.post("/connect/meta/embedded-signup", json={}, headers={"X-CSRF-Token": "test-csrf-token"})
        assert r.status_code == 503


class TestDisconnect:
    @responses.activate
    def test_disconnect_erases_token(self, app, seed, fake_db):
        responses.add(responses.DELETE, f"{GRAPH}/{WABA_A}/subscribed_apps", json={"success": True})
        with app.app_context():
            connections.disconnect(TEST_ORG_ID, CONN_A_ID, TEST_USER_ID)
            row = connections.get_connection(TEST_ORG_ID, CONN_A_ID)
        assert row["status"] == "disconnected"
        assert row["access_token_encrypted"] is None

    def test_cannot_disconnect_other_org(self, app, seed):
        with app.app_context(), pytest.raises(connections.ConnectionError_):
            connections.disconnect(OTHER_ORG_ID, CONN_A_ID, None)


class TestApiKeys:
    def test_hash_only_and_revoke(self, app, seed, fake_db):
        with app.app_context():
            created = api_keys.create_api_key(TEST_ORG_ID, "k", environment="live")
            assert created["key"].startswith("sk_live_")
            row = fake_db.rows("api_keys")[0]
            assert created["key"] not in str(row)
            assert api_keys.verify_api_key(created["key"])["organization_id"] == TEST_ORG_ID
            assert api_keys.revoke_api_key(TEST_ORG_ID, created["id"])
            assert api_keys.verify_api_key(created["key"]) is None

    def test_rotate(self, app, seed):
        with app.app_context():
            old = api_keys.create_api_key(TEST_ORG_ID, "k")
            new = api_keys.rotate_api_key(TEST_ORG_ID, old["id"])
            assert new["key"] != old["key"]
            assert api_keys.verify_api_key(old["key"]) is None
            assert api_keys.verify_api_key(new["key"]) is not None

    def test_expired(self, app, seed):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        with app.app_context():
            created = api_keys.create_api_key(TEST_ORG_ID, "k", expires_at=past)
            assert api_keys.verify_api_key(created["key"]) is None

    def test_invalid_input(self, app, seed):
        with app.app_context():
            with pytest.raises(ValueError):
                api_keys.create_api_key(TEST_ORG_ID, "", environment="live")
            with pytest.raises(ValueError):
                api_keys.create_api_key(TEST_ORG_ID, "k", environment="prod")
            with pytest.raises(ValueError):
                api_keys.create_api_key(TEST_ORG_ID, "k", scopes=["admin:all"])


class TestConfigAndOps:
    def test_dev_mode_boots_without_meta(self):
        app = create_app(TestConfig)
        assert app.config["META_APP_ID"] == ""
        assert app.test_client().get("/health").status_code == 200

    def test_production_requires_secrets(self):
        class Prod(Config):
            FLASK_ENV = "production"
            SECRET_KEY = ""
            SUPABASE_URL = ""
            SUPABASE_ANON_KEY = ""
            SUPABASE_SERVICE_ROLE_KEY = ""
            API_KEY_PEPPER = ""
            TOKEN_ENCRYPTION_KEY = ""
        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            create_app(Prod)

    def test_cron_requires_secret(self, client, seed):
        assert client.post("/internal/cron/deliver-webhooks").status_code == 403
        r = client.post("/internal/cron/deliver-webhooks", headers={"Authorization": "Bearer test-cron-secret"})
        assert r.status_code == 200 and r.get_json()["processed"] == 0

    def test_purge_respects_retention(self, app, seed, fake_db):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        fake_db.insert_row("messages", {"organization_id": TEST_ORG_ID, "direction": "inbound",
                                        "contact_wa_id": "1", "type": "text", "created_at": old})
        fake_db.insert_row("messages", {"organization_id": TEST_ORG_ID, "direction": "inbound",
                                        "contact_wa_id": "2", "type": "text"})
        fake_db.insert_row("api_request_logs", {"organization_id": TEST_ORG_ID, "request_id": "r",
                                                "method": "GET", "endpoint": "/me", "status_code": 200,
                                                "created_at": old})
        with app.app_context():
            removed = maintenance.purge_expired()
        assert removed["messages"] == 1 and removed["api_request_logs"] == 1
        assert [m["contact_wa_id"] for m in fake_db.rows("messages")] == ["2"]
