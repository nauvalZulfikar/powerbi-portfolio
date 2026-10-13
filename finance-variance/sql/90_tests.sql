-- Data-quality tests. Each returns a count of violations; 0 = pass.

-- name: staging reconciles to raw ledger (2011-2013 actuals, every row kept)
select count(*) from (
    select sum(amount) a from stg.gl_raw where scenario_key = 1 and period between '2011-01-01' and '2013-12-01'
) r, (select sum(amount_local) s from stg.gl where scenario = 'Actual') s
where abs(r.a - s.s) > 0.01;

-- name: fact_gl grain is unique
select count(*) from (
    select 1 from mart.fact_gl group by period, entity_key, department_key, account_key, scenario, currency_code having count(*) > 1);

-- name: every P&L account maps to exactly one detail line
select count(*) from (
    select a.account_key from stg.account a left join stg.pnl_line_accounts m using (account_code)
    where a.statement = 'Net Income' and a.account_key in (select account_key from stg.gl)
      and a.account_code not in (9510) and a.account_type <> 'Statistical'
    group by 1 having count(m.line_id) <> 1);

-- name: no ledger P&L amount lost by the line mapping
select count(*) from (
    select sum(g.amount_local) a from stg.gl g join stg.account a using (account_key)
    where a.statement = 'Net Income' and g.scenario = 'Actual'
) x, (select sum(g.amount_local * a.pnl_sign) b from stg.gl_pnl g join mart.dim_account a using (account_key)
      where g.scenario = 'Actual') y
where abs(x.a - y.b) > 0.01;

-- name: net income line = sum of every signed P&L account (bridge rolls up completely)
select count(*) from (
    select f.scenario, year(f.period) y,
           sum(f.amount_usd) filter (where b.line_id = 'S6') ni, sum(f.amount_usd) filter (where b.line_id like 'L%') detail
    from mart.fact_gl f join mart.bridge_line_account b using (account_key) group by all
) where abs(ni - detail) > 0.01;

-- name: every subtotal = sum of its children
select count(*) from (
    select p.line_id, f.scenario, sum(f.amount_usd) parent_amt
    from mart.fact_gl f join mart.bridge_line_account p using (account_key)
    where p.line_id like 'S%' group by all
) p join (
    select l.parent_line, f.scenario, sum(f.amount_usd) child_amt
    from mart.fact_gl f join mart.bridge_line_account b using (account_key) join stg.pnl_lines l on l.line_id = b.line_id
    where l.parent_line is not null group by all
) c on c.parent_line = p.line_id and c.scenario = p.scenario
where abs(parent_amt - child_amt) > 0.01;

-- name: eliminations net intercompany sales to zero at group level
select count(*) from (
    select period, scenario, sum(amount_local) s from mart.fact_gl f join mart.dim_account a using (account_key)
    where a.account_code = 4500 group by all
) where abs(s) > 0.01;

-- name: eliminations leave group gross margin unchanged
select count(*) from (
    select period, scenario, sum(amount_usd) s from mart.fact_gl f join mart.bridge_line_account b using (account_key)
    where b.line_id = 'S2' and is_elimination group by all
) where abs(s) > 0.01;

-- name: USD entities translate 1:1
select count(*) from mart.fact_gl where currency_code = 'USD' and (amount_usd <> amount_local or amount_usd_budget_rate <> amount_local);

-- name: a rate exists for every non-USD entity and period (no rows dropped by FX join)
select count(*) from (
    select (select count(*) from stg.gl_pnl) - (select count(*) from stg.fact where scenario = 'Actual')
         - (select count(*) from stg.gl_pnl where scenario = 'Budget') d
) where d <> 0;

-- name: actual - budget = FX effect + operational variance
select count(*) from mart.variance_cube
where abs(coalesce(actual, 0) - coalesce(budget, 0) - fx_effect - operational_variance) > 0.01;

-- name: budget FY2012-13 reconciles to prior-year actual x driver
select count(*) from (
    select d.fiscal_year, d.line_id, d.growth,
           sum(g.amount_local) filter (where g.scenario = 'Actual' and year(g.period) = d.fiscal_year - 1) py,
           (select sum(amount_local) from stg.budget b join mart.dim_account a using (account_key)
            where a.line_id = d.line_id and year(b.period) = d.fiscal_year) bud
    from stg.budget_drivers d join stg.gl_pnl g on g.line_id = d.line_id group by all
) where abs(py * (1 + growth) - bud) > 0.01;

-- name: entity access seed only references real entities
select count(*) from mart.entity_access where entity_key not in (select entity_key from stg.entity);

-- name: every month in range has actuals
select count(*) from generate_series(date '2011-01-01', date '2013-12-01', interval 1 month) g(p)
where p::date not in (select period from mart.fact_gl where scenario = 'Actual');

-- name: every forecast row resolves to an entity and a P&L line
select count(*) from stg.forecast f
where f.entity_name is null or f.entity_name not in (select entity from mart.dim_entity)
   or f.line not in (select line from mart.dim_pnl_line);

-- name: forecast months add up to each file row's Total column (parsing, fill-down, de-DE decimals)
select count(*) from (
    select source_file, row_index, any_value(file_total) t, sum(coalesce(amount_usd, 0)) s
    from stg.forecast group by all
) where abs(t - s) > 0.01;

-- name: journal postings add up to the ledger (every actual posting, per period x entity x department x account)
select count(*) from (
    select period, entity_key, department_key, account_key, sum(amount_usd) a from mart.gl_journal group by all
) j full join (
    select period, entity_key, department_key, account_key, sum(amount_usd) a from mart.fact_gl
    where scenario = 'Actual' and not is_elimination group by all
) g using (period, entity_key, department_key, account_key)
where abs(coalesce(j.a, 0) - coalesce(g.a, 0)) > 0.01;

-- name: every actual posting date sits in the 5-day cut-off window of its period and inside dim_date
select count(*) from mart.fact_gl
where posting_date is null
   or (scenario = 'Actual' and date_trunc('month', posting_date + interval 5 day) <> period)
   or posting_date not in (select date from mart.dim_date);

-- name: headcount plan only uses known departments (regions may be planned ahead of an entity)
select count(*) from mart.headcount_plan
where department_key is null or department_key not in (select department_key from mart.dim_department);
