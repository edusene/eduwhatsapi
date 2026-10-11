# Deploy no Render

## Antes de tudo: banco

1. Supabase → **SQL Editor → New query** → cole `supabase/schema.sql` → **Run**.
   O arquivo é idempotente: rode-o de novo a cada atualização da plataforma.
2. Supabase → **Authentication → URL Configuration**:
   - Site URL: `https://SEU-DOMINIO`
   - Redirect URLs: `https://SEU-DOMINIO/auth/callback` e `https://SEU-DOMINIO/reset-password`

## Criar o serviço

**Opção A — Blueprint:** Render → New → **Blueprint** → selecione o repositório. O
`render.yaml` cria o Web Service (`gunicorn wsgi:app`, health check `/health`) e gera
`SECRET_KEY`, `API_KEY_PEPPER`, `META_WEBHOOK_VERIFY_TOKEN` e `CRON_SECRET`.

**Opção B — manual:** New → Web Service → repositório →
Build `pip install -r requirements.txt` · Start
`gunicorn wsgi:app --workers 2 --threads 4 --timeout 120 --access-logfile - --error-logfile -` ·
Health check `/health`.

## Variáveis obrigatórias (Environment)

| Variável | Valor |
|---|---|
| `FLASK_ENV` | `production` |
| `APP_BASE_URL` | `https://SEU-DOMINIO` (sem barra final) |
| `SECRET_KEY`, `API_KEY_PEPPER` | aleatórios (o Blueprint gera) |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY` | do Supabase |
| `TOKEN_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SESSION_COOKIE_SECURE` | `true` |

Para a Meta: `META_APP_ID`, `META_APP_SECRET`, `META_WEBHOOK_VERIFY_TOKEN` e, quando aprovado,
`META_ES_CONFIG_ID`. Em produção a aplicação **não sobe** se faltar alguma obrigatória — a
mensagem de erro no log lista quais.

## Depois do primeiro deploy

1. Abra `https://SEU-DOMINIO/health` → `{"status":"ok"}`.
2. Configure o webhook da Meta (`docs/meta-setup.md`) com `https://SEU-DOMINIO/webhooks/meta`.
3. Crie sua conta em `/register`.
4. **Agendamentos** (recomendado): crie um *Cron Job* no Render (ou use qualquer agendador
   externo) chamando:
   - a cada 5 min: `curl -X POST -H "Authorization: Bearer $CRON_SECRET" https://SEU-DOMINIO/internal/cron/deliver-webhooks`
   - diariamente: `curl -X POST -H "Authorization: Bearer $CRON_SECRET" https://SEU-DOMINIO/internal/cron/purge`

## Domínio próprio e HTTPS

Settings → Custom Domains → adicione o domínio e crie o CNAME indicado no seu DNS. O Render
emite o certificado. Depois atualize `APP_BASE_URL`, as URLs do Supabase Auth, o webhook da
Meta e os domínios permitidos do SDK JavaScript (Embedded Signup).

## Observações

- O disco do Render é efêmero: todo dado persistente fica no Supabase.
- **Plano gratuito hiberna** após inatividade: o primeiro webhook da Meta pode demorar e
  retries de saída só rodam com o serviço acordado. O cron externo acima também o mantém ativo.
  Para produção real, use um plano pago.
- Com mais de uma instância, configure Redis em `RATE_LIMIT_STORAGE_URI`.
- Logs vão para stdout/stderr (aba Logs do Render).
