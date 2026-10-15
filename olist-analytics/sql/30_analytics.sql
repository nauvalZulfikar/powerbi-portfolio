-- =====================================================================
-- ANALYTICS: pre-aggregated marts for questions that are awkward in DAX.
-- =====================================================================

-- Monthly cohort retention: of customers whose first valid order was in
-- cohort month C, what share bought again k months later?
create or replace table mart.cohort_retention as
with orders as (
    select fo.customer_unique_id,
           date_trunc('month', fo.order_date)::date as order_month,
           dc.cohort_month
    from mart.fact_orders fo
    join mart.dim_customer dc using (customer_unique_id)
    where fo.is_valid
),
activity as (
    select cohort_month,
           date_diff('month', cohort_month, order_month) as month_offset,
           count(distinct customer_unique_id)            as active_customers
    from orders group by 1, 2
),
sizes as (
    select cohort_month, active_customers as cohort_size
    from activity where month_offset = 0
)
select
    a.cohort_month,
    strftime(a.cohort_month, '%Y-%m')                     as cohort_label,
    s.cohort_size,
    a.month_offset,
    'M+' || lpad(a.month_offset::varchar, 2, '0')         as month_offset_label,
    a.active_customers,
    round(a.active_customers / s.cohort_size, 5)          as retention_rate
from activity a join sizes s using (cohort_month)
where a.cohort_month between date '2017-01-01' and date '2018-07-01'
  and a.month_offset between 1 and 12;

-- Seller × month scorecard (drives the seller page small multiples).
create or replace table mart.seller_monthly as
select
    seller_id,
    date_trunc('month', order_date)::date       as month_start,
    count(distinct order_id)                    as orders,
    sum(price)                                  as revenue,
    avg(review_score)                           as avg_review,
    avg(is_late::int)                           as late_rate
from mart.fact_order_items
where is_valid
group by 1, 2;

-- State-to-state freight lanes (seller state -> customer state).
create or replace table mart.freight_lanes as
select
    ds.state                                    as seller_state,
    dc.state                                    as customer_state,
    ds.state || ' → ' || dc.state               as lane,
    count(*)                                    as items,
    avg(fi.distance_km)                         as avg_distance_km,
    avg(fi.freight)                             as avg_freight,
    avg(fi.freight_share)                       as avg_freight_share,
    avg(fo.delivery_days)                       as avg_delivery_days,
    avg(fo.is_late::int)                        as late_rate
from mart.fact_order_items fi
join mart.fact_orders  fo using (order_id)
join mart.dim_seller   ds using (seller_id)
join mart.dim_customer dc on dc.customer_unique_id = fi.customer_unique_id
where fo.is_delivered
group by 1, 2, 3
having count(*) >= 30;


-- Daily backlog: delivered orders that were bought but not yet delivered at the end of each day.
-- A snapshot (semi-additive): sum it across categories, never across days (use the last day of the period).
create or replace table mart.fact_backlog_daily as
select d.date,
       count(o.order_id)                                    as in_transit,
       count(o.order_id) filter (where o.order_date = d.date) as purchased_today
from mart.dim_date d
left join mart.fact_orders o
       on o.is_delivered and o.order_date <= d.date and o.delivered_date > d.date
where d.date <= (select max(delivered_date) from mart.fact_orders)
group by 1;


-- Seller tier transition 2017 -> 2018 (10-s10). Tier per year with the same Pareto cut as dim_seller
-- (A = top 50% of that year's revenue, B = next 30%, C = tail). 2018 is Jan-Aug (the extract ends there), so
-- 2017 is cut to Jan-Aug too: a seller is compared on the same months. 'New' = no 2017 sale, 'Churned' = no 2018 sale.
create or replace table mart.seller_tier_transition as
with yearly as (
    select seller_id, year(month_start) as yr, sum(revenue) as revenue
    from mart.seller_monthly
    where year(month_start) in (2017, 2018) and month(month_start) <= 8
    group by 1, 2
),
ranked as (
    select *, sum(revenue) over (partition by yr order by revenue desc, seller_id rows unbounded preceding)
              / sum(revenue) over (partition by yr) as cum_share
    from yearly
),
tiered as (
    select seller_id, yr,
           case when cum_share <= 0.50 then 'A' when cum_share <= 0.80 then 'B' else 'C' end as tier
    from ranked
),
pairs as (
    select coalesce(a.seller_id, b.seller_id) as seller_id,
           coalesce(a.tier, 'New')    as tier_2017,
           coalesce(b.tier, 'Churned') as tier_2018
    from (select * from tiered where yr = 2017) a
    full join (select * from tiered where yr = 2018) b using (seller_id)
)
select tier_2017, tier_2018,
       case tier_2017 when 'A' then 1 when 'B' then 2 when 'C' then 3 else 4 end as tier_2017_order,
       case tier_2018 when 'A' then 1 when 'B' then 2 when 'C' then 3 else 4 end as tier_2018_order,
       count(*) as sellers
from pairs
group by all;
