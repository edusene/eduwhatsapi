# API pública v1

A especificação completa está em `docs/openapi.yaml` (OpenAPI 3.0.3), servida publicamente em `/api/v1/openapi.yaml`.

## Autenticação

Todas as requisições (exceto `/openapi.yaml`) devem incluir:

```
Authorization: Bearer sk_test_SUA_CHAVE
```

> `sk_test_...` e `sk_live_...` são chaves internas da plataforma, NÃO tokens da Meta.

## Endpoints principais

- `GET /api/v1/me` — retorna organização, ambiente e escopos da chave usada.
- `GET /api/v1/channels` — lista canais conectados da organização (dados sanitizados).
- `POST /api/v1/whatsapp/{phone_number_id}/messages` — envia mensagem de texto.
- `GET /api/v1/usage` — métricas de uso da API nos últimos registros.

## Exemplo: envio de mensagem

```bash
curl -X POST https://SEU-DOMINIO.onrender.com/api/v1/whatsapp/123456789012345/messages \
  -H "Authorization: Bearer sk_test_SUBSTITUA_PELA_SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: idem-123456" \
  -d '{
    "to": "5511999999999",
    "type": "text",
    "text": {"body": "Mensagem de teste"}
  }'
```

Resposta de sucesso (200):
```json
{
  "data": {
    "messages": [{"id": "wamid.XXX"}]
  },
  "request_id": "..."
}
```

## Idempotência

Inclua o header `Idempotency-Key` (8–128 caracteres). A resposta da primeira requisição bem-sucedida é armazenada e reutilizada em chamadas subsequentes com a mesma chave + endpoint + organização.

## Erros

Formato padronizado:
```json
{
  "error": {
    "code": "invalid_recipient",
    "message": "...",
    "request_id": "..."
  }
}
```

Códigos comuns:
- `missing_api_key` / `invalid_api_key` (401)
- `insufficient_scope` (403)
- `invalid_json`, `invalid_recipient`, `unsupported_message_type` (400)
- `channel_not_found` (404)
- `meta_api_error` (502)
- `gateway_timeout` (504)
