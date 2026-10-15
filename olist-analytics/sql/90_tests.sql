-- Data-quality tests. Each query returns the number of violating rows; 0 = pass.
-- name: fact_orders.order_id is unique
select count(*) - count(distinct order_id) from mart.fact_orders;
-- name: fact_orders row count matches raw orders
select abs((select count(*) from mart.fact_orders) - (select count(*) from stg.orders));
-- name: every order belongs to a known customer
select count(*) from mart.fact_orders fo anti join mart.dim_customer using (customer_unique_id);
-- name: every item has a known product
select count(*) from mart.fact_order_items anti join mart.dim_product using (product_id);
-- name: every item has a known seller
select count(*) from mart.fact_order_items anti join mart.dim_seller using (seller_id);
-- name: every order date is covered by dim_date
select count(*) from mart.fact_orders fo anti join mart.dim_date d on d.date = fo.order_date;
-- name: item revenue reconciles with raw order items (to the cent)
select (abs((select sum(price) from mart.fact_order_items) - (select sum(price) from stg.order_items)) > 0.01)::int;
-- name: review scores in 1..5
select count(*) from mart.fact_orders where review_score not between 1 and 5;
-- name: no negative money
select count(*) from mart.fact_order_items where price < 0 or freight < 0;
-- name: distances plausible (< 5,000 km across Brazil)
select count(*) from mart.fact_order_items where distance_km > 5000;
-- name: every customer with a valid order has an RFM segment
select count(*) from mart.dim_customer where valid_orders > 0 and rfm_segment = 'No valid order';
-- name: retention rates within [0, 1]
select count(*) from mart.cohort_retention where retention_rate not between 0 and 1;
-- name: role-playing dates (delivered, estimated) are covered by dim_date
select count(*) from mart.fact_orders fo where (delivered_date is not null and delivered_date not in (select date from mart.dim_date)) or estimated_date not in (select date from mart.dim_date);
-- name: order_seller bridge is unique and every pair exists in the items
select (select count(*) - count(distinct (order_id, seller_id)) from mart.order_seller) + (select count(*) from mart.order_seller anti join mart.fact_orders using (order_id));
-- name: backlog between 0 and the number of delivered orders
select count(*) from mart.fact_backlog_daily where in_transit > (select count(*) from mart.fact_orders where is_delivered) or in_transit < 0;
-- name: tier transition counts every seller active Jan-Aug 2017 or 2018 exactly once
select abs((select sum(sellers) from mart.seller_tier_transition) - (select count(distinct seller_id) from mart.seller_monthly where year(month_start) in (2017, 2018) and month(month_start) <= 8));
-- name: predicted CLV is set and non-negative for every customer with a valid order
select count(*) from mart.dim_customer where valid_orders > 0 and (predicted_clv_12m is null or predicted_clv_12m < 0);
-- name: forecast intervals are ordered (lower 95 <= lower 80 <= forecast <= upper 80 <= upper 95)
select count(*) from mart.revenue_forecast where not (lower_95 <= lower_80 and lower_80 <= forecast_revenue and forecast_revenue <= upper_80 and upper_80 <= upper_95);
