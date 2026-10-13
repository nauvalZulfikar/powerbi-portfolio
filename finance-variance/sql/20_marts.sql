-- Marts: star schema for Power BI. All P&L amounts are SIGNED (revenue +, cost -),
-- so every variance is "actual minus comparison" and positive always means favourable.
create schema if not exists mart;

-- ------------------------------------------------------------------ dimensions
create or replace table mart.dim_date as
select d::date as date, year(d) as year, quarter(d) as quarter, month(d) as month_no,
       strftime(d, '%b') as month_name, date_trunc('month', d)::date as month_start,
       'Q' || quarter(d) || ' ' || year(d) as year_quarter, year(d) * 10 + quarter(d) as year_quarter_sort,
       strftime(d, '%b %Y') as month_year, year(d) * 100 + month(d) as month_year_sort
from generate_series(date '2010-12-01', date '2013-12-31', interval 1 day) g(d);   -- Dec 2010: first posting run (29 Dec) for Jan 2011

-- Posting entities only (holding nodes carry no ledger) + a group eliminations entity.
create or replace table mart.dim_entity as
with posting as (
    select e.*, min(g.period) as first_period
    from stg.entity e join stg.gl g using (entity_key)
    where g.scenario = 'Actual'
    group by all
)
select entity_key, entity, region, sub_region as unit,
       case when sub_region = 'USA Operations' then 'United States'
            when sub_region = 'Canadian Division' then 'Canada' else entity end as country,
       currency_code, effective_ownership, key_path as entity_path, first_period,
       entity_key as entity_sort
from posting
union all
select 99, 'Group eliminations', 'Eliminations', 'Eliminations', 'Group', 'USD', 1.0, '1|99', date '2011-01-01', 99;

create or replace table mart.dim_department as
select department_key, case when parent_key is null then 'Corporate (unallocated)' else department end as department
from stg.department;

create or replace table mart.dim_pnl_line as
select line_id, line, sort as line_sort, kind as line_kind, sort <= 150 as is_operating from stg.pnl_lines;

-- P&L accounts with their detail line.
create or replace table mart.dim_account as
select a.account_key, a.account_code, a.account, a.pnl_sign, m.line_id, l.line as detail_line, l.sort as detail_line_sort
from stg.account a
join stg.pnl_line_accounts m using (account_code)
join stg.pnl_lines l using (line_id)
where a.statement = 'Net Income';

-- Bridge: every P&L line (detail and subtotal) → the accounts it sums.
create or replace table mart.bridge_line_account as
with recursive up(line_id, account_key) as (
    select line_id, account_key from mart.dim_account
    union all
    select l.parent_line, u.account_key
    from up u join stg.pnl_lines l on l.line_id = u.line_id
    where l.parent_line is not null
)
select line_id, account_key from up;

-- ---------------------------------------------------------------- ledger + FX
create or replace table stg.gl_pnl as
select g.period, g.entity_key, g.department_key, g.account_key, g.scenario,
       g.amount_local * a.pnl_sign as amount_local, a.line_id, g.posting_date
from stg.gl g join mart.dim_account a using (account_key);

-- Budget. FY2011 is AdventureWorks' own budget (North American entities, operating lines).
-- FY2012-13 are driver-based: prior-year actual for the same entity, department, account
-- and month x (1 + growth for that P&L line). Entities with no prior-year actual (first
-- year after acquisition) get no budget, as in practice.
create or replace table stg.budget as
-- A budget has no posting run: its posting date is the first of its month.
select period, entity_key, department_key, account_key, amount_local, 'AdventureWorks FY2011 budget' as budget_source
from stg.gl_pnl where scenario = 'Budget'
union all
select (g.period + interval 1 year)::date, g.entity_key, g.department_key, g.account_key,
       g.amount_local * (1 + d.growth), 'Driver-based (prior-year actual x growth)'
from stg.gl_pnl g
join stg.budget_drivers d on d.line_id = g.line_id and d.fiscal_year = year(g.period) + 1
where g.scenario = 'Actual' and year(g.period) in (2011, 2012);

create or replace table stg.fact as
with lines as (
    select period, entity_key, department_key, account_key, 'Actual' as scenario, amount_local, null as budget_source, posting_date
    from stg.gl_pnl where scenario = 'Actual'
    union all
    select period, entity_key, department_key, account_key, 'Budget', amount_local, budget_source, period from stg.budget
)
select l.*, e.currency_code,
       case l.scenario when 'Actual' then m.avg_rate else b.budget_rate end as fx_rate,
       b.budget_rate
