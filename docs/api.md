# API pública v1

A referência completa, com exemplos em curl, Python, Node.js e n8n, está na própria
aplicação em **`/docs`** e na especificação **OpenAPI** em `docs/openapi.yaml`
(servida em `/api/v1/openapi.yaml`).

## Resumo dos endpoints

| Método | Rota | Escopo |
|---|---|---|
| GET | `/api/v1/me` | — |
| GET | `/api/v1/channels` · `/channels/{phone_number_id}` | `channels:read` |
| POST | `/api/v1/whatsapp/{phone_number_id}/messages` | `messages:send` |
| POST | `/api/v1/whatsapp/{phone_number_id}/messages/read` | `messages:send` |
| GET | `/api/v1/messages` · `/messages/{id ou wamid}` | `messages:read` |
| GET | `/api/v1/whatsapp/{phone_number_id}/templates` | `templates:read` |
| POST | `/api/v1/whatsapp/{phone_number_id}/media` | `media:write` |
| GET | `/api/v1/whatsapp/{phone_number_id}/media/{media_id}` | `media:read` |
| GET | `/api/v1/conversations` · `/conversations/{id}/messages` | `conversations:read` (plano API + Chat) |
| GET | `/api/v1/usage` | `usage:read` |

Rotas que **não** são da API pública: `/webhooks/meta` (entrada da Meta),
`/connect/meta/*` (painel, sessão + CSRF), `/internal/cron/*` (agendador, `CRON_SECRET`).

## Exemplo mínimo

```bash
curl -X POST https://SEU-DOMINIO/api/v1/whatsapp/PHONE_NUMBER_ID/messages \
  -H "Authorization: Bearer sk_test_SUBSTITUA_PELA_SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: pedido-123-aviso" \
  -d '{"to": "55DDDNUMERO", "type": "text", "text": {"body": "Mensagem de teste"}}'
```

Com `sk_test_` nada é enviado à Meta. Para enviar de verdade, use `sk_live_` e um número
ativo, respeitando a janela de 24h e o consentimento do destinatário.

## Webhooks de saída

Eventos: `message.received`, `message.status.updated`, `message.sent`, `channel.connected`,
`channel.disconnected`, `template.status.updated`, `api.request.failed` (e `test`).

Cabeçalhos: `X-Webhook-Id`, `X-Webhook-Event`, `X-Webhook-Timestamp`,
`X-Webhook-Signature: sha256=HMAC_SHA256(segredo, "<timestamp>.<corpo>")`.
Responda 2xx em até 10 s; falhas são repetidas com backoff (até 6 tentativas).
