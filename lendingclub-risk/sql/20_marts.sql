-- =====================================================================
-- MARTS: star schema for Power BI.
--   fact_loans  (1 row per loan)  ->  dim_date, dim_grade, dim_state, dim_purpose
-- Requires stg.pd_scores, written by src/pd_model.py.
-- =====================================================================
create schema if not exists mart;

-- ---------------------------------------------------------------- dims
create or replace table mart.dim_date as
select
    d::date                                  as date,
    year(d)                                  as year,
    quarter(d)                               as quarter,
    'Q' || quarter(d) || ' ' || year(d)      as year_quarter,
    year(d) * 10 + quarter(d)                as year_quarter_sort,
    month(d)                                 as month,
    strftime(d, '%b')                        as month_name,
    strftime(d, '%b %Y')                     as month_year,
    year(d) * 100 + month(d)                 as month_year_sort,
    date_trunc('month', d)::date             as month_start
from range(date '2007-01-01', date '2019-03-01', interval 1 day) t(d);   -- through the last payment month on the tape (Feb 2019)

create or replace table mart.dim_grade as
select distinct
    sub_grade,
    grade,
    ascii(grade) - 64                                     as grade_sort,
    (ascii(grade) - 65) * 5 + right(sub_grade, 1)::int    as sub_grade_sort,
    case grade when 'A' then 'Prime' when 'B' then 'Prime'
               when 'C' then 'Near-prime' when 'D' then 'Near-prime'
               else 'Sub-prime' end                       as risk_tier,
    case grade when 'A' then 1 when 'B' then 1 when 'C' then 2 when 'D' then 2 else 3 end
                                                          as risk_tier_sort
from stg.loans;

create or replace table mart.dim_state as
select s.*, t.tile_col, t.tile_row, 'United States' as country
from (values
    ('AL','Alabama','South','East South Central'), ('AK','Alaska','West','Pacific'),
    ('AZ','Arizona','West','Mountain'), ('AR','Arkansas','South','West South Central'),
    ('CA','California','West','Pacific'), ('CO','Colorado','West','Mountain'),
    ('CT','Connecticut','Northeast','New England'), ('DE','Delaware','South','South Atlantic'),
    ('DC','District of Columbia','South','South Atlantic'), ('FL','Florida','South','South Atlantic'),
    ('GA','Georgia','South','South Atlantic'), ('HI','Hawaii','West','Pacific'),
    ('ID','Idaho','West','Mountain'), ('IL','Illinois','Midwest','East North Central'),
    ('IN','Indiana','Midwest','East North Central'), ('IA','Iowa','Midwest','West North Central'),
    ('KS','Kansas','Midwest','West North Central'), ('KY','Kentucky','South','East South Central'),
    ('LA','Louisiana','South','West South Central'), ('ME','Maine','Northeast','New England'),
    ('MD','Maryland','South','South Atlantic'), ('MA','Massachusetts','Northeast','New England'),
    ('MI','Michigan','Midwest','East North Central'), ('MN','Minnesota','Midwest','West North Central'),
    ('MS','Mississippi','South','East South Central'), ('MO','Missouri','Midwest','West North Central'),
    ('MT','Montana','West','Mountain'), ('NE','Nebraska','Midwest','West North Central'),
    ('NV','Nevada','West','Mountain'), ('NH','New Hampshire','Northeast','New England'),
    ('NJ','New Jersey','Northeast','Middle Atlantic'), ('NM','New Mexico','West','Mountain'),
    ('NY','New York','Northeast','Middle Atlantic'), ('NC','North Carolina','South','South Atlantic'),
    ('ND','North Dakota','Midwest','West North Central'), ('OH','Ohio','Midwest','East North Central'),
    ('OK','Oklahoma','South','West South Central'), ('OR','Oregon','West','Pacific'),
    ('PA','Pennsylvania','Northeast','Middle Atlantic'), ('RI','Rhode Island','Northeast','New England'),
    ('SC','South Carolina','South','South Atlantic'), ('SD','South Dakota','Midwest','West North Central'),
    ('TN','Tennessee','South','East South Central'), ('TX','Texas','South','West South Central'),
    ('UT','Utah','West','Mountain'), ('VT','Vermont','Northeast','New England'),
    ('VA','Virginia','South','South Atlantic'), ('WA','Washington','West','Pacific'),
    ('WV','West Virginia','South','South Atlantic'), ('WI','Wisconsin','Midwest','East North Central'),
    ('WY','Wyoming','West','Mountain')
) s(state, state_name, region, division)
-- Tile-grid map coordinates (one square per state), drawn as a scatter in Power BI.
join (values
    ('AK',0,0), ('ME',11,0), ('VT',10,1), ('NH',11,1),
    ('WA',1,2), ('ID',2,2), ('MT',3,2), ('ND',4,2), ('MN',5,2), ('IL',6,2), ('WI',7,2), ('MI',8,2), ('NY',9,2), ('RI',10,2), ('MA',11,2),
    ('OR',1,3), ('NV',2,3), ('WY',3,3), ('SD',4,3), ('IA',5,3), ('IN',6,3), ('OH',7,3), ('PA',8,3), ('NJ',9,3), ('CT',10,3),
    ('CA',1,4), ('UT',2,4), ('CO',3,4), ('NE',4,4), ('MO',5,4), ('KY',6,4), ('WV',7,4), ('VA',8,4), ('MD',9,4), ('DE',10,4),
    ('AZ',2,5), ('NM',3,5), ('KS',4,5), ('AR',5,5), ('TN',6,5), ('NC',7,5), ('SC',8,5), ('DC',9,5),
    ('OK',4,6), ('LA',5,6), ('MS',6,6), ('AL',7,6), ('GA',8,6),
    ('HI',0,7), ('TX',4,7), ('FL',9,7)
) t(state, tile_col, tile_row) using (state);

