-- =============================================================================
-- WhatsApp AI Assistant — Supabase Schema
-- Run this in the Supabase SQL Editor (Project > SQL Editor > New query)
-- =============================================================================

create extension if not exists pgcrypto;

-- -----------------------------------------------------------------------------
-- Trigger helper: keep `updated_at` current on row updates
-- -----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- =============================================================================
-- users
-- =============================================================================
create table if not exists public.users (
    id          uuid primary key default gen_random_uuid(),
    phone       text not null unique,
    name        text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

comment on table public.users is 'WhatsApp end users, identified by phone number.';
comment on column public.users.phone is 'E.164-ish phone number extracted from the WhatsApp JID (no @s.whatsapp.net suffix).';

create index if not exists idx_users_phone on public.users (phone);

drop trigger if exists trg_users_updated_at on public.users;
create trigger trg_users_updated_at
    before update on public.users
    for each row
    execute function public.set_updated_at();

-- =============================================================================
-- leads
-- =============================================================================
create table if not exists public.leads (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references public.users (id) on delete cascade,
    budget       numeric(12, 2),
    status       text not null default 'new'
                 check (status in ('new', 'qualified', 'contacted', 'won', 'lost')),
    crm_synced   boolean not null default false,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

comment on table public.leads is 'Qualified leads captured by the AI assistant via the save_qualified_lead tool.';

create index if not exists idx_leads_user_id on public.leads (user_id);
create index if not exists idx_leads_status on public.leads (status);
create index if not exists idx_leads_crm_synced on public.leads (crm_synced) where crm_synced = false;

drop trigger if exists trg_leads_updated_at on public.leads;
create trigger trg_leads_updated_at
    before update on public.leads
    for each row
    execute function public.set_updated_at();

-- =============================================================================
-- chat_history
-- =============================================================================
create table if not exists public.chat_history (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references public.users (id) on delete cascade,
    role          text not null check (role in ('user', 'assistant')),
    message_text  text not null,
    created_at    timestamptz not null default now()
);

comment on table public.chat_history is 'Full conversation log per user, used to build Claude context windows.';

create index if not exists idx_chat_history_user_id on public.chat_history (user_id);
-- Composite index: fetching "last N messages for this user" is the hot query.
create index if not exists idx_chat_history_user_created
    on public.chat_history (user_id, created_at desc);

-- WhatsApp gateways (Evolution API/Baileys included) can redeliver the same
-- webhook event more than once. wa_message_id lets the backend recognize and
-- skip a message it already processed, so a retry never double-replies or
-- double-inserts a lead. Only inbound messages carry one; the assistant's own
-- replies leave it null.
alter table public.chat_history add column if not exists wa_message_id text;
create unique index if not exists idx_chat_history_wa_message_id
    on public.chat_history (wa_message_id) where wa_message_id is not null;

-- =============================================================================
-- Row Level Security
-- =============================================================================
-- The FastAPI backend talks to Supabase using the SERVICE_ROLE key, which
-- bypasses RLS entirely by design. Enabling RLS here is defense-in-depth: it
-- guarantees that if the SUPABASE_ANON_KEY is ever used (e.g. leaked into a
-- client app, or a future public-facing feature), no rows are readable or
-- writable through it, since no policies are defined for the `anon` or
-- `authenticated` roles below.

alter table public.users enable row level security;
alter table public.leads enable row level security;
alter table public.chat_history enable row level security;

-- Explicit policies for the service_role (redundant with RLS bypass, but
-- documents intent and keeps the schema self-explanatory).
create policy "service_role_full_access_users"
    on public.users
    for all
    to service_role
    using (true)
    with check (true);

create policy "service_role_full_access_leads"
    on public.leads
    for all
    to service_role
    using (true)
    with check (true);

create policy "service_role_full_access_chat_history"
    on public.chat_history
    for all
    to service_role
    using (true)
    with check (true);

-- No policies are created for `anon` / `authenticated` — with RLS enabled and
-- zero matching policies, all access from those roles is denied by default.
