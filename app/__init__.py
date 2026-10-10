"""Application factory da plataforma.

Inicializa configuração, logging, rate limiting, headers de segurança, CSRF,
request IDs e registra os blueprints:

- app.auth.routes        -> autenticação por sessão (Supabase Auth)
- app.dashboard.routes   -> painel web (prefixo /dashboard)
- app.api                -> API pública versionada (prefixo /api/v1)
- app.meta.oauth         -> conexão OAuth com a Meta (prefixo /connect/meta)
- app.meta.webhooks      -> webhook público da Meta (/webhooks/meta)
"""
from __future__ import annotations

import logging
import uuid

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

from app.config import Config, TestConfig
from app.extensions import limiter
from app.services import organizations, security


def create_app(config_object: type[Config] | None = None) -> Flask:
    app = Flask(__name__)
    cfg_cls = config_object or Config
    app.config.from_object(cfg_cls())
    cfg = cfg_cls()

    _configure_logging(app)
    _validate_startup(app, cfg)

    limiter.init_app(app)
    limiter.enabled = app.config.get("FLASK_ENV") != "testing"
    limiter.default_limits = [app.config["RATE_LIMIT_DEFAULT"]]
    limiter.storage_uri = app.config["RATE_LIMIT_STORAGE_URI"]

    _register_hooks(app)
    _register_blueprints(app)
    _register_cli(app)
    _register_error_handlers(app)
    _register_core_routes(app)
    return app


def _configure_logging(app: Flask) -> None:
    level = getattr(logging, app.config["LOG_LEVEL"], logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app.logger.setLevel(level)


def _validate_startup(app: Flask, cfg: Config) -> None:
    missing = cfg.missing_critical()
    if missing and cfg.is_production:
        raise RuntimeError(
            "Variáveis de ambiente obrigatórias ausentes em produção: "
            + ", ".join(missing)
        )
    if missing:
        app.logger.warning(
            "Configuração incompleta (%s). A aplicação sobe em modo de "
            "desenvolvimento com integrações externas desativadas.",
            ", ".join(missing),
        )
    if not cfg.meta_oauth_configured:
        app.logger.info("OAuth da Meta não configurado (META_APP_ID/SECRET/REDIRECT_URI).")


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def attach_request_id() -> None:
        g.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex

    @app.before_request
    def load_user_context() -> None:
        if request.endpoint == "static":
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
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'",
        )
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        origin = request.headers.get("Origin")
        if origin and origin in app.config["CORS_ORIGINS"] and request.path.startswith("/api/"):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        response.headers["X-Request-Id"] = g.get("request_id", "")
        return response

    @app.context_processor
    def inject_globals() -> dict:
        cfg = Config()
        return {
            "csrf_token": security.generate_csrf_token,
            "current_user": g.get("user"),
            "membership": g.get("membership"),
            "meta_oauth_configured": cfg.meta_oauth_configured,
            "app_base_url": app.config["APP_BASE_URL"],
        }


def _register_blueprints(app: Flask) -> None:
    from app.api import bp as api_bp
    from app.auth.routes import bp as auth_bp
    from app.dashboard.routes import bp as dashboard_bp
    from app.meta.oauth import bp as meta_oauth_bp
    from app.meta.webhooks import bp as meta_webhooks_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp, url_prefix="/dashboard")
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(meta_oauth_bp, url_prefix="/connect/meta")
    app.register_blueprint(meta_webhooks_bp)


def _register_cli(app: Flask) -> None:
    from app.services.outbound_webhooks import register_cli

    register_cli(app)


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def _error_payload(code: str, message: str, status: int):
    payload = {
        "error": {
            "code": code,
            "message": message,
            "request_id": g.get("request_id", ""),
        }
    }
    return jsonify(payload), status


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(400)
    def bad_request(e):
        if _wants_json():
            return _error_payload("bad_request", getattr(e, "description", "Requisição inválida."), 400)
        return render_template("error.html", status=400,
                               message="Requisição inválida."), 400

    @app.errorhandler(403)
    def forbidden(e):
        if _wants_json():
            return _error_payload("forbidden", getattr(e, "description", "Acesso negado."), 403)
        return render_template("error.html", status=403,
                               message=getattr(e, "description", "Acesso negado.")), 403

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return _error_payload("not_found", "Recurso não encontrado.", 404)
        return render_template("error.html", status=404,
                               message="Página não encontrada."), 404

    @app.errorhandler(413)
    def too_large(e):
        return _error_payload("payload_too_large", "Corpo da requisição excede o limite.", 413)

    @app.errorhandler(429)
    def rate_limited(e):
        return _error_payload("rate_limited", "Limite de requisições excedido. Tente novamente em instantes.", 429)

    @app.errorhandler(500)
    def internal_error(e):
        app.logger.exception("Erro interno: %s", e)
        if _wants_json():
            return _error_payload("internal_error", "Erro interno. Consulte o request_id nos logs.", 500)
        return render_template("error.html", status=500,
                               message="Erro interno. Tente novamente."), 500


def _register_core_routes(app: Flask) -> None:
    @app.get("/health")
    def health():
        # Não expõe detalhes internos; apenas disponibilidade do processo.
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        if g.get("user"):
            return redirect(url_for("dashboard.overview"))
        return redirect(url_for("auth.login"))
