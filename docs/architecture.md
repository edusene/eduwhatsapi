# Arquitetura

```
Navegador (painel, chat) ──sessão + CSRF──┐
Sistemas do cliente (n8n, Make…) ──sk_*──┤
                                          ▼
                              Flask (Gunicorn no Render)
   app/public     landing, /docs, páginas legais
   app/auth       Supabase Auth (cadastro, login, confirmação, convites)
   app/dashboard  overview, números, chat, chaves, webhooks, logs, configurações
   app/api        REST /api/v1 (chaves internas, escopos, idempotência)
   app/meta       connect.py (Embedded Signup/manual) · webhooks.py (entrada)
                  graph_api.py (HTTP) · whatsapp.py (adaptador) · adapters.py (registro)
   app/services   regras de negócio (connections, messaging, outbound_webhooks,
                  organizations, api_keys, idempotency, usage, tasks, maintenance…)
   app/internal   /internal/cron/* para agendadores externos
          │                         │
          ▼                         ▼
   Supabase (Postgres + Auth)    Meta Graph API / WhatsApp Cloud API
```

## Decisões

- **Multi-tenant por organização.** O `organization_id` vem sempre do servidor: da sessão
  (associação em `organization_members`) ou da chave de API validada. Toda consulta filtra por
  ele; recursos de outra organização respondem 404. RLS no banco é defesa em profundidade.
- **Dois planos.** `api` guarda só metadados das mensagens (o conteúdo vai para o webhook do
  cliente); `api_chat` guarda conteúdo para a caixa de entrada, com retenção configurável.
- **Adaptadores de canal.** `get_adapter("whatsapp")` isola a Cloud API. Instagram/Messenger
  entram como novos adaptadores quando houver permissões aprovadas — sem mexer nas rotas.
- **Entrada da Meta rápida e idempotente.** Assinatura validada no corpo bruto → evento
  registrado em `webhook_events` com `dedupe_key` único → processamento leve → webhooks de
  saída despachados em thread. Reenvios da Meta são ignorados pelo índice único.
- **Saída confiável sem fila externa.** Cada entrega é uma linha em `webhook_deliveries`
  (criada travada por 2 min). Uma thread entrega na hora; falhas recebem `next_retry_at` com
  backoff (2, 4, 8… até 120 min, 6 tentativas). Um worker por processo (e/ou cron externo)
  reivindica as vencidas com `FOR UPDATE SKIP LOCKED` (`claim_due_webhook_deliveries`), seguro
  com vários workers. Se o processo cair, a trava expira e a entrega é retomada.
- **Idempotência de envio por reserva.** `Idempotency-Key` é inserida como `processing` ANTES
  de chamar a Meta (índice único). Repetições concorrentes recebem 409; concluídas recebem a
  resposta salva; falhas liberam a chave. POSTs à Meta nunca são repetidos automaticamente.
- **Chat por polling.** A caixa de entrada consulta endpoints JSON a cada 3–6 s. Funciona em
  qualquer hospedagem, sem WebSocket; pode ser trocado por Supabase Realtime no futuro.
- **Modo de teste.** Chaves `sk_test_` percorrem toda a validação e gravam a mensagem com
  status `test`, sem chamar a Meta — claramente sinalizado na resposta.

## Modelo de dados (resumo)

`organizations` (plan, retenção) · `organization_members` (papéis) · `organization_invitations` ·
`meta_connections` (token criptografado, tipo de conexão, saúde) · `api_keys` (hash) ·
`contacts` · `conversations` (janela de 24h, não lidas, atribuição) · `messages` (status de
entrega, conteúdo opcional) · `webhook_endpoints` (segredo criptografado) · `webhook_deliveries` ·
`webhook_events` (dedupe) · `idempotency_keys` · `api_request_logs` · `audit_logs`.
Detalhes e políticas RLS em `supabase/schema.sql`.

## Papéis

| Papel | Pode |
|---|---|
| owner | Tudo, incluindo plano, retenção, apagar mensagens e gerenciar administradores |
| admin | Conectar/desconectar números, chaves, webhooks, modelos, convidar membros |
| developer | Enviar mensagens (chat/testes), testar e reenviar webhooks, revalidar números |
| viewer | Somente leitura (painel e conversas) |
