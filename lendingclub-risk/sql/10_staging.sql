-- =====================================================================
-- STAGING: type and clean the raw LendingClub loan tape (2.26M loans,
-- 145 columns). Keep only origination-time attributes plus the realised
-- cash flows needed for loss and return. No business rules beyond typing.
-- =====================================================================
create schema if not exists stg;

create or replace macro lc_month(s) as try_strptime(s, '%b-%Y')::date;

create or replace table stg.loans as
select
    -- The public tape blanks out `id`; mint a stable surrogate key from file order.
    row_number() over ()                                   as loan_id,
    lc_month(issue_d)                                      as issue_date,
    trim(term)::varchar                                    as term,
    trim(replace(term, 'months', ''))::int                 as term_months,
    loan_amnt::double                                      as loan_amount,
    funded_amnt::double                                    as funded_amount,
    int_rate / 100                                         as int_rate,
    installment,
    grade,
    sub_grade,
    case emp_length
        when 'n/a'       then null
        when '< 1 year'  then 0
        when '10+ years' then 10
        else regexp_extract(emp_length, '(\d+)')::int
    end                                                    as emp_years,
    case when home_ownership in ('ANY', 'OTHER', 'NONE') then 'OTHER'
         else home_ownership end                           as home_ownership,
    nullif(annual_inc, 0)                                  as annual_income,
    verification_status,
    purpose,
    addr_state                                             as state,
    case when dti between 0 and 100 then dti end           as dti,
    delinq_2yrs,
    inq_last_6mths,
    open_acc,
    pub_rec,
    revol_bal,
    case when revol_util <= 150 then revol_util / 100 end  as revol_util,
    total_acc,
    mort_acc,
    pub_rec_bankruptcies,
    date_diff('month', lc_month(earliest_cr_line), lc_month(issue_d)) / 12.0
                                                           as credit_history_years,
    application_type,
    loan_status                                            as status_raw,
    replace(loan_status, 'Does not meet the credit policy. Status:', '')
                                                           as status,
    loan_status like 'Does not meet%'                      as outside_policy,
    total_pymnt,
    total_rec_prncp,
    total_rec_int,
    total_rec_late_fee,
    recoveries,
    collection_recovery_fee,
    out_prncp,
    lc_month(last_pymnt_d)                                 as last_payment_date,
    hardship_flag = 'Y'                                    as had_hardship,
    debt_settlement_flag = 'Y'                             as had_settlement
from read_csv_auto('{raw}/loan.csv', sample_size = -1)
where funded_amnt > 0;
