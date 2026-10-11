# Configuração da Meta (WhatsApp Cloud API)

> Ter acesso de desenvolvedor à Meta **não** concede automaticamente permissões avançadas,
> status de Tech Provider ou aprovação para atender terceiros. Este guia descreve o que a
> plataforma suporta e o que depende de aprovação da Meta. Nada aqui contorna App Review,
> limites ou políticas.

Documentação oficial: <https://developers.facebook.com/docs/whatsapp/> ·
Embedded Signup: <https://developers.facebook.com/documentation/business-messaging/whatsapp/embedded-signup/implementation/> ·
Tech Providers: <https://developers.facebook.com/documentation/business-messaging/whatsapp/solution-providers/get-started-for-tech-providers>

## As três formas de conectar um número

| Forma | O que exige da SUA conta Meta | Quando usar |
|---|---|---|
| **Token de System User** (manual) | Nada além de um app com o produto WhatsApp. O cliente (ou você) gera o token no Gerenciador de Negócios. | Já disponível hoje. Ideal para começar e para clientes que têm o próprio app Meta. |
| **Embedded Signup** (cadastro oficial) | App aprovado como **Tech Provider** (ou Solution Partner), permissões `whatsapp_business_management` e `whatsapp_business_messaging` com acesso avançado, verificação do negócio e uma configuração de Embedded Signup. | Para oferecer onboarding "em 2 cliques" a terceiros. |
| **Coexistência** | Mesmos requisitos do Embedded Signup + configuração com suporte a usuários do app WhatsApp Business. | Cliente quer manter o número no app do celular e também usar a API. |

Enquanto `META_ES_CONFIG_ID` estiver vazio, os botões de Embedded Signup e coexistência
aparecem desativados com uma explicação — o restante da plataforma funciona normalmente.

## 1. Criar o app

1. <https://developers.facebook.com/apps> → **Criar app** → tipo **Empresa** (Business).
2. Adicione o produto **WhatsApp**.
3. Em **Configurações do app → Básico**, copie **ID do app** (`META_APP_ID`) e **Chave secreta** (`META_APP_SECRET`).
4. Preencha as URLs de **Política de Privacidade** e **Termos de Serviço** (a plataforma publica
   modelos em `/privacidade` e `/termos` — revise com um advogado).

## 2. Webhook global (obrigatório para receber mensagens e status)

1. Gere um token: `python -c "import secrets; print(secrets.token_urlsafe(32))"` e defina
   `META_WEBHOOK_VERIFY_TOKEN`. `META_WEBHOOK_APP_SECRET` pode ficar vazio (usa `META_APP_SECRET`).
2. No app: **WhatsApp → Configuração → Webhook**
   - URL de callback: `https://SEU-DOMINIO/webhooks/meta`
   - Token de verificação: o mesmo de `META_WEBHOOK_VERIFY_TOKEN`
3. Assine os campos: **`messages`** (obrigatório), `message_template_status_update` e
   `account_update` (recomendados). Para coexistência, também `smb_message_echoes`,
   `history` e `smb_app_state_sync`.
4. Cada WABA precisa estar **inscrita** no app (`POST /{WABA_ID}/subscribed_apps`). A plataforma
   faz isso automaticamente ao conectar e oferece o botão "Inscrever agora" na página do número.

Como a plataforma valida: `GET` compara `hub.verify_token` em tempo constante e devolve
`hub.challenge`; `POST` valida `X-Hub-Signature-256` (HMAC-SHA256 do corpo bruto com o App
Secret) **antes** de processar. Eventos são deduplicados pelo id da mensagem/status.

## 3. Conexão manual (token de System User)

1. Gerenciador de Negócios → **Usuários → Usuários do sistema** → crie um usuário admin.
2. Atribua a ele o app e a conta do WhatsApp (WABA) com controle total.
3. **Gerar token** com `whatsapp_business_management` e `whatsapp_business_messaging`
   (prefira token sem expiração para servidores).
