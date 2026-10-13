-- Marts: the star schema Power BI reads. Export date (the last sync) is the "today" of every age.
create schema if not exists mart;

create or replace macro export_date() as (select max(as_of) from raw.sync_runs);

-- ------------------------------------------------------------------ dimensions
create or replace table mart.dim_date as
select d::date                                   as date,
       year(d)                                   as year,
       'Q' || quarter(d) || ' ' || year(d)       as quarter,
       year(d) * 10 + quarter(d)                 as quarter_sort,
       strftime(d, '%b %Y')                      as month,
       year(d) * 100 + month(d)                  as month_sort,
       date_trunc('month', d)::date              as month_start,
       date_trunc('week', d)::date               as week_start
-- Runs to Mar 2018 (three months past the last sync) so the Revenue Trend Forecast has months to project into.
from range(date '2016-10-01', date '2018-04-01', interval 1 day) t(d);

create or replace table mart.dim_account as
select company_id                                       as account_id,
       account, sector, country,
       coalesce(parent_company, account)                as parent_company,
       parent_company is not null                       as is_subsidiary,
       employees, annual_revenue, founded_year,
       case when employees < 1000 then 'Small (<1k)'
            when employees < 5000 then 'Mid (1k–5k)'
            else 'Large (5k+)' end                      as size_band,
       case when employees < 1000 then 1 when employees < 5000 then 2 else 3 end as size_sort
from stg.companies
union all
select '0', '(no account)', '(unknown)', '(unknown)', '(no account)', false, null, null, null, '(unknown)', 4;

create or replace table mart.dim_product as
select product_id, product, series, list_price,
       row_number() over (order by list_price desc)::int as product_sort
from stg.products;

create or replace table mart.dim_agent as
select o.owner_id, o.agent, o.manager, o.regional_office, o.email,
       exists (select 1 from stg.deals d where d.owner_id = o.owner_id) as has_deals
from stg.owners o;

create or replace table mart.dim_stage as
select * from (values
    ('prospecting', 'Prospecting', 1, true,  false, false),
    ('engaging',    'Engaging',    2, true,  false, false),
    ('closedwon',   'Won',         3, false, true,  true),
    ('closedlost',  'Lost',        4, false, true,  false)
) t(stage_key, stage, stage_sort, is_open, is_closed, is_won);

-- Days from engage to close. 0–14 is its own band: those deals win noticeably less often.
create or replace table mart.dim_cycle_band as
select * from (values
    ('0–14 days', 1, 0, 14), ('15–30 days', 2, 15, 30), ('31–60 days', 3, 31, 60),
    ('61–90 days', 4, 61, 90), ('91+ days', 5, 91, 100000)
) t(cycle_band, cycle_sort, low_days, high_days);

-- Age of an open deal. 104 days = 90th percentile of the time closed deals took; older than that is stale.
create or replace table mart.dim_age_band as
select * from (values
    ('0–30 days', 1, 0, 30), ('31–60 days', 2, 31, 60), ('61–104 days', 3, 61, 104),
    ('105–180 days', 4, 105, 180), ('181–365 days', 5, 181, 365), ('365+ days', 6, 366, 100000)
) t(age_band, age_sort, low_days, high_days);

-- ------------------------------------------------------------------ facts
create or replace table mart.fact_opportunity as
with d as (
    select s.*, p.list_price, st.is_open, st.is_won,
           export_date() - s.created_date                         as open_age,
           s.closed_date - s.engaged_date                         as cycle
    from stg.deals s
    join stg.products p using (product)
    join mart.dim_stage st using (stage_key)
)
select opportunity_id, owner_id,
       coalesce(company_id, '0')                                  as account_id,
       p.product_id, stage_key,
       created_date, engaged_date, closed_date,
       case when not is_open then coalesce(amount, 0) end         as close_value,
       d.list_price,
       case when is_open then d.list_price end                    as open_value,
       case when is_won then amount / d.list_price end            as price_realisation,
       case when not is_open then cycle end                       as cycle_days,
       (select cycle_band from mart.dim_cycle_band where cycle between low_days and high_days and not is_open) as cycle_band,
       case when is_open then open_age end                        as age_days,
       (select age_band from mart.dim_age_band where open_age between low_days and high_days and is_open) as age_band,
       company_id is not null                                     as has_account,
       modified_at
from d join mart.dim_product p using (product);

-- The pipeline as each month-end sync saw it: latest version of every deal known on that date.
create or replace table mart.fact_pipeline_snapshot as
with runs as (select as_of as snapshot_date from raw.sync_runs),
v as (
    select r.snapshot_date, v.*
    from runs r join stg.deal_versions v on v.synced_at <= r.snapshot_date
    qualify row_number() over (partition by r.snapshot_date, v.opportunity_id
                               order by v.modified_at desc, v.synced_at desc) = 1
)
select v.snapshot_date, v.opportunity_id, v.owner_id,
       coalesce(v.company_id, '0')                                as account_id,
       p.product_id, v.stage_key,
       v.snapshot_date - v.created_date                           as age_days,
       case when st.is_open then (select age_band from mart.dim_age_band
                                  where v.snapshot_date - v.created_date between low_days and high_days) end as age_band,
       case when st.is_open then p.list_price end                 as open_value,
       case when not st.is_open then coalesce(v.amount, 0) end    as close_value
from v join mart.dim_product p using (product) join mart.dim_stage st using (stage_key);

-- Stage at the previous month-end sync vs this one, per deal: the transition matrix. A deal first seen at
-- this sync comes from '(new)'. Deals already closed at the previous sync are left out (they cannot move).
create or replace table mart.fact_stage_transition as
with s as (
    select snapshot_date, opportunity_id, owner_id, account_id, product_id, stage_key,
           lag(stage_key) over (partition by opportunity_id order by snapshot_date)     as prev_key,
           lag(snapshot_date) over (partition by opportunity_id order by snapshot_date) as prev_date
    from mart.fact_pipeline_snapshot
),
runs as (select snapshot_date, lag(snapshot_date) over (order by snapshot_date) as prev_run
         from (select distinct snapshot_date from mart.fact_pipeline_snapshot))
select s.snapshot_date, s.opportunity_id, s.owner_id, s.account_id, s.product_id,
       coalesce(f.stage, '(new)')  as from_stage, coalesce(f.stage_sort, 0) as from_sort,
       t.stage                     as to_stage,   t.stage_sort              as to_sort
from s join runs r using (snapshot_date)
join mart.dim_stage t on t.stage_key = s.stage_key
left join mart.dim_stage f on f.stage_key = s.prev_key and s.prev_date = r.prev_run
where r.prev_run is not null and (f.stage_key is null or f.is_open);

-- One row per deal per funnel step it reached.
create or replace table mart.fact_funnel as
select opportunity_id, owner_id, account_id, product_id, step, step_sort
from mart.fact_opportunity,
     (values ('Created', 1), ('Engaged', 2), ('Closed', 3), ('Won', 4)) s(step, step_sort)
where step_sort = 1
   or (step_sort = 2 and engaged_date is not null)
   or (step_sort = 3 and stage_key in ('closedwon', 'closedlost'))
   or (step_sort = 4 and stage_key = 'closedwon');

create or replace table mart.sync_runs as
select run_id, as_of, mode, started_at, seconds, requests, retries, rate_limited, server_errors,
       owners, companies, products, deals_seen, deal_versions_added, watermark_before, watermark_after
from raw.sync_runs order by as_of;

create or replace table mart.manager_access as
select user_email, regional_office, coalesce(manager, '') as manager
from read_csv('{seeds}/manager_access.csv', all_varchar = true);
