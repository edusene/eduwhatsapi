"""Cliente HTTP mínimo para a Meta Graph API.

- Autenticação via ``Authorization: Bearer <token>`` (omitido quando o token
  é vazio — ex.: troca do código do Embedded Signup, que usa query params).
- Erros da Meta (``{"error": {...}}``) viram :class:`MetaAPIError`, preservando
  code/subcode/type/fbtrace_id e a mensagem original para diagnóstico.
- POST nunca faz retry automático: reenviar ``/messages`` pode duplicar a
  mensagem entregue. GET faz no máximo 1 retry, só em falha de rede.
- Download de mídia só é permitido em hosts da Meta (o token vai no header;
  enviar para outro host vazaria a credencial).
- Nunca logar tokens nem corpos de resposta aqui.
"""
from __future__ import annotations

import logging
from typing import Any, Iterator
from urllib.parse import urlsplit

import requests
from flask import current_app

logger = logging.getLogger(__name__)

GRAPH_HOST = "https://graph.facebook.com"
_MEDIA_HOST_SUFFIXES = (".fbsbx.com", ".facebook.com", ".fbcdn.net", ".whatsapp.net")
MAX_MEDIA_BYTES = 100 * 1024 * 1024


class MetaAPIError(Exception):
    """Erro ao chamar a Meta Graph API (rede, timeout ou erro retornado)."""

    def __init__(self, message: str, http_status: int | None = None, fb_code: int | None = None,
                 fb_subcode: int | None = None, fb_type: str | None = None,
                 fbtrace_id: str | None = None, is_timeout: bool = False,
                 details: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.fb_code = fb_code
        self.fb_subcode = fb_subcode
        self.fb_type = fb_type
        self.fbtrace_id = fbtrace_id
        self.is_timeout = is_timeout
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        """Representação segura (sem tokens) para devolver ao cliente."""
        return {k: v for k, v in {
            "message": self.message,
            "code": self.fb_code,
            "error_subcode": self.fb_subcode,
            "type": self.fb_type,
            "details": self.details,
            "fbtrace_id": self.fbtrace_id,
        }.items() if v is not None}


class MetaGraphClient:
    """Cliente sincrônico da Graph API com timeout explícito em toda chamada."""

    def __init__(self, api_version: str, timeout: int = 20) -> None:
        self.api_version = api_version
        self.timeout = timeout
        self.base_url = f"{GRAPH_HOST}/{api_version}"

    # -- verbos ---------------------------------------------------------------
    def get(self, path: str, token: str | None, params: dict | None = None) -> Any:
        try:
            return self._request("GET", path, token, params=params)
        except MetaAPIError as exc:
            if exc.http_status is not None:
                raise  # erro HTTP da Meta: não adianta repetir
            return self._request("GET", path, token, params=params)

    def post(self, path: str, token: str | None, json: dict | None = None,
             params: dict | None = None) -> Any:
        return self._request("POST", path, token, json=json, params=params)

    def post_multipart(self, path: str, token: str, data: dict, files: dict) -> Any:
        return self._request("POST", path, token, data=data, files=files, timeout=max(self.timeout, 60))

    def delete(self, path: str, token: str | None, params: dict | None = None) -> Any:
        return self._request("DELETE", path, token, params=params)

    def download(self, url: str, token: str) -> tuple[Iterator[bytes], str, int | None]:
        """Baixa uma mídia (URL retornada por GET /{media-id}). Somente hosts Meta."""
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not any(host.endswith(s) for s in _MEDIA_HOST_SUFFIXES):
            raise MetaAPIError("URL de mídia retornada pela Meta não é confiável.")
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                                timeout=max(self.timeout, 60), stream=True, allow_redirects=False)
        except requests.Timeout as exc:
            raise MetaAPIError("Tempo esgotado ao baixar a mídia.", is_timeout=True) from exc
        except requests.RequestException as exc:
            raise MetaAPIError("Falha de rede ao baixar a mídia.") from exc
        if resp.status_code >= 400:
            resp.close()
            raise MetaAPIError("A Meta recusou o download da mídia.", http_status=resp.status_code)
        length = resp.headers.get("Content-Length")
        size = int(length) if length and length.isdigit() else None
        if size and size > MAX_MEDIA_BYTES:
            resp.close()
            raise MetaAPIError("Mídia maior que o limite permitido.")

        def stream() -> Iterator[bytes]:
            try:
                yield from resp.iter_content(chunk_size=64 * 1024)
            finally:
                resp.close()

        return stream(), resp.headers.get("Content-Type", "application/octet-stream"), size

    # -- núcleo ---------------------------------------------------------------
    def _request(self, method: str, path: str, token: str | None, params: dict | None = None,
                 json: dict | None = None, data: dict | None = None, files: dict | None = None,
                 timeout: int | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = requests.request(method, url, headers=headers, params=params, json=json,
                                    data=data, files=files, timeout=timeout or self.timeout)
        except requests.Timeout as exc:
            raise MetaAPIError("Tempo esgotado ao chamar a Meta Graph API.", is_timeout=True) from exc
        except requests.RequestException as exc:
            raise MetaAPIError("Falha de rede ao chamar a Meta Graph API.") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise MetaAPIError("Resposta inválida (não-JSON) da Meta Graph API.",
                               http_status=resp.status_code) from exc

        error = body.get("error") if isinstance(body, dict) else None
        if resp.status_code >= 400 or error:
            error = error if isinstance(error, dict) else {}
            error_data = error.get("error_data") if isinstance(error.get("error_data"), dict) else {}
            raise MetaAPIError(
                message=str(error.get("message") or "Erro na Meta Graph API.")[:500],
                http_status=resp.status_code,
                fb_code=error.get("code"),
                fb_subcode=error.get("error_subcode"),
                fb_type=error.get("type"),
                fbtrace_id=error.get("fbtrace_id"),
                details=(str(error_data.get("details"))[:500] if error_data.get("details") else None),
            )
        return body


def get_client() -> MetaGraphClient:
    """Cliente configurado a partir da aplicação atual."""
    return MetaGraphClient(
        api_version=current_app.config["META_GRAPH_API_VERSION"],
        timeout=int(current_app.config.get("META_HTTP_TIMEOUT", 20)),
    )
