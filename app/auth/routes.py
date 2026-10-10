"""Rotas de autenticação (Supabase Auth) e páginas legais."""
from __future__ import annotations

import re

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app.extensions import limiter
from app.services import organizations, security
from app.services.supabase_client import create_user_client, get_auth_client

bp = Blueprint("auth", __name__)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_next(next_url: str | None) -> str:
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return url_for("dashboard.overview")


@bp.get("/login")
def login():
    if organizations.get_current_user():
        return redirect(url_for("dashboard.overview"))
    return render_template("auth/login.html")


@bp.post("/login")
@limiter.limit("10 per minute")
def login_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        flash("E-mail e senha são obrigatórios.", "error")
        return render_template("auth/login.html"), 400
    try:
        resp = get_auth_client().auth.sign_in_with_password(
            {"email": email, "password": password}
        )
        user = resp.user
        access_token = resp.session.access_token if resp.session else None
    except Exception as exc:
        flash("E-mail ou senha inválidos.", "error")
        return render_template("auth/login.html"), 400
    session.clear()
    session["user"] = {"id": str(user.id), "email": user.email}
    if access_token:
        session["access_token"] = access_token
    security.generate_csrf_token()
    return redirect(_safe_next(request.form.get("next")))


@bp.get("/register")
def register():
    if organizations.get_current_user():
        return redirect(url_for("dashboard.overview"))
    return render_template("auth/register.html")


@bp.post("/register")
@limiter.limit("10 per minute")
def register_post():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    org_name = request.form.get("org_name", "").strip()
    errors = []
    if not email or not _EMAIL_RE.match(email) or len(email) > 254:
        errors.append("E-mail inválido.")
    if not password or len(password) < 10:
        errors.append("A senha deve ter pelo menos 10 caracteres.")
    if not org_name or len(org_name) > 120:
        errors.append("Nome da organização é obrigatório (máx. 120 caracteres).")
    if errors:
        for e in errors:
            flash(e, "error")
        return render_template("auth/register.html"), 400
    try:
        resp = get_auth_client().auth.sign_up(
            {"email": email, "password": password}
        )
        user = resp.user
        access_token = resp.session.access_token if resp.session else None
    except Exception as exc:
        msg = str(exc)
        if "already" in msg.lower() or "exists" in msg.lower():
            flash("Este e-mail já está cadastrado.", "error")
        elif "password" in msg.lower():
            flash("A senha é muito fraca ou inválida.", "error")
        else:
            flash("Não foi possível criar a conta. Tente novamente.", "error")
        return render_template("auth/register.html"), 400
    if user:
        try:
            organizations.create_organization_for_user(str(user.id), org_name)
        except Exception:
            pass
        session["user"] = {"id": str(user.id), "email": user.email}
        if access_token:
            session["access_token"] = access_token
        security.generate_csrf_token()
        if access_token:
            flash("Conta criada! Bem-vindo.", "success")
            return redirect(url_for("dashboard.overview"))
        flash(
            "Conta criada. Confira seu e-mail para confirmar o cadastro "
            "antes de acessar.",
            "info",
        )
        return redirect(url_for("auth.login"))
    flash("Não foi possível criar a conta.", "error")
    return render_template("auth/register.html"), 400


@bp.post("/logout")
def logout():
    session.clear()
    flash("Sessão encerrada.", "success")
    return redirect(url_for("auth.login"))


@bp.get("/logout")
def logout_get():
    return redirect(url_for("dashboard.overview"))


@bp.get("/forgot-password")
def forgot_password():
    return render_template("auth/forgot_password.html")


@bp.post("/forgot-password")
@limiter.limit("10 per minute")
def forgot_password_post():
    email = request.form.get("email", "").strip().lower()
    if not email or not _EMAIL_RE.match(email):
        flash("Informe um e-mail válido.", "error")
        return render_template("auth/forgot_password.html"), 400
    try:
        base = request.root_url.rstrip("/")
        get_auth_client().auth.reset_password_for_email(
            email, {"redirect_to": f"{base}/reset-password"}
        )
    except Exception:
        pass
    flash(
        "Se o e-mail existir em nossa base, enviaremos instruções "
        "de recuperação.",
        "info",
    )
    return redirect(url_for("auth.login"))


@bp.get("/reset-password")
def reset_password():
    return render_template("auth/reset_password.html")


@bp.post("/reset-password")
@limiter.limit("10 per minute")
def reset_password_post():
    token = request.form.get("access_token", "").strip()
    new_password = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    if not token:
        flash("Token de recuperação ausente ou expirado.", "error")
        return render_template("auth/reset_password.html"), 400
    if not new_password or len(new_password) < 10:
        flash("A nova senha deve ter pelo menos 10 caracteres.", "error")
        return render_template("auth/reset_password.html"), 400
    if new_password != confirm:
        flash("As senhas não coincidem.", "error")
        return render_template("auth/reset_password.html"), 400
    try:
        create_user_client(token).auth.update_user({"password": new_password})
    except Exception:
        flash(
            "Não foi possível alterar a senha. O link pode ter expirado.",
            "error",
        )
        return render_template("auth/reset_password.html"), 400
    flash("Senha atualizada. Faça login com a nova senha.", "success")
    return redirect(url_for("auth.login"))


@bp.get("/termos")
def termos():
    return render_template("legal/termos.html")


@bp.get("/privacidade")
def privacidade():
    return render_template("legal/privacidade.html")
