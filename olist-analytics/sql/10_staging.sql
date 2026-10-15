-- =====================================================================
-- STAGING: type, clean, and de-duplicate the 9 raw Kaggle CSVs.
-- One view/table per source. No business logic yet.
-- =====================================================================
create schema if not exists stg;

-- DuckDB has no INITCAP; "são paulo" -> "São Paulo".
create or replace macro initcap(s) as
    array_to_string(list_transform(string_split(lower(s), ' '), w -> upper(w[1]) || w[2:]), ' ');

create or replace table stg.orders as
select
    order_id,
    customer_id,
    order_status,
    order_purchase_timestamp::timestamp        as purchased_at,
    order_approved_at::timestamp               as approved_at,
    order_delivered_carrier_date::timestamp    as shipped_at,
    order_delivered_customer_date::timestamp   as delivered_at,
    order_estimated_delivery_date::date        as estimated_delivery_date
from read_csv_auto('{raw}/olist_orders_dataset.csv');

create or replace table stg.customers as
select
    customer_id,
    customer_unique_id,
    lpad(customer_zip_code_prefix::varchar, 5, '0') as zip_prefix,
    initcap(customer_city)                          as city,
    customer_state                                  as state
from read_csv_auto('{raw}/olist_customers_dataset.csv', types={'customer_zip_code_prefix': 'VARCHAR'});

create or replace table stg.sellers as
select
    seller_id,
    lpad(seller_zip_code_prefix::varchar, 5, '0') as zip_prefix,
    initcap(seller_city)                          as city,
    seller_state                                  as state
from read_csv_auto('{raw}/olist_sellers_dataset.csv', types={'seller_zip_code_prefix': 'VARCHAR'});

create or replace table stg.order_items as
select
    order_id,
    order_item_id,
    product_id,
    seller_id,
    shipping_limit_date::timestamp as shipping_limit_at,
    price::decimal(12, 2)          as price,
    freight_value::decimal(12, 2)  as freight
from read_csv_auto('{raw}/olist_order_items_dataset.csv');

create or replace table stg.payments as
select
    order_id,
    payment_sequential,
    case payment_type when 'not_defined' then 'other' else payment_type end as payment_type,
    greatest(payment_installments, 1)                                       as installments,
    payment_value::decimal(12, 2)                                           as payment_value
from read_csv_auto('{raw}/olist_order_payments_dataset.csv');

-- Reviews: ~550 orders carry several reviews. Keep the most recent answer per order.
create or replace table stg.reviews as
select
    review_id,
    order_id,
    review_score,
    nullif(trim(review_comment_title), '')   as comment_title,
    nullif(trim(review_comment_message), '') as comment_message,
    review_creation_date::timestamp          as created_at,
    review_answer_timestamp::timestamp       as answered_at
from read_csv_auto('{raw}/olist_order_reviews_dataset.csv')
qualify row_number() over (partition by order_id order by review_answer_timestamp desc) = 1;

-- Products: fix the source's "lenght" typo, translate categories, keep 2 missing translations.
create or replace table stg.products as
select
    p.product_id,
    coalesce(p.product_category_name, 'unknown') as category_pt,
    coalesce(
        t.product_category_name_english,
        case p.product_category_name
            when 'pc_gamer' then 'pc_gamer'
            when 'portateis_cozinha_e_preparadores_de_alimentos' then 'kitchen_portables'
        end,
        'unknown'
    )                                            as category_raw_en,
    p.product_name_lenght                        as name_length,
    p.product_description_lenght                 as description_length,
    p.product_photos_qty                         as photos_qty,
    p.product_weight_g                           as weight_g,
    p.product_length_cm * p.product_height_cm * p.product_width_cm as volume_cm3
from read_csv_auto('{raw}/olist_products_dataset.csv') p
left join read_csv_auto('{raw}/product_category_name_translation.csv') t
    using (product_category_name);

-- Geolocation: 1M rows, many points per ZIP prefix. Collapse to one centroid,
-- dropping points that fall outside Brazil's bounding box (known source noise).
create or replace table stg.zip_centroids as
select
    lpad(geolocation_zip_code_prefix::varchar, 5, '0') as zip_prefix,
    median(geolocation_lat)                            as lat,
    median(geolocation_lng)                            as lng
from read_csv_auto('{raw}/olist_geolocation_dataset.csv', types={'geolocation_zip_code_prefix': 'VARCHAR'})
where geolocation_lat between -33.75 and 5.27
  and geolocation_lng between -73.99 and -34.79
group by 1;

-- Reference: Brazilian states -> full name + macro-region.
create or replace table stg.states as
select * from (values
    ('AC','Acre','North'), ('AL','Alagoas','Northeast'), ('AM','Amazonas','North'),
    ('AP','Amapá','North'), ('BA','Bahia','Northeast'), ('CE','Ceará','Northeast'),
    ('DF','Distrito Federal','Center-West'), ('ES','Espírito Santo','Southeast'),
    ('GO','Goiás','Center-West'), ('MA','Maranhão','Northeast'), ('MG','Minas Gerais','Southeast'),
    ('MS','Mato Grosso do Sul','Center-West'), ('MT','Mato Grosso','Center-West'),
    ('PA','Pará','North'), ('PB','Paraíba','Northeast'), ('PE','Pernambuco','Northeast'),
    ('PI','Piauí','Northeast'), ('PR','Paraná','South'), ('RJ','Rio de Janeiro','Southeast'),
    ('RN','Rio Grande do Norte','Northeast'), ('RO','Rondônia','North'), ('RR','Roraima','North'),
    ('RS','Rio Grande do Sul','South'), ('SC','Santa Catarina','South'), ('SE','Sergipe','Northeast'),
    ('SP','São Paulo','Southeast'), ('TO','Tocantins','North')
) t(state, state_name, region);
