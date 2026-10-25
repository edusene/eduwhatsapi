-- ==========================================================================
-- AutoZap — schema completo (Supabase / PostgreSQL)
--
-- COMO INSTALAR OU ATUALIZAR:
--   1. Supabase Dashboard > SQL Editor > New query
--   2. Cole este arquivo inteiro e clique em "Run"
--   3. Confira em Table Editor que as tabelas existem
--
-- Este arquivo é IDEMPOTENTE: pode ser executado novamente para atualizar um
-- banco criado por versões anteriores (usa IF NOT EXISTS / DROP ... IF EXISTS).
--
-- MODELO DE SEGURANÇA:
--   - O backend Flask usa a service role (ignora RLS) e SEMPRE filtra por
--     organization_id derivado da sessão/chave validada no servidor.
--   - RLS está habilitada em todas as tabelas de tenants como defesa em
--     profundidade para acessos com anon key + JWT de usuário.
--   - Nenhuma política usa USING (true). Escritas só ocorrem pelo backend.
--   - Funções auxiliares ficam no schema "private" (não exposto pela API).
--   - Timestamps em UTC (timestamptz).
-- ==========================================================================

create extension if not exists "pgcrypto";
create schema if not exists private;

-- --------------------------------------------------------------------------
-- Função de updated_at
-- --------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

