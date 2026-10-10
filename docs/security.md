# Segurança e Privacidade

## Autenticação e autorização

- Supabase Auth gerencia senhas, tokens JWT e e-mails de confirmação/recuperação.
- O backend nunca armazena senhas.
- Cada usuário pertence a uma organização; `organization_id` é validado no servidor a cada requisição.
- Papéis: `owner`, `admin`, `developer`, `viewer`.

## Dados sensíveis

- Tokens da Meta: criptografados com Fernet (`TOKEN_ENCRYPTION_KEY`).
- Chaves internas de API: apenas hash HMAC-SHA256 com pepper (`API_KEY_PEPPER`).
- Segredos de webhook de saída: criptografados com Fernet.

## Headers e cookies

- `HttpOnly`, `Secure` (produção), `SameSite=Lax`.
- CSP, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`.
- CORS restrito às origens configuradas em `CORS_ORIGINS`.

## Rate limiting

- Login, cadastro e recuperação: 10/minuto por IP.
- Criação de chaves e webhooks: 20/minuto por sessão.
- API pública: 60/minuto por chave.

## Webhooks

- **Entrada**: validação de assinatura `X-Hub-Signature-256` com HMAC-SHA256 e comparação em tempo constante.
- **Saída**: validação anti-SSRF (bloqueio de IPs privados, localhost, metadata cloud), HTTPS obrigatório em produção.

## LGPD (diretrizes)

- Minimização: não armazenamos conteúdo completo de mensagens além do necessário.
- Retenção: sugere-se limpeza periódica de logs e eventos (ex.: 90 dias).
- Modelos de Termos de Uso e Política de Privacidade são fornecidos como rascunho editável.
