create table if not exists public.login_history (
    id uuid primary key default gen_random_uuid(),
    user_id uuid references auth.users(id) on delete set null,
    email text,
    event_type text not null check (event_type in ('success', 'failed', 'blocked', 'logout')),
    reason text,
    session_id uuid,
    ip_address text,
    user_agent text,
    created_at timestamptz not null default now()
);

create index if not exists login_history_created_at_idx
    on public.login_history (created_at desc);
create index if not exists login_history_email_created_at_idx
    on public.login_history (email, created_at desc);
create index if not exists login_history_user_id_created_at_idx
    on public.login_history (user_id, created_at desc);

alter table public.login_history enable row level security;
revoke all on public.login_history from anon, authenticated;
grant all on public.login_history to service_role;
