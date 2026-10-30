-- Admins can mark a case as a rush job; technicians see it first in their pipeline.
alter table public.dental_cases
    add column if not exists is_rush boolean not null default false;
