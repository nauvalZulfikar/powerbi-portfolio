-- Power Query mirror. The model builds these tables in Power Query straight from the raw CSV exports
-- (build_model.py: PRICE_LIST_RAW, PRICE_LIST_2017, STOCK_COUNTS, VENDOR_CATALOG, FORECAST_METRIC).
-- Power Query cannot run on the Mac this is built on, so the same steps are repeated here in SQL:
-- build_model.py reads the column types from these tables, the tests check their row counts,
-- and the HTML twin takes its numbers from them. They are not exported to Parquet.

-- Price List 2017: drop the one row without a size, tidy the size labels, split "750mL 4 Pk" into
-- size in mL + pack units (1 when there is no pack count), one row per brand.
create or replace table mart.pq_price_list_2017 as
with t as (
    select *, string_split(replace(replace(Size, 'ml', 'mL'), '  ', ' '), ' ') as parts
    from raw.price_list
    where Brand is not null and Size is not null and Size <> ''
)
select distinct on (Brand)
       Brand as "Brand No", trim(Description) as "List Description",
       try_cast(split_part(parts[1], 'mL', 1) as double) as "Size mL",
       coalesce(try_cast(parts[2] as bigint), 1) as "Pack Units",
       Price as "List Price", PurchasePrice as "Purchase Price 2017",
       VendorNumber as "List Vendor No", initcap(trim(VendorName)) as "List Vendor"
from t order by Brand;

-- Stock Counts: the 1 Jan and 31 Dec counts appended; store 46's 31 Dec rows have no city, so the
-- city is filled down after sorting by store and count date (the store's 1 Jan rows come first).
create or replace table mart.pq_stock_counts as
with counts as (
    select InventoryId, Store, nullif(City, '') as City, Brand, onHand, Price, startDate as count_date
    from raw.begin_inventory
    union all
    select InventoryId, Store, nullif(City, ''), Brand, onHand, Price, endDate from raw.end_inventory
), filled as (
    select *, last_value(City ignore nulls) over (order by Store, count_date, City nulls last
                                                  rows between unbounded preceding and current row) as city_filled
    from counts where onHand is not null
)
select distinct InventoryId as "Count Inventory ID", Store as "Count Store", initcap(trim(city_filled)) as "Count City",
       Brand as "Count Brand", onHand as "Counted Units", Price as "Count Price", count_date as "Count Date"
from filled;

-- Vendor Catalog: the raw price list grouped per vendor (Group By in Power Query).
create or replace table mart.pq_vendor_catalog as
select VendorNumber as "Catalog Vendor No", count(*) as "Catalog Items", avg(Price) as "Catalog Avg Price"
from raw.price_list where Brand is not null group by 1;

-- Forecast Metric: Forecast Scores unpivoted, one row per method x metric (for small multiples).
create or replace table mart.pq_forecast_metric as
unpivot (select method_label as "Forecast Method", method_sort as "Method Order", wape_line_day as "WAPE Line Day",
                wape_line_week as "WAPE Line Week", wape_chain_day as "WAPE Chain Day", bias as "Bias"
         from mart.forecast_scores)
on "WAPE Line Day", "WAPE Line Week", "WAPE Chain Day", "Bias" into name "Metric" value "Metric Value";