4. Pegue o **WABA ID** e o **Phone Number ID** em *WhatsApp → Configuração da API*.
5. No painel: **Números → Conectar número → Token de System User**.

A plataforma confere na Graph API se o número pertence à WABA e se o token funciona antes
de salvar. O token é criptografado (Fernet) e nunca é exibido de novo.

**Cliente com app Meta próprio:** se o token vier do app do cliente, os webhooks são
enviados pelo app DELE. Informe o **App Secret** desse app no formulário: a plataforma cria
uma URL exclusiva (`/webhooks/meta/<id-da-conexão>`) e um token de verificação, exibidos na
página do número, para o cliente configurar no app dele.

## 4. Embedded Signup e coexistência (quando aprovado)

1. Torne-se Tech Provider seguindo o guia oficial (verificação do negócio, App Review das
   permissões, acesso avançado).
2. Adicione o produto **Facebook Login for Business** e crie uma **configuração** do tipo
   *WhatsApp Embedded Signup* (versão v4). Copie o **Configuration ID** → `META_ES_CONFIG_ID`.
3. Em Facebook Login for Business → Configurações, adicione o domínio da plataforma em
   **Domínios permitidos para o SDK JavaScript** e mantenha HTTPS.
4. Para coexistência, habilite na configuração o suporte a usuários do app WhatsApp Business.

Fluxo implementado (conforme a documentação oficial):

1. O navegador carrega `https://connect.facebook.net/en_US/sdk.js`, chama `FB.init` e
   `FB.login({config_id, response_type: 'code', override_default_response_type: true, extras: {setup: {}}})`
   (coexistência acrescenta `featureType: 'whatsapp_business_app_onboarding'`).
2. A Meta envia `postMessage` `WA_EMBEDDED_SIGNUP` com `waba_id`, `phone_number_id` e
   `business_id` (aceito só de origens `*.facebook.com`).
3. O código (validade ~30 s) vai ao servidor, que:
   - troca em `GET /oauth/access_token?client_id&client_secret&code`;
   - confere o token em `GET /debug_token` (app correto e WABA entre os `target_ids` concedidos);
   - consulta o número, inscreve o app na WABA (`POST /{WABA}/subscribed_apps`);
   - registra o número com o PIN de 6 dígitos (`POST /{PHONE}/register`) — **exceto** em
     coexistência, onde o número já está registrado e a plataforma solicita a sincronização
     (`POST /{PHONE}/smb_app_data` com `smb_app_state_sync` e `history`, prazo de 24h da Meta).
4. Só então a conexão é salva como ativa.

O cliente ainda precisa cadastrar uma forma de pagamento no WhatsApp Manager para enviar
mensagens pagas — a cobrança é feita pela Meta diretamente na conta dele.

## 5. Limites e políticas que continuam valendo

- Janela de atendimento de 24h: fora dela, apenas modelos aprovados.
- Modelos passam por análise; categoria influencia preço.
- Limites de mensagens por número e classificação de qualidade são definidos pela Meta.
- Opt-in do destinatário é obrigatório. A plataforma não oferece disparo em massa irrestrito.
- Mídias recebidas ficam disponíveis na Meta por tempo limitado.

## Erros comuns

| Sintoma | Causa provável |
|---|---|
| Verificação do webhook falha na Meta | `META_WEBHOOK_VERIFY_TOKEN` diferente, ou URL sem HTTPS/inacessível. |
| Webhook recebido mas "Assinatura inválida" nos logs | `META_WEBHOOK_APP_SECRET` não é o App Secret do app que envia os eventos. |
| Mensagens recebidas não aparecem | WABA não inscrita (veja "Saúde da conexão") ou campo `messages` não assinado. |
| Erro 131047 ao enviar | Fora da janela de 24h — envie um modelo aprovado. |
| Erro 190 | Token expirado/revogado — reconecte o número. |
| Erro 133010 | Número não registrado — registre com o PIN na página do número. |
| Botão "Conectar pela Meta" desativado | `META_ES_CONFIG_ID` (ou App ID/Secret) não configurado. |
