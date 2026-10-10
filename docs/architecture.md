# Arquitetura

## Visão geral

AutoZap é uma plataforma SaaS multi-tenant que conecta ativos autorizados da Meta (inicialmente WhatsApp Cloud API) e os disponibiliza para clientes por meio de uma API própria, chaves internas, webhooks e painel administrativo.

## Camadas

1. **Frontend** — HTML semântico, CSS responsivo e JavaScript vanilla. Jinja2 no servidor. Sem frameworks pesados.
2. **Backend Flask** — autenticação por sessão (Supabase Auth), autorização por papel, CSRF, rate limiting, headers de segurança.
3. **Banco de dados** — PostgreSQL no Supabase com RLS (Row Level Security). O backend usa a service role key e SEMPRE filtra por `organization_id` validado no servidor.
4. **Adaptadores Meta** — `MetaGraphClient` genérico + `WhatsAppAdapter` específico. Registro de canais desacoplado (`CHANNEL_ADAPTERS`) para Instagram/Messenger futuros.
5. **Webhooks**
   - **Entrada**: endpoint público `/webhooks/meta` com verificação de challenge e assinatura HMAC-SHA256 (`X-Hub-Signature-256`).
   - **Saída**: entregas para URLs HTTPS dos clientes com assinatura HMAC, proteção anti-SSRF, retries com backoff e log sanitizado.
6. **API pública v1** — autenticação por chaves internas (`sk_test_` / `sk_live_`), rate limit por chave, idempotência para envio de mensagens, logs sanitizados.

## Fluxo de uma mensagem

1. Cliente autentica com chave interna (`Authorization: Bearer sk_...`).
2. Backend valida chave, organização e escopos.
3. Busca conexão ativa do WhatsApp para a org.
4. Descriptografa o token da Meta (Fernet).
5. Encaminha para `https://graph.facebook.com/{version}/{phone_number_id}/messages`.
6. Retorna a resposta da Meta ao cliente + `request_id`.
7. Registra métricas sanitizadas em `api_request_logs`.

## Segurança em camadas

- Cookies `HttpOnly`, `Secure` em produção, `SameSite=Lax`.
- CSRF em todas as operações de sessão (exceto API e webhooks públicos).
- RLS no Supabase como defesa em profundidade.
- Tokens da Meta criptografados em repouso (Fernet).
- Chaves internas armazenadas apenas como hash HMAC-SHA256 + pepper.
- Rate limiting por IP (login) e por chave (API).
