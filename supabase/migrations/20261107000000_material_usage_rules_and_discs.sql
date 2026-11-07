-- Material usage rules and zirconia disc tracking.
--
-- 1) Usage rules: how much of an Inventory item a case uses at a production stage, so the
--    technician confirms a calculated amount instead of guessing a percentage.
--      usage_stage       stage it is used at (milling, packing or finishing)
--      usage_applies_to  comma list matched against the case: prescription materials
--                        (zirconia, emax, porcelain, pfm, acrylic, pmma, metal...) or words in
--                        the case type (e.g. "denture" matches upper_full_denture)
--      usage_basis       unit (per tooth/unit), arch (per upper/lower arch) or case
--      usage_amount      amount of one stock unit used per basis, e.g. 0.047 disc per unit
-- 2) Disc tracking: when the lab opens a zirconia disc it writes an ID on the box and keeps
--    milling from it until it is used up. Opening a disc takes one whole disc out of stock;
--    each case records how many units it milled from which disc, which gives the real yield.
alter table public.inventory
    add column if not exists usage_stage text
        check (usage_stage in ('milling_or_printing', 'packing_and_curing', 'finishing_and_quality_check')),
    add column if not exists usage_applies_to text,
    add column if not exists usage_basis text check (usage_basis in ('unit', 'arch', 'case')),
    add column if not exists usage_amount numeric check (usage_amount > 0),
    add column if not exists tracks_discs boolean not null default false;

create table if not exists public.zirconia_discs (
    id uuid primary key default gen_random_uuid(),
    inventory_id uuid not null references public.inventory(id) on delete cascade,
    label text not null,                       -- the ID the lab wrote on the disc's box
    status text not null default 'open' check (status in ('open', 'used_up')),
    opened_at timestamptz not null default now(),
    opened_by uuid references public.profiles(id) on delete set null,
    used_up_at timestamptz,
    used_up_by uuid references public.profiles(id) on delete set null
);
create unique index if not exists zirconia_discs_item_label_idx on public.zirconia_discs (inventory_id, lower(label));

create table if not exists public.zirconia_disc_usage (
    id uuid primary key default gen_random_uuid(),
    disc_id uuid not null references public.zirconia_discs(id) on delete cascade,
    case_id uuid references public.dental_cases(id) on delete set null,
    units integer not null check (units > 0),
    logged_by uuid references public.profiles(id) on delete set null,
    created_at timestamptz not null default now()
);
create index if not exists zirconia_disc_usage_disc_idx on public.zirconia_disc_usage (disc_id);

-- Only the server (service role) reads and writes these.
alter table public.zirconia_discs enable row level security;
alter table public.zirconia_disc_usage enable row level security;

-- Starting rules: typical values for the lab to correct on the Inventory page. Only items
-- that have no rule yet are changed.
update public.inventory set usage_stage = 'milling_or_printing', usage_applies_to = 'zirconia',
       usage_basis = 'unit', usage_amount = 0.047, tracks_discs = true      -- ~21 units per disc
 where item_name ilike '%zirconia disc%' and usage_stage is null;
update public.inventory set usage_stage = 'milling_or_printing', usage_applies_to = 'emax',
       usage_basis = 'unit', usage_amount = 1                               -- one block/ingot per unit
 where item_name ilike '%e.max%block%' and usage_stage is null;
update public.inventory set usage_stage = 'milling_or_printing', usage_applies_to = 'zirconia',
       usage_basis = 'unit', usage_amount = 0.005                           -- ~200 units per bottle
 where item_name ilike '%zirconia coloring liquid%' and usage_stage is null;
update public.inventory set usage_stage = 'packing_and_curing', usage_applies_to = 'acrylic, denture',
       usage_basis = 'arch', usage_amount = 0.05                            -- ~20 arches per pack
 where item_name ilike '%acrylic powder%' and usage_stage is null;
update public.inventory set usage_stage = 'packing_and_curing', usage_applies_to = 'acrylic, denture',
       usage_basis = 'arch', usage_amount = 0.03                            -- ~33 arches per bottle
 where item_name ilike '%monomer%' and usage_stage is null;
update public.inventory set usage_stage = 'finishing_and_quality_check', usage_applies_to = 'zirconia, emax, porcelain, pfm',
       usage_basis = 'unit', usage_amount = 0.002                           -- ~500 units per kit
 where item_name ilike '%stain kit%' and usage_stage is null;
update public.inventory set usage_stage = 'finishing_and_quality_check', usage_applies_to = 'zirconia, emax, porcelain, pfm',
       usage_basis = 'unit', usage_amount = 0.005                           -- ~200 units per jar
 where item_name ilike '%glaze%' and usage_stage is null;
update public.inventory set usage_stage = 'finishing_and_quality_check', usage_applies_to = 'acrylic, denture, pmma',
       usage_basis = 'case', usage_amount = 0.01                            -- ~100 cases per tube
 where item_name ilike '%polishing paste%' and usage_stage is null;