-- Cash bridge steps for the "where the money went" waterfall (values come from a DAX SWITCH).
create or replace table mart.dim_cash_bridge as
select * from (values
    (1, 'Funded'), (2, 'Principal'), (3, 'Interest'), (4, 'Late fees'), (5, 'Recoveries')
) b(step_sort, step);

create or replace table mart.dim_purpose as
select
    purpose,
    upper(purpose[1]) || replace(purpose[2:], '_', ' ')    as purpose_label,
    case when purpose in ('debt_consolidation', 'credit_card') then 'Refinancing'
         when purpose in ('home_improvement', 'house', 'moving', 'renewable_energy') then 'Home'
         when purpose in ('small_business', 'educational') then 'Business & education'
         when purpose in ('car', 'major_purchase', 'vacation', 'wedding', 'medical') then 'Personal spending'
         else 'Other' end                                  as purpose_group
from (select distinct purpose from stg.loans);

-- ---------------------------------------------------------------- fact
create or replace table mart.fact_loans as
with base as (
    select
        l.*,
        case when status in ('Charged Off', 'Default') then 'Defaulted'
             when status = 'Fully Paid'                 then 'Fully Paid'
             when status = 'Current'                    then 'Current'
             else 'Delinquent' end                                         as outcome,
        p.pd,
        p.is_test
    from stg.loans l
    left join stg.pd_scores p using (loan_id)
),
lgd_by_grade as (   -- empirical LGD per grade, used to price the live book
    select grade,
           sum(funded_amount - total_rec_prncp - (recoveries - collection_recovery_fee))
             / sum(funded_amount - total_rec_prncp)                        as lgd
    from base where outcome = 'Defaulted' group by 1
)
select
    b.loan_id,
    b.issue_date,
    year(b.issue_date)                                                     as vintage_year,
    b.sub_grade,
    b.state,
    b.purpose,
    b.term,
    b.home_ownership,
    b.verification_status,
    b.application_type,
    b.outcome,
    case b.outcome when 'Fully Paid' then 1 when 'Current' then 2
                   when 'Delinquent' then 3 else 4 end                     as outcome_sort,
    b.status,
    b.outcome in ('Fully Paid', 'Defaulted')                               as is_resolved,
    -- Matured = the full term has elapsed before the tape ends (Sep 2018). Rates on
    -- matured loans are final; rates on all resolved loans over-count defaults,
    -- because young loans that default resolve early while good payers are still current.
    b.issue_date + to_months(b.term_months) <= date '2018-09-01'          as is_matured,
    (b.outcome = 'Defaulted')::int                                         as is_default,
    b.outside_policy,
    b.had_hardship,
    b.had_settlement,

    -- borrower bands (label + sort key for Power BI)
    case when b.annual_income < 40000 then '< $40k'
         when b.annual_income < 60000 then '$40–60k'
         when b.annual_income < 80000 then '$60–80k'
         when b.annual_income < 100000 then '$80–100k'
         when b.annual_income < 150000 then '$100–150k'
         when b.annual_income >= 150000 then '$150k+'
         else 'Unknown' end                                                as income_band,
    case when b.annual_income < 40000 then 1 when b.annual_income < 60000 then 2
         when b.annual_income < 80000 then 3 when b.annual_income < 100000 then 4
         when b.annual_income < 150000 then 5 when b.annual_income >= 150000 then 6
         else 7 end                                                        as income_band_sort,
    case when b.dti < 10 then '0–10%' when b.dti < 20 then '10–20%'
         when b.dti < 30 then '20–30%' when b.dti >= 30 then '30%+'
         else 'Unknown' end                                                as dti_band,
    case when b.dti < 10 then 1 when b.dti < 20 then 2 when b.dti < 30 then 3
         when b.dti >= 30 then 4 else 5 end                                as dti_band_sort,
    coalesce(case when b.emp_years = 10 then '10+ yrs'
                  when b.emp_years = 0 then '< 1 yr'
                  else b.emp_years || ' yrs' end, 'Unknown')               as emp_length,
    coalesce(b.emp_years, 11)                                              as emp_length_sort,
    case when b.revol_util < 0.3 then '< 30%' when b.revol_util < 0.6 then '30–60%'
         when b.revol_util < 0.9 then '60–90%' when b.revol_util >= 0.9 then '90%+'
         else 'Unknown' end                                                as revol_util_band,
    case when b.revol_util < 0.3 then 1 when b.revol_util < 0.6 then 2
         when b.revol_util < 0.9 then 3 when b.revol_util >= 0.9 then 4
         else 5 end                                                        as revol_util_band_sort,

    -- origination terms
    b.loan_amount,
    b.funded_amount,
    b.int_rate,
    b.installment,
    b.annual_income,
    b.dti,
    b.revol_util,
    b.credit_history_years,
    b.emp_years,

    -- realised cash flows
    b.total_pymnt                                                          as total_paid,
    b.total_rec_prncp                                                      as principal_paid,
    b.total_rec_int                                                        as interest_paid,
    b.total_rec_late_fee                                                   as late_fees,
    b.recoveries - b.collection_recovery_fee                               as net_recoveries,
    case when b.outcome = 'Defaulted'
         then b.funded_amount - b.total_rec_prncp else 0 end               as exposure_at_default,
    case when b.outcome = 'Defaulted'
         then b.funded_amount - b.total_rec_prncp
              - (b.recoveries - b.collection_recovery_fee) else 0 end      as net_loss,
    case when b.outcome in ('Fully Paid', 'Defaulted')
         then b.total_pymnt - b.collection_recovery_fee - b.funded_amount end as net_profit,
    b.out_prncp                                                            as outstanding_principal,
    -- LendingClub charges a loan off ~5 months after the last payment.
    case when b.outcome = 'Defaulted'
         then date_diff('month', b.issue_date, coalesce(b.last_payment_date, b.issue_date)) + 5
    end                                                                    as months_to_default,
    b.last_payment_date,                                                   -- role-playing date (inactive relationship in Power BI)

    -- model
    b.pd,
    case when b.pd < 0.05 then '1 · < 5%' when b.pd < 0.10 then '2 · 5–10%'
         when b.pd < 0.20 then '3 · 10–20%' when b.pd < 0.30 then '4 · 20–30%'
         else '5 · 30%+' end                                               as pd_band,
    b.is_test                                                              as in_model_test_set,
    case when b.outcome in ('Current', 'Delinquent')
         then b.out_prncp * b.pd * g.lgd else 0 end                        as expected_loss
from base b
join lgd_by_grade g using (grade);
