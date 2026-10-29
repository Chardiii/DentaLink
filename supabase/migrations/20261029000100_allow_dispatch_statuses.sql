-- Allow the QC review and QR dispatch statuses on dental_cases.
-- Runs in one transaction: if any existing row has a status missing from this
-- list, the new constraint fails and the original constraint is kept.

begin;

alter table public.dental_cases drop constraint if exists dental_cases_status_check;

alter table public.dental_cases add constraint dental_cases_status_check check (
    status in (
        -- Intake
        'draft',
        'submitted',
        'assigned',
        'needs_revision',
        -- Bench stages
        'in_progress',
        'cad_designing',
        'wax_up_or_try_in',
        'milling_or_printing',
        'packing_and_curing',
        'finishing_and_quality_check',
        -- QC review and dispatch
        'pending_admin_review',
        'ready_for_dispatch',
        'in_transit',
        'delivered',
        -- Closed before QR dispatch existed, or cancelled
        'completed',
        'cancelled'
    )
);

commit;
