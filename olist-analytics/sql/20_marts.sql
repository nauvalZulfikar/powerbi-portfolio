-- =====================================================================
-- MARTS: star schema consumed by Power BI.
--   dims : dim_date, dim_customer, dim_seller, dim_product
--   facts: fact_orders (1 row/order), fact_order_items (1 row/item), fact_payments
-- =====================================================================
create schema if not exists mart;

-- ---------------------------------------------------------------- dim_date
create or replace table mart.dim_date as
select
    d::date                                   as date,
    year(d)                                   as year,
    quarter(d)                                as quarter,
    'Q' || quarter(d) || ' ' || year(d)       as year_quarter,
    year(d) * 10 + quarter(d)                 as year_quarter_key,
    month(d)                                  as month_no,
    strftime(d, '%b')                         as month_name,
    strftime(d, '%b %Y')                      as year_month,
    year(d) * 100 + month(d)                  as year_month_key,
    date_trunc('month', d)::date              as month_start,
    isodow(d)                                 as weekday_no,
    strftime(d, '%a')                         as weekday_name,
    isodow(d) >= 6                            as is_weekend,
    weekofyear(d)                             as iso_week
-- through Dec 2018: the plan and the forecast need the last quarter complete, and promise dates run to Nov 2018
from range(date '2016-09-01', date '2019-01-01', interval 1 day) t(d);

-- ------------------------------------------------------------ dim_product
create or replace table mart.dim_product as
with base as (
    select *,
        -- fix source typos (fashio, costruction, confort) before prettifying
        initcap(replace(replace(replace(replace(regexp_replace(category_raw_en, '_2$', ''),
            'fashio_', 'fashion_'), 'costruction', 'construction'), 'confort', 'comfort'), '_', ' ')) as category
    from stg.products
)
select
    product_id,
    category,
    case
        when category_raw_en similar to '.*(computers|electronics|telephony|tablets|consoles|audio|cine_photo|pc_gamer|small_appliances|home_appliances).*' then 'Electronics & Appliances'
        when category_raw_en similar to '.*(furniture|bed_bath|housewares|home_|la_cuisine|kitchen|christmas|flowers|garden_tools|air_conditioning).*' then 'Home & Living'
        when category_raw_en similar to '.*(fashio|luggage|watches).*' then 'Fashion & Accessories'
        when category_raw_en similar to '.*(health_beauty|perfumery|diapers).*' then 'Health & Beauty'
        when category_raw_en similar to '.*(sports|toys|baby|cool_stuff|party|musical|music|art|books|cds|dvds|stationery).*' then 'Leisure, Kids & Culture'
        when category_raw_en similar to '.*(construction|costruction|auto|agro|industry|security|signaling|office|market_place).*' then 'Tools, Auto & Business'
        when category_raw_en similar to '.*(food|drinks|pet_shop).*' then 'Food, Drink & Pets'
        else 'Uncategorised'
    end                                                    as category_group,
    photos_qty,
    weight_g,
    volume_cm3,
    case
        when weight_g is null then 'Unknown'
        when weight_g < 500   then '< 0.5 kg'
        when weight_g < 2000  then '0.5–2 kg'
        when weight_g < 10000 then '2–10 kg'
        else '10 kg +'
    end                                                    as weight_band,
    case when weight_g is null then 5 when weight_g < 500 then 1 when weight_g < 2000 then 2 when weight_g < 10000 then 3 else 4 end as weight_band_order
from base;

