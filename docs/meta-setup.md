# Configuração do app da Meta

## Pré-requisitos

- Conta de desenvolvedor em [developers.facebook.com](https://developers.facebook.com).
- App do tipo "Business" ou "Consumer" com o produto **WhatsApp** adicionado.
- Página de Política de Privacidade e Termos de Uso públicas (mesmo que em modelo).

## O que você PRECISA ter no app

1. **App ID** e **App Secret** — Dashboard > Configurações > Básico.
2. **Token de verificação do webhook** — você define um valor aleatório longo.
3. **App Secret para assinatura** — o mesmo App Secret é usado para validar `X-Hub-Signature-256`.

## Escopos OAuth solicitados

- `whatsapp_business_management`
- `whatsapp_business_messaging`

> A concessão desses escopos em produção para uso por terceiros pode exigir **App Review** e **verificação empresarial** da Meta. Não presuma aprovação.

## URLs de callback

- **OAuth Redirect URI**: `https://SEU-DOMINIO.onrender.com/connect/meta/callback`
- **Webhook**: `https://SEU-DOMINIO.onrender.com/webhooks/meta`

Cadastre ambas no painel do app da Meta (Configurações > Básico e WhatsApp > Configuração).

## Embedded Signup / Tech Provider

O MVP implementa o fluxo OAuth padrão (diálogo de permissões). O **Embedded Signup** como Tech Provider requer aprovação específica da Meta. Se o seu app ainda não tiver essa aprovação, use a **Conexão manual** no painel (token de sistema de usuário) para desenvolvimento/testes.

## Testes sem app real

O sistema sobe normalmente sem as variáveis da Meta (`META_APP_ID`, etc.) e exibe instruções no painel. A suíte de testes usa mocks HTTP (`responses`) e não depende de credenciais reais.
