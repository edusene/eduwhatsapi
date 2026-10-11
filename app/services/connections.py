"""Conexões de números do WhatsApp (meta_connections).

Três formas oficiais de conectar:

1. ``embedded_signup`` — o cliente percorre o Embedded Signup da Meta (SDK
   oficial) e devolve um código trocado no servidor por um business token.
   Exige que SEU app seja Tech Provider/Solution Partner com as permissões
   ``whatsapp_business_management`` e ``whatsapp_business_messaging``
   aprovadas e uma configuração de Embedded Signup (META_ES_CONFIG_ID).
2. ``coexistence`` — mesmo fluxo com featureType de coexistência: o número
   continua no app WhatsApp Business do celular e também na Cloud API.
3. ``manual`` — o cliente informa WABA ID, Phone Number ID e um token
   (System User) gerado no app Meta dele ou no seu. Opcionalmente informa o
   App Secret do app dele para que os webhooks enviados por esse app sejam
   aceitos em uma URL exclusiva da conexão.

Nenhuma conexão é marcada como ativa sem confirmar os dados na Graph API.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from flask import current_app

from app.meta.adapters import get_adapter
from app.meta.graph_api import MetaAPIError, get_client
from app.services import audit, outbound_webhooks
from app.services.encryption import DecryptionError, EncryptionNotConfigured, decrypt_token, encrypt_token
from app.services.security import random_token
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

META_ID_RE = re.compile(r"\d{5,25}")
PIN_RE = re.compile(r"\d{6}")
PUBLIC_FIELDS = (
    "id, organization_id, channel, connection_type, waba_id, business_id, phone_number_id,"
    " display_phone_number, verified_name, quality_rating, name_status, status,"
    " webhook_subscribed, registered, last_error, connected_at, last_validated_at,"
    " disconnected_at, created_at, app_secret_encrypted"
)


class ConnectionError_(ValueError):
    """Erro de negócio com mensagem segura para o usuário."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_meta_error(exc: MetaAPIError) -> str:
    text = exc.message or "Erro na Meta."
    if exc.details:
        text = f"{text} — {exc.details}"
    return text[:300]


def public_view(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in row.items()
           if k not in ("access_token_encrypted", "app_secret_encrypted", "webhook_verify_token_encrypted")}
    out["own_app_webhook"] = bool(row.get("app_secret_encrypted"))
    return out


# ---------------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------------
def list_connections(org_id: str, include_disconnected: bool = True) -> list[dict[str, Any]]:
    query = (
        get_service_client().table("meta_connections").select(PUBLIC_FIELDS)
        .eq("organization_id", org_id)
    )
    if not include_disconnected:
        query = query.neq("status", "disconnected")
    return [public_view(r) for r in query.order("created_at", desc=True).execute().data]


def get_connection(org_id: str, connection_id: str) -> dict[str, Any] | None:
    rows = (
        get_service_client().table("meta_connections").select("*")
        .eq("id", connection_id).eq("organization_id", org_id).limit(1).execute().data
    )
    return rows[0] if rows else None


def get_active_by_phone(org_id: str, phone_number_id: str) -> dict[str, Any] | None:
    rows = (
        get_service_client().table("meta_connections").select("*")
        .eq("organization_id", org_id).eq("phone_number_id", phone_number_id)
        .eq("status", "active").limit(1).execute().data
    )
    return rows[0] if rows else None


def find_live_by_phone(phone_number_id: str) -> dict[str, Any] | None:
    """Conexão não-desconectada de um número (para roteamento de webhooks)."""
    rows = (
        get_service_client().table("meta_connections").select("*")
        .eq("phone_number_id", str(phone_number_id)).neq("status", "disconnected")
        .limit(1).execute().data
    )
    return rows[0] if rows else None


def token_for(connection: dict[str, Any]) -> str:
    encrypted = connection.get("access_token_encrypted")
    if not encrypted:
        raise ConnectionError_("Esta conexão não tem token armazenado. Reconecte o número.")
    try:
        return decrypt_token(encrypted)
    except EncryptionNotConfigured as exc:
        raise ConnectionError_("TOKEN_ENCRYPTION_KEY não configurada no servidor.") from exc
    except DecryptionError as exc:
        logger.error("Token ilegível na conexão %s (chave de criptografia trocada?).", connection.get("id"))
        raise ConnectionError_("Não foi possível ler o token salvo. Reconecte o número.") from exc


def webhook_verify_token(connection: dict[str, Any]) -> str | None:
    encrypted = connection.get("webhook_verify_token_encrypted")
    if not encrypted:
        return None
    try:
        return decrypt_token(encrypted)
    except (DecryptionError, EncryptionNotConfigured):
        return None