-- --------------------------------------------------------------------------
-- Organizações (tenants)
--   plan: 'api'      -> somente API oficial (sem armazenar conteúdo de mensagens)
--         'api_chat' -> API + caixa de entrada (armazena conversas para o chat)
-- --------------------------------------------------------------------------
create table if not exists public.organizations (
    id            uuid primary key default gen_random_uuid(),
    name          text not null check (char_length(name) between 1 and 120),
    slug          text not null unique check (char_length(slug) between 1 and 140),
    owner_user_id uuid not null references auth.users (id) on delete cascade,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
alter table public.organizations add column if not exists plan text not null default 'api_chat';
alter table public.organizations add column if not exists message_retention_days integer not null default 90;
alter table public.organizations drop constraint if exists organizations_plan_check;
alter table public.organizations add constraint organizations_plan_check check (plan in ('api', 'api_chat'));
alter table public.organizations drop constraint if exists organizations_retention_check;
alter table public.organizations add constraint organizations_retention_check
    check (message_retention_days between 1 and 3650);
comment on table public.organizations is 'Empresas/clientes (tenants). plan define se o chat (inbox) está ativo.';
create index if not exists idx_organizations_owner on public.organizations (owner_user_id);

-- --------------------------------------------------------------------------
-- Membros e papéis
-- --------------------------------------------------------------------------
create table if not exists public.organization_members (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    user_id         uuid not null references auth.users (id) on delete cascade,
    role            text not null default 'viewer'
                    check (role in ('owner', 'admin', 'developer', 'viewer')),
    invited_by      uuid references auth.users (id) on delete set null,
    created_at      timestamptz not null default now(),
    unique (organization_id, user_id)
);
alter table public.organization_members add column if not exists email text;
comment on table public.organization_members is 'Associação usuário (Supabase Auth) <-> organização, com papel.';
create index if not exists idx_org_members_user on public.organization_members (user_id);
create index if not exists idx_org_members_org on public.organization_members (organization_id);

-- --------------------------------------------------------------------------
-- Convites (token guardado apenas como hash)
-- --------------------------------------------------------------------------
create table if not exists public.organization_invitations (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    email           text not null check (char_length(email) between 3 and 254),
    role            text not null check (role in ('admin', 'developer', 'viewer')),
    token_hash      text not null unique,
    invited_by      uuid references auth.users (id) on delete set null,
    expires_at      timestamptz not null,
    accepted_at     timestamptz,
    revoked_at      timestamptz,
    created_at      timestamptz not null default now()
);
comment on table public.organization_invitations is 'Convites de membros. O link é exibido uma única vez; só o hash fica no banco.';
create index if not exists idx_invitations_org on public.organization_invitations (organization_id);

-- --------------------------------------------------------------------------
-- Conexões com a Meta (números de WhatsApp)
-- --------------------------------------------------------------------------
create table if not exists public.meta_connections (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references public.organizations (id) on delete cascade,
    channel              text not null default 'whatsapp' check (channel in ('whatsapp')),
    waba_id              text,
    phone_number_id      text,
    display_phone_number text,
    verified_name        text,
    access_token_encrypted text,           -- Fernet. NUNCA texto puro.
    status               text not null default 'pending'
                         check (status in ('pending', 'active', 'error', 'disconnected')),
    granted_scopes       jsonb not null default '[]'::jsonb,
    connected_by         uuid references auth.users (id) on delete set null,
    connected_at         timestamptz,
    disconnected_at      timestamptz,
    last_validated_at    timestamptz,
    metadata             jsonb not null default '{}'::jsonb,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);
alter table public.meta_connections add column if not exists connection_type text not null default 'manual';
alter table public.meta_connections add column if not exists business_id text;
alter table public.meta_connections add column if not exists quality_rating text;
alter table public.meta_connections add column if not exists name_status text;
alter table public.meta_connections add column if not exists webhook_subscribed boolean not null default false;
alter table public.meta_connections add column if not exists registered boolean not null default false;
alter table public.meta_connections add column if not exists last_error text;
-- App próprio do cliente (conexão manual): segredo para validar a assinatura
-- dos webhooks que o app DELE envia e token de verificação do handshake.
alter table public.meta_connections add column if not exists app_secret_encrypted text;
alter table public.meta_connections add column if not exists webhook_verify_token_encrypted text;
alter table public.meta_connections drop constraint if exists meta_connections_connection_type_check;
alter table public.meta_connections add constraint meta_connections_connection_type_check
    check (connection_type in ('embedded_signup', 'coexistence', 'manual'));
-- Versões antigas tinham unique (organization_id, channel, phone_number_id),
-- que permitia o MESMO número em duas organizações (roteamento ambíguo de
-- webhooks). Agora um número só pode estar ativo/pendente em uma organização.
alter table public.meta_connections drop constraint if exists meta_connections_organization_id_channel_phone_number_id_key;
create unique index if not exists uq_meta_connections_live_phone
    on public.meta_connections (channel, phone_number_id)
    where status <> 'disconnected' and phone_number_id is not null;
comment on table public.meta_connections is 'Números de WhatsApp autorizados por organização. Tokens sempre criptografados.';
create index if not exists idx_meta_connections_org on public.meta_connections (organization_id);
create index if not exists idx_meta_connections_phone on public.meta_connections (phone_number_id);

-- --------------------------------------------------------------------------
-- Chaves internas de API (hash apenas)
-- --------------------------------------------------------------------------
create table if not exists public.api_keys (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    name            text not null check (char_length(name) between 1 and 80),
    environment     text not null default 'test' check (environment in ('test', 'live')),
    key_prefix      text not null,
    key_suffix      text not null,
    key_hash        text not null unique,  -- HMAC-SHA256(pepper, chave) em hex
    scopes          jsonb not null default '[]'::jsonb,
    created_by      uuid references auth.users (id) on delete set null,
    last_used_at    timestamptz,
    expires_at      timestamptz,
    revoked_at      timestamptz,
    created_at      timestamptz not null default now()
);
comment on table public.api_keys is 'Chaves internas (sk_test_/sk_live_). Apenas hash HMAC é armazenado.';
create index if not exists idx_api_keys_org on public.api_keys (organization_id);
-- Números (phone_number_id) que a chave pode usar; lista vazia = todos da organização.
alter table public.api_keys add column if not exists phone_number_ids jsonb not null default '[]'::jsonb;

-- --------------------------------------------------------------------------
-- Contatos e conversas (somente plano api_chat)
-- --------------------------------------------------------------------------
create table if not exists public.contacts (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    wa_id           text not null check (wa_id ~ '^[0-9]{6,20}$'),
    profile_name    text check (profile_name is null or char_length(profile_name) <= 256),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (organization_id, wa_id)
);
comment on table public.contacts is 'Contatos do WhatsApp que conversaram com a organização (plano api_chat).';

create table if not exists public.conversations (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references public.organizations (id) on delete cascade,
    connection_id        uuid not null references public.meta_connections (id) on delete cascade,
    contact_id           uuid not null references public.contacts (id) on delete cascade,
    status               text not null default 'open' check (status in ('open', 'resolved')),
    assigned_to          uuid references auth.users (id) on delete set null,
    unread_count         integer not null default 0 check (unread_count >= 0),
    last_message_at      timestamptz,
    last_inbound_at      timestamptz,  -- abre a janela de atendimento de 24h
    last_message_preview text check (last_message_preview is null or char_length(last_message_preview) <= 160),
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    unique (connection_id, contact_id)
);
comment on table public.conversations is 'Conversas da caixa de entrada (um contato por número conectado).';
create index if not exists idx_conversations_org_last on public.conversations (organization_id, last_message_at desc);
create index if not exists idx_conversations_contact on public.conversations (contact_id);
create index if not exists idx_conversations_connection on public.conversations (connection_id);

-- --------------------------------------------------------------------------
-- Mensagens (entrada e saída)
--   content só é preenchido no plano api_chat; no plano api ficam apenas
--   metadados (tipo, status, ids) para rastrear o estado da entrega.
-- --------------------------------------------------------------------------
create table if not exists public.messages (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    connection_id   uuid references public.meta_connections (id) on delete set null,
    conversation_id uuid references public.conversations (id) on delete cascade,
    direction       text not null check (direction in ('inbound', 'outbound')),
    wamid           text,
    contact_wa_id   text not null,
    type            text not null,
    content         jsonb,
    status          text not null default 'accepted'
                    check (status in ('received', 'accepted', 'sent', 'delivered', 'read', 'failed', 'test')),
    error           jsonb,
    source          text not null default 'api' check (source in ('api', 'dashboard', 'webhook')),
    api_key_id      uuid references public.api_keys (id) on delete set null,
    sent_by         uuid references auth.users (id) on delete set null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
comment on table public.messages is 'Mensagens enviadas/recebidas. Conteúdo apenas no plano api_chat, sujeito à retenção.';
create unique index if not exists uq_messages_connection_wamid
    on public.messages (connection_id, wamid) where wamid is not null;
create index if not exists idx_messages_wamid on public.messages (wamid);
create index if not exists idx_messages_org_time on public.messages (organization_id, created_at desc);
create index if not exists idx_messages_conversation_time on public.messages (conversation_id, created_at);
create index if not exists idx_messages_api_key on public.messages (api_key_id);
create index if not exists idx_messages_sent_by on public.messages (sent_by);

-- --------------------------------------------------------------------------
-- Webhooks de saída (endpoints dos clientes) e entregas
-- --------------------------------------------------------------------------
create table if not exists public.webhook_endpoints (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    url             text not null check (char_length(url) <= 2048),
    description     text check (description is null or char_length(description) <= 200),
    events          jsonb not null default '[]'::jsonb,
    secret_encrypted text not null,    -- Fernet; exibido só na criação/rotação
    status          text not null default 'active' check (status in ('active', 'disabled')),
    created_by      uuid references auth.users (id) on delete set null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
comment on table public.webhook_endpoints is 'URLs HTTPS de clientes, validadas contra SSRF no backend.';
create index if not exists idx_webhook_endpoints_org on public.webhook_endpoints (organization_id);
-- Números cujos eventos o endpoint recebe; lista vazia = todos da organização.
alter table public.webhook_endpoints add column if not exists phone_number_ids jsonb not null default '[]'::jsonb;

create table if not exists public.webhook_deliveries (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    endpoint_id     uuid not null references public.webhook_endpoints (id) on delete cascade,
    event_type      text not null,
    payload         jsonb not null,
    attempts        integer not null default 0,
    max_attempts    integer not null default 6,
    status          text not null default 'pending' check (status in ('pending', 'delivered', 'failed')),
    response_status integer,
    response_body   text,
    last_error      text,
    next_retry_at   timestamptz,
    delivered_at    timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
alter table public.webhook_deliveries add column if not exists locked_until timestamptz;
comment on table public.webhook_deliveries is 'Fila/histórico de entregas de webhooks de saída (retries com backoff).';
create index if not exists idx_webhook_deliveries_org on public.webhook_deliveries (organization_id, created_at desc);
create index if not exists idx_webhook_deliveries_endpoint on public.webhook_deliveries (endpoint_id);
drop index if exists public.idx_webhook_deliveries_retry;
create index if not exists idx_webhook_deliveries_due
    on public.webhook_deliveries (next_retry_at) where status = 'pending';

-- --------------------------------------------------------------------------
-- Eventos recebidos da Meta (deduplicados)
-- --------------------------------------------------------------------------
create table if not exists public.webhook_events (
    id              uuid primary key default gen_random_uuid(),
    dedupe_key      text not null unique,
    organization_id uuid references public.organizations (id) on delete set null,
    channel         text not null default 'whatsapp',
    event_type      text not null,
    payload         jsonb not null,   -- mínimo: ids, tipo, timestamps (sem texto)
    status          text not null default 'processed' check (status in ('processed', 'ignored', 'error')),
    received_at     timestamptz not null default now()
);
comment on table public.webhook_events is 'Eventos de webhook da Meta deduplicados. Reter pelo mínimo necessário.';
create index if not exists idx_webhook_events_org on public.webhook_events (organization_id, received_at desc);

-- --------------------------------------------------------------------------
-- Idempotência de operações sensíveis (envio de mensagens)
-- --------------------------------------------------------------------------
create table if not exists public.idempotency_keys (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    key             text not null check (char_length(key) between 8 and 128),
    endpoint        text not null,
    response        jsonb,
    created_at      timestamptz not null default now(),
    unique (organization_id, key, endpoint)
);
alter table public.idempotency_keys alter column response drop not null;
alter table public.idempotency_keys add column if not exists status text not null default 'completed';
alter table public.idempotency_keys add column if not exists status_code integer;
alter table public.idempotency_keys drop constraint if exists idempotency_keys_status_check;
alter table public.idempotency_keys add constraint idempotency_keys_status_check
    check (status in ('processing', 'completed'));
comment on table public.idempotency_keys is 'Reserva + resposta de requisições com Idempotency-Key (limpar após 24h).';

-- --------------------------------------------------------------------------
-- Logs de requisições e auditoria
-- --------------------------------------------------------------------------
create table if not exists public.api_request_logs (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid references public.organizations (id) on delete set null,
    api_key_id      uuid references public.api_keys (id) on delete set null,
    request_id      text not null,
    method          text not null,
    endpoint        text not null,
    status_code     integer not null,
    latency_ms      integer,
    error_code      text,
    created_at      timestamptz not null default now()
);
comment on table public.api_request_logs is 'Métricas de uso da API. Sem Authorization, tokens, cookies ou corpo.';
create index if not exists idx_api_logs_org_time on public.api_request_logs (organization_id, created_at desc);
create index if not exists idx_api_logs_key on public.api_request_logs (api_key_id);

create table if not exists public.audit_logs (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid references public.organizations (id) on delete set null,
    actor_user_id   uuid references auth.users (id) on delete set null,
    action          text not null,
    target_type     text,
    target_id       text,
    metadata        jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now()
);
comment on table public.audit_logs is 'Trilha de auditoria. Nunca armazenar segredos em metadata.';
create index if not exists idx_audit_logs_org_time on public.audit_logs (organization_id, created_at desc);

-- --------------------------------------------------------------------------
-- Triggers de updated_at
-- --------------------------------------------------------------------------
do $$
declare t text;
begin
    foreach t in array array['organizations', 'meta_connections', 'webhook_endpoints',
                             'webhook_deliveries', 'contacts', 'conversations', 'messages']
    loop
        execute format('drop trigger if exists trg_%1$s_updated on public.%1$I', t);
        execute format('create trigger trg_%1$s_updated before update on public.%1$I
                        for each row execute function public.set_updated_at()', t);
    end loop;
end;
$$;

-- --------------------------------------------------------------------------
-- Fila de retries: reivindica entregas vencidas sem bloquear outros workers.
-- Chamada pelo backend (service role) via RPC. Não exposta a anon/authenticated.
-- --------------------------------------------------------------------------
create or replace function public.claim_due_webhook_deliveries(p_limit integer default 25)
returns setof public.webhook_deliveries
language sql
set search_path = ''
as $$
    update public.webhook_deliveries d
       set locked_until = now() + interval '2 minutes'
     where d.id in (
        select id from public.webhook_deliveries
         where status = 'pending'
           and next_retry_at <= now()
           and (locked_until is null or locked_until < now())
         order by next_retry_at
         limit greatest(1, least(p_limit, 100))
         for update skip locked
     )
    returning d.*;
$$;
revoke execute on function public.claim_due_webhook_deliveries(integer) from public, anon, authenticated;
grant execute on function public.claim_due_webhook_deliveries(integer) to service_role;

-- --------------------------------------------------------------------------
-- Incrementa não lidas de forma atômica (evita condição de corrida).
-- --------------------------------------------------------------------------
create or replace function public.bump_conversation(
    p_conversation_id uuid, p_preview text, p_inbound boolean, p_at timestamptz)
returns void
language sql
set search_path = ''
as $$
    update public.conversations
       set unread_count = unread_count + case when p_inbound then 1 else 0 end,
           last_message_at = greatest(coalesce(last_message_at, p_at), p_at),
           last_inbound_at = case when p_inbound
                                  then greatest(coalesce(last_inbound_at, p_at), p_at)
                                  else last_inbound_at end,
           last_message_preview = left(p_preview, 160),
           status = case when p_inbound then 'open' else status end
     where id = p_conversation_id;
$$;
revoke execute on function public.bump_conversation(uuid, text, boolean, timestamptz) from public, anon, authenticated;
grant execute on function public.bump_conversation(uuid, text, boolean, timestamptz) to service_role;

-- ==========================================================================
-- ROW LEVEL SECURITY
-- ==========================================================================
create or replace function private.is_org_member(org_id uuid)
returns boolean
language sql
security definer
set search_path = ''
stable
as $$
    select exists (
        select 1 from public.organization_members m
         where m.organization_id = org_id and m.user_id = (select auth.uid())
    );
$$;

create or replace function private.has_org_role(org_id uuid, min_role text)
returns boolean
language sql
security definer
set search_path = ''
stable
as $$
    select exists (
        select 1 from public.organization_members m
         where m.organization_id = org_id
           and m.user_id = (select auth.uid())
           and (case m.role when 'owner' then 4 when 'admin' then 3
                            when 'developer' then 2 else 1 end)
               >= (case min_role when 'owner' then 4 when 'admin' then 3
                                 when 'developer' then 2 else 1 end)
    );
$$;

revoke all on function private.is_org_member(uuid) from public, anon;
revoke all on function private.has_org_role(uuid, text) from public, anon;
grant usage on schema private to authenticated;
grant execute on function private.is_org_member(uuid) to authenticated;
grant execute on function private.has_org_role(uuid, text) to authenticated;

-- Remove políticas/funções de versões anteriores (nomes antigos).
do $$
declare r record;
begin
    for r in select policyname, tablename from pg_policies where schemaname = 'public'
              and tablename in ('organizations', 'organization_members', 'organization_invitations',
                                'meta_connections', 'api_keys', 'contacts', 'conversations',
                                'messages', 'webhook_endpoints', 'webhook_deliveries',
                                'webhook_events', 'idempotency_keys', 'api_request_logs', 'audit_logs')
    loop
        execute format('drop policy if exists %I on public.%I', r.policyname, r.tablename);
    end loop;
end;
$$;
drop function if exists public.is_org_member(uuid);
drop function if exists public.has_org_role(uuid, text);

-- Leitura por membros da organização; escrita apenas pelo backend.
do $$
declare t text;
begin
    foreach t in array array['organization_members', 'organization_invitations', 'meta_connections',
                             'api_keys', 'contacts', 'conversations', 'messages',
                             'webhook_endpoints', 'webhook_deliveries', 'idempotency_keys']
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('create policy %1$s_member_select on public.%1$I for select to authenticated
                        using ((select private.is_org_member(organization_id)))', t);
    end loop;

    foreach t in array array['webhook_events', 'api_request_logs', 'audit_logs']
    loop
        execute format('alter table public.%I enable row level security', t);
        execute format('create policy %1$s_member_select on public.%1$I for select to authenticated
                        using (organization_id is not null
                               and (select private.is_org_member(organization_id)))', t);
    end loop;
end;
$$;

alter table public.organizations enable row level security;
create policy organizations_member_select on public.organizations for select to authenticated
    using ((select private.is_org_member(id)));
create policy organizations_admin_update on public.organizations for update to authenticated
    using ((select private.has_org_role(id, 'admin')))
    with check ((select private.has_org_role(id, 'admin')));

-- Colunas sensíveis nunca devem ser lidas com a anon key + JWT de usuário,
-- mesmo pelos membros (o painel lê tudo pelo backend). Privilégio de coluna só
-- restringe se o privilégio de TABELA for revogado antes.
revoke select on public.meta_connections, public.api_keys, public.webhook_endpoints,
                 public.organization_invitations from anon, authenticated;
grant select (id, organization_id, channel, waba_id, phone_number_id, display_phone_number,
              verified_name, status, granted_scopes, connected_by, connected_at, disconnected_at,
              last_validated_at, metadata, created_at, updated_at, connection_type, business_id,
              quality_rating, name_status, webhook_subscribed, registered, last_error)
    on public.meta_connections to authenticated;
grant select (id, organization_id, name, environment, key_prefix, key_suffix, scopes, created_by,
              last_used_at, expires_at, revoked_at, created_at, phone_number_ids)
    on public.api_keys to authenticated;
grant select (id, organization_id, url, description, events, status, created_by, created_at, updated_at,
              phone_number_ids)
    on public.webhook_endpoints to authenticated;
grant select (id, organization_id, email, role, invited_by, expires_at, accepted_at, revoked_at, created_at)
    on public.organization_invitations to authenticated;

-- ==========================================================================
-- RETENÇÃO — execute periodicamente (pg_cron no Supabase, ou o comando
-- `flask purge-data` / endpoint /internal/cron/purge do backend):
--   delete from public.api_request_logs where created_at < now() - interval '90 days';
--   delete from public.webhook_events   where received_at < now() - interval '30 days';
--   delete from public.webhook_deliveries where status <> 'pending' and created_at < now() - interval '30 days';
--   delete from public.idempotency_keys where created_at < now() - interval '1 day';
--   Mensagens: por organização, conforme organizations.message_retention_days.
-- ==========================================================================
