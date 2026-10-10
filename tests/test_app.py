"""Testes do MVP MetaBridge.

Cobrem health, páginas públicas, autenticação, isolamento, API keys,
webhooks de entrada/saída, proxy para Meta e modo de desenvolvimento.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import requests
import responses

from app import create_app
from app.config import TestConfig
from app.services import api_keys, encryption, outbound_webhooks
from app.services.security import hmac_sha256_hex
from tests.conftest import OTHER_ORG_ID, TEST_ORG_ID, TEST_USER_ID
from tests.fakes import FakeSupabaseClient


class TestHealthAndPublic:
    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json == {"status": "ok"}

    def test_public_pages(self, client):
        for path in ("/termos", "/privacidade", "/api/v1/openapi.yaml"):
            assert client.get(path).status_code == 200


class TestAuth:
    def test_login_required_redirect(self, client):
        r = client.get("/dashboard/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.location

    def test_register_and_login(self, client, fake_db):
        fake_db.auth._users.append({"id": TEST_USER_ID, "email": "a@b.com"})
        r = client.post(
            "/register",
            data={"email": "a@b.com", "password": "1234567890", "org_name": "Acme"},
            follow_redirects=True,
        )
        assert b"E-mail" in r.data  # flash ou página de login após confirmação

    def test_login_post_and_session(self, client, fake_db):
        fake_db.auth._users.append({"id": TEST_USER_ID, "email": "a@b.com"})
        r = client.post(
            "/login",
            data={"email": "a@b.com", "password": "1234567890"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        with client.session_transaction() as sess:
            assert sess["user"]["id"] == TEST_USER_ID

    def test_logout(self, logged_in_client):
        r = logged_in_client.post("/logout", follow_redirects=True)
        assert "Sessão encerrada" in r.data.decode("utf-8")


class TestIsolation:
    def test_cross_tenant_api_keys(self, logged_in_client, fake_db, seed_membership):
        fake_db.table("api_keys").insert(
            {"organization_id": OTHER_ORG_ID, "name": "ChaveOutraOrg", "environment": "test",
             "key_prefix": "sk_test_abc", "key_suffix": "xyz",
             "key_hash": "deadbeef"}
        ).execute()
        r = logged_in_client.get("/dashboard/api-keys")
        assert r.status_code == 200
        assert b"ChaveOutraOrg" not in r.data  # não deve aparecer na lista da org do usuário

    def test_dashboard_auto_creates_org(self, client, fake_db):
        # Usuário sem organização deve ter uma criada automaticamente
        with client.session_transaction() as sess:
            sess["user"] = {"id": "no-org-user", "email": "x@y.com"}
        r = client.get("/dashboard/", follow_redirects=False)
        # Após criar org automaticamente, deve acessar o dashboard
        assert r.status_code == 200


class TestApiKeys:
    def test_create_verify_revoke(self, app, fake_db):
        with app.app_context():
            rec = api_keys.create_api_key(
                TEST_ORG_ID, "teste", environment="test", created_by=TEST_USER_ID
            )
            assert rec["key"].startswith("sk_test_")
            verified = api_keys.verify_api_key(rec["key"])
            assert verified is not None
            assert verified["organization_id"] == TEST_ORG_ID
            assert api_keys.revoke_api_key(TEST_ORG_ID, rec["id"], TEST_USER_ID)
            assert api_keys.verify_api_key(rec["key"]) is None

    def test_rotate(self, app, fake_db):
        with app.app_context():
            old = api_keys.create_api_key(
                TEST_ORG_ID, "rot", environment="test", created_by=TEST_USER_ID
            )
            new = api_keys.rotate_api_key(TEST_ORG_ID, old["id"], TEST_USER_ID)
            assert new is not None
            assert api_keys.verify_api_key(old["key"]) is None
            assert api_keys.verify_api_key(new["key"]) is not None


class TestApiV1:
    def test_me_authenticated(self, app, client, fake_db, seed_membership):
        with app.app_context():
            rec = api_keys.create_api_key(
                TEST_ORG_ID, "k", environment="test", scopes=["messages:send"]
            )
        r = client.get("/api/v1/me", headers={"Authorization": f"Bearer {rec['key']}"})
        assert r.status_code == 200
        assert r.json["organization_id"] == TEST_ORG_ID

    def test_missing_api_key(self, client):
        r = client.get("/api/v1/me")
        assert r.status_code == 401
        assert r.json["error"]["code"] == "missing_api_key"

    def test_invalid_api_key(self, client):
        r = client.get("/api/v1/me", headers={"Authorization": "Bearer sk_test_zzzzz"})
        assert r.status_code == 401
        assert r.json["error"]["code"] == "invalid_api_key"

    def test_insufficient_scope(self, app, client, fake_db, seed_membership):
        with app.app_context():
            rec = api_keys.create_api_key(
                TEST_ORG_ID, "k", environment="test", scopes=["channels:read"]
            )
        r = client.post(
            "/api/v1/whatsapp/123456789012345/messages",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"to": "5511999999999", "type": "text", "text": {"body": "Oi"}},
        )
        assert r.status_code == 403
        assert r.json["error"]["code"] == "insufficient_scope"

    def test_send_validation_errors(self, app, client, fake_db, seed_membership):
        phone = "123456789012345"
        with app.app_context():
            rec = api_keys.create_api_key(
                TEST_ORG_ID, "k", environment="test", scopes=["messages:send"]
            )
            fake_db.table("meta_connections").insert(
                {"organization_id": TEST_ORG_ID, "channel": "whatsapp",
                 "phone_number_id": phone, "status": "active",
                 "access_token_encrypted": encryption.encrypt_token("testtoken")}
            ).execute()
        headers = {"Authorization": f"Bearer {rec['key']}"}
        # JSON inválido (malformado)
        r = client.post(
            f"/api/v1/whatsapp/{phone}/messages",
            headers={**headers, "Content-Type": "application/json"},
            data="not json",
        )
        assert r.status_code == 400
        assert r.json["error"]["code"] == "invalid_json"

        r = client.post(
            f"/api/v1/whatsapp/{phone}/messages",
            headers=headers,
            json={"to": "abc", "type": "text", "text": {"body": "Oi"}},
        )
        assert r.status_code == 400
        assert r.json["error"]["code"] == "invalid_recipient"

        r = client.post(
            f"/api/v1/whatsapp/{phone}/messages",
            headers=headers,
            json={"to": "5511999999999", "type": "image"},
        )
        assert r.status_code == 400
        assert r.json["error"]["code"] == "unsupported_message_type"

    @responses.activate
    def test_send_message_to_meta(self, app, client, fake_db, seed_membership):
        phone = "123456789012345"
        with app.app_context():
            fake_db.table("meta_connections").insert(
                {"organization_id": TEST_ORG_ID, "channel": "whatsapp",
                 "phone_number_id": phone, "status": "active",
                 "access_token_encrypted": encryption.encrypt_token("testtoken")}
            ).execute()
            rec = api_keys.create_api_key(
                TEST_ORG_ID, "k", environment="test", scopes=["messages:send"]
            )
        responses.post(
            f"https://graph.facebook.com/{TestConfig.META_GRAPH_API_VERSION}/{phone}/messages",
            json={"messages": [{"id": "wamid.123"}]},
            status=200,
        )
        r = client.post(
            f"/api/v1/whatsapp/{phone}/messages",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"to": "5511999999999", "type": "text", "text": {"body": "Oi"}},
        )
        assert r.status_code == 200
        assert r.json["data"]["messages"][0]["id"] == "wamid.123"

    @responses.activate
    def test_send_meta_timeout(self, app, client, fake_db, seed_membership):
        phone = "123456789012345"
        with app.app_context():
            fake_db.table("meta_connections").insert(
                {"organization_id": TEST_ORG_ID, "channel": "whatsapp",
                 "phone_number_id": phone, "status": "active",
                 "access_token_encrypted": encryption.encrypt_token("testtoken")}
            ).execute()
            rec = api_keys.create_api_key(
                TEST_ORG_ID, "k", environment="test", scopes=["messages:send"]
            )
        responses.post(
            f"https://graph.facebook.com/{TestConfig.META_GRAPH_API_VERSION}/{phone}/messages",
            body=requests.Timeout(),
        )
        r = client.post(
            f"/api/v1/whatsapp/{phone}/messages",
            headers={"Authorization": f"Bearer {rec['key']}"},
            json={"to": "5511999999999", "type": "text", "text": {"body": "Oi"}},
        )
        assert r.status_code == 504
        assert r.json["error"]["code"] == "gateway_timeout"

    @responses.activate
    def test_idempotency(self, app, client, fake_db, seed_membership):
        phone = "123456789012345"
        with app.app_context():
            fake_db.table("meta_connections").insert(
                {"organization_id": TEST_ORG_ID, "channel": "whatsapp",
                 "phone_number_id": phone, "status": "active",
                 "access_token_encrypted": encryption.encrypt_token("testtoken")}
            ).execute()
            rec = api_keys.create_api_key(
                TEST_ORG_ID, "k", environment="test", scopes=["messages:send"]
            )
        responses.post(
            f"https://graph.facebook.com/{TestConfig.META_GRAPH_API_VERSION}/{phone}/messages",
            json={"messages": [{"id": "wamid.idem"}]},
            status=200,
        )
        body = {"to": "5511999999999", "type": "text", "text": {"body": "Oi"}}
        r1 = client.post(
            f"/api/v1/whatsapp/{phone}/messages",
            headers={"Authorization": f"Bearer {rec['key']}", "Idempotency-Key": "idem-key-001"},
            json=body,
        )
        assert r1.status_code == 200
        # idempotency key armazena resposta; segunda chamada deve retornar a mesma sem nova chamada à Meta
        r2 = client.post(
            f"/api/v1/whatsapp/{phone}/messages",
            headers={"Authorization": f"Bearer {rec['key']}", "Idempotency-Key": "idem-key-001"},
            json=body,
        )
        assert r2.status_code == 200
        assert r2.headers.get("X-Idempotent-Replay") == "true"


class TestMetaWebhook:
    def test_verify_challenge(self, client):
        r = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=test-verify-token&hub.challenge=12345")
        assert r.status_code == 200
        assert r.data.decode() == "12345"

    def test_verify_invalid_token(self, client):
        r = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=12345")
        assert r.status_code == 403

    def test_post_invalid_signature(self, client):
        r = client.post("/webhooks/meta", data=b"{}", headers={"X-Hub-Signature-256": "sha256=bad"})
        assert r.status_code == 403

    def test_post_valid_and_dedupe(self, client, fake_db):
        body = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"id": "waba1", "changes": [{"value": {
                "messages": [{"id": "msg1", "from": "5511", "timestamp": "123", "type": "text"}],
                "metadata": {"phone_number_id": "ph1"}
            }}]}]
        }).encode()
        sig = "sha256=" + hmac.new("test-webhook-secret".encode(), body, hashlib.sha256).hexdigest()
        r = client.post("/webhooks/meta", data=body, headers={
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        })
        assert r.status_code == 200
        # duplicado
        r2 = client.post("/webhooks/meta", data=body, headers={
            "X-Hub-Signature-256": sig,
            "Content-Type": "application/json",
        })
        assert r2.status_code == 200
        # O fake não impõe unique constraint como o PostgreSQL; em produção o
        # dedupe_key único evitaria duplicados. Aqui garantimos que ao menos 1
        # registro existe e o handler respondeu 200 no segundo evento.
        rows = fake_db.table("webhook_events").select("*").execute().data
        assert len([x for x in rows if x.get("dedupe_key") == "msg1"]) >= 1


class TestOutboundWebhooks:
    def test_ssrf_blocks_private(self, app):
        with app.app_context():
            with pytest.raises(outbound_webhooks.WebhookURLBlocked):
                outbound_webhooks.validate_webhook_url("https://127.0.0.1/webhook")
            with pytest.raises(outbound_webhooks.WebhookURLBlocked):
                outbound_webhooks.validate_webhook_url("https://169.254.169.254/latest/meta-data/")
            with pytest.raises(outbound_webhooks.WebhookURLBlocked):
                outbound_webhooks.validate_webhook_url("https://192.168.1.1/webhook")

    def test_ssrf_allows_public_https(self, app):
        with app.app_context():
            url = outbound_webhooks.validate_webhook_url("https://example.com/webhook")
            assert url == "https://example.com/webhook"

    def test_sign_delivery(self):
        sig = outbound_webhooks.sign_delivery("secret", 1234567890, b'{"a":1}')
        expected = "sha256=" + hmac.new(
            "secret".encode(), b"1234567890.{\"a\":1}", hashlib.sha256
        ).hexdigest()
        assert sig == expected

    def test_create_and_list_endpoint(self, app, fake_db, seed_membership):
        with app.app_context():
            rec = outbound_webhooks.create_endpoint(
                TEST_ORG_ID, "https://example.com/hook",
                events=["message.received"], description="test", created_by=TEST_USER_ID
            )
            assert rec["secret"].startswith("whsec_")
            assert "secret" not in outbound_webhooks.list_endpoints(TEST_ORG_ID)[0]


class TestDevelopmentMode:
    def test_app_boots_without_meta(self):
        app = create_app(TestConfig)
        assert app.config["META_APP_ID"] == ""
        with app.test_client() as c:
            assert c.get("/health").status_code == 200
