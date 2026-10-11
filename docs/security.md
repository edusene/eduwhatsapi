# Segurança e privacidade

## Controles implementados

| Área | Como |
|---|---|
| Autenticação | Supabase Auth (senha nunca passa pelo nosso banco). Sessão Flask assinada, `HttpOnly`, `SameSite=Lax`, `Secure` em produção, expira em `SESSION_LIFETIME_HOURS`. Sessão recriada no login (anti-fixação). Tokens de links de e-mail validados no servidor (`auth.get_user`). |
| Autorização | Organização derivada no servidor; papéis owner/admin/developer/viewer verificados em cada rota; recursos de outro tenant retornam 404. RLS em todas as tabelas de tenants; colunas sensíveis sem `SELECT` para `anon`/`authenticated`. |
| CSRF | Token por sessão em todos os formulários e no header `X-CSRF-Token` das chamadas JSON do painel. Isentos apenas `/api/` (chave), `/webhooks/` (assinatura) e `/internal/` (segredo). |
| Chaves de API | `sk_live_`/`sk_test_` com 256 bits; armazenadas como HMAC-SHA256 com pepper; exibidas uma vez; escopos; expiração; revogação imediata; rotação; rate limit por chave. |
| Tokens da Meta | Criptografados com Fernet (`TOKEN_ENCRYPTION_KEY`); nunca retornados pela API, logs ou telas; apagados ao desconectar. |
| Webhooks da Meta | `X-Hub-Signature-256` validado em tempo constante no corpo bruto antes de processar; handshake com verify token; deduplicação por id. |
| Webhooks de saída | Somente HTTPS; bloqueio de IPs privados, loopback, link-local, reservados, metadados de nuvem e hosts `.internal`; DNS revalidado a cada entrega; sem seguir redirecionamentos; timeout; tentativas limitadas; assinatura HMAC com timestamp. |
| Mídia | Download só de hosts da Meta (o token nunca vai para outro domínio); resposta servida com CSP `sandbox`. |
| Navegador | CSP restritiva (SDK da Meta liberado só na página de números), `X-Frame-Options: DENY`, `nosniff`, HSTS em produção, `Cache-Control: no-store` em páginas autenticadas. Conteúdo de clientes inserido com `textContent` no chat. |
| Entradas | Validação de payloads, limites de tamanho (`MAX_CONTENT_LENGTH`), regex para IDs, consultas pelo SDK (sem SQL concatenado), listas explícitas de campos atualizáveis (sem mass assignment). |
| Logs | Sem `Authorization`, cookies, tokens ou texto de mensagens. `api_request_logs` guarda método, rota, status, latência, código de erro e `request_id`. Auditoria em `audit_logs`. |

## LGPD e retenção

- **Minimização:** plano `api` não armazena conteúdo; `webhook_events` guarda só ids/tipo.
- **Retenção:** `flask --app wsgi purge-data` ou `POST /internal/cron/purge` remove logs após
  `LOG_RETENTION_DAYS`, eventos e entregas após 30 dias, chaves de idempotência após 1 dia e
  mensagens conforme `organizations.message_retention_days`. Agende diariamente.
- **Exclusão:** o proprietário pode apagar todas as mensagens/conversas/contatos em
  Configurações. Excluir a organização (via banco) remove tudo em cascata.
- Termos e Política de Privacidade são **modelos editáveis**, não aconselhamento jurídico.

## Rotação de segredos

- **SUPABASE_SERVICE_ROLE_KEY vazada:** no Supabase, gere novas chaves de API (ou rotacione o
  JWT secret), atualize o Render e invalide as antigas imediatamente.
- **TOKEN_ENCRYPTION_KEY:** trocar torna ilegíveis os tokens e segredos de webhook salvos.
  Procedimento: (1) com a chave antiga ainda configurada, descriptografe e recriptografe cada
  `meta_connections.access_token_encrypted`, `app_secret_encrypted`,
  `webhook_verify_token_encrypted` e `webhook_endpoints.secret_encrypted` com a nova (script
  com `cryptography.fernet.MultiFernet([nova, antiga]).rotate(token)`); (2) troque a variável.
  Alternativa simples: reconectar os números e rotacionar os segredos de webhook pelo painel.
- **API_KEY_PEPPER:** trocar invalida todas as chaves de API e convites pendentes.
- **CRON_SECRET / META_WEBHOOK_VERIFY_TOKEN:** troque no Render e no agendador/painel da Meta.

## Limitações conhecidas

- `RATE_LIMIT_STORAGE_URI=memory://` conta por processo; com várias instâncias use Redis.
- Validação anti-SSRF tem janela TOCTOU entre a resolução DNS e a conexão; mitigada pela
  revalidação a cada entrega e por não seguir redirecionamentos.
- Se um evento da Meta falhar no processamento após ser registrado, ele não é reprocessado
  automaticamente (fica nos logs do servidor com o `request_id`).
