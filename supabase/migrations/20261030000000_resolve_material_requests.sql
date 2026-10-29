-- Let administrators close restock requests: record who resolved each one and when.
-- Runs in one transaction: if an existing row has a status outside this list,
-- the new constraint fails and nothing is changed.

begin;

alter table public.material_requests
    add column if not exists resolved_at timestamptz,
    add column if not exists resolved_by uuid;

alter table public.material_requests drop constraint if exists material_requests_status_check;

alter table public.material_requests add constraint material_requests_status_check check (
    status in ('pending', 'fulfilled', 'dismissed')
);

commit;
