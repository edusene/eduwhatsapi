"""Application factory da plataforma.

Blueprints:
- app.public.routes      -> landing page, páginas legais, documentação pública
- app.auth.routes        -> autenticação por sessão (Supabase Auth) e convites
- app.dashboard          -> painel web (prefixo /dashboard): canais, chat, chaves...
- app.api                -> API pública versionada (prefixo /api/v1)
- app.meta.connect       -> conexão de números (Embedded Signup / manual)
- app.meta.webhooks      -> webhooks públicos da Meta (/webhooks/meta)
- app.internal           -> tarefas agendadas (/internal/cron/*, protegidas)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from flask import Flask, g, jsonify, render_template, request

from app.config import Config
from app.extensions import limiter
from app.services import organizations, security

_REQUEST_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def create_app(config_object: type[Config] | None = None) -> Flask:
    app = Flask(__name__)
    cfg = (config_object or Config)()
    app.config.from_object(cfg)
    if app.config.get("TRUST_PROXY"):
        # Render (e outros PaaS) ficam atrás de um proxy: usa X-Forwarded-For/Proto
        # para o IP real (rate limit) e o esquema https (cookies/URLs).
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    _configure_logging(app)
    _validate_startup(app, cfg)

    app.config["RATELIMIT_ENABLED"] = app.config.get("FLASK_ENV") != "testing"
    app.config["RATELIMIT_STORAGE_URI"] = app.config["RATE_LIMIT_STORAGE_URI"]
    app.config["RATELIMIT_DEFAULT"] = app.config["RATE_LIMIT_DEFAULT"]
    app.config["RATELIMIT_HEADERS_ENABLED"] = True
    limiter.init_app(app)

    _register_hooks(app)
    _register_blueprints(app)
    _register_cli(app)
    _register_error_handlers(app)
    _register_core_routes(app)

    from app.services import tasks
    tasks.init_app(app)
    return app


def _configure_logging(app: Flask) -> None:
    level = getattr(logging, app.config["LOG_LEVEL"], logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app.logger.setLevel(level)


def _validate_startup(app: Flask, cfg: Config) -> None:
    missing = cfg.missing_critical()
    if missing and cfg.is_production:
        raise RuntimeError(
            "Variáveis de ambiente obrigatórias ausentes em produção: " + ", ".join(missing)
        )
    if missing:
        app.logger.warning(
            "Configuração incompleta (%s). A aplicação sobe em modo de desenvolvimento "
            "com integrações externas desativadas.", ", ".join(missing),
        )
    if not cfg.embedded_signup_configured:
        app.logger.info("Embedded Signup desativado (META_APP_ID/META_APP_SECRET/META_ES_CONFIG_ID).")
    if not cfg.meta_webhook_configured:
        app.logger.info("Webhook global da Meta desativado (META_WEBHOOK_VERIFY_TOKEN/APP_SECRET).")


def _content_security_policy() -> str:
    script = "'self'"
    frame = "'none'"
    connect = "'self'"
    if g.get("allow_facebook_sdk"):
        # Necessário apenas na página de canais (SDK oficial do Embedded Signup).
        script += " https://connect.facebook.net"
        frame = "https://www.facebook.com https://web.facebook.com https://business.facebook.com"
        connect += " https://*.facebook.com https://*.facebook.net"
    return (
        f"default-src 'self'; script-src {script}; style-src 'self' 'unsafe-inline'; "
        f"img-src 'self' data: blob: https://*.fbcdn.net https://*.facebook.com; "
        f"media-src 'self' blob:; frame-src {frame}; connect-src {connect}; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def attach_request_id() -> None:
        incoming = request.headers.get("X-Request-Id", "")
        if incoming and len(incoming) <= 64 and set(incoming) <= _REQUEST_ID_ALLOWED:
            g.request_id = incoming
        else:
            g.request_id = uuid.uuid4().hex

    @app.before_request
    def load_user_context() -> None:
        if request.endpoint == "static" or request.path.startswith(("/api/", "/webhooks/")):
            g.user = None
            g.membership = None
            return
        organizations.load_current_context()

    @app.before_request
    def csrf_protect() -> None:
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not security.is_csrf_exempt():
            security.validate_csrf()

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", _content_security_policy())
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if g.get("user") and request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        origin = request.headers.get("Origin")
        if origin and origin in app.config["CORS_ORIGINS"] and request.path.startswith("/api/"):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Idempotency-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Vary"] = "Origin"
        response.headers["X-Request-Id"] = g.get("request_id", "")
        return response

    @app.context_processor
    def inject_globals() -> dict:
        return {
            "csrf_token": security.generate_csrf_token,
            "current_user": g.get("user"),
            "membership": g.get("membership"),
            "memberships": g.get("memberships") or [],
            "app_name": app.config["APP_NAME"],
            "app_base_url": app.config["APP_BASE_URL"],
            "legal_terms_url": app.config["LEGAL_TERMS_URL"],
            "legal_privacy_url": app.config["LEGAL_PRIVACY_URL"],
            "support_email": app.config["SUPPORT_EMAIL"],
            "now": datetime.now(timezone.utc),
        }

    from app.services import formatting
    formatting.register_filters(app)


def _register_blueprints(app: Flask) -> None:
    from app.api import bp as api_bp
    from app.auth.routes import bp as auth_bp
    from app.dashboard import bp as dashboard_bp
    from app.internal import bp as internal_bp
    from app.meta.connect import bp as meta_connect_bp
    from app.meta.webhooks import bp as meta_webhooks_bp
    from app.public.routes import bp as public_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(meta_connect_bp, url_prefix="/connect/meta")
    app.register_blueprint(meta_webhooks_bp)
    app.register_blueprint(internal_bp, url_prefix="/internal")


def _register_cli(app: Flask) -> None:
    from app.services.maintenance import register_cli

    register_cli(app)


def _wants_json() -> bool:
    return (
        request.path.startswith(("/api/", "/internal/"))
        or "/inbox/api/" in request.path
        or request.accept_mimetypes.best == "application/json"
    )


def _error_payload(code: str, message: str, status: int):
    payload = {"error": {"code": code, "message": message, "request_id": g.get("request_id", "")}}
    return jsonify(payload), status


def _register_error_handlers(app: Flask) -> None:
    def _html(status: int, title: str, message: str):
        return render_template("error.html", status=status, title=title, message=message), status

    @app.errorhandler(400)
    def bad_request(e):
        description = getattr(e, "description", None) or "Requisição inválida."
        if _wants_json():
            return _error_payload("bad_request", description, 400)
        return _html(400, "Requisição inválida", description)

    @app.errorhandler(403)
    def forbidden(e):
        description = getattr(e, "description", None) or "Acesso negado."
        if _wants_json():
            return _error_payload("forbidden", description, 403)
        return _html(403, "Acesso negado", description)

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return _error_payload("not_found", "Recurso não encontrado.", 404)
        return _html(404, "Página não encontrada", "O endereço acessado não existe ou foi removido.")

    @app.errorhandler(405)
    def method_not_allowed(e):
        if _wants_json():
            return _error_payload("method_not_allowed", "Método HTTP não permitido.", 405)
        return _html(405, "Método não permitido", "Esta ação não aceita este tipo de requisição.")

    @app.errorhandler(413)
    def too_large(e):
        return _error_payload("payload_too_large", "Corpo da requisição excede o limite.", 413)

    @app.errorhandler(429)
    def rate_limited(e):
        if _wants_json():
            return _error_payload("rate_limited", "Limite de requisições excedido. Tente novamente em instantes.", 429)
        return _html(429, "Muitas tentativas", "Aguarde alguns instantes e tente novamente.")

    @app.errorhandler(500)
    def internal_error(e):
        app.logger.exception("Erro interno (request_id=%s)", g.get("request_id"))
        if _wants_json():
            return _error_payload("internal_error", "Erro interno. Informe o request_id ao suporte.", 500)
        return _html(500, "Erro interno", "Algo deu errado do nosso lado. Tente novamente.")

    from app.services.supabase_client import SupabaseNotConfigured

    @app.errorhandler(SupabaseNotConfigured)
    def supabase_missing(e):
        if _wants_json():
            return _error_payload("service_unavailable", "Banco de dados não configurado no servidor.", 503)
        return _html(503, "Serviço indisponível",
                     "O banco de dados (Supabase) não está configurado neste servidor.")


def _register_core_routes(app: Flask) -> None:
    @app.get("/health")
    def health():
        # Não expõe detalhes internos; apenas disponibilidade do processo.
        return jsonify({"status": "ok"})
