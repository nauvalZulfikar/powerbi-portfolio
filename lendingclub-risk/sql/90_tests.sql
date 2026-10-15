-- Data-quality tests. Each query returns the number of violating rows; 0 = pass.
-- name: fact_loans.loan_id is unique
select count(*) - count(distinct loan_id) from mart.fact_loans;
-- name: no loan lost between staging and fact
select abs((select count(*) from mart.fact_loans) - (select count(*) from stg.loans));
-- name: every loan has an issue date inside dim_date
select count(*) from mart.fact_loans f anti join mart.dim_date d on d.date = f.issue_date;
-- name: every loan has a known sub-grade
select count(*) from mart.fact_loans anti join mart.dim_grade using (sub_grade);
-- name: every loan has a known state
select count(*) from mart.fact_loans anti join mart.dim_state using (state);
-- name: every loan has a known purpose
select count(*) from mart.fact_loans anti join mart.dim_purpose using (purpose);
-- name: every loan is scored by the PD model
select count(*) from mart.fact_loans where pd is null or pd not between 0 and 1;
-- name: funded amount reconciles with raw tape (to the dollar)
select (abs((select sum(funded_amount) from mart.fact_loans) - (select sum(funded_amount) from stg.loans)) > 1)::int;
-- name: interest rates plausible (5%–31%)
select count(*) from mart.fact_loans where int_rate not between 0.05 and 0.31;
-- name: defaulted loans have a charge-off month on book
select count(*) from mart.fact_loans where is_default = 1 and months_to_default is null;
-- name: net loss never exceeds funded amount
select count(*) from mart.fact_loans where net_loss > funded_amount + 1;
-- name: vintage curves are monotone non-decreasing
select count(*) from (
    select cum_default_rate < lag(cum_default_rate) over (partition by vintage, grade order by mob) as dip
    from mart.vintage_curve) where dip;
-- name: LGD within [0, 1]
select count(*) from mart.grade_lgd where lgd not between 0 and 1;
-- name: every state has a tile position
select 51 - count(tile_col) from mart.dim_state;
-- name: cash bridge steps add up to net profit (within 0.01% of funded)
select (abs(sum(principal_paid + interest_paid + late_fees + net_recoveries - funded_amount) - sum(net_profit))
        > 0.0001 * sum(funded_amount))::int
from mart.fact_loans where is_resolved;
-- name: matured loans are almost all resolved (< 1% still open)
select (avg((not is_resolved)::int) filter (where is_matured) > 0.01)::int from mart.fact_loans;
-- name: every last payment date falls inside dim_date
select count(*) from mart.fact_loans f where last_payment_date is not null
  and not exists (select 1 from mart.dim_date d where d.date = f.last_payment_date);
-- name: book balance at Feb 2019 within 10% of the tape's outstanding principal
select (abs((select sum(est_outstanding) from mart.book_balance where month_end = date '2019-02-28')
            / (select sum(outstanding_principal) from mart.fact_loans where not is_resolved) - 1) > 0.10)::int;
-- name: book balance never negative and every month end is in dim_date
select count(*) from mart.book_balance b where est_outstanding < 0
  or not exists (select 1 from mart.dim_date d where d.date = b.month_end);
