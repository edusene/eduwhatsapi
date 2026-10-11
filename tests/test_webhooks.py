"""Webhooks: entrada da Meta (handshake, assinatura, deduplicação, status,
roteamento por número) e saída para clientes (SSRF, HMAC, retries)."""
from __future__ import annotations

import hashlib
import hmac
import json
import socket

import pytest
import requests
import responses

from app.services import outbound_webhooks
from app.services.encryption import encrypt_token
from tests.conftest import CONN_A_ID, OTHER_ORG_ID, PHONE_A, PHONE_B, TEST_ORG_ID

SECRET = "test-webhook-secret"


def _signed(body: dict, secret: str = SECRET) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _message_event(phone_id: str = PHONE_A, wamid: str = "wamid.IN1", text: str = "Olá, tudo bem?") -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "900000000000001", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "551140000001", "phone_number_id": phone_id},
            "contacts": [{"profile": {"name": "Ana"}, "wa_id": "5511988887777"}],
            "messages": [{"from": "5511988887777", "id": wamid, "timestamp": "1760000000",
                          "type": "text", "text": {"body": text}}]}}]}]}


def _status_event(wamid: str, status: str) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "900000000000001", "changes": [{
        "field": "messages", "value": {
            "metadata": {"phone_number_id": PHONE_A},
            "statuses": [{"id": wamid, "status": status, "timestamp": "1760000100",
                          "recipient_id": "5511988887777"}]}}]}]}


class TestInboundHandshake:
    def test_verify_ok(self, client):
        r = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=test-verify-token&hub.challenge=42")
        assert r.status_code == 200 and r.data == b"42"

    def test_verify_wrong_token(self, client):
        r = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=errado&hub.challenge=42")
        assert r.status_code == 403

    def test_invalid_signature_rejected_before_processing(self, client, seed, fake_db):
        raw, _ = _signed(_message_event())
        r = client.post("/webhooks/meta", data=raw, headers={"X-Hub-Signature-256": "sha256=deadbeef",
                                                              "Content-Type": "application/json"})
        assert r.status_code == 403
        assert not fake_db.rows("webhook_events")

    def test_missing_signature(self, client, seed):
        r = client.post("/webhooks/meta", json=_message_event())
        assert r.status_code == 403


