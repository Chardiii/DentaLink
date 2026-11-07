-- Scheduled backups. The admin sets how often the system backs itself up (daily, weekly or
-- monthly, at a time in Philippine time); the server checks every minute and saves the backup
-- ZIP to the private "system-backups" storage bucket, keeping the latest few.

-- One row holding the schedule.
create table if not exists public.backup_settings (
    id integer primary key default 1 check (id = 1),
    frequency text not null default 'off' check (frequency in ('off', 'daily', 'weekly', 'monthly')),
    time_of_day text not null default '12:00' check (time_of_day ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
    weekday integer not null default 6 check (weekday between 0 and 6),        -- 0 = Monday, 6 = Sunday
    month_day integer not null default 1 check (month_day between 1 and 28),  -- 28 so every month has it
    keep_count integer not null default 7 check (keep_count between 1 and 30),
    updated_at timestamptz not null default now(),
    updated_by uuid references public.profiles(id) on delete set null
);
insert into public.backup_settings (id) values (1) on conflict (id) do nothing;

-- Every backup the server made, scheduled or from the Backup page.
create table if not exists public.backup_runs (
    id uuid primary key default gen_random_uuid(),
    kind text not null check (kind in ('scheduled', 'manual')),
    status text not null default 'running' check (status in ('running', 'succeeded', 'failed')),
    due_at timestamptz,                    -- the scheduled time this run covers (scheduled runs)
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    file_path text,                        -- object in the system-backups bucket
    size_bytes bigint,
    error text,
    created_by uuid references public.profiles(id) on delete set null
);
-- A scheduled time is claimed by exactly one run, so two app processes never both back it up.
create unique index if not exists backup_runs_due_once_idx on public.backup_runs (due_at) where kind = 'scheduled';
create index if not exists backup_runs_started_idx on public.backup_runs (started_at desc);

alter table public.backup_settings enable row level security;
alter table public.backup_runs enable row level security;

-- Private bucket for the backup ZIPs (only the server's service role reads and writes it).
insert into storage.buckets (id, name, public)
values ('system-backups', 'system-backups', false)
on conflict (id) do nothing;
