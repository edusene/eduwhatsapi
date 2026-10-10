# MetaBridge — Plataforma SaaS para APIs Oficiais da Meta

MetaBridge é uma plataforma multi-tenant que conecta ativos autorizados da Meta (inicialmente WhatsApp Cloud API) e os disponibiliza para seus clientes por meio de uma API própria, chaves internas, webhooks e painel administrativo.

> **Aviso**: esta plataforma NÃO é um produto oficial da Meta, não é parceira certificada e não garante aprovação de permissões ou App Review. Todos os recursos dependem das permissões efetivamente concedidas ao seu app de desenvolvedor.

---

## Arquitetura

- **Backend**: Python 3.12+ e Flask
- **Frontend**: HTML semântico, CSS responsivo, JavaScript vanilla, Jinja2
- **Banco/Auth**: Supabase (PostgreSQL + Supabase Auth) com RLS
- **Deploy**: Render (Web Service + Gunicorn)
- **Integração Meta**: APIs oficiais HTTPS e Graph API (versão configurável, padrão v26.0)
- **Testes**: pytest, mocks HTTP (`responses`)

Leia mais em:
- [`docs/architecture.md`](docs/architecture.md)
- [`docs/security.md`](docs/security.md)
- [`docs/api.md`](docs/api.md)
- [`docs/meta-setup.md`](docs/meta-setup.md)
- [`docs/deployment-render.md`](docs/deployment-render.md)

---

## Estrutura de arquivos

```
.
├── app/
│   ├── __init__.py               # Application factory
│   ├── config.py                 # Configuração central (env vars)
│   ├── extensions.py             # Flask-Limiter
│   ├── auth/
│   │   ├── __init__.py
│   │   └── routes.py             # Login, cadastro, logout, recuperação
│   ├── dashboard/
│   │   ├── __init__.py
│   │   └── routes.py             # Painel, canais, chaves, webhooks, logs
│   ├── api/
│   │   ├── __init__.py           # Blueprint v1 + handlers de erro
│   │   ├── auth.py               # Decorador require_api_key
│   │   ├── routes.py             # Endpoints da API pública
│   │   └── schemas.py            # Validação de payloads
│   ├── meta/
│   │   ├── __init__.py
│   │   ├── graph_api.py          # Cliente HTTP da Graph API
│   │   ├── whatsapp.py           # Adaptador WhatsApp Cloud API
│   │   ├── adapters.py           # Registro desacoplado de canais
│   │   ├── oauth.py              # Fluxo OAuth e conexão manual
│   │   └── webhooks.py           # Webhook público da Meta
│   ├── services/
│   │   ├── supabase_client.py    # Clientes Supabase (service / auth)
│   │   ├── encryption.py         # Fernet para tokens da Meta
│   │   ├── security.py           # Hash de chaves, CSRF, HMAC
│   │   ├── api_keys.py           # CRUD de chaves internas
│   │   ├── organizations.py      # Contexto de tenant/papéis
│   │   ├── audit.py              # Logs de auditoria
│   │   └── outbound_webhooks.py  # Webhooks de saída + anti-SSRF
│   ├── templates/                # Jinja2 (pt-BR)
│   └── static/
│       ├── css/main.css
│       └── js/
│           ├── main.js
│           └── reset-password.js
├── supabase/
│   └── schema.sql                # Tabelas, índices, triggers, RLS
├── tests/
│   ├── conftest.py               # Fixtures (app, client, fake_db)
│   ├── fakes.py                  # Cliente Supabase falso em memória
│   └── test_app.py               # Testes pytest
├── docs/
│   ├── architecture.md
│   ├── meta-setup.md
│   ├── api.md
│   ├── security.md
│   └── deployment-render.md
│   └── openapi.yaml              # Especificação OpenAPI 3.0.3
├── .env.example
├── .gitignore
├── requirements.txt
├── render.yaml
├── wsgi.py
└── README.md
```

---

## Pré-requisitos

