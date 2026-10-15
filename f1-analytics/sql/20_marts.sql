-- =====================================================================
-- MARTS: star schema consumed by Power BI.
--   dims : dim_season, dim_race, dim_driver, dim_constructor, dim_circuit
--   facts: fact_results, fact_qualifying, fact_pit_stops, fact_lap_times, fact_standings
-- =====================================================================
create schema if not exists mart;

create or replace macro era(y) as case
    when y <= 1960 then '1 · Front-engine (1950–60)'
    when y <= 1976 then '2 · Rear-engine & wings (1961–76)'
    when y <= 1988 then '3 · Ground effect & turbo (1977–88)'
    when y <= 2005 then '4 · V10 era (1989–2005)'
    when y <= 2013 then '5 · V8 era (2006–13)'
    when y <= 2021 then '6 · Hybrid era (2014–21)'
    else                '7 · Ground effect returns (2022– )' end;

-- Today's 25-18-15-12-10-8-6-4-2-1 scale, so points compare across 75 years of rule changes.
create or replace macro modern_points(pos) as
    coalesce(list_extract([25, 18, 15, 12, 10, 8, 6, 4, 2, 1], pos), 0);

-- ---------------------------------------------------------- fact_results
create or replace table mart.fact_results as
with r as (
    select r.*, ra.year, ra.round, ra.circuit_id, s.status, s.status_group,
        count(*) over (partition by r.race_id) as field_size
    from stg.results r
    join stg.races ra using (race_id)
    join stg.status s using (status_id)
)
select
    result_id, race_id, driver_id, constructor_id, circuit_id, year, round,
    grid,
    case when grid is null then 'Pit lane / none' when grid = 1 then 'P1 (pole)' when grid <= 3 then 'P2–3'
         when grid <= 10 then 'P4–10' else 'P11+' end                              as grid_band,
    case when grid is null then 5 when grid = 1 then 1 when grid <= 3 then 2 when grid <= 10 then 3 else 4 end as grid_band_order,
    finish_position,
    position_order,
    field_size,
    points,
    modern_points(finish_position)                                               as points_modern,
    laps,
    status,
    status_group,
    status_group in ('Finished', 'Finished (lapped)')                             as is_finished,
    status_group in ('Mechanical failure', 'Crash / driver', 'Retired (other)')   as is_dnf,
    status_group = 'Mechanical failure'                                           as is_mechanical_dnf,
    coalesce(finish_position = 1, false)                                          as is_win,
    coalesce(finish_position <= 3, false)                                         as is_podium,
    coalesce(grid = 1, false)                                                     as is_pole,
    coalesce(points > 0, false)                                                   as is_points_finish,
    coalesce(fastest_lap_rank = 1, false)                                         as is_fastest_lap,
    case when grid is not null and finish_position is not null then grid - finish_position end as positions_gained,
    -- 1 = won, 0 = classified last among starters: comparable across field sizes
    round(1 - (position_order - 1) / greatest(field_size - 1, 1), 4)               as finish_percentile,
    fastest_lap_ms,
    fastest_lap_kph
from r;