class TestInboundProcessing:
    def test_message_creates_conversation_and_dedupes(self, client, seed, fake_db):
        raw, headers = _signed(_message_event())
        assert client.post("/webhooks/meta", data=raw, headers=headers).status_code == 200
        assert client.post("/webhooks/meta", data=raw, headers=headers).status_code == 200  # reenvio da Meta
        messages = [m for m in fake_db.rows("messages") if m["wamid"] == "wamid.IN1"]
        assert len(messages) == 1
        assert messages[0]["organization_id"] == TEST_ORG_ID
        assert messages[0]["content"]["body"] == "Olá, tudo bem?"
        conv = fake_db.rows("conversations")[0]
        assert conv["unread_count"] == 1 and conv["last_inbound_at"]
        contact = fake_db.rows("contacts")[0]
        assert contact["profile_name"] == "Ana"
        # webhook_events guarda apenas o mínimo (sem texto)
        assert "Olá" not in json.dumps(fake_db.rows("webhook_events"))

    def test_routes_to_correct_org_and_plan(self, client, seed, fake_db):
        raw, headers = _signed(_message_event(phone_id=PHONE_B, wamid="wamid.INB"))
        client.post("/webhooks/meta", data=raw, headers=headers)
        msg = [m for m in fake_db.rows("messages") if m["wamid"] == "wamid.INB"][0]
        assert msg["organization_id"] == OTHER_ORG_ID
        assert msg["content"] is None  # plano api: sem conteúdo armazenado
        assert not fake_db.rows("conversations")

    def test_unknown_number_ignored(self, client, seed, fake_db):
        raw, headers = _signed(_message_event(phone_id="999999999"))
        assert client.post("/webhooks/meta", data=raw, headers=headers).status_code == 200
        assert not fake_db.rows("messages")
        assert fake_db.rows("webhook_events")[0]["status"] == "ignored"

    def test_status_progression_never_regresses(self, client, seed, fake_db):
        fake_db.insert_row("messages", {"organization_id": TEST_ORG_ID, "connection_id": CONN_A_ID,
                                        "direction": "outbound", "wamid": "wamid.OUT", "contact_wa_id": "5511",
                                        "type": "text", "status": "accepted"})
        for status in ("sent", "read", "delivered"):
            raw, headers = _signed(_status_event("wamid.OUT", status))
            client.post("/webhooks/meta", data=raw, headers=headers)
        msg = [m for m in fake_db.rows("messages") if m["wamid"] == "wamid.OUT"][0]
        assert msg["status"] == "read"

    def test_inbound_forwarded_to_customer_webhook(self, client, app, seed, fake_db, monkeypatch):
        monkeypatch.setattr(outbound_webhooks.socket, "getaddrinfo",
                            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
        with app.app_context():
            fake_db.insert_row("webhook_endpoints", {"organization_id": TEST_ORG_ID, "url": "https://cliente.example.com/hook",
                                                     "events": ["message.received"], "status": "active",
                                                     "secret_encrypted": encrypt_token("whsec_teste")})
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, "https://cliente.example.com/hook", status=200)
            raw, headers = _signed(_message_event(wamid="wamid.FWD"))
            client.post("/webhooks/meta", data=raw, headers=headers)
            delivered = json.loads(rsps.calls[0].request.body)
            sig = rsps.calls[0].request.headers["X-Webhook-Signature"]
            ts = rsps.calls[0].request.headers["X-Webhook-Timestamp"]
            sent_body = rsps.calls[0].request.body
        assert delivered["event"] == "message.received"
        assert delivered["data"]["message"]["text"]["body"] == "Olá, tudo bem?"
        expected = "sha256=" + hmac.new(b"whsec_teste", ts.encode() + b"." + sent_body,
                                        hashlib.sha256).hexdigest()
        assert sig == expected
        assert fake_db.rows("webhook_deliveries")[0]["status"] == "delivered"

    def test_own_app_connection_webhook(self, client, app, seed, fake_db):
        own_secret = "0123456789abcdef0123456789abcdef"
        with app.app_context():
            conn = fake_db.insert_row("meta_connections", {
                "organization_id": TEST_ORG_ID, "waba_id": "777", "phone_number_id": "333333333",
                "status": "active", "app_secret_encrypted": encrypt_token(own_secret),
                "webhook_verify_token_encrypted": encrypt_token("verifica-123"),
                "access_token_encrypted": encrypt_token("EAA-own")})
        url = f"/webhooks/meta/{conn['id']}"
        ok = client.get(f"{url}?hub.mode=subscribe&hub.verify_token=verifica-123&hub.challenge=7")
        assert ok.status_code == 200 and ok.data == b"7"
        # Assinado com o secret global não vale nesta URL
        raw, headers = _signed(_message_event(phone_id="333333333", wamid="wamid.OWN"))
        assert client.post(url, data=raw, headers=headers).status_code == 403
        raw, headers = _signed(_message_event(phone_id="333333333", wamid="wamid.OWN"), secret=own_secret)
        assert client.post(url, data=raw, headers=headers).status_code == 200
        assert any(m["wamid"] == "wamid.OWN" for m in fake_db.rows("messages"))
        # Esta URL não aceita eventos de outro número (ex.: de outra organização)
        raw, headers = _signed(_message_event(phone_id=PHONE_B, wamid="wamid.SPOOF"), secret=own_secret)
        client.post(url, data=raw, headers=headers)
        assert not any(m["wamid"] == "wamid.SPOOF" for m in fake_db.rows("messages"))