-- ---------------------------------------------------------- fact_orders
create or replace table mart.fact_orders as
with items as (
    select order_id,
        count(*)                  as item_count,
        count(distinct seller_id) as seller_count,
        sum(price)                as items_value,
        sum(freight)              as freight_value
    from stg.order_items group by 1
),
pay as (
    select order_id,
        sum(payment_value)                          as paid_value,
        arg_max(payment_type, payment_value)        as main_payment_type,
        max(installments)                           as installments,
        count(*) > 1                                as used_multiple_methods
    from stg.payments group by 1
),
o as (
    select o.*, c.customer_unique_id, c.state as customer_state
    from stg.orders o join stg.customers c using (customer_id)
)
select
    o.order_id,
    o.customer_unique_id,
    o.purchased_at::date                                               as order_date,
    o.purchased_at,
    o.delivered_at::date                                               as delivered_date,   -- role-playing Date keys
    o.estimated_delivery_date                                          as estimated_date,
    hour(o.purchased_at)                                               as order_hour,
    o.order_status,
    o.order_status not in ('canceled', 'unavailable')                  as is_valid,
    o.order_status = 'delivered' and o.delivered_at is not null        as is_delivered,
    row_number() over (partition by o.customer_unique_id order by o.purchased_at) as customer_order_seq,
    o.customer_state,
    coalesce(i.item_count, 0)                                          as item_count,
    coalesce(i.seller_count, 0)                                        as seller_count,
    coalesce(i.items_value, 0)                                         as items_value,
    coalesce(i.freight_value, 0)                                       as freight_value,
    coalesce(p.paid_value, 0)                                          as paid_value,
    coalesce(p.main_payment_type, 'other')                             as main_payment_type,
    coalesce(p.installments, 1)                                        as installments,
    coalesce(p.used_multiple_methods, false)                           as used_multiple_methods,
    -- logistics (hours / days)
    round(date_diff('minute', o.purchased_at, o.approved_at) / 60.0, 1) as approval_hours,
    round(date_diff('hour', o.purchased_at, o.shipped_at) / 24.0, 1)    as days_to_carrier,
    round(date_diff('hour', o.purchased_at, o.delivered_at) / 24.0, 1)  as delivery_days,
    date_diff('day', o.purchased_at::date, o.estimated_delivery_date)   as promised_days,
    date_diff('day', o.estimated_delivery_date, o.delivered_at::date)   as delay_days,
    case when o.delivered_at is null then null
         else o.delivered_at::date > o.estimated_delivery_date end      as is_late,
    case
        when o.delivered_at is null then 'Not delivered'
        when o.delivered_at::date <= o.estimated_delivery_date - 7 then '7+ days early'
        when o.delivered_at::date <= o.estimated_delivery_date     then 'On time'
        when o.delivered_at::date <= o.estimated_delivery_date + 7 then 'Late ≤ 7 days'
        else 'Late > 7 days'
    end                                                                 as delivery_bucket,
    case
        when o.delivered_at is null then 5
        when o.delivered_at::date <= o.estimated_delivery_date - 7 then 1
        when o.delivered_at::date <= o.estimated_delivery_date     then 2
        when o.delivered_at::date <= o.estimated_delivery_date + 7 then 3
        else 4
    end                                                                 as delivery_bucket_order,
    -- review
    r.review_score,
    coalesce(r.review_score::varchar || ' ★', 'No review')              as review_label,
    r.comment_message is not null                                       as has_comment,
    round(date_diff('hour', r.created_at, r.answered_at) / 24.0, 1)     as review_answer_days
from o
left join items i using (order_id)
left join pay   p using (order_id)
left join stg.reviews r using (order_id);

-- ------------------------------------------------------ fact_order_items
-- Item grain with seller→customer haversine distance (km).
create or replace table mart.fact_order_items as
with geo as (
    select
        oi.*,
        round(2 * 6371 * asin(sqrt(
            power(sin(radians(zc.lat - zs.lat) / 2), 2) +
            cos(radians(zs.lat)) * cos(radians(zc.lat)) * power(sin(radians(zc.lng - zs.lng) / 2), 2)
        )), 0) as distance_km
    from stg.order_items oi
    join stg.orders o     on o.order_id = oi.order_id
    join stg.customers c  on c.customer_id = o.customer_id
    join stg.sellers   s  on s.seller_id = oi.seller_id
    left join stg.zip_centroids zc on zc.zip_prefix = c.zip_prefix
    left join stg.zip_centroids zs on zs.zip_prefix = s.zip_prefix
)
select
    g.order_id,
    g.order_item_id,
    g.product_id,
    g.seller_id,
    fo.customer_unique_id,
    fo.order_date,
    fo.is_valid,
    fo.is_late,
    fo.review_score,
    g.price,
    g.freight,
    round(g.freight / nullif(g.price + g.freight, 0), 4) as freight_share,
    g.distance_km,
    case when g.distance_km is null then 'Unknown'
         when g.distance_km < 100  then '< 100 km'
         when g.distance_km < 500  then '100–500 km'
         when g.distance_km < 1000 then '500–1,000 km'
         when g.distance_km < 2000 then '1,000–2,000 km'
         else '2,000 km +' end                              as distance_band,
    case when g.distance_km is null then 6
         when g.distance_km < 100  then 1
         when g.distance_km < 500  then 2
         when g.distance_km < 1000 then 3
         when g.distance_km < 2000 then 4
         else 5 end                                         as distance_band_order
from geo g
join mart.fact_orders fo using (order_id);

-- --------------------------------------------------------- fact_payments
create or replace table mart.fact_payments as
select
    p.order_id,
    p.payment_sequential,
    fo.order_date,
    fo.is_valid,
    initcap(replace(p.payment_type, '_', ' ')) as payment_type,
    p.installments,
    case
        when p.installments = 1 then '1x'
        when p.installments <= 3 then '2–3x'
        when p.installments <= 6 then '4–6x'
        else '7x +'
    end                                        as installment_band,
    case when p.installments = 1 then 1 when p.installments <= 3 then 2 when p.installments <= 6 then 3 else 4 end as installment_band_order,
    p.payment_value
from stg.payments p
join mart.fact_orders fo using (order_id);

