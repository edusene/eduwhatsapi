"""Páginas públicas, autenticação, painel, papéis e isolamento entre empresas."""
from __future__ import annotations

import json

import pytest
import responses

from app.services import api_keys, organizations
from tests.conftest import CONN_A_ID, CONN_B_ID, CSRF, GRAPH, OTHER_ORG_ID, PHONE_A, TEST_ORG_ID, TEST_USER_ID, WABA_A


def post(client, url, data=None, **kwargs):
    data = dict(data or {})
    data.setdefault("csrf_token", CSRF)
    return client.post(url, data=data, **kwargs)


def post_json(client, url, body=None):
    return client.post(url, json=body or {}, headers={"X-CSRF-Token": CSRF})


class TestPublic:
    @pytest.mark.parametrize("url", ["/", "/docs", "/termos", "/privacidade", "/login", "/register",
                                     "/forgot-password", "/reset-password", "/auth/callback"])
    def test_public_pages(self, client, url):
        r = client.get(url)
        assert r.status_code == 200, url
        assert b"AutoZap" in r.data

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200 and r.get_json() == {"status": "ok"}

    def test_security_headers(self, client):
        r = client.get("/")
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]
        assert "connect.facebook.net" not in r.headers["Content-Security-Policy"]
        assert r.headers["X-Request-Id"]

    def test_404_html_and_json(self, client):
        assert client.get("/nao-existe").status_code == 404
        r = client.get("/api/v1/nao-existe")
        assert r.status_code == 404 and r.get_json()["error"]["code"] == "not_found"

    def test_protected_redirects_to_login(self, client):
        for url in ("/dashboard/", "/dashboard/channels", "/dashboard/inbox", "/dashboard/api-keys"):
            r = client.get(url)
            assert r.status_code == 302 and "/login" in r.headers["Location"], url