-- ------------------------------------------------------------ dim_season
create or replace table mart.dim_season as
with last_round as (
    select year, max(race_id) filter (where round = max_round) as race_id, max(max_round) as races
    from (select year, round, race_id, max(round) over (partition by year) as max_round from stg.races) group by 1
),
d as (
    select lr.year, ds.driver_id, ds.points, ds.position, ds.wins
    from last_round lr join stg.driver_standings ds using (race_id)
),
c as (
    select lr.year, cs.constructor_id, cs.points, cs.position
    from last_round lr join stg.constructor_standings cs using (race_id)
),
wins as (
    select year, count(distinct driver_id) filter (where is_win) as different_winners,
           count(distinct constructor_id) filter (where is_win) as winning_teams,
           count(distinct driver_id) as drivers_entered
    from mart.fact_results group by 1
),
team_share as (
    select year, max(pts) / sum(pts) as top_team_points_share
    from (select year, constructor_id, sum(points_modern) as pts from mart.fact_results group by 1, 2) group by 1
)
select
    lr.year,
    (lr.year // 10 * 10)::varchar || 's'                               as decade,
    era(lr.year)                                                       as era,
    lr.races,
    (select count(*) from stg.races r where r.year = lr.year and r.has_sprint) as sprint_weekends,
    w.drivers_entered,
    w.different_winners,
    w.winning_teams,
    ch.driver_id                                                       as champion_driver_id,
    drv.driver_name                                                    as champion,
    ch.points                                                          as champion_points,
    ch.wins                                                            as champion_wins,
    round(ch.wins / lr.races, 3)                                       as champion_win_rate,
    ch.points - ru.points                                              as title_margin,
    round((ch.points - ru.points) / nullif(ch.points, 0), 3)           as title_margin_pct,
    ru_d.driver_name                                                   as runner_up,
    cc.constructor_name                                                as constructor_champion,   -- from 1958
    round(ts.top_team_points_share, 3)                                 as top_team_points_share
from last_round lr
join d ch on ch.year = lr.year and ch.position = 1
join d ru on ru.year = lr.year and ru.position = 2
join stg.drivers drv on drv.driver_id = ch.driver_id
join stg.drivers ru_d on ru_d.driver_id = ru.driver_id
join wins w on w.year = lr.year
join team_share ts on ts.year = lr.year
left join (select c.year, k.constructor_name from c join stg.constructors k using (constructor_id)
           where c.position = 1 and c.year >= 1958) cc on cc.year = lr.year;

-- -------------------------------------------------------------- dim_race
create or replace table mart.dim_race as
select
    r.race_id,
    r.year,
    r.round,
    r.race_name,
    r.race_short,
    r.year::varchar || ' · R' || lpad(r.round::varchar, 2, '0') || ' · ' || r.race_short as race_label,
    r.year * 100 + r.round                                              as race_label_order,
    r.race_date,
    r.circuit_id,
    era(r.year)                                                        as era,
    r.has_sprint,
    r.round = max(r.round) over (partition by r.year)                  as is_season_finale,
    r.race_name = 'Indianapolis 500'                                   as is_indy500,
    win.driver_name                                                    as winner,
    wk.constructor_name                                                as winning_team,
    pole.driver_name                                                   as pole_sitter,
    exists (select 1 from stg.lap_times lt where lt.race_id = r.race_id) as has_lap_data
from stg.races r
left join mart.fact_results fw on fw.race_id = r.race_id and fw.is_win
left join stg.drivers win on win.driver_id = fw.driver_id
left join stg.constructors wk on wk.constructor_id = fw.constructor_id
left join mart.fact_results fp on fp.race_id = r.race_id and fp.is_pole
left join stg.drivers pole on pole.driver_id = fp.driver_id
where r.race_id in (select race_id from mart.fact_results)             -- drop scheduled-but-unraced rounds
qualify row_number() over (partition by r.race_id order by fw.result_id, fp.result_id) = 1;

-- ------------------------------------------------------------ dim_driver
create or replace table mart.dim_driver as
with career as (
    select driver_id,
        min(year) as first_season, max(year) as last_season,
        count(*) filter (where grid is not null or is_finished)     as starts,
        count(*) filter (where is_win)                              as wins,
        count(*) filter (where is_podium)                           as podiums,
        count(*) filter (where is_pole)                             as poles,
        count(*) filter (where is_fastest_lap)                      as fastest_laps,
        sum(points)                                                 as career_points,
        sum(points_modern)::bigint                                  as career_points_modern,
        count(*) filter (where is_dnf)                              as dnfs,
        count(distinct constructor_id)                              as teams_driven_for,
        arg_max(constructor_id, race_id)                            as last_constructor_id
    from mart.fact_results group by 1
),
titles as (select champion_driver_id as driver_id, count(*) as titles, string_agg(year::varchar, ', ' order by year) as title_years
           from mart.dim_season group by 1)
select
    d.driver_id,
    d.driver_name,
    coalesce(d.driver_code, upper(left(d.surname, 3)))                as driver_code,
    d.surname,
    d.dob,
    d.nationality,
    n.country,
    n.world_region,
    c.first_season,
    c.last_season,
    c.last_season - c.first_season + 1                                as career_span,
    date_diff('year', d.dob, make_date(c.first_season, 7, 1))         as debut_age,
    era(c.first_season)                                               as debut_era,
    c.starts, c.wins, c.podiums, c.poles, c.fastest_laps,
    c.career_points, c.career_points_modern, c.dnfs, c.teams_driven_for,
    coalesce(t.titles, 0)                                             as titles,
    t.title_years,
    k.constructor_name                                                as last_team,
    d.wikipedia_url,
    round(c.wins / nullif(c.starts, 0), 4)                            as win_rate,
    round(c.podiums / nullif(c.starts, 0), 4)                         as podium_rate,
    case when coalesce(t.titles, 0) > 0 then 'World Champion'
         when c.wins > 0              then 'Race Winner'
         when c.podiums > 0           then 'Podium Finisher'
         when c.career_points > 0     then 'Points Scorer'
         else 'No Points' end                                         as achievement_tier,
    case when coalesce(t.titles, 0) > 0 then 1 when c.wins > 0 then 2 when c.podiums > 0 then 3
         when c.career_points > 0 then 4 else 5 end                   as achievement_tier_order,
    c.last_season = (select max(year) from stg.races)                 as is_active
from stg.drivers d
join career c using (driver_id)
left join titles t using (driver_id)
left join stg.nationalities n using (nationality)
left join stg.constructors k on k.constructor_id = c.last_constructor_id;

-- ------------------------------------------------------- dim_constructor
create or replace table mart.dim_constructor as
with perf as (
    select constructor_id,
        min(year) as first_season, max(year) as last_season,
        count(distinct race_id)                           as races_entered,
        count(*) filter (where is_win)                    as wins,
        count(*) filter (where is_podium)                 as podiums,
        count(*) filter (where is_pole)                   as poles,
        sum(points)                                       as total_points,
        sum(points_modern)::bigint                        as total_points_modern,
        count(distinct driver_id)                         as drivers_used,
        avg(is_mechanical_dnf::int)                       as mechanical_dnf_rate
    from mart.fact_results group by 1
),
titles as (select constructor_champion as constructor_name, count(*) as constructor_titles
           from mart.dim_season where constructor_champion is not null group by 1),
driver_titles as (  -- credited to the team the champion scored most points with that season
    select constructor_id, count(*) as driver_titles
    from (select year, arg_max(constructor_id, pts) as constructor_id
          from (select s.year, fr.constructor_id, sum(fr.points) as pts
                from mart.dim_season s join mart.fact_results fr on fr.year = s.year and fr.driver_id = s.champion_driver_id
                group by 1, 2)
          group by 1)
    group by 1)
select
    c.constructor_id,
    c.constructor_name,
    c.nationality,
    n.country,
    p.first_season, p.last_season, p.races_entered, p.wins, p.podiums, p.poles,
    p.total_points, p.total_points_modern, p.drivers_used,
    round(p.mechanical_dnf_rate, 4)                                   as mechanical_dnf_rate,
    coalesce(t.constructor_titles, 0)                                 as constructor_titles,
    coalesce(dt.driver_titles, 0)                                     as driver_titles,
    round(p.wins / nullif(p.races_entered, 0), 4)                     as win_rate,
    p.last_season = (select max(year) from stg.races)                 as is_active
from stg.constructors c
join perf p using (constructor_id)
left join titles t using (constructor_name)
left join driver_titles dt using (constructor_id)
left join stg.nationalities n using (nationality);

-- ----------------------------------------------------------- dim_circuit
create or replace table mart.dim_circuit as
with hosted as (
    select circuit_id, count(*) as races_hosted, min(year) as first_race, max(year) as last_race
    from mart.dim_race group by 1
),
craft as (
    select circuit_id,
        avg(is_win::int) filter (where is_pole)                              as pole_win_rate,
        avg(abs(positions_gained)) filter (where year >= 1996)               as avg_position_change,
        min(fastest_lap_ms)                                                  as lap_record_ms,
        arg_min(driver_id, fastest_lap_ms)                                   as lap_record_driver_id,
        arg_min(year, fastest_lap_ms)                                        as lap_record_year
    from mart.fact_results group by 1
)
select
    c.circuit_id, c.circuit_name, c.location, c.country,
    c.location || ', ' || c.country                                         as circuit_geo,
    c.lat, c.lng, c.alt,
    h.races_hosted, h.first_race, h.last_race,
    h.last_race = (select max(year) from stg.races)                         as on_current_calendar,
    round(k.pole_win_rate, 4)                                               as pole_win_rate,
    round(k.avg_position_change, 2)                                         as avg_position_change,
    k.lap_record_ms,
    printf('%d:%06.3f', k.lap_record_ms // 60000, (k.lap_record_ms % 60000) / 1000.0) as lap_record,
    d.driver_name                                                           as lap_record_holder,
    k.lap_record_year
from stg.circuits c
join hosted h using (circuit_id)
left join craft k using (circuit_id)
left join stg.drivers d on d.driver_id = k.lap_record_driver_id;

-- ------------------------------------------------------ fact_qualifying
create or replace table mart.fact_qualifying as
select
    q.race_id, q.driver_id, q.constructor_id, r.year, q.quali_position, q.best_quali_ms,
    round(q.best_quali_ms / min(q.best_quali_ms) over (partition by q.race_id) - 1, 5) as gap_to_pole_pct
from stg.qualifying q join stg.races r using (race_id);

-- ------------------------------------------------------- fact_pit_stops
-- Pit-lane time (entry to exit). > 60 s = red-flag / repair stop, flagged not dropped.
create or replace table mart.fact_pit_stops as
select
    p.race_id, p.driver_id, fr.constructor_id, fr.year, p.stop_no, p.lap,
    p.pit_ms / 1000.0                                  as pit_seconds,
    p.pit_ms <= 60000                                  as is_routine_stop,
    max(p.stop_no) over (partition by p.race_id, p.driver_id) as stops_in_race
from stg.pit_stops p
join mart.fact_results fr using (race_id, driver_id);

-- ------------------------------------------------------- fact_lap_times
create or replace table mart.fact_lap_times as
select lt.race_id, lt.driver_id, lt.lap, lt.position, lt.lap_ms / 1000.0 as lap_seconds,
       ps.race_id is not null as is_pit_lap,
       r.race_date  -- incremental refresh filters on this (RangeStart / RangeEnd in Power BI)
from stg.lap_times lt
join stg.races r using (race_id)
left join (select distinct race_id, driver_id, lap from stg.pit_stops) ps using (race_id, driver_id, lap);

-- ------------------------------------------------------- fact_standings
-- Championship table after every round: drives the "title fight" race charts.
create or replace table mart.fact_standings as
select ds.race_id, ds.driver_id, r.year, r.round, ds.points, ds.position, ds.wins,
       ds.points - max(ds.points) over (partition by ds.race_id) as gap_to_leader
from stg.driver_standings ds join stg.races r using (race_id);
