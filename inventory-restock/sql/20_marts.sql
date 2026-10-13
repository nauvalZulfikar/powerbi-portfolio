-- Star schema + restock engine, everything as of 31 Dec 2016 (the last physical stock count).
create schema if not exists mart;

-- Defaults the report opens with; the Power BI what-if sliders recompute the same formulas.
create or replace macro service_z() as 1.6448536;     -- 95% cycle service level
create or replace macro review_days() as 7;       -- orders are placed weekly
create or replace macro overstock_days() as 180;  -- more than ~6 months of cover
create or replace macro initcap(s) as array_to_string(list_transform(string_split(lower(s), ' '), w -> upper(w[1]) || w[2:]), ' ');

create or replace table mart.dim_date as
select d::date as date, year(d) as year, strftime(d, '%b %Y') as month, year(d) * 100 + month(d) as month_sort,
       date_trunc('month', d)::date as month_start, strftime(d, '%a') as weekday, isodow(d) as weekday_sort,
       -- calendar columns for calendar-based time intelligence (Gregorian calendar in the model)
       'Q' || quarter(d) as quarter, year(d) * 10 + quarter(d) as year_quarter,
       month(d) as month_of_year, strftime(d, '%b') as month_name
-- Dec 2015 (first PO dates) to Feb 2017 (last invoice payment), so the inactive invoice and payment dates find a row.
from range(date '2015-12-01', date '2017-03-01', interval 1 day) t(d);

create or replace table mart.dim_store as
select s.store, initcap(s.city) as city, 'Store ' || s.store || ' · ' || initcap(s.city) as store_label,
       min(b.begin_units) = 0 as opened_2016,
       -- nothing on any shelf at the year-end count: the store closed during 2016 (store 46: last receipt 27 Jul)
       min(b.end_units) = 0 as closed_2016
from stg.stores s join (select store, sum(begin_units) begin_units, sum(end_units) end_units from stg.stock group by 1) b
     using (store)
group by all;

-- Shipments: one PO is received store by store on different days, so lead time is measured per
-- PO x store x receiving date.
create or replace table mart.dim_vendor as
with ship as (
    select distinct vendor_no, po_number, store, po_date, receiving_date, lead_days from stg.purchases
), lt as (
    select vendor_no, count(*) shipments, avg(lead_days) lead_days, stddev_samp(lead_days) lead_sd,
           quantile_cont(lead_days, 0.9) lead_p90
    from ship group by 1
), inv as (
    select vendor_no, count(*) pos, sum(dollars) spend, sum(freight) freight, avg(pay_date - invoice_date) days_to_pay
    from stg.invoices group by 1
)
select v.vendor_no, v.vendor, coalesce(lt.shipments, 0) as shipments,
       -- vendors with no 2016 shipments get the chain-wide lead time
       coalesce(lt.lead_days, (select avg(lead_days) from ship)) as lead_days,
       coalesce(lt.lead_sd, (select stddev_samp(lead_days) from ship)) as lead_sd, lt.lead_p90,
       lt.vendor_no is null as lead_estimated,
       coalesce(inv.pos, 0) as purchase_orders, inv.spend, inv.freight, inv.freight / inv.spend as freight_pct,
       inv.days_to_pay
from stg.vendors v left join lt using (vendor_no) left join inv using (vendor_no);

-- ABC on 2016 chain-wide sales value (implied units sold x shelf price).
create or replace table mart.dim_item as
with cost as (  -- latest 2016 cost and vendor; the Dec 2017 price list fills brands not bought in 2016
    select brand, arg_max(unit_cost, receiving_date) unit_cost, arg_max(vendor_no, receiving_date) vendor_no
    from stg.purchases group by 1
), val as (
    select k.brand, sum(k.begin_units + k.received_units - k.end_units) * any_value(i.retail_price) sales_value
    from stg.stock k join stg.items i using (brand) group by 1
), ranked as (
    select brand, sales_value,
           sum(sales_value) over (order by sales_value desc, brand rows unbounded preceding) / sum(sales_value) over () cum
    from val
)
select i.brand, i.description, i.size, i.volume_ml, i.category, coalesce(c.vendor_no, i.vendor_no) as vendor_no, i.retail_price,
       coalesce(c.unit_cost, i.list_cost) as unit_cost,
       coalesce(r.sales_value, 0) as sales_value_2016,
       case when r.cum <= 0.80 then 'A' when r.cum <= 0.95 then 'B' else 'C' end as abc