def app_secret_for(connection: dict[str, Any]) -> str | None:
    encrypted = connection.get("app_secret_encrypted")
    if not encrypted:
        return None
    try:
        return decrypt_token(encrypted)
    except (DecryptionError, EncryptionNotConfigured):
        return None


# ---------------------------------------------------------------------------
# Criação
# ---------------------------------------------------------------------------
def _ensure_not_taken(phone_number_id: str, org_id: str) -> None:
    existing = find_live_by_phone(phone_number_id)
    if existing:
        if existing["organization_id"] == org_id:
            raise ConnectionError_("Este número já está conectado nesta organização.")
        raise ConnectionError_("Este número já está conectado em outra conta da plataforma.")


def _save(org_id: str, user_id: str | None, *, connection_type: str, waba_id: str,
          phone_number_id: str, token: str, info: dict[str, Any], business_id: str | None = None,
          granted_scopes: list[str] | None = None, app_secret: str | None = None,
          registered: bool = False) -> dict[str, Any]:
    try:
        row = {
            "organization_id": org_id,
            "channel": "whatsapp",
            "connection_type": connection_type,
            "waba_id": waba_id,
            "business_id": business_id,
            "phone_number_id": phone_number_id,
            "display_phone_number": info.get("display_phone_number"),
            "verified_name": info.get("verified_name"),
            "quality_rating": info.get("quality_rating"),
            "name_status": info.get("name_status"),
            "access_token_encrypted": encrypt_token(token),
            "granted_scopes": granted_scopes or [],
            "status": "active",
            "registered": registered,
            "connected_by": user_id,
            "connected_at": _now(),
            "last_validated_at": _now(),
        }
        if app_secret:
            row["app_secret_encrypted"] = encrypt_token(app_secret)
            row["webhook_verify_token_encrypted"] = encrypt_token(random_token(24))
    except EncryptionNotConfigured as exc:
        raise ConnectionError_("TOKEN_ENCRYPTION_KEY não configurada no servidor.") from exc
    try:
        record = get_service_client().table("meta_connections").insert(row).execute().data[0]
    except Exception as exc:
        if "duplicate" in str(exc).lower() or "23505" in str(exc):
            raise ConnectionError_("Este número já está conectado na plataforma.") from exc
        raise
    audit.log_action(org_id, user_id, "channel.connected", "meta_connection", record["id"],
                     {"channel": "whatsapp", "type": connection_type, "phone_number_id": phone_number_id})
    outbound_webhooks.enqueue_event(org_id, "channel.connected", {
        "connection_id": record["id"],
        "channel": "whatsapp",
        "phone_number_id": phone_number_id,
        "display_phone_number": info.get("display_phone_number"),
        "verified_name": info.get("verified_name"),
    })
    return record


def connect_manual(org_id: str, user_id: str | None, *, token: str, waba_id: str,
                   phone_number_id: str, app_secret: str | None = None,
                   subscribe: bool = True) -> dict[str, Any]:
    token = (token or "").strip()
    waba_id = (waba_id or "").strip()
    phone_number_id = (phone_number_id or "").strip()
    app_secret = (app_secret or "").strip() or None
    if not token or len(token) > 2048:
        raise ConnectionError_("Informe o token de acesso gerado no painel da Meta.")
    if not META_ID_RE.fullmatch(waba_id) or not META_ID_RE.fullmatch(phone_number_id):
        raise ConnectionError_("WABA ID e Phone Number ID devem conter apenas números.")
    if app_secret and not re.fullmatch(r"[0-9a-f]{32}", app_secret):
        raise ConnectionError_("O App Secret da Meta tem 32 caracteres hexadecimais.")
    _ensure_not_taken(phone_number_id, org_id)

    adapter = get_adapter("whatsapp")
    try:
        numbers = adapter.list_phone_numbers(waba_id, token)
        info = adapter.get_phone_number(phone_number_id, token)
    except MetaAPIError as exc:
        logger.warning("Conexão manual recusada pela Meta (http=%s code=%s).", exc.http_status, exc.fb_code)
        raise ConnectionError_(
            "A Meta não reconheceu o token, a WABA ou o número: " + _sanitize_meta_error(exc)
        ) from exc
    if not any(str(n.get("id")) == phone_number_id for n in numbers):
        raise ConnectionError_("Este Phone Number ID não pertence à WABA informada.")

    record = _save(org_id, user_id, connection_type="manual", waba_id=waba_id,
                   phone_number_id=phone_number_id, token=token, info=info, app_secret=app_secret,
                   registered=True)
    if subscribe:
        subscribe_webhooks(org_id, record["id"], user_id, quiet=True)
    return record


