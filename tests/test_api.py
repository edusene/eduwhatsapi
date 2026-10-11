"""API pública /api/v1: autenticação por chave, escopos, validação, envio
(com mocks HTTP da Meta), idempotência, modo de teste e isolamento."""
from __future__ import annotations

import json

import pytest
import requests
import responses

from tests.conftest import GRAPH, OTHER_ORG_ID, PHONE_A, PHONE_B, TEST_ORG_ID, WABA_A, auth

SEND_URL = f"/api/v1/whatsapp/{PHONE_A}/messages"
META_SEND = f"{GRAPH}/{PHONE_A}/messages"


def _meta_ok(wamid: str = "wamid.ABC123"):
    responses.add(responses.POST, META_SEND, json={
        "messaging_product": "whatsapp", "contacts": [{"input": "5511999998888", "wa_id": "5511999998888"}],
        "messages": [{"id": wamid}]}, status=200)


class TestAuthentication:
    def test_missing_key(self, client, seed):
        r = client.get("/api/v1/me")
        assert r.status_code == 401
        body = r.get_json()
        assert body["error"]["code"] == "missing_api_key"
        assert body["error"]["request_id"]

    def test_invalid_key(self, client, seed):
        r = client.get("/api/v1/me", headers=auth("sk_live_invalida"))
        assert r.status_code == 401
        assert r.get_json()["error"]["code"] == "invalid_api_key"

    def test_me(self, client, make_key):
        key = make_key()
        r = client.get("/api/v1/me", headers=auth(key))
        assert r.status_code == 200
        body = r.get_json()
        assert body["organization"]["id"] == TEST_ORG_ID
        assert body["organization"]["plan"] == "api_chat"
        assert "key_hash" not in json.dumps(body)

    def test_insufficient_scope(self, client, make_key):
        key = make_key(scopes=["usage:read"])
        r = client.post(SEND_URL, json={"to": "5511999998888", "type": "text", "text": {"body": "oi"}},
                        headers=auth(key))
        assert r.status_code == 403
        assert r.get_json()["error"]["code"] == "insufficient_scope"

    def test_request_is_logged_without_secrets(self, client, make_key, fake_db):
        key = make_key()
        client.get("/api/v1/me", headers=auth(key))
        logs = fake_db.rows("api_request_logs")
        assert logs and logs[-1]["organization_id"] == TEST_ORG_ID
        assert key not in json.dumps(logs)


class TestTenantIsolation:
    def test_channels_only_own_org(self, client, make_key):
        r = client.get("/api/v1/channels", headers=auth(make_key()))
        ids = [c["phone_number_id"] for c in r.get_json()["data"]]
        assert ids == [PHONE_A]
        assert "access_token_encrypted" not in json.dumps(r.get_json())

    def test_cannot_send_with_other_org_phone(self, client, make_key):
        r = client.post(f"/api/v1/whatsapp/{PHONE_B}/messages",
                        json={"to": "5511999998888", "type": "text", "text": {"body": "x"}},
                        headers=auth(make_key()))
        assert r.status_code == 404
        assert r.get_json()["error"]["code"] == "channel_not_found"

    def test_cannot_read_other_org_message(self, client, make_key, fake_db):
        msg = fake_db.insert_row("messages", {"organization_id": OTHER_ORG_ID, "direction": "outbound",
                                              "contact_wa_id": "5511", "type": "text", "wamid": "wamid.B"})
        r = client.get(f"/api/v1/messages/{msg['id']}", headers=auth(make_key()))
        assert r.status_code == 404


class TestValidation:
    @pytest.mark.parametrize("payload,code", [
        ({"to": "abc", "type": "text", "text": {"body": "x"}}, "invalid_recipient"),
        ({"to": "5511999998888", "type": "carrier_pigeon"}, "unsupported_message_type"),
        ({"to": "5511999998888", "type": "text", "text": {"body": ""}}, "invalid_field"),
        ({"to": "5511999998888", "type": "text", "text": {"body": "x" * 4097}}, "invalid_field"),
        ({"to": "5511999998888", "type": "image", "image": {"link": "http://inseguro.com/a.jpg"}}, "invalid_field"),
        ({"to": "5511999998888", "type": "image", "image": {"id": "1", "link": "https://a.com/x.jpg"}}, "invalid_field"),
        ({"to": "5511999998888", "type": "template", "template": {"name": "Nome Inválido", "language": {"code": "pt_BR"}}}, "invalid_field"),
        ({"to": "5511999998888", "type": "location", "location": {"latitude": 200, "longitude": 0}}, "invalid_field"),
    ])
    def test_invalid_payloads(self, client, make_key, payload, code):
        r = client.post(SEND_URL, json=payload, headers=auth(make_key()))
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == code

    def test_invalid_json(self, client, make_key):
        r = client.post(SEND_URL, data="nao-json", headers={**auth(make_key()), "Content-Type": "application/json"})
        assert r.status_code == 400
        assert r.get_json()["error"]["code"] == "invalid_json"

    def test_bad_phone_id(self, client, make_key):
        r = client.post("/api/v1/whatsapp/abc/messages", json={}, headers=auth(make_key()))
        assert r.status_code == 400