from stg.items i left join cost c using (brand) left join ranked r using (brand);

create or replace table mart.dim_abc as
select * from (values ('A', 'A · top 80% of sales', 1), ('B', 'B · next 15%', 2), ('C', 'C · last 5%', 3)) t(abc, abc_label, abc_sort);

create or replace table mart.dim_status as
select * from (values
    ('Out of stock', 1, 'Nothing on the shelf, demand expected'),
    ('Reorder now',  2, 'At or below the reorder point'),
    ('Healthy',      3, 'Above the reorder point, under 180 days of cover'),
    ('Overstock',    4, 'More than 180 days of cover'),
    ('Dead stock',   5, 'Stock on hand, nothing sold in 2016'),
    ('Inactive',     6, 'No stock and no demand')
) t(status, status_sort, status_rule);

create or replace table mart.dim_cover_band as
-- Band edges as data (cover_days > min_days and <= max_days; No demand = no cover at all): the model's
-- Lines in Band (Dynamic) measure counts lines with these edges instead of the cover_band text column.
select * from (values ('Out of stock', 1, -1, 0), ('1–7 days', 2, 0, 7), ('8–30 days', 3, 7, 30),
                      ('31–90 days', 4, 30, 90), ('91–180 days', 5, 90, 180), ('181–365 days', 6, 180, 365),
                      ('365+ days', 7, 365, 1000000), ('No demand', 8, null, null)) t(cover_band, cover_sort, min_days, max_days);

-- Chain weekday profile from January's daily sales (index 1 = an average day).
create or replace table mart.weekday_profile as
with d as (select sales_date, sum(units) u from stg.sales group by 1)
select strftime(sales_date, '%a') as weekday, isodow(sales_date) as weekday_sort,
       avg(u) / (select avg(u) from d) as weekday_index, sum(u) as jan_units
from d group by all order by weekday_sort;

