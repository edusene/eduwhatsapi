"""Cliente HTTP mínimo para a Meta Graph API.

- Autenticação via header ``Authorization: Bearer <token>`` (omitido quando o
  token é vazio — ex.: troca de código OAuth, que usa apenas query params).
- Erros da Meta (``{"error": {...}}``) viram :class:`MetaAPIError` com os
  códigos originais para diagnóstico.
- POST nunca faz retry automático: reenviar um POST de ``/messages`` pode
  duplicar a mensagem entregue ao destinatário. GET faz no máximo 1 retry,
  somente em falha de rede (sem resposta HTTP).
- Nunca logar tokens nem corpos de resposta aqui.
"""
from __future__ import annotations

import logging
from typing import Any

import requests
from flask import current_app

logger = logging.getLogger(__name__)


class MetaAPIError(Exception):
    """Erro ao chamar a Meta Graph API (rede, timeout ou erro retornado)."""

    def __init__(
        self,
        message: str,
        http_status: int | None = None,
        fb_code: int | None = None,
        fb_subcode: int | None = None,
        fb_type: str | None = None,
        is_timeout: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.fb_code = fb_code
        self.fb_subcode = fb_subcode
        self.fb_type = fb_type
        self.is_timeout = is_timeout


class MetaGraphClient:
    """Cliente sincrônico da Graph API com timeout explícito em toda chamada."""

    def __init__(self, api_version: str, timeout: int = 15) -> None:
        self.api_version = api_version
        self.timeout = timeout
        self.base_url = f"https://graph.facebook.com/{api_version}"

    def get(self, path: str, token: str | None, params: dict | None = None) -> Any:
        """GET com até 1 retry em falha de rede (operação idempotente)."""
        try:
            return self._request("GET", path, token, params=params)
        except MetaAPIError as exc:
            if exc.http_status is not None:
                raise  # erro HTTP da Meta: não adianta repetir
            return self._request("GET", path, token, params=params)

    def post(self, path: str, token: str | None, json: dict | None = None) -> Any:
        """POST sem retry: duplicar um envio de mensagem é pior que falhar."""
        return self._request("POST", path, token, json=json)

    def _request(
        self,
        method: str,
        path: str,
        token: str | None,
        params: dict | None = None,
        json: dict | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = requests.request(
                method, url, headers=headers, params=params, json=json,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise MetaAPIError(
                "Tempo esgotado ao chamar a Meta Graph API.", is_timeout=True
            ) from exc
        except requests.RequestException as exc:
            raise MetaAPIError("Falha de rede ao chamar a Meta Graph API.") from exc

        try:
            data = resp.json()
        except ValueError as exc:
            raise MetaAPIError(
                "Resposta inválida (não-JSON) da Meta Graph API.",
                http_status=resp.status_code,
            ) from exc

        error = data.get("error") if isinstance(data, dict) else None
        if resp.status_code >= 400 or error:
            error = error or {}
            raise MetaAPIError(
                message=str(error.get("message") or "Erro na Meta Graph API."),
                http_status=resp.status_code,
                fb_code=error.get("code"),
                fb_subcode=error.get("error_subcode"),
                fb_type=error.get("type"),
            )
        return data


def get_client() -> MetaGraphClient:
    """Client configurado a partir da aplicação atual."""
    return MetaGraphClient(
        api_version=current_app.config["META_GRAPH_API_VERSION"],
        timeout=int(current_app.config.get("META_HTTP_TIMEOUT", 15)),
    )
