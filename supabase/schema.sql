-- ==========================================================================
-- Schema da plataforma SaaS (Supabase / PostgreSQL)
--
-- COMO INSTALAR:
--   1. Crie um projeto em https://supabase.com
--   2. Abra: SQL Editor > New query
--   3. Cole este arquivo inteiro e clique em "Run"
--   4. Confirme em Database > Tables que as tabelas foram criadas
--
-- NOTAS:
--   - O backend usa a service role (bypass de RLS) e SEMPRE filtra por
--     organization_id validado no servidor. As políticas RLS abaixo são
--     defesa em profundidade para qualquer acesso com a anon key + JWT
--     de usuário (auth.uid()).
--   - Nenhuma política usa USING (true) para dados privados.
--   - Timestamps em UTC (timestamptz default now()).
-- ==========================================================================

create extension if not exists "pgcrypto";

-- --------------------------------------------------------------------------
-- Organizações (tenants)
-- --------------------------------------------------------------------------
create table if not exists public.organizations (
    id            uuid primary key default gen_random_uuid(),
    name          text not null check (char_length(name) between 1 and 120),
    slug          text not null unique check (char_length(slug) between 1 and 140),
    owner_user_id uuid not null references auth.users (id) on delete cascade,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

comment on table public.organizations is 'Empresas/clientes (tenants) da plataforma.';

-- --------------------------------------------------------------------------
-- Membros de organização e papéis
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

comment on table public.organization_members is 'Associação usuário Supabase Auth <-> organização, com papel.';
create index if not exists idx_org_members_user on public.organization_members (user_id);
create index if not exists idx_org_members_org on public.organization_members (organization_id);

-- --------------------------------------------------------------------------
-- Conexões com a Meta (canais/ativos autorizados)
-- --------------------------------------------------------------------------
create table if not exists public.meta_connections (
    id                   uuid primary key default gen_random_uuid(),
    organization_id      uuid not null references public.organizations (id) on delete cascade,
    channel              text not null default 'whatsapp'
                         check (channel in ('whatsapp')),
    waba_id              text,
    phone_number_id      text,
    display_phone_number text,
    verified_name        text,
    -- Access token da Meta criptografado (Fernet). NUNCA texto puro.
    access_token_encrypted text,
    status               text not null default 'pending'
                         check (status in ('pending', 'active', 'error', 'disconnected')),
    granted_scopes       jsonb not null default '[]'::jsonb,
    connected_by         uuid references auth.users (id) on delete set null,
    connected_at         timestamptz,
    disconnected_at      timestamptz,
    last_validated_at    timestamptz,
    metadata             jsonb not null default '{}'::jsonb,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    -- Impede conexão duplicada do mesmo número dentro da mesma organização.
    unique (organization_id, channel, phone_number_id)
);

comment on table public.meta_connections is 'Ativos da Meta autorizados por organização (WABA/números). Token sempre criptografado.';
create index if not exists idx_meta_connections_org on public.meta_connections (organization_id);
create index if not exists idx_meta_connections_phone on public.meta_connections (phone_number_id);

-- --------------------------------------------------------------------------
-- Chaves internas de API (hash apenas — nunca o segredo)
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

comment on table public.api_keys is 'Chaves internas da plataforma (sk_test_/sk_live_). Apenas hash HMAC é armazenado.';
create index if not exists idx_api_keys_org on public.api_keys (organization_id);
create index if not exists idx_api_keys_hash on public.api_keys (key_hash);

-- --------------------------------------------------------------------------
-- Endpoints de webhook dos clientes (saída)
-- --------------------------------------------------------------------------
create table if not exists public.webhook_endpoints (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    url             text not null check (char_length(url) <= 2048),
    description     text check (description is null or char_length(description) <= 200),
    events          jsonb not null default '[]'::jsonb,
    -- Segredo HMAC criptografado (Fernet). Exibido somente na criação/rotação.
    secret_encrypted text not null,
    status          text not null default 'active' check (status in ('active', 'disabled')),
    created_by      uuid references auth.users (id) on delete set null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

comment on table public.webhook_endpoints is 'URLs HTTPS de clientes para eventos de saída, com validação anti-SSRF no backend.';
create index if not exists idx_webhook_endpoints_org on public.webhook_endpoints (organization_id);

-- --------------------------------------------------------------------------
-- Entregas de webhooks de saída (com retries)
-- --------------------------------------------------------------------------
create table if not exists public.webhook_deliveries (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    endpoint_id     uuid not null references public.webhook_endpoints (id) on delete cascade,
    event_type      text not null,
    payload         jsonb not null,
    attempts        integer not null default 0,
    max_attempts    integer not null default 5,
    status          text not null default 'pending'
                    check (status in ('pending', 'delivered', 'failed')),
    response_status integer,
    response_body   text,  -- sanitizado e truncado pelo backend
    last_error      text,  -- mensagem sanitizada
    next_retry_at   timestamptz,
    delivered_at    timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

comment on table public.webhook_deliveries is 'Histórico e fila de entregas de webhooks de saída.';
create index if not exists idx_webhook_deliveries_org on public.webhook_deliveries (organization_id);
create index if not exists idx_webhook_deliveries_retry
    on public.webhook_deliveries (status, next_retry_at) where status = 'pending';

-- --------------------------------------------------------------------------
-- Eventos recebidos da Meta (entrada), com deduplicação
-- --------------------------------------------------------------------------
create table if not exists public.webhook_events (
    id              uuid primary key default gen_random_uuid(),
    dedupe_key      text not null unique,  -- ex.: wamid da mensagem/status ou hash do evento
    organization_id uuid references public.organizations (id) on delete set null,
    channel         text not null default 'whatsapp',
    event_type      text not null,         -- message.received | message.status.updated | ...
    payload         jsonb not null,        -- mínimo necessário, sem conteúdo sensível extra
    status          text not null default 'processed'
                    check (status in ('processed', 'ignored', 'error')),
    received_at     timestamptz not null default now()
);

comment on table public.webhook_events is 'Eventos de webhook da Meta deduplicados. Reter pelo mínimo necessário.';
create index if not exists idx_webhook_events_org on public.webhook_events (organization_id, received_at desc);

-- --------------------------------------------------------------------------
-- Chaves de idempotência (envio de mensagens e operações sensíveis)
-- --------------------------------------------------------------------------
create table if not exists public.idempotency_keys (
    id              uuid primary key default gen_random_uuid(),
    organization_id uuid not null references public.organizations (id) on delete cascade,
    key             text not null check (char_length(key) between 8 and 128),
    endpoint        text not null,
    response        jsonb not null,
    created_at      timestamptz not null default now(),
    unique (organization_id, key, endpoint)
);

comment on table public.idempotency_keys is 'Respostas armazenadas para requisições com Idempotency-Key. Limpar registros antigos periodicamente.';
create index if not exists idx_idempotency_org on public.idempotency_keys (organization_id);

-- --------------------------------------------------------------------------
-- Logs sanitizados de requisições da API própria
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
    error_code      text,  -- código de erro sanitizado (nunca tokens/PII)
    created_at      timestamptz not null default now()
);

comment on table public.api_request_logs is 'Métricas de uso da API. Sem Authorization, tokens, cookies ou corpo.';
create index if not exists idx_api_logs_org_time on public.api_request_logs (organization_id, created_at desc);

-- --------------------------------------------------------------------------
-- Auditoria de ações administrativas
-- --------------------------------------------------------------------------
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
-- Trigger de updated_at
-- --------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_organizations_updated on public.organizations;
create trigger trg_organizations_updated before update on public.organizations
    for each row execute function public.set_updated_at();

drop trigger if exists trg_meta_connections_updated on public.meta_connections;
create trigger trg_meta_connections_updated before update on public.meta_connections
    for each row execute function public.set_updated_at();

drop trigger if exists trg_webhook_endpoints_updated on public.webhook_endpoints;
create trigger trg_webhook_endpoints_updated before update on public.webhook_endpoints
    for each row execute function public.set_updated_at();

drop trigger if exists trg_webhook_deliveries_updated on public.webhook_deliveries;
create trigger trg_webhook_deliveries_updated before update on public.webhook_deliveries
    for each row execute function public.set_updated_at();

-- ==========================================================================
-- ROW LEVEL SECURITY
-- ==========================================================================
-- Funções auxiliares (SECURITY DEFINER) para checar associação/papel sem
-- recursão infinita nas políticas de organization_members.
-- Executam com privilégios do dono da função; mantenha-as mínimas.

create or replace function public.is_org_member(org_id uuid)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
    select exists (
        select 1 from public.organization_members m
        where m.organization_id = org_id and m.user_id = auth.uid()
    );
$$;

create or replace function public.has_org_role(org_id uuid, min_role text)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
    select exists (
        select 1 from public.organization_members m
        where m.organization_id = org_id
          and m.user_id = auth.uid()
          and case m.role
                when 'owner' then 4
                when 'admin' then 3
                when 'developer' then 2
                else 1
              end >= case min_role
                when 'owner' then 4
                when 'admin' then 3
                when 'developer' then 2
                else 1
              end
    );
$$;

-- organizations ------------------------------------------------------------
alter table public.organizations enable row level security;

create policy org_select on public.organizations
    for select using (public.is_org_member(id));

create policy org_update on public.organizations
    for update using (public.has_org_role(id, 'admin'))
    with check (public.has_org_role(id, 'admin'));

-- Inserção de organizações ocorre via backend (service role).

-- organization_members -----------------------------------------------------
alter table public.organization_members enable row level security;

create policy org_members_select on public.organization_members
    for select using (public.is_org_member(organization_id));

-- Gestão de membros (convites) ocorre via backend (service role).

-- meta_connections ---------------------------------------------------------
alter table public.meta_connections enable row level security;

create policy conn_select on public.meta_connections
    for select using (public.is_org_member(organization_id));

create policy conn_delete on public.meta_connections
    for delete using (public.has_org_role(organization_id, 'admin'));

-- Insert/update de conexões ocorrem via backend após validação OAuth.

-- api_keys -----------------------------------------------------------------
alter table public.api_keys enable row level security;

create policy api_keys_select on public.api_keys
    for select using (public.is_org_member(organization_id));

-- Criação/revogação via backend (hash, pepper e auditoria no servidor).

-- webhook_endpoints --------------------------------------------------------
alter table public.webhook_endpoints enable row level security;

create policy wh_endpoints_select on public.webhook_endpoints
    for select using (public.is_org_member(organization_id));

-- webhook_deliveries -------------------------------------------------------
alter table public.webhook_deliveries enable row level security;

create policy wh_deliveries_select on public.webhook_deliveries
    for select using (public.is_org_member(organization_id));

-- webhook_events -----------------------------------------------------------
alter table public.webhook_events enable row level security;

create policy wh_events_select on public.webhook_events
    for select using (
        organization_id is not null and public.is_org_member(organization_id)
    );

-- api_request_logs ---------------------------------------------------------
alter table public.api_request_logs enable row level security;

create policy api_logs_select on public.api_request_logs
    for select using (
        organization_id is not null and public.is_org_member(organization_id)
    );

-- audit_logs ---------------------------------------------------------------
alter table public.audit_logs enable row level security;

create policy audit_select on public.audit_logs
    for select using (
        organization_id is not null and public.is_org_member(organization_id)
    );

-- idempotency_keys ---------------------------------------------------------
alter table public.idempotency_keys enable row level security;

create policy idem_select on public.idempotency_keys
    for select using (public.is_org_member(organization_id));

-- ==========================================================================
-- RETENÇÃO (sugestão): limpeza periódica via cron no Supabase (pg_cron) ou
-- job externo. Exemplos:
--   delete from public.api_request_logs where created_at < now() - interval '90 days';
--   delete from public.webhook_events where received_at < now() - interval '30 days';
--   delete from public.webhook_deliveries
--     where status <> 'pending' and created_at < now() - interval '90 days';
-- ==========================================================================