-- Demand. A line's 2016 sales follow from the stock identity: begin + received - end
-- (this includes shrinkage, which the data cannot separate). Lines that started the year empty
-- only sell from their first receipt, so their rate uses the days since then (at least 28).
create or replace table mart.fact_stock as
with base as (
    select k.*, i.category, i.vendor_no, i.unit_cost, i.abc, case when st.closed_2016 then 0 else 1 end as live,
           coalesce(e.Price, i.retail_price) as unit_price,
           k.begin_units + k.received_units - k.end_units as sold_units,
           greatest(28, date '2016-12-31' - case when k.begin_units = 0 and k.first_receipt is not null
                                                 then k.first_receipt else date '2016-01-01' end + 1) as days_active
    from stg.stock k
    join mart.dim_item i using (brand)
    join mart.dim_store st using (store)
    left join raw.end_inventory e on e.InventoryId = k.inventory_id
), jan as (
    select inventory_id, sum(units) u, sum(units * units) u2 from stg.sales group by 1
), season as (  -- January vs an average 2016 day, per category, from lines active all year
    select b.category, (sum(coalesce(j.u, 0)) / 31) / (sum(b.sold_units) / 366) as jan_factor
    from base b left join jan j using (inventory_id) where b.days_active = 366 and b.live = 1 group by 1
), d as (
    select b.*, coalesce(j.u, 0) as jan_units, s.jan_factor,
           b.sold_units / b.days_active as daily_rate,
           -- a closed store has no demand to restock for
           b.sold_units / b.days_active * s.jan_factor * b.live as daily_forecast,
           -- January's day-to-day spread, scaled to the forecast level; Poisson when January had no sales
           case when coalesce(j.u, 0) > 0
                then sqrt(greatest(0, (j.u2 - j.u * j.u / 31) / 30)) / (j.u / 31) * (b.sold_units / b.days_active * s.jan_factor * b.live)
                else sqrt(b.sold_units / b.days_active * s.jan_factor * b.live) end as daily_sd,
           v.lead_days, v.lead_sd
    from base b left join jan j using (inventory_id) join season s using (category) join mart.dim_vendor v using (vendor_no)
), r as (
    select *, service_z() * sqrt(lead_days * daily_sd ^ 2 + daily_forecast ^ 2 * lead_sd ^ 2) as safety_units
    from d
), p as (
    select *, daily_forecast * lead_days + safety_units as reorder_point,
           daily_forecast * (lead_days + review_days()) + safety_units as order_up_to,
           case when daily_forecast > 0 then end_units / daily_forecast end as cover_days
    from r
)
select inventory_id, store, brand, vendor_no, category, abc,
       begin_units, received_units, end_units, sold_units, days_active, jan_units,
       new_line_2016, line_origin, last_receipt,
       case when begin_units > 0 then 'Stocked 1 Jan' else 'Empty 1 Jan' end as opening_state,
       unit_cost, unit_price, end_units * unit_cost as stock_cost, end_units * unit_price as stock_retail,
       sold_units * unit_price as sales_value,
       jan_factor, daily_rate, daily_forecast, daily_sd, lead_days, lead_sd,
       safety_units, reorder_point, order_up_to, cover_days,
       case when daily_forecast = 0 then 'No demand' when end_units = 0 then 'Out of stock'
            when cover_days <= 7 then '1–7 days' when cover_days <= 30 then '8–30 days'
            when cover_days <= 90 then '31–90 days' when cover_days <= 180 then '91–180 days'
            when cover_days <= 365 then '181–365 days' else '365+ days' end as cover_band,
       case when daily_forecast = 0 and end_units > 0 then 'Dead stock'
            when daily_forecast = 0 then 'Inactive'
            when end_units = 0 then 'Out of stock'
            when end_units <= reorder_point then 'Reorder now'
            when cover_days > overstock_days() then 'Overstock'
            else 'Healthy' end as status,
       case when daily_forecast > 0 and end_units <= reorder_point then ceil(order_up_to - end_units) else 0 end as order_units,
       case when daily_forecast > 0 and end_units <= reorder_point then ceil(order_up_to - end_units) else 0 end * unit_cost as order_cost
from p;

create or replace table mart.fact_sales_daily as
select sales_date as date, store, brand, sum(units) as units, sum(dollars) as dollars
from stg.sales group by all;

create or replace table mart.fact_receipts as
select date_trunc('month', p.receiving_date)::date as month_start, p.store, p.vendor_no, i.category,
       sum(p.units) as units, sum(p.dollars) as dollars, count(*) as lines
from stg.purchases p join stg.items i using (brand) group by all;

-- Lead time distribution: shipments (PO x store x receiving date, as in dim_vendor) per vendor, store and
-- lead-time day. Feeds the Vendor Detail drillthrough page.
create or replace table mart.fact_shipment as
select vendor_no, store, lead_days, count(*) as shipments
from (select distinct vendor_no, po_number, store, po_date, receiving_date, lead_days from stg.purchases)
group by all;

-- Vendor invoices. They reconcile to the receiving lines per vendor (units exact), but the PO number
-- on about a quarter of them points at another vendor's order, so lead times come from the receiving
-- lines (dim_vendor) and invoices are used for freight and payment terms only.
create or replace table mart.fact_po as
select i.po_number, i.vendor_no, i.po_date, i.invoice_date, i.pay_date,
       i.units, i.dollars, i.freight, i.freight / i.dollars as freight_pct,
       i.pay_date - i.invoice_date as days_to_pay, i.approval is not null and i.approval <> 'None' as approved,
       coalesce(p.u = i.units and p.vendor_no = i.vendor_no, false) as po_matches_receipts
from stg.invoices i
left join (select po_number, any_value(vendor_no) vendor_no, sum(units) u from stg.purchases group by 1) p using (po_number);

-- Forecast backtest. Train on 1-24 Jan, forecast the full week 25-31 Jan for every inventory line,
-- score against what actually sold. The last method is the one the restock engine uses.
create or replace table mart.forecast_lines as
with days as (select d::date as date from range(date '2016-01-25', date '2016-02-01', interval 1 day) t(d)),
train as (select inventory_id, sum(units) / 24 as avg_u from stg.sales where sales_date < date '2016-01-25' group by 1),
lastwk as (select inventory_id, isodow(sales_date) dow, sum(units) u from stg.sales
           where sales_date between date '2016-01-18' and date '2016-01-24' group by all),
