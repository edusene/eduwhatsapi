# Deploy no Render

## 1. Repositório

Envie o código para um repositório GitHub (ou GitLab). Certifique-se de que `.env` e segredos estão no `.gitignore`.

## 2. Criar Web Service no Render

1. Acesse [dashboard.render.com](https://dashboard.render.com) e crie um **Web Service**.
2. Conecte o repositório.
3. Selecione **Python 3** como ambiente.
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn wsgi:app --workers 2 --threads 4 --timeout 60 --access-logfile - --error-logfile -`
6. Configure o **health check path** como `/health`.

## 3. Variáveis de ambiente

No painel do Render (Environment), defina TODAS as variáveis de `.env.example` com valores reais.

Importantes:
- `APP_BASE_URL` = URL pública do Render (ex.: `https://autozap.onrender.com`).
- `SESSION_COOKIE_SECURE` = `true` (HTTPS).
- `META_REDIRECT_URI` = `https://.../connect/meta/callback`.

NÃO inclua segredos no `render.yaml` (ele usa `sync: false` para valores sensíveis).

## 4. Banco de dados Supabase

1. Crie o projeto em [supabase.com](https://supabase.com).
2. Acesse **SQL Editor > New query**.
3. Cole o conteúdo de `supabase/schema.sql` e execute.
4. Copie a **Project URL**, **anon key** e **service role key** para as variáveis de ambiente do Render.

## 5. Configuração do app da Meta

1. No painel de desenvolvedor, cadastre:
   - Redirect URI OAuth: `https://.../connect/meta/callback`
   - Webhook URL: `https://.../webhooks/meta`
   - Verify token: o valor de `META_WEBHOOK_VERIFY_TOKEN`
2. Assine os campos de webhook desejados (mensagens, status).

## 6. Primeiro deploy

1. Faça push do código.
2. O Render faz o build automaticamente.
3. Acesse a URL pública e crie a primeira conta (registro).

## Observações

- O sistema de arquivos local do Render é efêmero. Todos os dados persistentes devem ficar no Supabase.
- Em produção, configure um storage de rate limiting externo (Redis) se o tráfego crescer.
