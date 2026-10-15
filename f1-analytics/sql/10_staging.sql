-- =====================================================================
-- STAGING: type and clean the 14 raw Kaggle CSVs ('\N' = null).
-- One table per source, no business logic yet.
-- =====================================================================
create schema if not exists stg;

create or replace macro raw(f) as table
    select * from read_csv('{raw}/' || f || '.csv', nullstr = '\N', header = true);

-- "1:27.438" / "27.438" -> milliseconds
create or replace macro lap_ms(t) as
    case when t is null or t = '' then null
         when t like '%:%' then (split_part(t, ':', 1)::int * 60000 + (split_part(t, ':', 2)::double * 1000))::int
         else (t::double * 1000)::int end;

create or replace table stg.circuits as
select circuitId as circuit_id, circuitRef as circuit_ref, name as circuit_name, location,
       case country when 'UK' then 'United Kingdom' when 'USA' then 'United States'
                    when 'UAE' then 'United Arab Emirates' when 'Korea' then 'South Korea' else country end as country,
       lat, lng, alt
from raw('circuits');

create or replace table stg.races as
select raceId as race_id, year, round, circuitId as circuit_id,
       replace(name, ' Grand Prix', ' GP') as race_short, name as race_name, date as race_date,
       sprint_date is not null as has_sprint
from raw('races');

create or replace table stg.drivers as
select driverId as driver_id, driverRef as driver_ref, code as driver_code, number as permanent_number,
       forename || ' ' || surname as driver_name, surname, dob, trim(nationality) as nationality, url as wikipedia_url
from raw('drivers');

create or replace table stg.constructors as
select constructorId as constructor_id, constructorRef as constructor_ref, name as constructor_name, nationality
from raw('constructors');

-- 140 raw statuses -> 6 analysable outcome groups
create or replace table stg.status as
select statusId as status_id, status,
    case
        when status = 'Finished'                                               then 'Finished'
        when regexp_matches(status, '^\+\d+ Laps?$')                           then 'Finished (lapped)'
        when status in ('Disqualified', 'Excluded', 'Underweight')             then 'Disqualified'
        when status in ('Did not qualify', 'Did not prequalify', '107% Rule',
                        'Withdrew', 'Not restarted', 'Safety concerns')        then 'Did not start'
        when regexp_matches(status, 'Accident|Collision|Spun off|Damage|Debris|Fatal|Injur|Physical|Illness|unwell|Eye')
                                                                               then 'Crash / driver'
        when status in ('Retired', 'Not classified', 'Safety', 'Stalled')      then 'Retired (other)'
        else 'Mechanical failure'
    end as status_group
from raw('status');

create or replace table stg.results as
select resultId as result_id, raceId as race_id, driverId as driver_id, constructorId as constructor_id,
       nullif(grid, 0) as grid,               -- 0 = pit-lane start / no grid slot
       position as finish_position,           -- null = not classified
       positionOrder as position_order, points, laps, milliseconds as race_ms,
       rank as fastest_lap_rank, lap_ms(fastestLapTime) as fastest_lap_ms, fastestLapSpeed as fastest_lap_kph,
       statusId as status_id
from raw('results');

create or replace table stg.sprint_results as
select raceId as race_id, driverId as driver_id, constructorId as constructor_id, grid, position as finish_position, points
from raw('sprint_results');

create or replace table stg.qualifying as
select raceId as race_id, driverId as driver_id, constructorId as constructor_id, position as quali_position,
       nullif(least(coalesce(lap_ms(q1), 99999999), coalesce(lap_ms(q2), 99999999), coalesce(lap_ms(q3), 99999999)), 99999999) as best_quali_ms
from raw('qualifying');

create or replace table stg.driver_standings as
select raceId as race_id, driverId as driver_id, points, position, wins from raw('driver_standings');

create or replace table stg.constructor_standings as
select raceId as race_id, constructorId as constructor_id, points, position, wins from raw('constructor_standings');

create or replace table stg.pit_stops as
select raceId as race_id, driverId as driver_id, stop as stop_no, lap, milliseconds as pit_ms from raw('pit_stops');

create or replace table stg.lap_times as
select raceId as race_id, driverId as driver_id, lap, position, milliseconds as lap_ms from raw('lap_times');

-- Reference: demonym -> country (drivers & constructors), plus world region.
create or replace table stg.nationalities as
select * from (values
    ('American','United States','Americas'), ('American-Italian','United States','Americas'),
    ('Argentine','Argentina','Americas'), ('Argentinian','Argentina','Americas'), ('Argentine-Italian','Argentina','Americas'),
    ('Australian','Australia','Asia-Pacific'), ('Austrian','Austria','Europe'), ('Belgian','Belgium','Europe'),
    ('Brazilian','Brazil','Americas'), ('British','United Kingdom','Europe'), ('Canadian','Canada','Americas'),
    ('Chilean','Chile','Americas'), ('Chinese','China','Asia-Pacific'), ('Colombian','Colombia','Americas'),
    ('Czech','Czechia','Europe'), ('Danish','Denmark','Europe'), ('Dutch','Netherlands','Europe'),
    ('East German','Germany','Europe'), ('Finnish','Finland','Europe'), ('French','France','Europe'),
    ('German','Germany','Europe'), ('Hong Kong','Hong Kong','Asia-Pacific'), ('Hungarian','Hungary','Europe'),
    ('Indian','India','Asia-Pacific'), ('Indonesian','Indonesia','Asia-Pacific'), ('Irish','Ireland','Europe'),
    ('Italian','Italy','Europe'), ('Japanese','Japan','Asia-Pacific'), ('Liechtensteiner','Liechtenstein','Europe'),
    ('Malaysian','Malaysia','Asia-Pacific'), ('Mexican','Mexico','Americas'), ('Monegasque','Monaco','Europe'),
    ('New Zealander','New Zealand','Asia-Pacific'), ('Polish','Poland','Europe'), ('Portuguese','Portugal','Europe'),
    ('Rhodesian','Zimbabwe','Africa'), ('Russian','Russia','Europe'), ('South African','South Africa','Africa'),
    ('Spanish','Spain','Europe'), ('Swedish','Sweden','Europe'), ('Swiss','Switzerland','Europe'),
    ('Thai','Thailand','Asia-Pacific'), ('Uruguayan','Uruguay','Americas'), ('Venezuelan','Venezuela','Americas')
) t(nationality, country, world_region);
