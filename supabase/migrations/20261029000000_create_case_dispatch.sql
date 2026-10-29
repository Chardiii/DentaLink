-- QR dispatch tracking: one active label per case and an append-only scan log.
--
-- New dental_cases.status values used by the app:
--   pending_admin_review -> ready_for_dispatch -> in_transit -> delivered
-- If dental_cases.status has a CHECK constraint or enum, add these values to it.

alter table public.dental_cases
    add column if not exists dispatched_at timestamptz,
    add column if not exists delivered_at timestamptz;

create table if not exists public.case_dispatches (
    id uuid primary key default gen_random_uuid(),
    case_id uuid not null references public.dental_cases(id) on delete cascade,
    token text not null unique,
    status text not null default 'active' check (status in ('active', 'voided')),
    created_by uuid,
    created_at timestamptz not null default now(),
    voided_at timestamptz
);

create unique index if not exists case_dispatches_one_active_per_case
    on public.case_dispatches (case_id) where status = 'active';

-- Foreign keys use the default (no action) so a case or label with history
-- cannot be deleted out from under its audit trail.
create table if not exists public.case_dispatch_events (
    id uuid primary key default gen_random_uuid(),
    case_id uuid not null references public.dental_cases(id),
    dispatch_id uuid references public.case_dispatches(id),
    event_type text not null check (event_type in ('issued', 'dispatched', 'delivered', 'voided', 'denied')),
    actor_id uuid,
    courier_name text,
    received_by text,
    notes text,
    ip_address text,
    user_agent text,
    created_at timestamptz not null default now()
);

create index if not exists case_dispatch_events_case_id_created_at_idx
    on public.case_dispatch_events (case_id, created_at);

create or replace function public.prevent_dispatch_event_changes()
returns trigger
language plpgsql
as $$
begin
    raise exception 'case_dispatch_events is append-only';
end;
$$;

drop trigger if exists case_dispatch_events_append_only on public.case_dispatch_events;
create trigger case_dispatch_events_append_only
    before update or delete on public.case_dispatch_events
    for each row execute function public.prevent_dispatch_event_changes();

alter table public.case_dispatches enable row level security;
alter table public.case_dispatch_events enable row level security;
revoke all on public.case_dispatches from anon, authenticated;
revoke all on public.case_dispatch_events from anon, authenticated;
grant all on public.case_dispatches to service_role;
grant all on public.case_dispatch_events to service_role;
