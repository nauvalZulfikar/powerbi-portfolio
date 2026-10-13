-- Staging: AdventureWorksDW finance extract → typed, de-duplicated, period-aligned tables.
create schema if not exists stg;

-- Chart of accounts, flattened. The source is a parent-child tree where each node's
-- unary operator (+/-) says how it rolls into its parent; the product of operators up
-- to "Net Income" is the sign that turns a positive ledger amount into a P&L amount.
create or replace table stg.account as
with recursive raw as (
    select * from read_csv('{raw}/DimAccount.csv', delim = '|', header = false, auto_detect = false, columns = {
        'account_key': 'INTEGER', 'parent_key': 'INTEGER', 'account_code': 'INTEGER', 'parent_code': 'INTEGER',
        'account': 'VARCHAR', 'account_type': 'VARCHAR', 'operator': 'VARCHAR', 'custom_members': 'VARCHAR',
        'value_type': 'VARCHAR', 'custom_member_options': 'VARCHAR'})
), tree as (
    select account_key, account, 0 as depth, account as root, 1 as sign, account_key::varchar as key_path
    from raw where parent_key is null
    union all
    select r.account_key, r.account, t.depth + 1, t.root,
           t.sign * case r.operator when '-' then -1 else 1 end,
           t.key_path || '|' || r.account_key
    from raw r join tree t on r.parent_key = t.account_key
)
select r.account_key, r.account_code, r.account, r.account_type, r.value_type,
       t.root as statement, t.depth, t.sign as pnl_sign, t.key_path
from raw r join tree t using (account_key);

-- Entities. Ownership is direct; effective ownership multiplies down the chain
-- (Australia = 50% of a 75%-owned Pacific holding = 37.5%).
create or replace table stg.entity as
with recursive raw as (
    select o.*, c.currency_code
    from read_csv('{raw}/DimOrganization.csv', delim = '|', header = false, auto_detect = false, columns = {
        'entity_key': 'INTEGER', 'parent_key': 'INTEGER', 'ownership': 'DOUBLE', 'entity': 'VARCHAR', 'currency_key': 'INTEGER'}) o
    join read_csv('{raw}/DimCurrency.csv', delim = '|', header = false, auto_detect = false, columns = {
        'currency_key': 'INTEGER', 'currency_code': 'VARCHAR', 'currency': 'VARCHAR'}) c using (currency_key)
), tree as (
    select entity_key, 0 as depth, ownership as effective_ownership, entity_key::varchar as key_path,
           null::varchar as region, null::varchar as sub_region
    from raw where parent_key is null
    union all
    select r.entity_key, t.depth + 1, t.effective_ownership * r.ownership, t.key_path || '|' || r.entity_key,
           coalesce(t.region, case when t.depth = 0 then r.entity end),
           coalesce(t.sub_region, case when t.depth = 1 then r.entity end)
    from raw r join tree t on r.parent_key = t.entity_key
)
select r.entity_key, r.parent_key, r.entity, r.currency_code, r.ownership, t.effective_ownership,
       t.depth, t.key_path, t.region, t.sub_region
from raw r join tree t using (entity_key);

-- General ledger. AdventureWorks shifted its sample dates, so period-end postings drift
-- between the 27th and the 1st of the next month; the fiscal period is the month that
-- contains (posting date + 5 days). Six source rows repeat the same grain — summed.
create or replace table stg.gl_raw as
select date_trunc('month', f.posted + interval 5 day)::date as period, f.*
from read_csv('{raw}/FactFinance.csv', delim = '|', header = false, auto_detect = false, columns = {
    'finance_key': 'INTEGER', 'date_key': 'INTEGER', 'entity_key': 'INTEGER', 'department_key': 'INTEGER',
    'scenario_key': 'INTEGER', 'account_key': 'INTEGER', 'amount': 'DOUBLE', 'posted': 'TIMESTAMP'}) f;

create or replace table stg.gl as
select period, entity_key, department_key, account_key,
       case scenario_key when 1 then 'Actual' when 2 then 'Budget' end as scenario,
       sum(amount) as amount_local,
       min(posted)::date as posting_date   -- one posting run per month (the 6 repeated rows differ by a day)
from stg.gl_raw
where period between date '2011-01-01' and date '2013-12-01'   -- Jan-2014 is a 33-row stub
group by all;

