# AutoZap

Plataforma SaaS multiempresa sobre a **WhatsApp Cloud API oficial da Meta**. Cada empresa
cliente conecta seus números pelos fluxos oficiais e escolhe como usar:

- **API Oficial** — chaves de API próprias (`sk_live_` / `sk_test_`), envio de todos os tipos
  de mensagem, webhooks assinados, status de entrega e logs. Ideal para n8n, Make e sistemas próprios.
- **API + Chat** — tudo acima e uma **caixa de entrada** para a equipe atender pelo navegador
  (mídias, modelos, janela de 24h, atribuição de conversas, papéis).

> Integra-se às APIs oficiais da Meta; não é um produto da Meta nem afirma parceria ou
> aprovação. Recursos como Embedded Signup exigem que **o seu app** seja aprovado como Tech
> Provider — veja [docs/meta-setup.md](docs/meta-setup.md).

## Stack

Python 3.12+ · Flask 3 · Jinja2 + CSS/JS próprios (sem framework, tema claro/escuro) ·
Supabase (Postgres + Auth, RLS) · Gunicorn no Render · pytest + responses.

## Estrutura

```
app/
  __init__.py          factory, CSP/headers, CSRF, erros
  config.py            variáveis de ambiente e validação no startup
  api/                 REST /api/v1 (auth por chave, rotas, validação)
  auth/                cadastro, login, confirmação de e-mail, senha, convites
  dashboard/           overview, channels, inbox (chat), developers (chaves/webhooks), logs/settings
  meta/                connect.py (Embedded Signup/manual), webhooks.py (entrada),
                       graph_api.py, whatsapp.py, adapters.py
  public/              landing, /docs, páginas legais
  internal/            /internal/cron/* (retries e retenção)
  services/            connections, messaging, outbound_webhooks, organizations, api_keys,
                       idempotency, usage, tasks, maintenance, encryption, security…
  templates/ static/   interface
supabase/schema.sql    schema completo, idempotente, com RLS
docs/                  arquitetura, Meta, API, OpenAPI, segurança, deploy
tests/                 126 testes (sem rede; Supabase e Meta simulados)
```

## Rodando localmente (Windows / PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env      # preencha SUPABASE_*, SECRET_KEY, TOKEN_ENCRYPTION_KEY, API_KEY_PEPPER
flask --app wsgi run --debug     # http://localhost:5000
```

Gerar segredos:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"                                   # SECRET_KEY / API_KEY_PEPPER
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # TOKEN_ENCRYPTION_KEY
```

Sem credenciais da Meta a aplicação funciona normalmente (cadastro, chaves em modo de teste,
webhooks de saída, painel); o painel indica o que falta configurar.

Para testar webhooks da Meta localmente, exponha a porta com um túnel HTTPS (ex.: `ngrok http 5000`)
e use a URL pública no app da Meta e em `APP_BASE_URL`.

## Supabase

1. Crie um projeto em <https://supabase.com>.
2. **SQL Editor** → cole `supabase/schema.sql` → **Run** (pode rodar de novo para atualizar).
3. **Authentication → URL Configuration**: Site URL = `APP_BASE_URL`; Redirect URLs:
   `APP_BASE_URL/auth/callback` e `APP_BASE_URL/reset-password`.
4. **Project Settings → API**: copie URL, `anon` e `service_role` para o `.env`.

## Testes

```powershell
python -m pytest -q
```

Cobrem: health e páginas; cadastro/login/confirmação; CSRF; papéis; isolamento entre empresas
(painel, API, chat, webhooks); chaves (hash, escopos, expiração, revogação, rotação); validação
de todos os tipos de mensagem; envio com mocks da Meta (sucesso, erro preservado, timeout sem
retry); modo de teste; idempotência (replay, conflito, liberação); webhook da Meta (handshake,
assinatura, deduplicação, roteamento, progressão de status, app próprio do cliente); webhooks
de saída (SSRF, HMAC, redirecionamento, retries); Embedded Signup e conexão manual com mocks;
convites; retenção; configuração ausente em produção.

## Usando a API

Documentação completa em `/docs` (na aplicação) e `docs/openapi.yaml`.

```bash
# Criar a chave: Painel → Chaves de API → Nova chave (copie na hora; só o hash é guardado)
curl https://SEU-DOMINIO/api/v1/me -H "Authorization: Bearer sk_test_SUBSTITUA_PELA_SUA_CHAVE"

curl -X POST https://SEU-DOMINIO/api/v1/whatsapp/PHONE_NUMBER_ID/messages \
  -H "Authorization: Bearer sk_live_SUA_CHAVE" -H "Content-Type: application/json" \
  -H "Idempotency-Key: pedido-123-aviso" \
  -d '{"to": "55DDDNUMERO", "type": "text", "text": {"body": "Mensagem de teste"}}'
```

O envio real exige um número **ativo**, um destinatário que tenha consentido e, fora da
janela de 24h, um **modelo aprovado**. Revogar/rotacionar: Painel → Chaves de API.

Validação dos webhooks de saída: `HMAC_SHA256(segredo, "<X-Webhook-Timestamp>.<corpo bruto>")`
comparado a `X-Webhook-Signature` (exemplos em Python e Node em `/docs#assinatura`).

## Deploy

Passo a passo em [docs/deployment-render.md](docs/deployment-render.md) (Blueprint `render.yaml`,
variáveis, domínio, cron de retries/retenção).

## Checklist antes de produção

- [ ] `schema.sql` executado; URLs de redirect do Supabase Auth configuradas
- [ ] `FLASK_ENV=production`, `SESSION_COOKIE_SECURE=true`, `APP_BASE_URL` com HTTPS
- [ ] `SECRET_KEY`, `API_KEY_PEPPER`, `TOKEN_ENCRYPTION_KEY` fortes e guardados em local seguro
- [ ] Webhook da Meta verificado e campo `messages` assinado
- [ ] Cron de `/internal/cron/deliver-webhooks` e `/internal/cron/purge` configurado
- [ ] Termos e Política de Privacidade revisados por um advogado
- [ ] Redis em `RATE_LIMIT_STORAGE_URI` se houver mais de uma instância
- [ ] Plano pago no Render (o gratuito hiberna)

## O que depende de configuração ou aprovação externa

| Item | Depende de |
|---|---|
| Receber mensagens/status | Webhook configurado no app da Meta + WABA inscrita |
| Embedded Signup / coexistência para terceiros | Seu app aprovado como Tech Provider, App Review das permissões, verificação do negócio, `META_ES_CONFIG_ID` |
| Enviar mensagens pagas | Forma de pagamento na conta do WhatsApp do cliente (cobrança pela Meta) |
| Modelos de mensagem | Aprovação da Meta para cada modelo |
| Limites de envio e qualidade | Políticas da Meta por número |
| E-mails de confirmação/recuperação | SMTP do Supabase Auth (o padrão tem limite baixo; configure SMTP próprio) |

## Custos

Supabase, Render e Meta cobram conforme seus planos e tabelas vigentes — consulte os sites
oficiais. A Meta cobra as mensagens diretamente na conta do WhatsApp de cada cliente.

## Licença

Ainda não definida. Até que o titular escolha uma licença, todos os direitos são reservados.