class TestSendMessage:
    @responses.activate
    def test_send_text_success(self, client, make_key, fake_db):
        _meta_ok("wamid.OK1")
        r = client.post(SEND_URL, json={"to": "+55 (11) 99999-8888", "type": "text", "text": {"body": "Olá"}},
                        headers=auth(make_key()))
        assert r.status_code == 201, r.get_json()
        data = r.get_json()["data"]
        assert data["wamid"] == "wamid.OK1" and data["status"] == "accepted"
        sent = json.loads(responses.calls[0].request.body)
        assert sent["messaging_product"] == "whatsapp"
        assert sent["to"] == "5511999998888"
        assert responses.calls[0].request.headers["Authorization"] == "Bearer EAA-token-org-a"
        stored = fake_db.rows("messages")[-1]
        assert stored["content"] == {"body": "Olá", "preview_url": False}  # plano api_chat guarda conteúdo
        assert fake_db.rows("conversations"), "plano api_chat cria conversa"

    @responses.activate
    def test_send_template(self, client, make_key):
        _meta_ok()
        payload = {"to": "5511999998888", "type": "template", "template": {
            "name": "pedido_enviado", "language": {"code": "pt_BR"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": "Ana"}]}]}}
        r = client.post(SEND_URL, json=payload, headers=auth(make_key()))
        assert r.status_code == 201
        sent = json.loads(responses.calls[0].request.body)
        assert sent["template"]["name"] == "pedido_enviado"

    @responses.activate
    def test_meta_error_preserved(self, client, make_key, fake_db):
        responses.add(responses.POST, META_SEND, status=400, json={"error": {
            "message": "Re-engagement message", "type": "OAuthException", "code": 131047,
            "error_data": {"details": "More than 24 hours have passed"}, "fbtrace_id": "TRACE1"}})
        r = client.post(SEND_URL, json={"to": "5511999998888", "type": "text", "text": {"body": "x"}},
                        headers=auth(make_key()))
        assert r.status_code == 502
        err = r.get_json()["error"]
        assert err["meta_error"]["code"] == 131047
        assert err["meta_error"]["fbtrace_id"] == "TRACE1"
        assert "EAA-token" not in json.dumps(r.get_json())
        assert fake_db.rows("messages")[-1]["status"] == "failed"

    @responses.activate
    def test_meta_timeout(self, client, make_key):
        responses.add(responses.POST, META_SEND, body=requests.Timeout())
        r = client.post(SEND_URL, json={"to": "5511999998888", "type": "text", "text": {"body": "x"}},
                        headers=auth(make_key()))
        assert r.status_code == 504
        assert r.get_json()["error"]["code"] == "meta_timeout"
        assert len(responses.calls) == 1  # POST nunca é repetido automaticamente

    @responses.activate
    def test_test_mode_does_not_call_meta(self, client, make_key, fake_db):
        r = client.post(SEND_URL, json={"to": "5511999998888", "type": "text", "text": {"body": "x"}},
                        headers=auth(make_key(environment="test")))
        assert r.status_code == 201
        body = r.get_json()
        assert body["test_mode"] is True
        assert body["data"]["wamid"].startswith("wamid.TEST.")
        assert len(responses.calls) == 0
        assert fake_db.rows("messages")[-1]["status"] == "test"

    @responses.activate
    def test_api_plan_does_not_store_content(self, client, make_key, fake_db):
        responses.add(responses.POST, f"{GRAPH}/{PHONE_B}/messages", json={"messages": [{"id": "wamid.B1"}]})
        r = client.post(f"/api/v1/whatsapp/{PHONE_B}/messages",
                        json={"to": "5511999998888", "type": "text", "text": {"body": "segredo"}},
                        headers=auth(make_key(org_id=OTHER_ORG_ID)))
        assert r.status_code == 201
        stored = fake_db.rows("messages")[-1]
        assert stored["content"] is None
        assert not fake_db.rows("conversations")


class TestIdempotency:
    @responses.activate
    def test_replay_returns_same_response(self, client, make_key):
        _meta_ok("wamid.IDEM")
        key = make_key()
        headers = {**auth(key), "Idempotency-Key": "pedido-123-aviso"}
        body = {"to": "5511999998888", "type": "text", "text": {"body": "x"}}
        r1 = client.post(SEND_URL, json=body, headers=headers)
        r2 = client.post(SEND_URL, json=body, headers=headers)
        assert r1.status_code == r2.status_code == 201
        assert r2.headers.get("Idempotent-Replayed") == "true"
        assert r1.get_json()["data"]["wamid"] == r2.get_json()["data"]["wamid"]
        assert len(responses.calls) == 1

    def test_in_progress_conflict(self, client, make_key, fake_db):
        fake_db.insert_row("idempotency_keys", {"organization_id": TEST_ORG_ID, "key": "chave-em-andamento",
                                                "endpoint": f"POST /whatsapp/{PHONE_A}/messages",
                                                "status": "processing"})
        r = client.post(SEND_URL, json={"to": "5511999998888", "type": "text", "text": {"body": "x"}},
                        headers={**auth(make_key()), "Idempotency-Key": "chave-em-andamento"})
        assert r.status_code == 409

    @responses.activate
    def test_failure_releases_key(self, client, make_key, fake_db):
        responses.add(responses.POST, META_SEND, status=500, json={"error": {"message": "boom", "code": 1}})
        r = client.post(SEND_URL, json={"to": "5511999998888", "type": "text", "text": {"body": "x"}},
                        headers={**auth(make_key()), "Idempotency-Key": "vai-falhar-123"})
        assert r.status_code == 502
        assert not fake_db.rows("idempotency_keys")

    def test_invalid_key_length(self, client, make_key):
        r = client.post(SEND_URL, json={"to": "5511999998888", "type": "text", "text": {"body": "x"}},
                        headers={**auth(make_key()), "Idempotency-Key": "curta"})
        assert r.status_code == 400


class TestOtherEndpoints:
    @responses.activate
    def test_templates(self, client, make_key):
        responses.add(responses.GET, f"{GRAPH}/{WABA_A}/message_templates", json={"data": [
            {"name": "boas_vindas", "status": "APPROVED", "language": "pt_BR"},
            {"name": "promo", "status": "REJECTED", "language": "pt_BR"}]})
        r = client.get(f"/api/v1/whatsapp/{PHONE_A}/templates?status=approved", headers=auth(make_key()))
        assert r.status_code == 200
        assert [t["name"] for t in r.get_json()["data"]] == ["boas_vindas"]

    @responses.activate
    def test_mark_read(self, client, make_key):
        responses.add(responses.POST, META_SEND, json={"success": True})
        r = client.post(f"/api/v1/whatsapp/{PHONE_A}/messages/read", json={"message_id": "wamid.X"},
                        headers=auth(make_key()))
        assert r.status_code == 200
        assert json.loads(responses.calls[0].request.body)["status"] == "read"

    @responses.activate
    def test_media_download_rejects_untrusted_host(self, client, make_key):
        responses.add(responses.GET, f"{GRAPH}/123456789", json={"url": "https://evil.example.com/x", "mime_type": "image/jpeg"})
        r = client.get(f"/api/v1/whatsapp/{PHONE_A}/media/123456789", headers=auth(make_key()))
        assert r.status_code == 502
        assert len(responses.calls) == 1  # token nunca enviado ao host não confiável

    @responses.activate
    def test_media_download_ok(self, client, make_key):
        responses.add(responses.GET, f"{GRAPH}/123456789",
                      json={"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1", "mime_type": "image/jpeg"})
        responses.add(responses.GET, "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1",
                      body=b"\xff\xd8bytes", content_type="image/jpeg")
        r = client.get(f"/api/v1/whatsapp/{PHONE_A}/media/123456789", headers=auth(make_key()))
        assert r.status_code == 200
        assert r.data == b"\xff\xd8bytes"
        assert r.mimetype == "image/jpeg"

    def test_conversations_require_chat_plan(self, client, make_key):
        r = client.get("/api/v1/conversations", headers=auth(make_key(org_id=OTHER_ORG_ID)))
        assert r.status_code == 403
        assert r.get_json()["error"]["code"] == "plan_required"

    def test_usage_counts_real_records(self, client, make_key, fake_db):
        fake_db.insert_row("messages", {"organization_id": TEST_ORG_ID, "direction": "outbound",
                                        "contact_wa_id": "5511", "type": "text", "status": "sent"})
        r = client.get("/api/v1/usage", headers=auth(make_key()))
        data = r.get_json()["data"]
        assert data["messages_sent"] == 1
        assert data["messages_received"] == 0

    def test_openapi_public(self, client):
        r = client.get("/api/v1/openapi.yaml")
        assert r.status_code == 200
        assert b"openapi:" in r.data
