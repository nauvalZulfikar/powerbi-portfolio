-- Marts: a star around one row per ticket, plus time-in-stage and a daily backlog snapshot.
-- Seeds map each team's own workflow, priorities and issue types onto one shared vocabulary.
create table stg.teams as select * from read_csv('{seeds}/teams.csv');
create table stg.status_map as select * from read_csv('{seeds}/status_map.csv');
create table stg.priority_map as select * from read_csv('{seeds}/priority_map.csv');
create table stg.type_map as select * from read_csv('{seeds}/type_map.csv');
create table stg.resolution_map as select * from read_csv('{seeds}/resolution_map.csv');

-- ------------------------------------------------------------------ dimensions
create table marts.dim_date as
select d::date "date", year(d) "year", year(d) * 10 + quarter(d) year_quarter, 'Q' || quarter(d) || ' ' || year(d) "quarter",
       year(d) * 100 + month(d) year_month, strftime(d, '%b %Y') "month", month(d) month_of_year, strftime(d, '%b') month_name,
       isoyear(d) iso_year, isoyear(d) * 100 + week(d) year_week, 'W' || lpad(week(d)::varchar, 2, '0') || ' ' || isoyear(d) "week",
       week(d) week_of_year, (d - (isodow(d) - 1) * interval 1 day)::date week_start, isodow(d) >= 6 is_weekend
from range(date '2024-10-01', date '2026-10-01', interval 1 day) t(d);

create table marts.dim_team as select team_key, team, product from stg.teams;

create table marts.dim_priority as
select distinct priority_key, priority, response_hours, resolution_days from stg.priority_map;

create table marts.dim_type as select distinct type_key, "type" from stg.type_map where counted;

create table marts.dim_stage as
select * from (values (1, 'Backlog', true), (2, 'In Progress', true), (3, 'In Review', true), (4, 'Done', false))
    t(stage_key, stage, is_open);

-- ------------------------------------------------------------------ tickets
create table stg.ticket as
select a.*, tm.type_key, pm.priority_key, pm.response_hours, pm.resolution_days, sm.stage,
       coalesce(rm.outcome, 'Open') outcome
from stg.issue_as_of a
join stg.type_map tm on tm.type_raw = a.type_raw
join stg.priority_map pm on pm.priority_raw = a.priority_raw
join stg.status_map sm on sm.team_key = a.team_key and sm.status = a.status
left join stg.resolution_map rm on rm.resolution = a.resolution
where tm.counted;

-- People are the assignees; names are replaced by a stable alias ranked by workload (volunteers, public data).
create table marts.dim_person as
with p as (
    select assignee login, count(*) tickets, arg_max(team_key, n) home_team
    from (select assignee, team_key, count(*) over (partition by assignee, team_key) n from stg.ticket where assignee is not null)
    group by all)
select row_number() over (order by tickets desc, md5(login)) person_key,
       'Engineer ' || lpad((row_number() over (order by tickets desc, md5(login)))::varchar, 3, '0') person,
       tm.team home_team, md5(login) login_hash
from p join stg.teams tm on tm.team_key = p.home_team
union all
select 0, 'Unassigned', null, null;

create table marts.fact_ticket as
with resp as (  -- first action by anyone other than the reporter (changes with no author are Jira itself)
    select c.key, min(c.changed_at) first_response_at
    from stg.change c join stg.ticket t using (key)
    where c.changed_at <= as_of() and c.author is not null and c.author <> coalesce(t.reporter, '')
    group by all),
res as (
    select key, max(changed_at) filter (where to_value is not null) resolved_at,
           count(*) filter (where to_value is null) reopened
    from stg.change where field = 'resolution' and changed_at <= as_of() group by all),
work as (
    select i.key, min(i.start_at) started_at
    from stg.status_interval i join stg.ticket t using (key) join stg.status_map m on m.team_key = t.team_key and m.status = i.status
    where m.stage in ('In Progress', 'In Review') group by all),
t as (
    select t.key ticket_key, t.team_key, t.type_key, t.priority_key, coalesce(p.person_key, 0) person_key,
           s.stage_key, s.stage, t.status, t.resolution, t.outcome, t.summary,
           t.created created_at, r.first_response_at,
           -- a resolution set without a change event (created resolved) counts as resolved on creation
           if(t.outcome = 'Open', null, coalesce(res.resolved_at, t.created)) resolved_at,
           w.started_at, coalesce(res.reopened, 0) reopened, t.response_hours, t.resolution_days
    from stg.ticket t
    join marts.dim_stage s on s.stage = t.stage
    left join marts.dim_person p on p.login_hash = md5(t.assignee)
    left join resp r on r.key = t.key
    left join res on res.key = t.key
    left join work w on w.key = t.key)
