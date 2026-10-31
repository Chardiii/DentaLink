-- The lab's paper job orders, transcribed and imported from CSV on the admin "Job Order
-- History" page. They are completed work from before DentaLink, kept apart from dental_cases
-- because the clinics that sent them have no DentaLink accounts. Patient names are not stored.
-- The forecast counts these as real demand.
create table if not exists public.job_order_history (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    imported_by uuid references public.profiles(id) on delete set null,
    jo_ref text not null unique,          -- e.g. JO-01, the transcription's own reference
    clinic text,
    dentist_name text,
    clinic_ref text,                      -- the clinic's own job-order number, if any
    cad_designer text,
    porcelain_technician text,
    date_sent date,
    lab_in date,
    lab_out date,
    due_date date,
    case_type text,
    material text not null,
    units integer not null check (units > 0),
    teeth text,
    shade text,
    is_remake text,
    notes text
);

create index if not exists job_order_history_lab_in_idx on public.job_order_history (lab_in);

-- Only the server (service role) reads and writes job order history.
alter table public.job_order_history enable row level security;