def exchange_embedded_signup_code(code: str) -> tuple[str, list[str]]:
    """Troca o código do Embedded Signup (TTL ~30s) por um business token."""
    cfg = current_app.config
    client = get_client()
    data = client.get("/oauth/access_token", token="", params={
        "client_id": cfg["META_APP_ID"],
        "client_secret": cfg["META_APP_SECRET"],
        "code": code,
    })
    access_token = data.get("access_token") if isinstance(data, dict) else None
    if not access_token:
        raise MetaAPIError("A Meta não retornou o token de acesso.")
    debug = client.get("/debug_token", token=f"{cfg['META_APP_ID']}|{cfg['META_APP_SECRET']}",
                       params={"input_token": access_token})
    info = (debug or {}).get("data") or {}
    if not info.get("is_valid") or str(info.get("app_id")) != str(cfg["META_APP_ID"]):
        raise MetaAPIError("Token retornado pela Meta não é válido para este app.")
    granular = info.get("granular_scopes") or []
    allowed_ids: set[str] = set()
    for scope in granular:
        if scope.get("scope") in ("whatsapp_business_management", "whatsapp_business_messaging"):
            allowed_ids.update(str(t) for t in scope.get("target_ids") or [])
    scopes = list(info.get("scopes") or [])
    return access_token, scopes + [f"target:{t}" for t in sorted(allowed_ids)]


def connect_embedded_signup(org_id: str, user_id: str | None, *, code: str, waba_id: str,
                            phone_number_id: str | None, business_id: str | None,
                            coexistence: bool = False, pin: str | None = None) -> dict[str, Any]:
    if not code or len(code) > 2048:
        raise ConnectionError_("Código de autorização ausente. Refaça o fluxo da Meta.")
    if not META_ID_RE.fullmatch(waba_id or ""):
        raise ConnectionError_("A Meta não retornou a conta do WhatsApp (WABA).")
    if not phone_number_id or not META_ID_RE.fullmatch(phone_number_id):
        raise ConnectionError_("Nenhum número foi selecionado no fluxo da Meta.")
    if business_id and not META_ID_RE.fullmatch(business_id):
        business_id = None
    if pin and not PIN_RE.fullmatch(pin):
        raise ConnectionError_("O PIN de verificação em duas etapas deve ter 6 dígitos.")
    _ensure_not_taken(phone_number_id, org_id)

    adapter = get_adapter("whatsapp")
    try:
        token, scopes = exchange_embedded_signup_code(code)
        targets = {s.split(":", 1)[1] for s in scopes if s.startswith("target:")}
        if targets and waba_id not in targets:
            raise ConnectionError_("A autorização concedida não inclui a WABA selecionada.")
        info = adapter.get_phone_number(phone_number_id, token)
    except MetaAPIError as exc:
        logger.warning("Embedded Signup falhou (http=%s code=%s).", exc.http_status, exc.fb_code)
        raise ConnectionError_("Não foi possível concluir com a Meta: " + _sanitize_meta_error(exc)) from exc

    record = _save(org_id, user_id, connection_type="coexistence" if coexistence else "embedded_signup",
                   waba_id=waba_id, phone_number_id=phone_number_id, token=token, info=info,
                   business_id=business_id, granted_scopes=[s for s in scopes if not s.startswith("target:")],
                   registered=coexistence)
    subscribe_webhooks(org_id, record["id"], user_id, quiet=True)
    if coexistence:
        # Números de coexistência já estão registrados; a Meta pede para iniciar a
        # sincronização de contatos e histórico em até 24h após o onboarding.
        for sync_type in ("smb_app_state_sync", "history"):
            try:
                adapter.request_coexistence_sync(phone_number_id, token, sync_type)
            except MetaAPIError as exc:
                logger.warning("Sync de coexistência %s falhou (code=%s).", sync_type, exc.fb_code)
    elif pin:
        try:
            register_number(org_id, record["id"], pin, user_id)
        except ConnectionError_ as exc:
            _set_error(record["id"], str(exc))
    return record


# ---------------------------------------------------------------------------
# Ações
# ---------------------------------------------------------------------------
def _set_error(connection_id: str, message: str | None) -> None:
    get_service_client().table("meta_connections").update({"last_error": message}).eq(
        "id", connection_id).execute()


