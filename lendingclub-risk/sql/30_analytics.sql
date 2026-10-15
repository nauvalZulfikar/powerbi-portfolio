-- =====================================================================
-- ANALYTICS: pre-aggregated tables for shapes that are awkward in DAX.
-- =====================================================================

-- Vintage curves: of the 36-month loans issued in a cohort period, what
-- cumulative share had defaulted by month-on-book k? Years are grouped into
-- five cohorts (a readable number of lines). Each curve stops at the last month
-- every loan in the cohort can be observed (the tape ends Sep 2018).
create or replace table mart.vintage_curve as
with loans as (
    select
        case when vintage_year <= 2009 then '2008–09 crisis'
             when vintage_year <= 2012 then '2010–12'
             when vintage_year <= 2014 then '2013–14'
             when vintage_year <= 2016 then '2015–16'
             else '2017' end                               as vintage,
        case when vintage_year <= 2009 then 2009 when vintage_year <= 2012 then 2012
             when vintage_year <= 2014 then 2014 when vintage_year <= 2016 then 2016
             else 2017 end                                 as last_year,
        grade, funded_amount, is_default, months_to_default
    from mart.fact_loans f join mart.dim_grade using (sub_grade)
    where term = '36 months' and vintage_year between 2008 and 2017
),
grid as (
    select v.vintage, v.last_year, g.grade, m.mob
    from (select distinct vintage, last_year from loans) v
    cross join (select distinct grade from loans union all select 'All') g(grade)
    cross join range(1, 43) m(mob)
    where date_add(make_date(v.last_year, 12, 1), interval (m.mob) month) <= date '2018-09-01'
)
select
    g.vintage,
    g.last_year                                                               as vintage_sort,
    g.grade,
    g.mob,
    count(*)                                                                  as loans,
    count(*) filter (where is_default = 1 and months_to_default <= g.mob)     as defaults,
    round(defaults / loans, 6)                                                as cum_default_rate,
    round(coalesce(sum(funded_amount) filter (where is_default = 1 and months_to_default <= g.mob), 0)
          / sum(funded_amount), 6)                                            as cum_default_rate_dollar,
    sum(funded_amount)                                                        as funded,
    coalesce(sum(funded_amount) filter (where is_default = 1 and months_to_default <= g.mob), 0) as defaulted_funded
from grid g
join loans l on l.vintage = g.vintage and (g.grade = 'All' or l.grade = g.grade)
group by all;

-- Loss given default by grade (the LGD the live-book expected loss uses).
create or replace table mart.grade_lgd as
select
    d.grade,
    count(*)                                             as defaulted_loans,
    sum(exposure_at_default)                             as exposure_at_default,
    sum(net_recoveries)                                  as net_recoveries,
    sum(net_loss) / sum(exposure_at_default)             as lgd,
    avg(months_to_default)                               as avg_months_to_default
from mart.fact_loans f join mart.dim_grade d using (sub_grade)
where outcome = 'Defaulted'
group by 1;

-- Book balance (semi-additive stock): estimated outstanding principal at each month end, per sub-grade.
-- The tape only has today's balance per loan, so the monthly history is rebuilt from each loan's
-- amortisation schedule (funded amount, rate, term): balance after k+1 scheduled payments, from the issue
-- month until the loan's last payment (resolved loans) or the tape's last payment month (Mar 2019, open loans).
-- Prepayments and missed payments are not in the schedule, so this is an estimate, reconciled in 90_tests.sql.
create or replace table mart.book_balance as
with l as (
    select sub_grade, issue_date, funded_amount as p, int_rate / 12 as r,
           split_part(term, ' ', 1)::int as n,
           case when is_resolved then coalesce(last_payment_date, issue_date) else date '2019-03-01' end as last_month
    from mart.fact_loans
    where funded_amount > 0
)
select
    last_day(issue_date + to_months(k))::date                               as month_end,
    sub_grade,
    round(sum(p * (power(1 + r, n) - power(1 + r, k + 1)) / (power(1 + r, n) - 1)), 2) as est_outstanding,
    count(*)                                                                as active_loans
from l, range(0, 60) t(k)
where k < n and issue_date + to_months(k) < last_month
group by all;
