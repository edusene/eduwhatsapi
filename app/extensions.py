"""Extensões Flask compartilhadas (inicializadas na factory).

A configuração (habilitado, storage, limite padrão) vem de app.config
(RATELIMIT_*), preenchida em create_app a partir das variáveis de ambiente.
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
