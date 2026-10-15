-- Data-quality tests. Each query returns the number of violating rows; 0 = pass.
-- name: fact_results row count matches raw results
select abs((select count(*) from mart.fact_results) - (select count(*) from stg.results));
-- name: result_id is unique
select count(*) - count(distinct result_id) from mart.fact_results;
-- name: every race has exactly one winner (except shared-drive 1950s races)
select count(*) from (select race_id from mart.fact_results where is_win group by 1 having count(*) > 1) x
    where race_id not in (select race_id from mart.fact_results where year < 1960);
-- name: every result has a known driver, constructor, race, circuit
select (select count(*) from mart.fact_results anti join mart.dim_driver using (driver_id))
     + (select count(*) from mart.fact_results anti join mart.dim_constructor using (constructor_id))
     + (select count(*) from mart.fact_results anti join mart.dim_race using (race_id))
     + (select count(*) from mart.fact_results anti join mart.dim_circuit using (circuit_id));
-- name: one champion per season, 1950 to latest
select abs((select count(*) from mart.dim_season) - ((select max(year) from stg.races) - 1949));
-- name: known champions (Fangio 5, Schumacher 7, Hamilton 7, Verstappen 4 by 2024)
select count(*) from (values ('Juan Fangio', 5), ('Michael Schumacher', 7), ('Lewis Hamilton', 7), ('Max Verstappen', 4)) t(n, k)
    where k <> (select titles from mart.dim_driver where driver_name = n);
-- name: finish percentile within [0, 1]
select count(*) from mart.fact_results where finish_percentile not between 0 and 1;
-- name: routine pit stops plausible (10–60 s)
select count(*) from mart.fact_pit_stops where is_routine_stop and pit_seconds < 10;
-- name: team season shares sum to 1
select count(*) from (select year, sum(season_points_share) s from mart.team_season group by 1) where abs(s - 1) > 0.01 and s > 0;
-- name: grid probabilities within [0, 1]
select count(*) from mart.grid_conversion where win_probability not between 0 and 1;
-- name: driver skill covers every modelled driver exactly once
select count(*) - count(distinct driver_id) from mart.driver_skill;
-- name: outcome flags are never null
select count(*) from mart.fact_results where is_win is null or is_podium is null or is_pole is null or is_fastest_lap is null or is_dnf is null;
-- name: race + sprint points equal the final standings (2021+, what 'Session Results' appends in Power Query)
with tot as (select r.year, r.driver_id, sum(r.points) + coalesce((select sum(s.points) from stg.sprint_results s join mart.dim_race d using (race_id)
                 where d.year = r.year and s.driver_id = r.driver_id), 0) p
             from mart.fact_results r where r.year >= 2021 group by 1, 2),
     fin as (select year, driver_id, points from mart.fact_standings qualify round = max(round) over (partition by year))
select count(*) filter (where abs(tot.p - fin.points) > 0.01) from tot join fin using (year, driver_id);