- Python 3.12+ (verificado em 3.14)
- Conta no [Supabase](https://supabase.com)
- Conta no [Render](https://render.com) (para deploy)
- Conta de desenvolvedor na [Meta](https://developers.facebook.com) (para integração real)

---

## Instalação local (Windows)

1. Clone o repositório e entre na pasta.
2. Crie o ambiente virtual:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

3. Copie o exemplo de variáveis:

```bash
cp .env.example .env
```

4. Edite `.env` com valores fictícios para desenvolvimento local (não precisa de credenciais reais da Meta para rodar os testes).

5. Execute o schema no Supabase:
   - Abra o **SQL Editor** do seu projeto Supabase.
   - Cole o conteúdo de `supabase/schema.sql` e clique em **Run**.

6. Inicie o Flask:

```bash
flask --app wsgi:app run
```

Acesse `http://localhost:5000`.

---

## Testes

```bash
pytest tests/ -v
```

Todos os testes usam mocks (sem rede real). Cobrem:
- health check, páginas públicas e protegidas;
- autenticação, autorização e isolamento entre tenants;
- ciclo de vida de chaves de API (criação, verificação, revogação, rotação);
- validação de payloads e erros JSON;
- webhook da Meta (challenge, assinatura, deduplicação);
- webhooks de saída (HMAC, SSRF);
- proxy para a Meta (mocks de sucesso, erro e timeout);
- idempotência;
- modo de desenvolvimento sem credenciais.

---

## Configuração do app da Meta (resumo)

1. Crie/acesse seu app em [developers.facebook.com](https://developers.facebook.com).
2. Anote o **App ID** e **App Secret** (Configurações > Básico).
3. Defina o **Verify Token** do webhook (valor aleatório longo).
4. Cadastre as URLs públicas:
   - OAuth Redirect URI: `https://SEU-DOMINIO.onrender.com/connect/meta/callback`
   - Webhook: `https://SEU-DOMINIO.onrender.com/webhooks/meta`
5. Solicite os escopos `whatsapp_business_management` e `whatsapp_business_messaging`.
6. Preencha as variáveis de ambiente no Render com esses valores.

> A aprovação de App Review e verificação empresarial pode ser necessária para uso em produção com clientes reais. Consulte [`docs/meta-setup.md`](docs/meta-setup.md).

---

## Deploy no Render (passo a passo)

1. Crie um **Web Service** no Render e conecte este repositório.
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn wsgi:app --workers 2 --threads 4 --timeout 60`
4. Health check path: `/health`
5. Configure todas as variáveis de ambiente do `.env.example` no painel do Render.
6. Execute o `supabase/schema.sql` no SQL Editor do Supabase.
7. Faça deploy e acesse a URL pública.

Detalhes completos em [`docs/deployment-render.md`](docs/deployment-render.md).

---

## Exemplos de uso da API

### Criar e usar uma chave interna

Acesse o painel (`/dashboard/api-keys`), crie uma chave e copie o valor exibido **uma única vez**.

### Enviar mensagem de texto

```bash
curl -X POST https://SEU-DOMINIO.onrender.com/api/v1/whatsapp/NUMERO_DO_CANAL/messages \
  -H "Authorization: Bearer sk_test_SUA_CHAVE" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: idem-abc-123" \
  -d '{
    "to": "5511999999999",
    "type": "text",
    "text": {"body": "Mensagem de teste"}
  }'
```

> Requer um número conectado e ativo no painel, com token da Meta válido.

### Consultar uso

```bash
curl https://SEU-DOMINIO.onrender.com/api/v1/usage \
  -H "Authorization: Bearer sk_test_SUA_CHAVE"
```

### Documentação OpenAPI

```bash
curl https://SEU-DOMINIO.onrender.com/api/v1/openapi.yaml
```

---

## Webhooks de saída (clientes)

Ao cadastrar um webhook no painel, o segredo de assinatura (`whsec_...`) é exibido uma vez. A plataforma envia requisições POST com:

```
X-Webhook-Signature: sha256=<HMAC-SHA256>
X-Webhook-Timestamp: <unix timestamp>
X-Webhook-Id: <uuid da entrega>
```

O corpo é um envelope:

```json
{
  "id": "...",
  "event": "message.received",
  "created_at": "...",
  "data": { ... }
}
```

Para validar, compute:

```python
import hmac, hashlib
expected = "sha256=" + hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, signature)
```

---

## Erros comuns

| Problema | Causa provável |
|---|---|
| `403 Token CSRF inválido` | Sessão expirada ou cookie não está sendo enviado. Limpe cookies e tente novamente. |
| `401 invalid_api_key` | Chave incorreta, revogada ou de outra organização. |
| `404 channel_not_found` | O `phone_number_id` não está conectado ou não pertence à sua organização. |
| `502 meta_api_error` | A Meta recusou a operação (token inválido, número bloqueado, fora da janela de 24h etc.). |
| `504 gateway_timeout` | A Meta não respondeu a tempo. Tente novamente com nova `Idempotency-Key`. |
| App não sobe no Render | Variáveis obrigatórias ausentes (`SECRET_KEY`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `TOKEN_ENCRYPTION_KEY`, `API_KEY_PEPPER`). |

---

## Checklist antes de produção

- [ ] Todas as variáveis de ambiente preenchidas com valores reais.
- [ ] Schema SQL executado no Supabase.
- [ ] `SESSION_COOKIE_SECURE=true` e HTTPS ativo.
- [ ] App da Meta configurado com URLs de callback e webhook corretas.
- [ ] App Review e verificação empresarial solicitados à Meta (se aplicável).
- [ ] Termos de Uso e Política de Privacidade publicados e revisados por um advogado.
- [ ] Rate limiter com storage persistente (Redis) se o tráfego for alto.
- [ ] Rotina de limpeza de logs e eventos antigos (retention policy).
- [ ] Backup e política de rotação de `TOKEN_ENCRYPTION_KEY` documentada.

---

## Custos e limites (indicativos)

- **Supabase**: plano gratuito inclui 500 MB de banco e limites de requisições; verifique [supabase.com/pricing](https://supabase.com/pricing).
- **Render**: plano gratuito para Web Services (dorme após inatividade); para produção contínua, considere o plano pago.
- **Meta**: cobrança por conversação do WhatsApp Business Platform varia por país e tipo de mensagem. Consulte a documentação oficial de preços da Meta. Limites de throughput dependem do quality score do número.

---

## Licenciamento

Os direitos de licenciamento ainda precisam ser definidos. Escolha uma licença antes de publicar o código abertamente.

---

## Limitações conhecidas

- O MVP suporta apenas mensagens de texto pelo WhatsApp. Outros tipos de mídia e canais (Instagram, Messenger) ficam para expansão futura via adaptadores.
- O Embedded Signup como Tech Provider requer aprovação da Meta e não está implementado como fluxo automático sem ela.
- O processamento de webhooks de saída é síncrono no MVP; para alta escala, recomenda-se uma fila externa (Redis/RabbitMQ) e workers separados.
