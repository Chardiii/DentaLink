-- Materials from the lab's supplier receipts (Y2 Dental Supply, DR-202644 on Oct 22, 2026 and
-- DR-202701 on Oct 30, 2026) and its job orders. Each starts at 0 in stock: the receipts show
-- what was bought, not what is left, so the lab enters a shelf count with Adjust stock.
-- Names carry the shade so Forecast Analytics can match each disc to its forecast.
-- Reorder levels follow the forecast's reorder points (stock covering a 2-4 working-day delivery).
-- Skips any material whose name is already in inventory.
insert into public.inventory (item_name, category, unit, supplier_name, unit_cost, minimum_threshold, quantity)
select v.item_name, v.category, v.unit, v.supplier_name, v.unit_cost, v.minimum_threshold, 0
from (values
    ('Sagemax Zirconia Disc A1 20mm',   'Zirconia',        'disc',   'Y2 Dental Supply', 12600, 2),
    ('Sagemax Zirconia Disc A2 20mm',   'Zirconia',        'disc',   'Y2 Dental Supply', 12600, 4),
    ('Sagemax Zirconia Disc A3 20mm',   'Zirconia',        'disc',   'Y2 Dental Supply', 12600, 3),
    ('Sagemax Zirconia Disc OM2 20mm',  'Zirconia',        'disc',   'Y2 Dental Supply',  8500, 1),
    ('Sagemax Zirconia Disc OM3 20mm',  'Zirconia',        'disc',   'Y2 Dental Supply', 14465, 1),
    ('Zirconia Coloring Liquid (Upcera)', 'Consumables',   'bottle', 'Y2 Dental Supply',     0, 1),
    ('IPS e.max Block / Press Ingot',   'Ceramic & E.max', 'pc',     null,                   0, 30),
    ('Ceramage Composite (Shofu)',      'Consumables',     'syringe', null,                  0, 1)
) as v(item_name, category, unit, supplier_name, unit_cost, minimum_threshold)
where not exists (
    select 1 from public.inventory i where lower(i.item_name) = lower(v.item_name)
);
