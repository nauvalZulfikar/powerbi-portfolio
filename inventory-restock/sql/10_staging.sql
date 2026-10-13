-- Staging: typed, cleaned tables over the raw exports. One fix per known source defect.
create schema if not exists stg;

-- The sales export was cut at Excel's 1,048,575-row limit part-way through February:
-- January is complete for all 79 stores, February only for 11. Keep January only.
create or replace table stg.sales as
select InventoryId as inventory_id, Store as store, Brand as brand, SalesDate as sales_date,
       SalesQuantity as units, SalesDollars as dollars, SalesPrice as price, VendorNo as vendor_no
from raw.sales
where SalesDate < date '2016-02-01';

create or replace table stg.purchases as
select InventoryId as inventory_id, Store as store, Brand as brand, VendorNumber as vendor_no,
       PONumber as po_number, PODate as po_date, ReceivingDate as receiving_date,
       InvoiceDate as invoice_date, PayDate as pay_date, PurchasePrice as unit_cost,
       Quantity as units, Dollars as dollars,
       ReceivingDate - PODate as lead_days
from raw.purchases;

create or replace table stg.invoices as
select VendorNumber as vendor_no, PONumber as po_number, PODate as po_date, InvoiceDate as invoice_date,
       PayDate as pay_date, Quantity as units, Dollars as dollars, Freight as freight, Approval as approval
from raw.invoices;

-- Vendor names carry trailing padding, and three vendor numbers appear under two names
-- (a rename, e.g. Southern Wine & Spirits -> Southern Glazer's). Keep the most recent name.
create or replace table stg.vendors as
select vendor_no, arg_max(trim(VendorName), PODate) as vendor
from (select VendorNumber as vendor_no, VendorName, PODate from raw.purchases
      union all
      select VendorNumber, VendorName, date '2016-12-31' from raw.price_list)
group by vendor_no;

-- One row per inventory line (store x brand) seen anywhere: begin count, end count or a receipt.
create or replace table stg.stock as
with ids as (
    select InventoryId as inventory_id, Store as store, Brand as brand from raw.begin_inventory
    union select InventoryId, Store, Brand from raw.end_inventory
    union select InventoryId, Store, Brand from raw.purchases
)
select i.inventory_id, i.store, i.brand,
       coalesce(b.onHand, 0) as begin_units,
       coalesce(r.units, 0)  as received_units,
       coalesce(e.onHand, 0) as end_units,
       r.first_receipt, r.last_receipt,
       -- listed at the year-end count but not at the opening count: a line added during 2016
       b.InventoryId is null and e.InventoryId is not null as new_line_2016,
       case when b.InventoryId is not null then 'Carried over' when e.InventoryId is not null then 'New in 2016'
            else 'Received, not counted' end as line_origin
from ids i
left join raw.begin_inventory b on b.InventoryId = i.inventory_id
left join raw.end_inventory  e on e.InventoryId = i.inventory_id
left join (select inventory_id, sum(units) units, min(receiving_date) first_receipt, max(receiving_date) last_receipt
           from stg.purchases group by 1) r using (inventory_id);

create or replace table stg.stores as
select Store as store, arg_max(City, src) as city
from (select Store, City, 1 src from raw.begin_inventory union all select Store, City, 2 from raw.end_inventory)
group by Store;

-- Item master: the Dec price list covers every brand; descriptions differ slightly between files
-- (vintage year, abbreviations), so the price list wins.
create or replace table stg.items as
select Brand as brand, trim(Description) as description, Size as size, Volume as volume_ml,
       case Classification when 1 then 'Spirits' else 'Wine' end as category,
       VendorNumber as vendor_no, Price as retail_price, PurchasePrice as list_cost
from raw.price_list;
