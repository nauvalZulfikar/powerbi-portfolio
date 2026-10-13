-- Each test returns the number of offending rows; 0 = pass.

-- name: every source deal arrived, once
select abs(count(*) - 8800) + count(*) - count(distinct opportunity_id) from mart.fact_opportunity;

-- name: won revenue reconciles to the source ($10,005,534)
select (sum(close_value) filter (where stage_key = 'closedwon') <> 10005534)::int from mart.fact_opportunity;

-- name: stage counts match the source (4238 won, 2473 lost, 1589 engaging, 500 prospecting)
select count(*) from (
    select stage_key, count(*) n from mart.fact_opportunity group by 1
) where (stage_key, n) not in (('closedwon', 4238), ('closedlost', 2473), ('engaging', 1589), ('prospecting', 500));

-- name: won deals have a value and close on/after engagement
select count(*) from mart.fact_opportunity
where stage_key = 'closedwon' and (close_value <= 0 or closed_date < engaged_date);

-- name: lost deals carry no revenue
select count(*) from mart.fact_opportunity where stage_key = 'closedlost' and close_value <> 0;

-- name: open deals have an age band, closed deals a cycle band
select count(*) from mart.fact_opportunity
where (stage_key in ('engaging', 'prospecting') and age_band is null)
   or (stage_key in ('closedwon', 'closedlost') and cycle_band is null);

-- name: every deal maps to a product, agent and account
select count(*) from mart.fact_opportunity f
where f.product_id not in (select product_id from mart.dim_product)
   or f.owner_id not in (select owner_id from mart.dim_agent)
   or f.account_id not in (select account_id from mart.dim_account);

-- name: source typos are cleaned
select (select count(*) from mart.dim_account where sector ilike 'technolgy' or country = 'Philipines')
     + (select count(*) from stg.deals where product = 'GTXPro');

-- name: the last snapshot equals the current state
select count(*) from (
    select opportunity_id, stage_key, close_value from mart.fact_pipeline_snapshot
    where snapshot_date = (select max(snapshot_date) from mart.fact_pipeline_snapshot)
    except
    select opportunity_id, stage_key, close_value from mart.fact_opportunity
);

-- name: no deal moves backwards between snapshots
select count(*) from (
    select s.opportunity_id, st.stage_sort,
           lag(st.stage_sort) over (partition by opportunity_id order by snapshot_date) prev,
           lag(st.is_open) over (partition by opportunity_id order by snapshot_date) prev_open
    from mart.fact_pipeline_snapshot s join mart.dim_stage st using (stage_key)
) where stage_sort < prev or prev_open = false and stage_sort <> prev;

-- name: the sync watermark never goes backwards
select count(*) from (
    select watermark_after, lag(watermark_after) over (order by as_of) prev from mart.sync_runs
) where watermark_after < prev;

-- name: only the first sync is a full load
select count(*) from (select mode, row_number() over (order by as_of) rn from mart.sync_runs)
where (rn = 1) <> (mode = 'full');

-- name: RLS seeds reference real regions and managers
select count(*) from mart.manager_access m
where not exists (select 1 from mart.dim_agent a where a.regional_office = m.regional_office
                  and (m.manager = '' or a.manager = m.manager));

-- name: stage transitions account for every deal open at the previous sync
select count(*) from (
    select r.snapshot_date, s.opportunity_id
    from (select snapshot_date, lead(snapshot_date) over (order by snapshot_date) nxt
          from (select distinct snapshot_date from mart.fact_pipeline_snapshot)) r
    join mart.fact_pipeline_snapshot s on s.snapshot_date = r.snapshot_date
    join mart.dim_stage st on st.stage_key = s.stage_key and st.is_open
    where r.nxt is not null
    except
    select (select max(snapshot_date) from mart.fact_pipeline_snapshot p where p.snapshot_date < t.snapshot_date), opportunity_id
    from mart.fact_stage_transition t where from_stage <> '(new)'
);

-- name: P90 of engage-to-close is 104 days (the stale default)
select (quantile_cont(cycle_days, 0.9) <> 104)::int from mart.fact_opportunity;
