-- Staging: typed, cleaned views over the raw API payloads. One fix per known source defect.
create schema if not exists stg;

create or replace table stg.owners as
select id                                  as owner_id,
       first_name || ' ' || last_name      as agent,
       email,
       split_part(team, ' / ', 1)          as regional_office,
       split_part(team, ' / ', 2)          as manager
from raw.owners;

create or replace table stg.companies as
select id                                                        as company_id,
       properties->>'name'                                       as account,
       -- source typo: "technolgy"
       case properties->>'industry' when 'technolgy' then 'Technology'
            else upper(left(properties->>'industry', 1)) || substr(properties->>'industry', 2) end as sector,
       (properties->>'founded_year')::int                        as founded_year,
       (properties->>'annualrevenue')::double                    as annual_revenue,
       (properties->>'numberofemployees')::int                   as employees,
       -- source typo: "Philipines"
       replace(properties->>'country', 'Philipines', 'Philippines') as country,
       properties->>'parent_company'                             as parent_company
from raw.companies;

create or replace table stg.products as
select id                              as product_id,
       properties->>'name'             as product,
       properties->>'product_series'   as series,
       (properties->>'price')::double  as list_price
from raw.products;

-- Every version of every deal the extractor ever saw.
create or replace table stg.deal_versions as
select v.id                                              as opportunity_id,
       v.modified_at,
       v.synced_at,
       properties->>'dealstage'                          as stage_key,
       (properties->>'amount')::double                   as amount,
       (properties->>'createdate')::timestamp::date      as created_date,
       (properties->>'engaged_at')::timestamp::date      as engaged_date,
       (properties->>'closedate')::timestamp::date       as closed_date,
       properties->>'hubspot_owner_id'                   as owner_id,
       properties->>'company_id'                         as company_id,
       -- source defect: the pipeline spells one product "GTXPro"; the price list says "GTX Pro"
       replace(properties->>'product_name', 'GTXPro', 'GTX Pro') as product
from raw.deal_versions v;

-- Current state: the latest version of each deal.
create or replace table stg.deals as
select * from stg.deal_versions
qualify row_number() over (partition by opportunity_id order by modified_at desc, synced_at desc) = 1;
