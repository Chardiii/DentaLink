-- Results of the material demand forecast (ml_models/engine.py). A run takes about a minute,
-- so the app runs it in the background and the Forecast Analytics page shows the latest result.
create table if not exists public.forecast_runs (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    finished_at timestamptz,
    requested_by uuid references public.profiles(id) on delete set null,
    source text not null default 'simulated' check (source in ('simulated', 'real')),
    status text not null default 'running' check (status in ('running', 'done', 'failed')),
    result jsonb,
    error text
);

create index if not exists forecast_runs_created_at_idx on public.forecast_runs (created_at desc);

-- Only the server (service role) reads and writes forecast runs.
alter table public.forecast_runs enable row level security;