from lines l
join stg.entity e using (entity_key)
join stg.fx_month m on m.currency_code = e.currency_code and m.period = l.period
join stg.fx_budget b on b.currency_code = e.currency_code and b.fiscal_year = year(l.period);

-- Intercompany sales are eliminated at group level against cost of sales (the buying
-- entity's purchase), so group revenue excludes them and group gross margin is unchanged.
-- Elimination rows keep the selling entity's currency so FX translation stays consistent;
-- grain of the fact is therefore (period, entity, department, account, scenario, currency).
create or replace table mart.fact_gl as
with ic as (
    select f.* from stg.fact f join mart.dim_account a using (account_key) where a.account_code = 4500
), elim as (
    select period, 99 as entity_key, department_key, account_key, scenario, -amount_local as amount_local,
           budget_source, posting_date, currency_code, fx_rate, budget_rate
    from ic
    union all
    select ic.period, 99, ic.department_key, (select account_key from mart.dim_account where account_code = 5020),
           ic.scenario, ic.amount_local, ic.budget_source, ic.posting_date, ic.currency_code, ic.fx_rate, ic.budget_rate
    from ic
), allrows as (
    select *, false as is_elimination from stg.fact
    union all
    select period, entity_key, department_key, account_key, scenario, sum(amount_local), budget_source,
           posting_date, currency_code, fx_rate, budget_rate, true
    from elim group by all
)
select period, posting_date, entity_key, department_key, account_key, scenario, budget_source, is_elimination,
       currency_code, fx_rate, budget_rate,
       amount_local,
       amount_local / fx_rate as amount_usd,                 -- actual @ monthly avg rate, budget @ budget rate
       amount_local / budget_rate as amount_usd_budget_rate  -- both @ budget rate → isolates FX effect
from allrows;

create or replace table mart.fact_headcount as
select g.period, g.entity_key, g.department_key, g.amount_local as headcount
from stg.gl g join stg.account a using (account_key)
where a.account_code = 9510 and g.scenario = 'Actual';

create or replace table mart.fx_rates as
select m.currency_code, m.period, m.avg_rate, b.budget_rate
from stg.fx_month m join stg.fx_budget b on b.currency_code = m.currency_code and b.fiscal_year = year(m.period)
where m.currency_code <> 'USD' and m.period >= date '2011-01-01';

create or replace table mart.budget_drivers as
select d.fiscal_year, d.line_id, l.line, l.sort as line_sort, d.growth, d.rationale
from stg.budget_drivers d join stg.pnl_lines l using (line_id);

create or replace table mart.entity_access as select user_email, entity_key from stg.entity_access;

-- Steps of the budget → actual operating-profit bridge (waterfall). The chart's own total = actual.
create or replace table mart.dim_variance_step as
select 'BUD' as step_id, 'Budget' as step, 0 as step_sort
union all select 'FX', 'FX translation', 1
union all select line_id, line, sort from stg.pnl_lines where kind = 'detail' and sort <= 150;

-- Reforecast (SYNTHETIC). Same columns the Power Query `Forecast` table produces; empty cells dropped.
create or replace table mart.forecast as
select e.entity_key, f.entity_code, l.line_id, f.period, f.fiscal_year, f.amount_usd, f.submitted,
       f.submitted > make_date(f.fiscal_year, 1, 10) as is_late_submission, f.source_file, f.row_index
from stg.forecast f
join mart.dim_entity e on e.entity = f.entity_name
join mart.dim_pnl_line l on l.line = f.line
where f.amount_usd is not null;

-- Posting-level journal: every actual P&L posting with its own posting date (fact_gl sums these per period).
-- In the Fabric variant model this table is DirectQuery; GL Journal Agg answers the totals.
create or replace table mart.gl_journal as
select r.finance_key as journal_id, r.posted::date as posting_date, r.period, r.entity_key, r.department_key,
       r.account_key, r.amount * a.pnl_sign as amount_local, r.amount * a.pnl_sign / m.avg_rate as amount_usd
from stg.gl_raw r
join mart.dim_account a using (account_key)
join stg.entity e using (entity_key)
join stg.fx_month m on m.currency_code = e.currency_code and m.period = r.period
where r.scenario_key = 1 and r.period between date '2011-01-01' and date '2013-12-01';

-- Headcount plan (SYNTHETIC): year-end heads per region x department. One planned region has no entity yet,
-- so the many-to-many (limited) relationship to Entity[Region] has nothing to match it with.
create or replace table mart.headcount_plan as
select region, department_key, fiscal_year, planned_heads from stg.headcount_plan;