create or replace table stg.department as
select * from read_csv('{raw}/DimDepartmentGroup.csv', delim = '|', header = false, auto_detect = false, columns = {
    'department_key': 'INTEGER', 'parent_key': 'INTEGER', 'department': 'VARCHAR'});

-- FX: rates are local-currency units per 1 USD. Monthly average for the P&L,
-- and a budget rate per fiscal year = December average of the prior year
-- (the rate in force when the budget was signed off).
create or replace table stg.fx_daily as
select c.currency_code, r.rate_date::date as rate_date, r.average_rate
from read_csv('{raw}/FactCurrencyRate.csv', delim = '|', header = false, auto_detect = false, columns = {
    'currency_key': 'INTEGER', 'date_key': 'INTEGER', 'average_rate': 'DOUBLE', 'end_of_day_rate': 'DOUBLE',
    'rate_date': 'TIMESTAMP'}) r
join read_csv('{raw}/DimCurrency.csv', delim = '|', header = false, auto_detect = false, columns = {
    'currency_key': 'INTEGER', 'currency_code': 'VARCHAR', 'currency': 'VARCHAR'}) c using (currency_key)
where c.currency_code in (select currency_code from stg.entity) and c.currency_code <> 'USD';  -- USD = 1 by definition

create or replace table stg.fx_month as
select currency_code, date_trunc('month', rate_date)::date as period, avg(average_rate) as avg_rate
from stg.fx_daily group by all
union all
select 'USD', p::date, 1.0 from generate_series(date '2010-12-01', date '2013-12-01', interval 1 month) g(p);

create or replace table stg.fx_budget as
select currency_code, year(period) + 1 as fiscal_year, avg_rate as budget_rate
from stg.fx_month where month(period) = 12 and year(period) between 2010 and 2012;

-- Seeds
create or replace table stg.pnl_lines as select * from read_csv('{seeds}/pnl_lines.csv', header = true);
create or replace table stg.pnl_line_accounts as select * from read_csv('{seeds}/pnl_line_accounts.csv', header = true);
create or replace table stg.budget_drivers as select * from read_csv('{seeds}/budget_drivers.csv', header = true);
create or replace table stg.entity_access as select * from read_csv('{seeds}/entity_access.csv', header = true);
-- SYNTHETIC year-end headcount plan per region x department (src/make_headcount_plan.py).
create or replace table stg.headcount_plan as select * from read_csv('{seeds}/headcount_plan.csv', header = true);

-- Forecast submissions (SYNTHETIC, seeds/forecast/): one file per entity, two title rows, months as
-- columns, entity name only on a block's first row, "n/a"/"-" for empty cells, Germany in de-DE.
-- Power BI parses the same files in Power Query (Forecast table); this is the testable twin.
create or replace table stg.forecast_raw as
with files as (   -- row_number() over () right on the scan keeps file order (insertion order is preserved)
    select *, 'en-US' as culture, row_number() over () as scan_order from read_csv('{seeds}/forecast/*_en-US.csv',
        delim = ',', skip = 2, header = true, all_varchar = true, filename = true)
    union all by name
    select *, 'de-DE' as culture, 1e6 + row_number() over () as scan_order from read_csv('{seeds}/forecast/*_de-DE.csv',
        delim = ';', skip = 2, header = true, all_varchar = true, filename = true)
)
select * exclude (scan_order), row_number() over (partition by filename order by scan_order) as row_index from files;

create or replace table stg.forecast as
with filled as (
    select *, last_value(nullif(trim("Entity"), '') ignore nulls) over (partition by filename order by row_index) as entity_label
    from stg.forecast_raw
), months as (
    unpivot filled on Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, "Dec" into name month value amount_text
)
select split_part(entity_label, ' - ', 1) as entity_code, split_part(entity_label, ' - ', 2) as entity_name,
       trim("Line") as line, regexp_extract(filename, 'FY(\d{4})', 1)::int as fiscal_year,
       strptime(month || ' 1 ' || regexp_extract(filename, 'FY(\d{4})', 1), '%b %d %Y')::date as period,
       case when amount_text in ('n/a', '-') then null
            else (case when culture = 'de-DE' then replace(amount_text, ',', '.') else amount_text end)::double end as amount_usd,
       (case when culture = 'de-DE' then replace("Total", ',', '.') else "Total" end)::double as file_total,
       case when culture = 'de-DE' then strptime("Submitted", '%d.%m.%Y')::date else "Submitted"::date end as submitted,
       regexp_extract(filename, '[^/]+$') as source_file, row_index
from months;
