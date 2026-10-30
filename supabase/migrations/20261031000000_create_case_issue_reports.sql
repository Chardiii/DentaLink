-- Delivery issue reports: a dentist reports a problem with a delivered case;
-- an administrator reviews it and decides whether it becomes a remake.

create table if not exists public.case_issue_reports (
    id uuid primary key default gen_random_uuid(),
    case_id uuid not null references public.dental_cases(id) on delete cascade,
    dentist_id uuid not null,
    issue_type text not null check (issue_type in ('poor_fit', 'wrong_shade', 'broken', 'wrong_case', 'other')),
    details text,
    status text not null default 'open' check (status in ('open', 'remake_accepted', 'no_action')),
    admin_note text,
    reviewed_by uuid,
    reviewed_at timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists case_issue_reports_case_id_idx on public.case_issue_reports (case_id);
create index if not exists case_issue_reports_status_created_at_idx on public.case_issue_reports (status, created_at desc);

-- At most one open report per case at a time.
create unique index if not exists case_issue_reports_one_open_per_case
    on public.case_issue_reports (case_id) where status = 'open';

alter table public.case_issue_reports enable row level security;
revoke all on public.case_issue_reports from anon, authenticated;
grant all on public.case_issue_reports to service_role;
