-- Analytics: monthly variance cube by entity x P&L detail line, used by the tests and the HTML twin.
-- The Power BI model computes the same numbers live from fact_gl through DAX measures.
create or replace table mart.variance_cube as
with base as (
    select f.period, f.entity_key, a.line_id,
           sum(amount_usd) filter (where scenario = 'Actual')                as actual,
           sum(amount_usd_budget_rate) filter (where scenario = 'Actual')    as actual_at_budget_rate,
           sum(amount_usd) filter (where scenario = 'Budget')                as budget
    from mart.fact_gl f join mart.dim_account a using (account_key)
    group by all
)
select b.*, py.actual as prior_year,
       coalesce(b.actual, 0) - coalesce(b.actual_at_budget_rate, 0)  as fx_effect,
       coalesce(b.actual_at_budget_rate, 0) - coalesce(b.budget, 0)   as operational_variance
from base b
left join base py on py.entity_key = b.entity_key and py.line_id = b.line_id
                 and py.period = (b.period - interval 1 year)::date;
