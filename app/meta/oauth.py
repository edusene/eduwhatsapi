"""Conexão de canais Meta: fluxo OAuth oficial e conexão manual.

Prefixo aplicado pela factory: ``/connect/meta``.

- ``GET /start`` inicia o OAuth (state assinado + nonce de sessão).
- ``GET /callback`` troca o código por access token e registra a conexão
  pendente (o usuário ainda vincula o phone_number_id na tela de canais).
- ``POST /manual`` conecta um número validando token + IDs de verdade na Meta.
- ``POST /<id>/disconnect`` e ``POST /<id>/validate`` gerenciam a conexão.

Tokens da Meta são sempre criptografados (Fernet) antes de persistir e nunca
aparecem em logs ou mensagens ao usuário.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    request,
    session,
    url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Config
from app.meta.adapters import get_adapter
from app.meta.graph_api import MetaAPIError, get_client
from app.services import audit, outbound_webhooks
from app.services.encryption import (
    DecryptionError,
    EncryptionNotConfigured,
    decrypt_token,
    encrypt_token,
)
from app.services.organizations import login_required, org_required, require_role
from app.services.supabase_client import get_service_client

logger = logging.getLogger(__name__)

bp = Blueprint("meta_oauth", __name__)

OAUTH_SCOPES = "whatsapp_business_management,whatsapp_business_messaging"
_STATE_SALT = "meta-oauth-state-v1"
_STATE_MAX_AGE_SECONDS = 600
_META_ID_RE = re.compile(r"\d{5,25}")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_STATE_SALT)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(message: object, limit: int = 200) -> str:
    """Mensagem segura para flash: texto simples, truncado, sem quebras."""
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    return text[:limit]


def _encryption_missing_redirect():
    flash(
        "Criptografia de tokens não configurada: defina TOKEN_ENCRYPTION_KEY "
        "no servidor antes de conectar canais.",
        "error",
    )
    return redirect(url_for("dashboard.channels"))


def _org_and_user() -> tuple[str, str]:
    return g.membership["organization_id"], g.user["id"]


# ---------------------------------------------------------------------------
# OAuth oficial
# ---------------------------------------------------------------------------
@bp.get("/start")
@login_required
@org_required
@require_role("owner", "admin")
def start():
    cfg = Config()
    if not cfg.meta_oauth_configured:
        flash(
            "OAuth da Meta não configurado. Defina META_APP_ID, META_APP_SECRET "
            "e META_REDIRECT_URI no servidor, ou use a conexão manual.",
            "warning",
        )
        return redirect(url_for("dashboard.channels"))

    nonce = secrets.token_urlsafe(16)
    session["meta_oauth_nonce"] = nonce
    state = _serializer().dumps(
        {"org": g.membership["organization_id"], "nonce": nonce}
    )
    params = urlencode({
        "client_id": cfg.META_APP_ID,
        "redirect_uri": cfg.META_REDIRECT_URI,
        "state": state,
        "scope": OAUTH_SCOPES,
        "response_type": "code",
    })
    return redirect(
        f"https://www.facebook.com/{cfg.META_GRAPH_API_VERSION}/dialog/oauth?{params}"
    )


@bp.get("/callback")
@login_required
@org_required
def callback():
    cfg = Config()

    error = request.args.get("error")
    if error:
        reason = request.args.get("error_description") or error
        logger.warning("OAuth Meta recusado: %s", _sanitize(reason))
        flash("A Meta recusou a autorização. Tente novamente.", "error")
        return redirect(url_for("dashboard.channels"))

    state = request.args.get("state", "")
    code = request.args.get("code", "")
    try:
        state_data = _serializer().loads(state, max_age=_STATE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        flash("Autorização inválida ou expirada. Inicie a conexão novamente.", "error")
        return redirect(url_for("dashboard.channels"))

    nonce = session.pop("meta_oauth_nonce", None)  # nonce é de uso único
    org_id, user_id = _org_and_user()
    if (
        not nonce
        or state_data.get("nonce") != nonce
        or state_data.get("org") != org_id
    ):
        flash("Autorização inválida. Inicie a conexão novamente.", "error")
        return redirect(url_for("dashboard.channels"))
    if not code:
        flash("A Meta não retornou o código de autorização.", "error")
        return redirect(url_for("dashboard.channels"))

    client = get_client()
    try:
        # Troca do código: endpoint usa apenas query params (sem bearer token).
        token_data = client.get("/oauth/access_token", token="", params={
            "client_id": cfg.META_APP_ID,
            "client_secret": cfg.META_APP_SECRET,
            "redirect_uri": cfg.META_REDIRECT_URI,
            "code": code,
        })
        access_token = token_data["access_token"]
        # App access token ({app_id}|{app_secret}) só existe no servidor.
        app_token = f"{cfg.META_APP_ID}|{cfg.META_APP_SECRET}"
        debug = client.get("/debug_token", token=app_token,
                           params={"input_token": access_token})
        granted_scopes = (debug.get("data") or {}).get("granted_scopes") or []
    except MetaAPIError as exc:
        logger.warning(
            "Falha na troca/inspeção do token OAuth (http=%s fb_code=%s).",
            exc.http_status, exc.fb_code,
        )
        flash("Não foi possível concluir a autorização com a Meta. Tente novamente.", "error")
        return redirect(url_for("dashboard.channels"))

    try:
        encrypted_token = encrypt_token(access_token)
    except EncryptionNotConfigured:
        return _encryption_missing_redirect()

    record = get_service_client().table("meta_connections").insert({
        "organization_id": org_id,
        "channel": "whatsapp",
        "status": "pending",  # falta vincular o phone_number_id
        "access_token_encrypted": encrypted_token,
        "granted_scopes": granted_scopes,
        "connected_by": user_id,
        "connected_at": _now_iso(),
    }).execute().data[0]
    audit.log_action(org_id, user_id, "channel.oauth_authorized",
                     "meta_connection", record["id"],
                     {"channel": "whatsapp", "granted_scopes": granted_scopes})
    flash(
        "Autorização concluída. Agora vincule o número do WhatsApp na tela de canais.",
        "success",
    )
    return redirect(url_for("dashboard.channels"))


# ---------------------------------------------------------------------------
# Conexão manual (token gerado no painel da Meta)
# ---------------------------------------------------------------------------
@bp.post("/manual")
@login_required
@org_required
@require_role("owner", "admin")
def manual():
    org_id, user_id = _org_and_user()
    token = (request.form.get("access_token") or "").strip()
    waba_id = (request.form.get("waba_id") or "").strip()
    phone_number_id = (request.form.get("phone_number_id") or "").strip()

    if not token:
        flash("Informe o access token gerado no painel da Meta.", "error")
        return redirect(url_for("dashboard.channels"))
    if not _META_ID_RE.fullmatch(waba_id) or not _META_ID_RE.fullmatch(phone_number_id):
        flash("WABA ID e Phone Number ID devem ser numéricos (5 a 25 dígitos).", "error")
        return redirect(url_for("dashboard.channels"))

    # Confirma de verdade na Meta antes de persistir qualquer coisa.
    adapter = get_adapter("whatsapp")(get_client())
    try:
        info = adapter.get_phone_number(phone_number_id, token)
    except MetaAPIError as exc:
        logger.warning(
            "Validação manual recusada pela Meta (http=%s fb_code=%s).",
            exc.http_status, exc.fb_code,
        )
        flash(
            "A Meta não reconheceu o token ou o número informado. "
            "Confira as credenciais e as permissões.",
            "error",
        )
        return redirect(url_for("dashboard.channels"))

    client = get_service_client()
    existing = (
        client.table("meta_connections")
        .select("id")
        .eq("organization_id", org_id)
        .eq("channel", "whatsapp")
        .eq("phone_number_id", phone_number_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        flash("Este número já está conectado nesta organização.", "error")
        return redirect(url_for("dashboard.channels"))

    try:
        encrypted_token = encrypt_token(token)
    except EncryptionNotConfigured:
        return _encryption_missing_redirect()

    now = _now_iso()
    try:
        record = client.table("meta_connections").insert({
            "organization_id": org_id,
            "channel": "whatsapp",
            "waba_id": waba_id,
            "phone_number_id": phone_number_id,
            "display_phone_number": info.get("display_phone_number"),
            "verified_name": info.get("verified_name"),
            "access_token_encrypted": encrypted_token,
            "status": "active",
            "connected_by": user_id,
            "connected_at": now,
            "last_validated_at": now,
        }).execute().data[0]
    except Exception as exc:
        if "duplicate" in str(exc) or "23505" in str(exc):
            flash("Este número já está conectado nesta organização.", "error")
        else:
            logger.exception("Falha ao salvar conexão manual.")
            flash("Não foi possível salvar a conexão. Tente novamente.", "error")
        return redirect(url_for("dashboard.channels"))

    audit.log_action(org_id, user_id, "channel.connected",
                     "meta_connection", record["id"],
                     {"channel": "whatsapp", "phone_number_id": phone_number_id})
    outbound_webhooks.enqueue_event(org_id, "channel.connected", {
        "connection_id": record["id"],
        "channel": "whatsapp",
        "phone_number_id": phone_number_id,
        "display_phone_number": info.get("display_phone_number"),
    })
    flash("Número de WhatsApp conectado com sucesso.", "success")
    return redirect(url_for("dashboard.channels"))


# ---------------------------------------------------------------------------
# Desconexão e revalidação
# ---------------------------------------------------------------------------
def _get_own_connection(connection_id: str) -> dict:
    """Conexão da organização atual ou 404 (sem vazar existência em outra org)."""
    result = (
        get_service_client()
        .table("meta_connections")
        .select("*")
        .eq("id", connection_id)
        .eq("organization_id", g.membership["organization_id"])
        .limit(1)
        .execute()
    )
    if not result.data:
        abort(404)
    return result.data[0]


@bp.post("/<connection_id>/disconnect")
@login_required
@org_required
@require_role("owner", "admin")
def disconnect(connection_id: str):
    org_id, user_id = _org_and_user()
    connection = _get_own_connection(connection_id)
    get_service_client().table("meta_connections").update({
        "status": "disconnected",
        "access_token_encrypted": None,
        "disconnected_at": _now_iso(),
    }).eq("id", connection_id).eq("organization_id", org_id).execute()
    audit.log_action(org_id, user_id, "channel.disconnected",
                     "meta_connection", connection_id,
                     {"channel": connection.get("channel")})
    outbound_webhooks.enqueue_event(org_id, "channel.disconnected", {
        "connection_id": connection_id,
        "channel": connection.get("channel"),
        "phone_number_id": connection.get("phone_number_id"),
    })
    flash("Canal desconectado.", "success")
    return redirect(url_for("dashboard.channels"))


@bp.post("/<connection_id>/validate")
@login_required
@org_required
@require_role("owner", "admin")
def validate(connection_id: str):
    connection = _get_own_connection(connection_id)
    client = get_service_client()

    encrypted = connection.get("access_token_encrypted")
    if not encrypted:
        flash("Esta conexão não tem token armazenado. Reconecte o canal.", "error")
        return redirect(url_for("dashboard.channels"))
    try:
        token = decrypt_token(encrypted)
    except EncryptionNotConfigured:
        return _encryption_missing_redirect()
    except DecryptionError:
        logger.error("Token ilegível na conexão %s (chave incorreta?).", connection_id)
        flash("Não foi possível ler o token armazenado. Reconecte o canal.", "error")
        return redirect(url_for("dashboard.channels"))

    if not connection.get("phone_number_id"):
        flash("Esta conexão ainda não tem número vinculado para validar.", "error")
        return redirect(url_for("dashboard.channels"))

    adapter = get_adapter("whatsapp")(get_client())
    try:
        info = adapter.get_phone_number(connection["phone_number_id"], token)
    except MetaAPIError as exc:
        logger.warning(
            "Revalidação recusada pela Meta (conexão=%s http=%s fb_code=%s).",
            connection_id, exc.http_status, exc.fb_code,
        )
        client.table("meta_connections").update({
            "status": "error",
            "last_validated_at": _now_iso(),
        }).eq("id", connection_id).execute()
        flash(
            "A Meta recusou o token desta conexão. Reconecte o canal com um novo token.",
            "error",
        )
        return redirect(url_for("dashboard.channels"))

    client.table("meta_connections").update({
        "display_phone_number": info.get("display_phone_number"),
        "verified_name": info.get("verified_name"),
        "status": "active",
        "last_validated_at": _now_iso(),
    }).eq("id", connection_id).execute()
    flash("Conexão validada com sucesso na Meta.", "success")
    return redirect(url_for("dashboard.channels"))
