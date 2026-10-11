"""Configuração central da aplicação, carregada de variáveis de ambiente.

Regras:
- Nenhum segredo tem valor padrão em produção.
- Em desenvolvimento/testes a aplicação sobe sem credenciais da Meta/Supabase,
  com as integrações externas sinalizadas como não configuradas.
"""
from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

DEFAULT_GRAPH_API_VERSION = "v26.0"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _base_url(raw: str) -> str:
    """Remove path acidental (ex.: /rest/v1) da URL do Supabase."""
    raw = raw.rstrip("/")
    if not raw:
        return ""
    parsed = urlparse(raw)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else raw


class Config:
    # Aplicação
    APP_NAME = _str("APP_NAME", "AutoZap")
    FLASK_ENV = _str("FLASK_ENV", "development").lower()
    SECRET_KEY = _str("SECRET_KEY")
    APP_BASE_URL = _str("APP_BASE_URL", "http://localhost:5000").rstrip("/")
    LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()
    MAX_CONTENT_LENGTH = _int("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)  # uploads de mídia
    SUPPORT_EMAIL = _str("SUPPORT_EMAIL")
    # Confiar em 1 proxy reverso (X-Forwarded-*). Ligado em produção (Render).
    TRUST_PROXY = _bool("TRUST_PROXY", FLASK_ENV == "production")

    # Cookies de sessão
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", FLASK_ENV == "production")
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_int("SESSION_LIFETIME_HOURS", 12))

    # Supabase
    SUPABASE_URL = _base_url(_str("SUPABASE_URL"))
    SUPABASE_ANON_KEY = _str("SUPABASE_ANON_KEY")
    SUPABASE_SERVICE_ROLE_KEY = _str("SUPABASE_SERVICE_ROLE_KEY")

    # Meta
    META_APP_ID = _str("META_APP_ID")
    META_APP_SECRET = _str("META_APP_SECRET")
    META_GRAPH_API_VERSION = _str("META_GRAPH_API_VERSION", DEFAULT_GRAPH_API_VERSION)
    # ID da configuração do Embedded Signup (Facebook Login for Business >
    # Configurations). Sem ele o botão de Embedded Signup fica desativado.
    META_ES_CONFIG_ID = _str("META_ES_CONFIG_ID")
    META_WEBHOOK_VERIFY_TOKEN = _str("META_WEBHOOK_VERIFY_TOKEN")
    META_WEBHOOK_APP_SECRET = _str("META_WEBHOOK_APP_SECRET") or _str("META_APP_SECRET")
    META_HTTP_TIMEOUT = _int("META_HTTP_TIMEOUT", 20)

    # Criptografia / hashing
    TOKEN_ENCRYPTION_KEY = _str("TOKEN_ENCRYPTION_KEY")
    API_KEY_PEPPER = _str("API_KEY_PEPPER")

    # Rede / limites
    CORS_ORIGINS = [o.strip() for o in _str("CORS_ORIGINS").split(",") if o.strip()]
    RATE_LIMIT_STORAGE_URI = _str("RATE_LIMIT_STORAGE_URI", "memory://")
    RATE_LIMIT_DEFAULT = _str("RATE_LIMIT_DEFAULT", "300 per minute")
    API_RATE_LIMIT_PER_KEY = _str("API_RATE_LIMIT_PER_KEY", "120 per minute")

    # Webhooks de saída e processamento em segundo plano
    OUTBOUND_WEBHOOK_TIMEOUT = _int("OUTBOUND_WEBHOOK_TIMEOUT", 10)
    OUTBOUND_WEBHOOK_MAX_ATTEMPTS = _int("OUTBOUND_WEBHOOK_MAX_ATTEMPTS", 6)
    OUTBOUND_WEBHOOK_ALLOW_INSECURE = _bool("OUTBOUND_WEBHOOK_ALLOW_INSECURE", False)
    # Worker interno (thread) que reprocessa retries de webhooks de saída.
    BACKGROUND_WORKER_ENABLED = _bool("BACKGROUND_WORKER_ENABLED", FLASK_ENV == "production")
    BACKGROUND_WORKER_INTERVAL = _int("BACKGROUND_WORKER_INTERVAL", 30)
    # Executa tarefas assíncronas de forma síncrona (testes/depuração).
    TASKS_EAGER = _bool("TASKS_EAGER", False)
    # Segredo para o endpoint /internal/cron/* (cron externo). Vazio = desativado.
    CRON_SECRET = _str("CRON_SECRET")

    # Retenção padrão (dias) de logs e eventos — ver docs/security.md
    LOG_RETENTION_DAYS = _int("LOG_RETENTION_DAYS", 90)

    # URLs de páginas legais (usadas no painel e para cadastro na Meta)
    LEGAL_TERMS_URL = _str("LEGAL_TERMS_URL") or f"{APP_BASE_URL}/termos"
    LEGAL_PRIVACY_URL = _str("LEGAL_PRIVACY_URL") or f"{APP_BASE_URL}/privacidade"

    @property
    def is_production(self) -> bool:
        return self.FLASK_ENV == "production"

    @property
    def supabase_configured(self) -> bool:
        return bool(self.SUPABASE_URL and self.SUPABASE_SERVICE_ROLE_KEY and self.SUPABASE_ANON_KEY)

    @property
    def embedded_signup_configured(self) -> bool:
        return bool(self.META_APP_ID and self.META_APP_SECRET and self.META_ES_CONFIG_ID)

    @property
    def meta_webhook_configured(self) -> bool:
        return bool(self.META_WEBHOOK_VERIFY_TOKEN and self.META_WEBHOOK_APP_SECRET)

    def missing_critical(self) -> list[str]:
        """Variáveis obrigatórias ausentes (em produção bloqueiam o startup)."""
        required = (
            "SECRET_KEY", "SUPABASE_URL", "SUPABASE_ANON_KEY",
            "SUPABASE_SERVICE_ROLE_KEY", "API_KEY_PEPPER", "TOKEN_ENCRYPTION_KEY",
        )
        return [name for name in required if not getattr(self, name)]


class TestConfig(Config):
    FLASK_ENV = "testing"
    TESTING = True
    SECRET_KEY = "test-secret-key"
    APP_BASE_URL = "http://localhost"
    SESSION_COOKIE_SECURE = False
    SUPABASE_URL = "https://test.supabase.co"
    SUPABASE_ANON_KEY = "test-anon-key"
    SUPABASE_SERVICE_ROLE_KEY = "test-service-role-key"
    API_KEY_PEPPER = "test-pepper"
    TOKEN_ENCRYPTION_KEY = "Jb8KdKc6V7WCp3TCii-hvcM9d-qdplJ6fVttOCFrFqk="
    META_APP_ID = ""
    META_APP_SECRET = ""
    META_ES_CONFIG_ID = ""
    META_WEBHOOK_VERIFY_TOKEN = "test-verify-token"
    META_WEBHOOK_APP_SECRET = "test-webhook-secret"
    RATE_LIMIT_STORAGE_URI = "memory://"
    OUTBOUND_WEBHOOK_ALLOW_INSECURE = True
    BACKGROUND_WORKER_ENABLED = False
    TASKS_EAGER = True
    CRON_SECRET = "test-cron-secret"
    LEGAL_TERMS_URL = "http://localhost/termos"
    LEGAL_PRIVACY_URL = "http://localhost/privacidade"
