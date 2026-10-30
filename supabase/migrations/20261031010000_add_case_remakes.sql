-- Remakes: an accepted delivery issue creates a new case linked to the original.

alter table public.dental_cases
    add column if not exists remake_of uuid references public.dental_cases(id) on delete set null,
    add column if not exists remake_reason text;

create index if not exists dental_cases_remake_of_idx on public.dental_cases (remake_of);

alter table public.case_issue_reports
    add column if not exists remake_case_id uuid references public.dental_cases(id) on delete set null,
    add column if not exists return_requested boolean not null default false;