select ticket_key, team_key, type_key, priority_key, person_key, stage_key, stage, status, resolution, outcome, summary,
       created_at, created_at::date created_date, resolved_at, resolved_at::date resolved_date,
       first_response_at, started_at,
       round(epoch(first_response_at - created_at) / 3600, 2) response_hours,
       round(epoch(coalesce(first_response_at, as_of()) - created_at) / 3600, 2) wait_hours,  -- hours to response, or still waiting
       round(epoch(resolved_at - created_at) / 86400, 3) lead_days,
       round(epoch(resolved_at - greatest(started_at, created_at)) / 86400, 3) cycle_days,
       if(resolved_at is null, round(epoch(as_of() - created_at) / 86400, 3), null) age_days,
       reopened,
       case when first_response_at is not null
                then if(epoch(first_response_at - created_at) / 3600 <= t.response_hours, 'Met', 'Breached')
            when epoch(as_of() - created_at) / 3600 > t.response_hours then 'Breached'
            else 'Pending' end response_sla,
       case when resolved_at is not null
                then if(epoch(resolved_at - created_at) / 86400 <= t.resolution_days, 'Met', 'Breached')
            when epoch(as_of() - created_at) / 86400 > t.resolution_days then 'Open, breached'
            else 'Open, within' end resolution_sla
from t;

-- Days each ticket spent in each stage (up to resolution, or to the as-of date if still open).
create table marts.fact_stage_time as
select i.key ticket_key, s.stage_key, round(sum(epoch(i.end_at - i.start_at)) / 86400, 3) "days"
from stg.status_interval i
join marts.fact_ticket f on f.ticket_key = i.key
join stg.status_map m on m.team_key = f.team_key and m.status = i.status
join marts.dim_stage s on s.stage = m.stage
where s.is_open
group by all;

-- Stage-to-stage moves (Backlog → In Progress → In Review → Done, and back): consecutive intervals in the same
-- shared stage are merged first, so a status change inside one stage is not a transition.
create table marts.fact_stage_transition as
with s as (
    select i.key, i.start_at, i.end_at, ds.stage_key
    from stg.status_interval i
    join marts.fact_ticket f on f.ticket_key = i.key
    join stg.status_map m on m.team_key = f.team_key and m.status = i.status
    join marts.dim_stage ds on ds.stage = m.stage),
runs as (
    select *, lag(stage_key) over (partition by key order by start_at, end_at) prev_stage from s),
starts as (select * from runs where prev_stage is distinct from stage_key)
select key ticket_key, stage_key from_stage_key, to_stage_key, transition_date
from (select key, stage_key, lead(stage_key) over w to_stage_key, (lead(start_at) over w)::date transition_date
      from starts window w as (partition by key order by start_at, end_at))
where to_stage_key is not null;

-- First delivery per person (all teams): the contributor cohort.
alter table marts.dim_person add column first_delivered date;
alter table marts.dim_person add column first_quarter varchar;
alter table marts.dim_person add column first_quarter_key bigint;
update marts.dim_person p set first_delivered = x.d, first_quarter = dd.quarter, first_quarter_key = dd.year_quarter
from (select person_key, min(resolved_date) d from marts.fact_ticket
      where outcome = 'Delivered' and person_key <> 0 group by all) x
join marts.dim_date dd on dd."date" = x.d
where p.person_key = x.person_key;

-- Open tickets at the end of every day, by team, priority, stage and assignee bucket.
create table marts.fact_backlog_daily as
select d."date", f.team_key, f.priority_key, s.stage_key, count(*) open_tickets
from marts.dim_date d
join stg.status_interval i on i.start_at < d."date" + interval 1 day
                          and (i.end_at >= d."date" + interval 1 day or (i.is_current and d."date" + interval 1 day > as_of()))
join marts.fact_ticket f on f.ticket_key = i.key
join stg.status_map m on m.team_key = f.team_key and m.status = i.status
join marts.dim_stage s on s.stage = m.stage
where s.is_open
group by all;

-- ------------------------------------------------------------------ lineage & access
create table marts.status_map as
select m.team_key, m.status, m.stage, count(a.key) tickets
from stg.status_map m left join stg.issue_as_of a on a.team_key = m.team_key and a.status = m.status
group by all;

create table marts.data_checks as
select team_key, 1 step, 'Pulled from the Jira API' "check", count(*) tickets from stg.issue group by all
union all
select a.team_key, 2, 'Excluded: ' || lower(tm."type") || 's (containers, not work)', count(*)
from stg.issue_as_of a join stg.type_map tm on tm.type_raw = a.type_raw where not tm.counted group by all
union all
select team_key, 3, 'Counted as tickets', count(*) from marts.fact_ticket group by all
union all
select team_key, 4, 'No first response by the as-of date', count(*) from marts.fact_ticket where first_response_at is null group by all
union all
select team_key, 5, 'Never marked in progress (cycle time unknown)', count(*) from marts.fact_ticket
where started_at is null and resolved_at is not null group by all
union all
select team_key, 6, 'Reopened at least once', count(*) from marts.fact_ticket where reopened > 0 group by all;

create table marts.team_access as select * from read_csv('{seeds}/team_access.csv');

-- Components: Jira's component list per ticket, as a dimension plus a bridge (a ticket can sit in several).
-- These are the CURRENT values from the API, not replayed as of 30 Sep 2026 (component edits are rare);
-- the README says so. Names are per project, so the key is project + name.
create table marts.dim_component as
select row_number() over (order by team_key, component) component_key, team_key, component
from (select distinct team_key, unnest(components) component from stg.issue
      where key in (select ticket_key from marts.fact_ticket));

create table marts.bridge_ticket_component as
select distinct i.key ticket_key, c.component_key
from (select key, team_key, unnest(components) component from stg.issue) i
join marts.dim_component c using (team_key, component)
where i.key in (select ticket_key from marts.fact_ticket);
