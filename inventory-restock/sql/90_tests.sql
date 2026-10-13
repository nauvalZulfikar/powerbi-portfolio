-- Each test returns the number of offending rows; anything above 0 fails the build.

-- name: one row per inventory line
select count(*) - count(distinct inventory_id) from mart.fact_stock;

-- name: stock identity never negative (begin + received - end >= 0)
select count(*) from mart.fact_stock where sold_units < 0;

-- name: stock reconciles to the raw counts and receipts
select (select sum(end_units) from mart.fact_stock) <> (select sum(onHand) from raw.end_inventory)
     + ((select sum(begin_units) from mart.fact_stock) <> (select sum(onHand) from raw.begin_inventory))::int
     + ((select sum(received_units) from mart.fact_stock) <> (select sum(Quantity) from raw.purchases))::int;

-- name: January sales cover all 79 stores (each trading at least 29 of 31 days)
select (select count(distinct store) from stg.sales) <> 79
     + (select count(*) from (select store from stg.sales group by 1 having count(distinct sales_date) < 29));

-- name: February (truncated by the export) is excluded
select count(*) from stg.sales where sales_date >= date '2016-02-01';

-- name: every line maps to an item, store and vendor with a lead time
select count(*) from mart.fact_stock k
left join mart.dim_item i using (brand) left join mart.dim_store s using (store) left join mart.dim_vendor v on v.vendor_no = k.vendor_no
where i.brand is null or s.store is null or v.lead_days is null;

-- name: restock maths consistent (order only at or below the reorder point, up to the target)
select count(*) from mart.fact_stock
where (order_units > 0 and end_units > reorder_point)
   or (order_units > 0 and end_units + order_units < order_up_to)
   or reorder_point < daily_forecast * lead_days
   or safety_units < 0;

-- name: every line has exactly one status and cover band in the dimensions
select count(*) from mart.fact_stock k left join mart.dim_status s using (status) left join mart.dim_cover_band c using (cover_band)
where s.status is null or c.cover_band is null;

-- name: ABC classes cover 80 / 15 / 5 percent of sales value
select count(*) from (select abc, sum(sales_value_2016) / sum(sum(sales_value_2016)) over () sh from mart.dim_item group by 1)
where (abc = 'A' and sh not between 0.78 and 0.81) or (abc = 'B' and sh not between 0.13 and 0.17);

-- name: lead time never negative
select count(*) from stg.purchases where lead_days < 0;

-- name: invoices reconcile to receiving lines per vendor (units exact, dollars to the cent)
select count(*) from (select vendor_no, sum(units) u, sum(dollars) d from mart.fact_po group by 1) i
full join (select vendor_no, sum(units) u, sum(dollars) d from stg.purchases group by 1) p using (vendor_no)
where i.u is distinct from p.u or abs(i.d - p.d) > 0.01;

-- name: backtest covers a full week and four methods
select (select count(distinct date) from mart.forecast_lines) <> 7
     + ((select count(*) from mart.forecast_scores) <> 4)::int;

-- name: RLS seeds reference real stores and cities
select count(*) from mart.store_access a
where upper(a.city) not in (select upper(city) from mart.dim_store)
   or (a.store is not null and a.store::int not in (select store from mart.dim_store where upper(city) = a.city_key));

-- name: stores closed during 2016 get no demand and no order
select count(*) from mart.fact_stock f join mart.dim_store s using (store)
where s.closed_2016 and (f.daily_forecast > 0 or f.order_units > 0);

-- name: item_supplier is one row per item × vendor and covers every purchase line
select (select count(*) - count(distinct (brand, vendor_no)) from mart.item_supplier)
     + abs((select sum(lines) from mart.item_supplier) - (select count(*) from stg.purchases));

-- name: Power Query price list drops only the row without a size and keeps one row per brand
select ((select count(*) from raw.price_list) - (select count(*) from mart.pq_price_list_2017) <> 1)::int
     + (select count(*) - count(distinct "Brand No") from mart.pq_price_list_2017);

-- name: Power Query stock counts keep every counted line and fill every city
select abs((select count(*) from mart.pq_stock_counts)
         - (select count(*) from raw.begin_inventory) - (select count(*) from raw.end_inventory))
     + (select count(*) from mart.pq_stock_counts where "Count City" is null);

-- name: closing count in Power Query equals the shelf units in fact_stock
select abs((select sum("Counted Units") from mart.pq_stock_counts where "Count Date" = date '2016-12-31')
         - (select sum(end_units) from mart.fact_stock));

-- name: vendor catalog counts every price-list row once and the forecast metric unpivot is 4 x 4
select abs((select sum("Catalog Items") from mart.pq_vendor_catalog) - (select count(*) from raw.price_list))
     + abs((select count(*) from mart.pq_forecast_metric) - 16);

-- name: Date covers every PO, invoice and payment date (inactive relationships need a row to land on)
select count(*) from mart.fact_po
where po_date not in (select date from mart.dim_date) or invoice_date not in (select date from mart.dim_date)
   or pay_date not in (select date from mart.dim_date);

-- name: category targets cover every category × ABC class once
select (select count(*) from (select distinct category, abc from mart.fact_stock) s
        anti join mart.category_target t using (category, abc))
     + (select count(*) - count(distinct (category, abc)) from mart.category_target);

-- name: dead and inactive lines have no forecast, safety stock or order (the Restock page filter drops them)
select count(*) from mart.fact_stock
where status in ('Dead stock', 'Inactive') and (daily_forecast > 0 or safety_units > 0 or order_units > 0);

-- name: shipments per vendor add up to dim_vendor shipments
select count(*) from mart.dim_vendor v
left join (select vendor_no, sum(shipments) n from mart.fact_shipment group by 1) s using (vendor_no)
where v.shipments <> coalesce(s.n, 0);

-- name: a new 2016 line had no opening count and no line was received after the year-end count
select (select count(*) from mart.fact_stock where new_line_2016 and begin_units > 0)
     + (select count(*) from mart.fact_stock where last_receipt > date '2016-12-31');

-- name: cover band edges in dim_cover_band reproduce every line's cover_band
select count(*) from mart.fact_stock k join mart.dim_cover_band b on
    case when b.min_days is null then k.cover_days is null
         else k.cover_days > b.min_days and k.cover_days <= b.max_days end
where k.cover_band <> b.cover_band;

-- name: every PO on a receiving line has exactly one invoice (funnel: PO raised = Invoiced)
select abs((select count(distinct po_number) from stg.purchases) - (select count(*) from mart.fact_po))
     + (select count(*) from (select distinct po_number from stg.purchases) anti join mart.fact_po using (po_number));