class TestAuthFlows:
    def test_register_creates_org_with_plan(self, client, fake_db):
        r = client.get("/register")
        assert r.status_code == 200
        with client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF
        r = post(client, "/register", {"email": "nova@empresa.com", "password": "senha-muito-forte",
                                       "org_name": "Nova Empresa", "plan": "api", "accept_terms": "1"})
        assert r.status_code == 302
        org = fake_db.rows("organizations")[0]
        assert org["name"] == "Nova Empresa" and org["plan"] == "api"
        assert fake_db.rows("organization_members")[0]["role"] == "owner"

    def test_register_requires_terms_and_strong_password(self, client, fake_db):
        client.get("/register")
        with client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF
        r = post(client, "/register", {"email": "x@y.com", "password": "curta", "org_name": "X"})
        assert r.status_code == 400
        assert not fake_db.rows("organizations")

    def test_register_with_email_confirmation(self, client, fake_db):
        fake_db.auth.confirm_email = True
        with client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF
        r = post(client, "/register", {"email": "conf@empresa.com", "password": "senha-muito-forte",
                                       "org_name": "Conf", "accept_terms": "1"})
        assert r.status_code == 200
        assert "Confirme seu e-mail".encode() in r.data
        with client.session_transaction() as sess:
            assert "user" not in sess

    def test_login_wrong_and_right_password(self, client, fake_db):
        fake_db.auth.users.append({"id": TEST_USER_ID, "email": "a@a.com", "password": "senha-correta-123"})
        with client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF
        assert post(client, "/login", {"email": "a@a.com", "password": "errada"}).status_code == 400
        with client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF
        r = post(client, "/login", {"email": "a@a.com", "password": "senha-correta-123", "next": "//evil.com"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/dashboard/")
        with client.session_transaction() as sess:
            assert sess["user"]["id"] == TEST_USER_ID

    def test_email_callback_session_validates_token(self, client, fake_db):
        fake_db.auth.tokens["jwt-valido"] = {"id": TEST_USER_ID, "email": "a@a.com"}
        with client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF
        bad = post_json(client, "/auth/session", {"access_token": "forjado"})
        assert bad.status_code == 400
        with client.session_transaction() as sess:
            sess["_csrf_token"] = CSRF
        ok = post_json(client, "/auth/session", {"access_token": "jwt-valido"})
        assert ok.status_code == 200 and ok.get_json()["ok"] is True

    def test_csrf_required(self, owner):
        r = owner.post("/dashboard/api-keys", data={"name": "x"})
        assert r.status_code == 403

    def test_logout_requires_post(self, owner):
        assert owner.get("/logout").status_code == 302
        with owner.session_transaction() as sess:
            assert "user" in sess
        post(owner, "/logout")
        with owner.session_transaction() as sess:
            assert "user" not in sess


class TestDashboardPages:
    @pytest.mark.parametrize("url", ["/dashboard/", "/dashboard/channels", f"/dashboard/channels/{CONN_A_ID}",
                                     "/dashboard/api-keys", "/dashboard/webhooks", "/dashboard/logs",
                                     "/dashboard/logs?tab=events", "/dashboard/logs?tab=audit",
                                     "/dashboard/settings", "/dashboard/inbox"])
    @responses.activate
    def test_pages_render(self, owner, url):
        responses.add(responses.GET, f"{GRAPH}/{WABA_A}/message_templates", json={"data": []})
        r = owner.get(url)
        assert r.status_code == 200, url
        assert b"Org A" in r.data or b"AutoZap" in r.data

    def test_logs_paginate(self, owner, fake_db):
        for i in range(30):
            fake_db.insert_row("audit_logs", {"organization_id": TEST_ORG_ID, "action": f"acao.{i:02d}",
                                              "created_at": f"2026-10-01T12:{i:02d}:00+00:00"})
        first = owner.get("/dashboard/logs?tab=audit").data.decode()
        assert "acao.29" in first and "acao.04" not in first and "page=2" in first
        second = owner.get("/dashboard/logs?tab=audit&page=2").data.decode()
        assert "acao.04" in second and "acao.29" not in second and "Anterior" in second
        assert owner.get("/dashboard/logs?tab=audit&page=abc").status_code == 200

    def test_inbox_filters_by_number(self, owner, fake_db):
        other = fake_db.insert_row("meta_connections", {"organization_id": TEST_ORG_ID, "waba_id": WABA_A,
                                                        "phone_number_id": "333333333333333", "status": "active"})
        for conn_id, wa_id in ((CONN_A_ID, "5511900000001"), (other["id"], "5511900000002")):
            contact = fake_db.insert_row("contacts", {"organization_id": TEST_ORG_ID, "wa_id": wa_id})
            fake_db.insert_row("conversations", {"organization_id": TEST_ORG_ID, "connection_id": conn_id,
                                                 "contact_id": contact["id"], "status": "open"})
        listed = owner.get(f"/dashboard/inbox/api/conversations?status=open&connection_id={CONN_A_ID}").get_json()
        assert [c["contact"]["wa_id"] for c in listed["conversations"]] == ["5511900000001"]
        everything = owner.get("/dashboard/inbox/api/conversations?status=open&connection_id=lixo").get_json()
        assert len(everything["conversations"]) == 2

    def test_channels_page_allows_facebook_sdk_only_there(self, owner):
        r = owner.get("/dashboard/channels")
        assert "connect.facebook.net" in r.headers["Content-Security-Policy"]

    def test_overview_shows_zero_not_fake_metrics(self, owner):
        r = owner.get("/dashboard/")
        assert b"Primeiros passos" in r.data

    def test_inbox_disabled_for_api_plan(self, owner, fake_db):
        fake_db.rows("organizations")[0]["plan"] = "api"
        r = owner.get("/dashboard/inbox")
        assert "plano API Oficial".encode() in r.data
        assert owner.get("/dashboard/inbox/api/conversations").status_code == 403


class TestRolesAndIsolation:
    def test_viewer_cannot_create_key(self, viewer, fake_db):
        r = post(viewer, "/dashboard/api-keys", {"name": "x", "environment": "live"})
        assert r.status_code == 403
        assert not fake_db.rows("api_keys")

    def test_viewer_cannot_send_from_inbox(self, viewer):
        r = post_json(viewer, "/dashboard/inbox/api/conversations/new", {"connection_id": CONN_A_ID})
        assert r.status_code == 403

    def test_other_org_channel_is_404(self, owner):
        assert owner.get(f"/dashboard/channels/{CONN_B_ID}").status_code == 404

    def test_cannot_disconnect_other_org_channel(self, owner, fake_db):
        post(owner, f"/connect/meta/{CONN_B_ID}/disconnect")
        conn_b = [c for c in fake_db.rows("meta_connections") if c["id"] == CONN_B_ID][0]
        assert conn_b["status"] == "active" and conn_b["access_token_encrypted"]

    def test_cannot_revoke_other_org_key(self, owner, app):
        with app.app_context():
            other = api_keys.create_api_key(OTHER_ORG_ID, "B")
        post(owner, f"/dashboard/api-keys/{other['id']}/revoke")
        with app.app_context():
            assert api_keys.verify_api_key(other["key"]) is not None

    def test_cannot_read_other_org_conversation(self, owner, fake_db):
        contact = fake_db.insert_row("contacts", {"organization_id": OTHER_ORG_ID, "wa_id": "5511999990000"})
        conv = fake_db.insert_row("conversations", {"organization_id": OTHER_ORG_ID, "connection_id": CONN_B_ID,
                                                    "contact_id": contact["id"]})
        assert owner.get(f"/dashboard/inbox/api/conversations/{conv['id']}/messages").status_code == 404

    def test_switch_org_only_to_own_memberships(self, owner):
        post(owner, "/switch-org", {"organization_id": OTHER_ORG_ID})
        r = owner.get("/dashboard/settings")
        assert b"Org A" in r.data and b"Org B" not in r.data


class TestDashboardActions:
    def test_create_key_shows_secret_once(self, owner, fake_db):
        r = post(owner, "/dashboard/api-keys", {"name": "n8n", "environment": "live",
                                                "scopes": ["messages:send", "channels:read"]})
        assert r.status_code == 200
        row = fake_db.rows("api_keys")[0]
        assert row["key_prefix"].encode() in r.data
        assert row["scopes"] == ["channels:read", "messages:send"]
        listing = owner.get("/dashboard/api-keys").data
        assert row["key_hash"].encode() not in listing

    @responses.activate
    def test_test_message_from_channel_page(self, owner, fake_db):
        responses.add(responses.POST, f"{GRAPH}/{PHONE_A}/messages", json={"messages": [{"id": "wamid.DASH"}]})
        r = post(owner, f"/dashboard/channels/{CONN_A_ID}/test-message",
                 {"to": "5511999998888", "kind": "text", "body": "teste"})
        assert r.status_code == 302
        assert fake_db.rows("messages")[-1]["source"] == "dashboard"

    @responses.activate
    def test_inbox_send_respects_24h_window(self, owner, fake_db):
        contact = fake_db.insert_row("contacts", {"organization_id": TEST_ORG_ID, "wa_id": "5511988887777"})
        conv = fake_db.insert_row("conversations", {"organization_id": TEST_ORG_ID, "connection_id": CONN_A_ID,
                                                    "contact_id": contact["id"], "last_inbound_at": None})
        r = post_json(owner, f"/dashboard/inbox/api/conversations/{conv['id']}/messages", {"type": "text", "body": "oi"})
        assert r.status_code == 409 and r.get_json()["code"] == "window_closed"
        from datetime import datetime, timezone
        conv_row = fake_db.rows("conversations")[0]
        conv_row["last_inbound_at"] = datetime.now(timezone.utc).isoformat()
        responses.add(responses.POST, f"{GRAPH}/{PHONE_A}/messages", json={"messages": [{"id": "wamid.INBOX"}]})
        r = post_json(owner, f"/dashboard/inbox/api/conversations/{conv['id']}/messages", {"type": "text", "body": "oi"})
        assert r.status_code == 200 and r.get_json()["message"]["wamid"] == "wamid.INBOX"
        listed = owner.get("/dashboard/inbox/api/conversations?status=open").get_json()["conversations"]
        assert listed[0]["last_message_preview"] == "oi"

    def test_invitation_flow(self, owner, client, app, fake_db):
        r = post(owner, "/dashboard/settings/invitations", {"email": "novo@a.com", "role": "developer"})
        assert r.status_code == 200
        token = r.data.decode().split("/invite/")[1].split("<")[0].strip()
        with app.test_request_context():
            assert organizations.find_invitation(token) is not None
        # Outra pessoa (e-mail diferente) não consegue aceitar
        other = app.test_client()
        with other.session_transaction() as sess:
            sess["user"] = {"id": "00000000-0000-4000-8000-000000000009", "email": "intruso@x.com"}
            sess["_csrf_token"] = CSRF
        post(other, f"/invite/{token}")
        assert not [m for m in fake_db.rows("organization_members") if m["user_id"].endswith("09")
                    and m["organization_id"] == TEST_ORG_ID]
        invited = app.test_client()
        with invited.session_transaction() as sess:
            sess["user"] = {"id": "00000000-0000-4000-8000-000000000010", "email": "novo@a.com"}
            sess["_csrf_token"] = CSRF
        post(invited, f"/invite/{token}")
        member = [m for m in fake_db.rows("organization_members") if m["user_id"].endswith("10")]
        assert member and member[0]["role"] == "developer" and member[0]["organization_id"] == TEST_ORG_ID
        with app.test_request_context():
            assert organizations.find_invitation(token) is None  # uso único

    def test_purge_messages_requires_confirmation(self, owner, fake_db):
        fake_db.insert_row("messages", {"organization_id": TEST_ORG_ID, "direction": "inbound",
                                        "contact_wa_id": "5511", "type": "text"})
        fake_db.insert_row("messages", {"organization_id": OTHER_ORG_ID, "direction": "inbound",
                                        "contact_wa_id": "5511", "type": "text"})
        post(owner, "/dashboard/settings/purge-messages", {"confirm": "nao"})
        assert len(fake_db.rows("messages")) == 2
        post(owner, "/dashboard/settings/purge-messages", {"confirm": "APAGAR"})
        assert [m["organization_id"] for m in fake_db.rows("messages")] == [OTHER_ORG_ID]

    def test_change_plan(self, owner, fake_db):
        post(owner, "/dashboard/settings/organization", {"name": "Org A", "plan": "api",
                                                         "message_retention_days": "30"})
        org = fake_db.rows("organizations")[0]
        assert org["plan"] == "api" and org["message_retention_days"] == 30
        assert json.dumps(fake_db.rows("audit_logs"))
