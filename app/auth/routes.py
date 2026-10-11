"""Autenticação (Supabase Auth): cadastro, login, confirmação de e-mail,
recuperação de senha, convites e troca de organização.

A senha nunca passa pelo nosso banco: o Supabase Auth valida as credenciais.
Tokens recebidos no navegador (confirmação/recuperação) são validados no
servidor com ``auth.get_user`` antes de criar a sessão.
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from app.extensions import limiter
from app.services import organizations, security
from app.services.supabase_client import SupabaseNotConfigured, create_user_client, get_auth_client

logger = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)
MIN_PASSWORD = 10


def _safe_next(next_url: str | None) -> str:
    if next_url and next_url.startswith("/") and not next_url.startswith("//") and "\\" not in next_url:
        return next_url
    return url_for("dashboard.overview")


def _auth_client():
    try:
        return get_auth_client()
    except SupabaseNotConfigured:
        return None


def _start_session(user_id: str, email: str | None) -> None:
    """Nova sessão (evita fixação de sessão) com token CSRF novo."""
    session.clear()
    session.permanent = True
    session["user"] = {"id": str(user_id), "email": (email or "").lower()}
    security.generate_csrf_token()


def _redirect_base() -> str:
    return current_app.config["APP_BASE_URL"].rstrip("/")


@bp.get("/login")
def login():
    if g.get("user"):
        return redirect(url_for("dashboard.overview"))
    return render_template("auth/login.html")


@bp.post("/login")
@limiter.limit("10 per minute")
def login_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        flash("Informe e-mail e senha.", "error")
        return render_template("auth/login.html", email=email), 400
    client = _auth_client()
    if client is None:
        flash("Autenticação indisponível: configure SUPABASE_URL e SUPABASE_ANON_KEY.", "error")
        return render_template("auth/login.html", email=email), 503
    try:
        user = client.auth.sign_in_with_password({"email": email, "password": password}).user
    except Exception as exc:
        logger.info("Login recusado (%s).", type(exc).__name__)
        if "not confirmed" in str(exc).lower():
            flash("Confirme seu e-mail antes de entrar. Verifique sua caixa de entrada.", "warning")
        else:
            flash("E-mail ou senha incorretos.", "error")
        return render_template("auth/login.html", email=email), 400
    _start_session(user.id, user.email)
    return redirect(_safe_next(request.form.get("next")))


@bp.get("/register")
def register():
    if g.get("user"):
        return redirect(url_for("dashboard.overview"))
    plan = request.args.get("plan")
    return render_template("auth/register.html", plans=organizations.PLANS,
                           selected_plan=plan if plan in organizations.PLANS else "api_chat",
                           invite=request.args.get("invite", ""))


@bp.post("/register")
@limiter.limit("5 per minute")
def register_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    org_name = request.form.get("org_name", "").strip()
    plan = request.form.get("plan", "api_chat")
    invite = request.form.get("invite", "").strip()
    ctx = {"plans": organizations.PLANS, "selected_plan": plan if plan in organizations.PLANS else "api_chat",
           "email": email, "org_name": org_name, "invite": invite}

    errors = []
    if not organizations.valid_email(email):
        errors.append("Informe um e-mail válido.")
    if len(password) < MIN_PASSWORD:
        errors.append(f"A senha deve ter pelo menos {MIN_PASSWORD} caracteres.")
    if not invite and (not org_name or len(org_name) > 120):
        errors.append("Informe o nome da empresa (até 120 caracteres).")
    if not request.form.get("accept_terms"):
        errors.append("É preciso aceitar os Termos de Uso e a Política de Privacidade.")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("auth/register.html", **ctx), 400

    client = _auth_client()
    if client is None:
        flash("Autenticação indisponível: configure SUPABASE_URL e SUPABASE_ANON_KEY.", "error")
        return render_template("auth/register.html", **ctx), 503
    try:
        resp = client.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"email_redirect_to": f"{_redirect_base()}/auth/callback"},
        })
        user = resp.user
        has_session = bool(resp.session and resp.session.access_token)
    except Exception as exc:
        msg = str(exc).lower()
        logger.info("Cadastro recusado (%s).", type(exc).__name__)
        if "already" in msg or "exists" in msg:
            flash("Este e-mail já tem conta. Entre ou recupere a senha.", "error")
        elif "password" in msg:
            flash("Senha recusada: use uma senha mais forte.", "error")
        elif "rate" in msg:
            flash("Muitas tentativas. Aguarde alguns minutos.", "error")
        else:
            flash("Não foi possível criar a conta agora. Tente novamente.", "error")
        return render_template("auth/register.html", **ctx), 400
    if not user:
        flash("Não foi possível criar a conta agora. Tente novamente.", "error")
        return render_template("auth/register.html", **ctx), 400

    if not invite:
        try:
            organizations.create_organization_for_user(str(user.id), org_name, plan, email=email)
        except Exception:
            logger.exception("Falha ao criar organização no cadastro.")
            flash("Conta criada, mas a empresa não pôde ser configurada agora. "
                  "Ela será criada no primeiro acesso.", "warning")

    if has_session:
        _start_session(user.id, user.email)
        if invite:
            return redirect(url_for("auth.invite", token=invite))
        flash("Conta criada! Vamos conectar seu primeiro número.", "success")
        return redirect(url_for("dashboard.overview"))
    if invite:
        session["pending_invite"] = invite
    return render_template("auth/check_email.html", email=email)


@bp.get("/auth/callback")
def auth_callback():
    """Destino dos links de e-mail do Supabase (tokens chegam no fragmento #)."""
    return render_template("auth/callback.html")


@bp.post("/auth/session")
@limiter.limit("10 per minute")
def auth_session():
    """Recebe o access_token do link de confirmação e valida no Supabase."""
    data = request.get_json(silent=True) or {}
    token = str(data.get("access_token") or "")
    if not token or len(token) > 4096:
        return jsonify({"ok": False, "message": "Link inválido ou expirado."}), 400
    client = _auth_client()
    if client is None:
        return jsonify({"ok": False, "message": "Autenticação indisponível."}), 503
    try:
        user = client.auth.get_user(token).user
    except Exception:
        return jsonify({"ok": False, "message": "Link inválido ou expirado. Faça login."}), 400
    if not user:
        return jsonify({"ok": False, "message": "Link inválido ou expirado. Faça login."}), 400
    pending_invite = session.get("pending_invite")
    _start_session(user.id, user.email)
    if pending_invite:
        return jsonify({"ok": True, "redirect": url_for("auth.invite", token=pending_invite)})
    flash("E-mail confirmado. Bem-vindo!", "success")
    return jsonify({"ok": True, "redirect": url_for("dashboard.overview")})


@bp.post("/logout")
def logout():
    session.clear()
    flash("Você saiu da sua conta.", "success")
    return redirect(url_for("auth.login"))


@bp.get("/logout")
def logout_get():
    # Logout exige POST com CSRF; GET apenas volta.
    return redirect(url_for("dashboard.overview") if g.get("user") else url_for("auth.login"))


@bp.get("/setup-problem")
def setup_problem():
    if not g.get("user"):
        return redirect(url_for("auth.login"))
    return render_template("auth/setup_problem.html")


@bp.get("/forgot-password")
def forgot_password():
    return render_template("auth/forgot_password.html")


@bp.post("/forgot-password")
@limiter.limit("5 per minute")
def forgot_password_post():
    email = request.form.get("email", "").strip().lower()
    if not organizations.valid_email(email):
        flash("Informe um e-mail válido.", "error")
        return render_template("auth/forgot_password.html"), 400
    client = _auth_client()
    if client is None:
        flash("Autenticação indisponível.", "error")
        return render_template("auth/forgot_password.html"), 503
    try:
        client.auth.reset_password_for_email(email, {"redirect_to": f"{_redirect_base()}/reset-password"})
    except Exception as exc:
        logger.info("Falha ao solicitar recuperação (%s).", type(exc).__name__)
    # Mesma resposta sempre: não revela se o e-mail existe.
    flash("Se o e-mail estiver cadastrado, você receberá um link para redefinir a senha.", "info")
    return redirect(url_for("auth.login"))


@bp.get("/reset-password")
def reset_password():
    return render_template("auth/reset_password.html")


@bp.post("/reset-password")
@limiter.limit("5 per minute")
def reset_password_post():
    token = request.form.get("access_token", "").strip()
    new_password = request.form.get("new_password", "")
    if not token:
        flash("Link de recuperação ausente ou expirado. Solicite outro.", "error")
        return render_template("auth/reset_password.html"), 400
    if len(new_password) < MIN_PASSWORD:
        flash(f"A nova senha deve ter pelo menos {MIN_PASSWORD} caracteres.", "error")
        return render_template("auth/reset_password.html"), 400
    if new_password != request.form.get("confirm_password", ""):
        flash("As senhas não coincidem.", "error")
        return render_template("auth/reset_password.html"), 400
    try:
        create_user_client(token).auth.update_user({"password": new_password})
    except Exception as exc:
        logger.info("Falha ao redefinir senha (%s).", type(exc).__name__)
        flash("Não foi possível alterar a senha. O link pode ter expirado.", "error")
        return render_template("auth/reset_password.html"), 400
    flash("Senha alterada. Entre com a nova senha.", "success")
    return redirect(url_for("auth.login"))


@bp.get("/invite/<token>")
def invite(token: str):
    inv = organizations.find_invitation(token)
    if not inv:
        return render_template("auth/invite.html", invalid=True), 404
    return render_template("auth/invite.html", inv=inv, token=token)


@bp.post("/invite/<token>")
@limiter.limit("10 per minute")
def invite_accept(token: str):
    if not g.get("user"):
        return redirect(url_for("auth.login", next=url_for("auth.invite", token=token)))
    try:
        inv = organizations.accept_invitation(token, g.user)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("auth.invite", token=token))
    session.pop("pending_invite", None)
    session[organizations.SESSION_ORG_KEY] = inv["organization_id"]
    flash(f"Você agora faz parte de {inv.get('org_name') or 'uma nova organização'}.", "success")
    return redirect(url_for("dashboard.overview"))


@bp.post("/switch-org")
def switch_org():
    org_id = request.form.get("organization_id", "")
    if g.get("user") and any(m["organization_id"] == org_id for m in g.get("memberships") or []):
        session[organizations.SESSION_ORG_KEY] = org_id
    return redirect(url_for("dashboard.overview"))
