"""Configuração central da aplicação, carregada de variáveis de ambiente.

Regras:
- Nenhum segredo tem valor padrão em produção.
- Em desenvolvimento/testes a aplicação sobe sem credenciais da Meta/Supabase,
  com as integrações externas sinalizadas como não configuradas.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

DEFAULT_GRAPH_API_VERSION = "v26.0"


class ConfigError(RuntimeError):
    """Erro de configuração obrigatória ausente ou inválida."""


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


class Config:
    # Aplicação
    FLASK_ENV = os.environ.get("FLASK_ENV", "development").strip().lower()
    SECRET_KEY = os.environ.get("SECRET_KEY", "")
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000").rstrip("/")
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
    MAX_CONTENT_LENGTH = _int("MAX_CONTENT_LENGTH", 1_048_576)

    # Cookies de sessão
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", FLASK_ENV == "production")

    # Supabase
    SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
    # Remove qualquer path acidental (ex: /rest/v1) da URL base
    if SUPABASE_URL and "/" in SUPABASE_URL.replace("https://", "").replace("http://", ""):
        from urllib.parse import urlparse
        parsed = urlparse(SUPABASE_URL)
        SUPABASE_URL = f"{parsed.scheme}://{parsed.netloc}"
    SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
    SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    # Meta
    META_APP_ID = os.environ.get("META_APP_ID", "")
    META_APP_SECRET = os.environ.get("META_APP_SECRET", "")
    META_GRAPH_API_VERSION = os.environ.get("META_GRAPH_API_VERSION", DEFAULT_GRAPH_API_VERSION)
    META_REDIRECT_URI = os.environ.get("META_REDIRECT_URI", "")
    META_WEBHOOK_VERIFY_TOKEN = os.environ.get("META_WEBHOOK_VERIFY_TOKEN", "")
    META_WEBHOOK_APP_SECRET = os.environ.get("META_WEBHOOK_APP_SECRET", "") or os.environ.get(
        "META_APP_SECRET", ""
    )

    # Criptografia / hashing
    TOKEN_ENCRYPTION_KEY = os.environ.get("TOKEN_ENCRYPTION_KEY", "")
    API_KEY_PEPPER = os.environ.get("API_KEY_PEPPER", "")

    # Rede / limites
    CORS_ORIGINS = [
        o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
    ]
    RATE_LIMIT_STORAGE_URI = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")
    RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "200 per minute")

    # Webhooks de saída
    OUTBOUND_WEBHOOK_TIMEOUT = _int("OUTBOUND_WEBHOOK_TIMEOUT", 10)
    OUTBOUND_WEBHOOK_MAX_ATTEMPTS = _int("OUTBOUND_WEBHOOK_MAX_ATTEMPTS", 5)
    OUTBOUND_WEBHOOK_ALLOW_INSECURE = _bool("OUTBOUND_WEBHOOK_ALLOW_INSECURE", False)

    # URLs de páginas legais (usadas no painel e para cadastro na Meta)
    LEGAL_TERMS_URL = os.environ.get("LEGAL_TERMS_URL", "").strip() or f"{APP_BASE_URL}/termos"
    LEGAL_PRIVACY_URL = os.environ.get("LEGAL_PRIVACY_URL", "").strip() or f"{APP_BASE_URL}/privacidade"

    @property
    def is_production(self) -> bool:
        return self.FLASK_ENV == "production"

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY)

    @property
    def meta_oauth_configured(self) -> bool:
        return bool(self.META_APP_ID and self.META_APP_SECRET and self.META_REDIRECT_URI)

    @property
    def meta_webhook_configured(self) -> bool:
        return bool(self.META_WEBHOOK_VERIFY_TOKEN and self.META_WEBHOOK_APP_SECRET)

    @property
    def encryption_configured(self) -> bool:
        return bool(self.TOKEN_ENCRYPTION_KEY)

    def missing_critical(self) -> list[str]:
        """Variáveis obrigatórias ausentes (em produção bloqueiam o startup)."""
        missing = []
        if not self.SECRET_KEY:
            missing.append("SECRET_KEY")
        if not self.supabase_configured:
            missing += [n for n in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
                        if not getattr(self, n)]
        if not self.API_KEY_PEPPER:
            missing.append("API_KEY_PEPPER")
        if not self.TOKEN_ENCRYPTION_KEY:
            missing.append("TOKEN_ENCRYPTION_KEY")
        return missing


class TestConfig(Config):
    FLASK_ENV = "testing"
    SECRET_KEY = "test-secret-key"
    SESSION_COOKIE_SECURE = False
    SUPABASE_URL = "https://test.supabase.co"
    SUPABASE_ANON_KEY = "test-anon-key"
    SUPABASE_SERVICE_ROLE_KEY = "test-service-role-key"
    API_KEY_PEPPER = "test-pepper"
    TOKEN_ENCRYPTION_KEY = "Jb8KdKc6V7WCp3TCii-hvcM9d-qdplJ6fVttOCFrFqk="
    META_APP_ID = ""
    META_APP_SECRET = ""
    META_REDIRECT_URI = ""
    META_WEBHOOK_VERIFY_TOKEN = "test-verify-token"
    META_WEBHOOK_APP_SECRET = "test-webhook-secret"
    RATE_LIMIT_STORAGE_URI = "memory://"
    OUTBOUND_WEBHOOK_ALLOW_INSECURE = True