chain as (select sales_date, sum(units) u from stg.sales where sales_date < date '2016-01-25' group by 1),
idx as (select isodow(sales_date) dow, avg(u) / (select avg(u) from chain) ix from chain group by 1),
season as (
    select k.category, (sum(t.avg_u)) / (sum(k.daily_rate)) as f
    from mart.fact_stock k join train t using (inventory_id) where k.days_active = 366 group by 1
),
actual as (select inventory_id, sales_date as date, sum(units) u from stg.sales where sales_date >= date '2016-01-25' group by all),
lines as (select inventory_id, category, daily_rate from mart.fact_stock where daily_rate > 0 or jan_units > 0)
select l.inventory_id, d.date, coalesce(a.u, 0) as actual,
       coalesce(w.u, 0)                                         as f_last_week,
       coalesce(t.avg_u, 0)                                     as f_flat_average,
       coalesce(t.avg_u, 0) * x.ix                              as f_average_weekday,
       l.daily_rate * s.f * x.ix                                as f_annual_season_weekday
from lines l cross join days d
join idx x on x.dow = isodow(d.date)
join season s using (category)
left join train t using (inventory_id)
left join lastwk w on w.inventory_id = l.inventory_id and w.dow = isodow(d.date)
left join actual a on a.inventory_id = l.inventory_id and a.date = d.date;

create or replace table mart.forecast_scores as
with m as (
    unpivot mart.forecast_lines on f_last_week, f_flat_average, f_average_weekday, f_annual_season_weekday
    into name method value forecast
), wk as (
    select method, inventory_id, sum(actual) a, sum(forecast) f from m group by all
), ch as (
    select method, date, sum(actual) a, sum(forecast) f from m group by all
)
select k.method,
       case k.method when 'f_last_week' then 'Same day last week' when 'f_flat_average' then 'Flat average'
            when 'f_average_weekday' then 'Average × weekday' else 'Annual rate × season × weekday' end as method_label,
       case k.method when 'f_last_week' then 1 when 'f_flat_average' then 2 when 'f_average_weekday' then 3 else 4 end as method_sort,
       (select sum(abs(forecast - actual)) / sum(actual) from m where m.method = k.method) as wape_line_day,
       sum(abs(k.f - k.a)) / sum(k.a) as wape_line_week,
       (select sum(abs(f - a)) / sum(a) from ch where ch.method = k.method) as wape_chain_day,
       sum(k.f) / sum(k.a) - 1 as bias
from wk k group by k.method;

create or replace table mart.forecast_daily as
select sales_date as date, 'Actual' as series, 0 as series_sort, sum(units) as units from stg.sales group by 1
union all
select m.date, s.method_label, s.method_sort, sum(m.forecast)
from (unpivot mart.forecast_lines on f_last_week, f_flat_average, f_average_weekday, f_annual_season_weekday
      into name method value forecast) m
join mart.forecast_scores s using (method) group by all;

-- SYNTHETIC planning targets (days of cover per category × ABC class), not in the Bibitor data:
-- seeds/category_target.csv. Many-to-many to Item[Category] in the model.
create or replace table mart.category_target as
select category, abc, target_cover_days::int as target_cover_days from read_csv('{seeds}/category_target.csv', all_varchar = true);

create or replace table mart.store_access as
select email, upper(city) as city_key, initcap(city) as city, store from read_csv('{seeds}/store_access.csv', all_varchar = true);

-- Item × vendor bridge: who supplied each item in 2016. Most items have one vendor, a few have two,
-- so item ↔ vendor is many-to-many; the model resolves it through this table.
create or replace table mart.item_supplier as
select brand, vendor_no, count(*) as lines, sum(units) as units, sum(dollars) as dollars,
       count(*) over (partition by brand) as vendors_for_item,
       count(*) over (partition by brand) > 1 as multi_sourced
from stg.purchases
where brand in (select brand from mart.dim_item) and vendor_no in (select vendor_no from mart.dim_vendor)
group by brand, vendor_no;