def _require(org_id: str, connection_id: str) -> dict[str, Any]:
    connection = get_connection(org_id, connection_id)
    if not connection or connection.get("status") == "disconnected":
        raise ConnectionError_("Conexão não encontrada.")
    return connection


def refresh(org_id: str, connection_id: str, user_id: str | None = None) -> dict[str, Any]:
    """Revalida token + número na Meta e atualiza os dados exibidos."""
    connection = _require(org_id, connection_id)
    token = token_for(connection)
    try:
        info = get_adapter("whatsapp").get_phone_number(connection["phone_number_id"], token)
    except MetaAPIError as exc:
        message = _sanitize_meta_error(exc)
        get_service_client().table("meta_connections").update({
            "status": "error", "last_error": message, "last_validated_at": _now(),
        }).eq("id", connection_id).execute()
        raise ConnectionError_("A Meta recusou o acesso a este número: " + message) from exc
    get_service_client().table("meta_connections").update({
        "display_phone_number": info.get("display_phone_number"),
        "verified_name": info.get("verified_name"),
        "quality_rating": info.get("quality_rating"),
        "name_status": info.get("name_status"),
        "status": "active",
        "last_error": None,
        "last_validated_at": _now(),
    }).eq("id", connection_id).execute()
    return info


def subscribe_webhooks(org_id: str, connection_id: str, user_id: str | None = None,
                       quiet: bool = False) -> bool:
    connection = _require(org_id, connection_id)
    try:
        get_adapter("whatsapp").subscribe_app(connection["waba_id"], token_for(connection))
    except (MetaAPIError, ConnectionError_) as exc:
        message = _sanitize_meta_error(exc) if isinstance(exc, MetaAPIError) else str(exc)
        _set_error(connection_id, "Falha ao inscrever webhooks: " + message)
        if quiet:
            return False
        raise ConnectionError_("Não foi possível inscrever os webhooks: " + message) from exc
    get_service_client().table("meta_connections").update(
        {"webhook_subscribed": True, "last_error": None}).eq("id", connection_id).execute()
    audit.log_action(org_id, user_id, "channel.webhooks_subscribed", "meta_connection", connection_id, {})
    return True


def register_number(org_id: str, connection_id: str, pin: str, user_id: str | None = None) -> None:
    if not PIN_RE.fullmatch(pin or ""):
        raise ConnectionError_("O PIN deve ter exatamente 6 dígitos.")
    connection = _require(org_id, connection_id)
    try:
        get_adapter("whatsapp").register_phone_number(connection["phone_number_id"], token_for(connection), pin)
    except MetaAPIError as exc:
        raise ConnectionError_("A Meta recusou o registro: " + _sanitize_meta_error(exc)) from exc
    get_service_client().table("meta_connections").update(
        {"registered": True, "last_error": None}).eq("id", connection_id).execute()
    audit.log_action(org_id, user_id, "channel.registered", "meta_connection", connection_id, {})


def disconnect(org_id: str, connection_id: str, user_id: str | None = None) -> None:
    """Remove o token, cancela a inscrição de webhooks (best-effort) e avisa o cliente."""
    connection = get_connection(org_id, connection_id)
    if not connection or connection.get("status") == "disconnected":
        raise ConnectionError_("Conexão não encontrada.")
    if connection.get("webhook_subscribed") and connection.get("waba_id"):
        try:
            get_adapter("whatsapp").unsubscribe_app(connection["waba_id"], token_for(connection))
        except (MetaAPIError, ConnectionError_):
            logger.info("Não foi possível cancelar a inscrição de webhooks da conexão %s.", connection_id)
    get_service_client().table("meta_connections").update({
        "status": "disconnected",
        "access_token_encrypted": None,
        "app_secret_encrypted": None,
        "webhook_verify_token_encrypted": None,
        "webhook_subscribed": False,
        "disconnected_at": _now(),
    }).eq("id", connection_id).eq("organization_id", org_id).execute()
    audit.log_action(org_id, user_id, "channel.disconnected", "meta_connection", connection_id,
                     {"phone_number_id": connection.get("phone_number_id")})
    outbound_webhooks.enqueue_event(org_id, "channel.disconnected", {
        "connection_id": connection_id,
        "channel": connection.get("channel"),
        "phone_number_id": connection.get("phone_number_id"),
    })


def mark_offboarded(connection: dict[str, Any], reason: str) -> None:
    """Chamado quando a Meta avisa que o cliente removeu o acesso (PARTNER_REMOVED etc.)."""
    get_service_client().table("meta_connections").update({
        "status": "error", "last_error": reason[:300], "webhook_subscribed": False,
    }).eq("id", connection["id"]).execute()