@pytest.fixture()
def public_dns(monkeypatch):
    monkeypatch.setattr(outbound_webhooks.socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


class TestOutboundSecurity:
    @pytest.fixture(autouse=True)
    def strict(self, app):
        app.config["OUTBOUND_WEBHOOK_ALLOW_INSECURE"] = False

    @pytest.mark.parametrize("url", [
        "https://127.0.0.1/hook", "https://169.254.169.254/latest/meta-data/", "https://10.0.0.5/x",
        "https://192.168.1.1/x", "https://[::1]/x", "https://localhost/x", "http://example.com/x",
        "ftp://example.com/x", "https://user:pass@example.com/x", "https://metadata.google.internal/x",
    ])
    def test_blocks_internal_and_insecure(self, app, url):
        with app.app_context(), pytest.raises(outbound_webhooks.WebhookURLBlocked):
            outbound_webhooks.validate_webhook_url(url)

    def test_blocks_dns_resolving_to_private(self, app, monkeypatch):
        monkeypatch.setattr(outbound_webhooks.socket, "getaddrinfo",
                            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 443))])
        with app.app_context(), pytest.raises(outbound_webhooks.WebhookURLBlocked):
            outbound_webhooks.validate_webhook_url("https://parece-publico.example.com/hook")

    def test_allows_public_https(self, app, public_dns):
        with app.app_context():
            assert outbound_webhooks.validate_webhook_url("https://example.com/hook") == "https://example.com/hook"

    def test_sign_delivery(self):
        sig = outbound_webhooks.sign_delivery("segredo", 1700000000, b'{"a":1}')
        assert sig == "sha256=" + hmac.new(b"segredo", b'1700000000.{"a":1}', hashlib.sha256).hexdigest()

    def test_secret_shown_once(self, app, seed, public_dns):
        with app.app_context():
            created = outbound_webhooks.create_endpoint(TEST_ORG_ID, "https://example.com/hook", ["message.received"])
            assert created["secret"].startswith("whsec_")
            listed = outbound_webhooks.list_endpoints(TEST_ORG_ID)
            assert "secret" not in listed[0] and "secret_encrypted" not in listed[0]

    def test_redirect_not_followed_and_retry_scheduled(self, app, seed, fake_db, public_dns):
        with app.app_context():
            endpoint = outbound_webhooks.create_endpoint(TEST_ORG_ID, "https://example.com/hook", ["message.sent"])
            with responses.RequestsMock() as rsps:
                rsps.add(responses.POST, "https://example.com/hook", status=302,
                         headers={"Location": "http://169.254.169.254/"})
                outbound_webhooks.enqueue_event(TEST_ORG_ID, "message.sent", {"x": 1})
                assert len(rsps.calls) == 1
        row = fake_db.rows("webhook_deliveries")[0]
        assert row["endpoint_id"] == endpoint["id"]
        assert row["status"] == "pending" and row["attempts"] == 1 and row["next_retry_at"]
        assert row["locked_until"] is None

    def test_max_attempts_marks_failed(self, app, seed, fake_db, public_dns):
        with app.app_context():
            outbound_webhooks.create_endpoint(TEST_ORG_ID, "https://example.com/hook", ["message.sent"])
            with responses.RequestsMock() as rsps:
                rsps.add(responses.POST, "https://example.com/hook", body=requests.ConnectionError("down"))
                outbound_webhooks.enqueue_event(TEST_ORG_ID, "message.sent", {"x": 1})
                row = fake_db.rows("webhook_deliveries")[0]
                row["next_retry_at"] = "2000-01-01T00:00:00+00:00"
                row["attempts"] = row["max_attempts"] - 1
                assert outbound_webhooks.process_pending_retries() == 1
        assert fake_db.rows("webhook_deliveries")[0]["status"] == "failed"

    def test_not_subscribed_event_not_delivered(self, app, seed, fake_db, public_dns):
        with app.app_context():
            outbound_webhooks.create_endpoint(TEST_ORG_ID, "https://example.com/hook", ["channel.connected"])
            assert outbound_webhooks.enqueue_event(TEST_ORG_ID, "message.sent", {}) == 0

    def test_other_org_endpoint_not_used(self, app, seed, fake_db, public_dns):
        with app.app_context():
            outbound_webhooks.create_endpoint(OTHER_ORG_ID, "https://example.com/b", ["message.sent"])
            assert outbound_webhooks.enqueue_event(TEST_ORG_ID, "message.sent", {}) == 0
