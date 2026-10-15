-- =====================================================================
-- ANALYTICS: pre-aggregated marts for questions that are awkward in DAX.
-- =====================================================================

-- Does the grid decide the race? Win / podium probability by starting slot, per era.
create or replace table mart.grid_conversion as
select
    era(year)                                           as era,
    grid,
    count(*)                                            as starts,
    count(*) filter (where is_win)                      as wins,
    count(*) filter (where is_podium)                   as podiums,
    round(avg(is_win::int), 4)                          as win_probability,
    round(avg(is_podium::int), 4)                       as podium_probability,
    round(avg(positions_gained), 2)                     as avg_positions_gained
from mart.fact_results
where grid between 1 and 20
group by 1, 2;

-- Constructor share of each season's points (modern scale) -> dynasty ribbon chart.
create or replace table mart.team_season as
with t as (
    select year, constructor_id, sum(points_modern)::bigint as points_modern,
           count(*) filter (where is_win) as wins, count(*) filter (where is_pole) as poles
    from mart.fact_results group by 1, 2
)
select *,
    round(points_modern / sum(points_modern) over (partition by year), 4) as season_points_share,
    rank() over (partition by year order by points_modern desc)          as season_rank
from t;

-- Teammate head-to-head: the only fair driver comparison (same car).
-- Qualifying from the grid, race from classified order, both-car races only.
create or replace table mart.teammate_h2h as
with pairs as (
    select a.year, a.driver_id, b.driver_id as teammate_id, a.constructor_id,
        count(*)                                                          as races_together,
        count(*) filter (where a.grid < b.grid)                            as quali_wins,
        count(*) filter (where a.position_order < b.position_order
                         and not a.is_mechanical_dnf and not b.is_mechanical_dnf) as race_wins,
        count(*) filter (where not a.is_mechanical_dnf and not b.is_mechanical_dnf) as comparable_races,
        (sum(a.points_modern) - sum(b.points_modern))::bigint                       as points_delta
    from mart.fact_results a
    join mart.fact_results b
      on a.race_id = b.race_id and a.constructor_id = b.constructor_id and a.driver_id <> b.driver_id
    group by 1, 2, 3, 4
)
select *,
    round(quali_wins / nullif(races_together, 0), 4)   as quali_h2h_rate,
    round(race_wins / nullif(comparable_races, 0), 4)  as race_h2h_rate
from pairs
where races_together >= 5;