-- ---------------------------------------------------------- dim_customer
-- Customer = customer_unique_id (Olist issues a new customer_id per order).
-- RFM scored on valid orders, snapshot = day after the last purchase in the data.
create or replace table mart.dim_customer as
with home as (      -- a customer's state/city at their latest order
    select c.customer_unique_id,
        arg_max(c.city, o.purchased_at)       as city,
        arg_max(c.state, o.purchased_at)      as state,
        arg_max(c.zip_prefix, o.purchased_at) as zip_prefix
    from stg.customers c join stg.orders o using (customer_id)
    group by 1
),
agg as (
    select customer_unique_id,
        min(order_date)                                   as first_order_date,
        max(order_date)                                   as last_order_date,
        count(*)                                          as orders,
        sum(paid_value)                                   as lifetime_value,
        avg(review_score)                                 as avg_review
    from mart.fact_orders where is_valid group by 1
),
snap as (select max(order_date) + 1 as d from mart.fact_orders),
scored as (
    select a.*,
        date_diff('day', a.last_order_date, (select d from snap)) as recency_days,
        ntile(5) over (order by date_diff('day', a.last_order_date, (select d from snap)) desc) as r_score,
        -- 97% of customers buy once, so frequency is bucketed, not ntiled
        case when a.orders >= 3 then 5 when a.orders = 2 then 4 else 1 end as f_score,
        ntile(5) over (order by a.lifetime_value) as m_score
    from agg a
)
select
    h.customer_unique_id,
    h.city,
    h.state,
    st.state_name,
    st.region,
    st.state_name || ', Brazil'                          as state_geo,
    zc.lat,
    zc.lng,
    s.first_order_date,
    date_trunc('month', s.first_order_date)::date        as cohort_month,
    s.last_order_date,
    coalesce(s.orders, 0)                                as valid_orders,
    coalesce(s.lifetime_value, 0)                        as lifetime_value,
    s.avg_review,
    s.recency_days,
    s.r_score, s.f_score, s.m_score,
    s.r_score::varchar || s.f_score::varchar || s.m_score::varchar as rfm_cell,
    case
        when s.customer_unique_id is null                 then 'No valid order'
        when s.f_score >= 4 and s.r_score >= 4            then 'Champions'
        when s.f_score >= 4                               then 'Loyal – Slipping'
        when s.r_score >= 4 and s.m_score >= 4            then 'New High-Value'
        when s.r_score >= 4                               then 'New Low-Value'
        when s.r_score = 3                                then 'Needs Attention'
        when s.m_score >= 4                               then 'At Risk – Big Spenders'
        else 'Hibernating'
    end                                                   as rfm_segment,
    case
        when s.customer_unique_id is null                 then 8
        when s.f_score >= 4 and s.r_score >= 4            then 1
        when s.f_score >= 4                               then 2
        when s.r_score >= 4 and s.m_score >= 4            then 3
        when s.r_score >= 4                               then 4
        when s.r_score = 3                                then 5
        when s.m_score >= 4                               then 6
        else 7
    end                                                   as rfm_segment_order
from home h
left join scored s using (customer_unique_id)
left join stg.states st on st.state = h.state
left join stg.zip_centroids zc on zc.zip_prefix = h.zip_prefix;

-- ------------------------------------------------------------ dim_seller
create or replace table mart.dim_seller as
with perf as (
    select seller_id,
        sum(price)                  as revenue,
        count(distinct order_id)    as orders,
        avg(review_score)           as avg_review,
        avg(is_late::int)           as late_rate,
        min(order_date)             as first_sale_date
    from mart.fact_order_items where is_valid group by 1
),
ranked as (
    select *,
        sum(revenue) over (order by revenue desc rows unbounded preceding) / sum(revenue) over () as cum_share
    from perf
)
select
    s.seller_id,
    'S-' || upper(left(s.seller_id, 6))                  as seller_code,
    s.city,
    s.state,
    st.state_name,
    st.region,
    zc.lat,
    zc.lng,
    r.first_sale_date,
    coalesce(r.revenue, 0)                               as lifetime_revenue,
    coalesce(r.orders, 0)                                as lifetime_orders,
    r.avg_review                                         as lifetime_avg_review,
    r.late_rate                                          as lifetime_late_rate,
    case
        when r.cum_share is null  then 'D · Inactive'
        when r.cum_share <= 0.50  then 'A · Top 50% revenue'
        when r.cum_share <= 0.80  then 'B · Next 30%'
        else 'C · Long tail'
    end                                                  as pareto_tier
from stg.sellers s
left join ranked r using (seller_id)
left join stg.states st on st.state = s.state
left join stg.zip_centroids zc on zc.zip_prefix = s.zip_prefix;


-- ------------------------------------------------------------ order_seller
-- Bridge: an order can hold items from several sellers, a seller has many orders.
-- Seller 1→* order_seller *→1 Orders lets order-level facts (review, late) filter by seller.
create or replace table mart.order_seller as
select distinct order_id, seller_id from mart.fact_order_items;
