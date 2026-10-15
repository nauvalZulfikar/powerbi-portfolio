-- Data tests: (test, passed, detail). The pipeline exports nothing unless every row passes.
with t as (
    select 'replaying the full history reproduces today''s status, priority, type, resolution and assignee' test,
           count(*) = 0 ok, count(*) || ' fields differ' detail
    from (select c.key, c.field, max({s: c.seq, v: if(c.field = 'assignee', c.to_id, c.to_value)}).v replayed
          from stg.change c group by all) r
    join stg.issue i using (key)
    where r.replayed is distinct from case r.field when 'status' then i.status_now when 'priority' then i.priority_now
                                                     when 'issuetype' then i.type_now when 'resolution' then i.resolution_now
                                                     when 'assignee' then i.assignee_now end
      and r.field in ('status', 'priority', 'issuetype', 'resolution', 'assignee')
    union all
    select 'every pulled issue is either a ticket or an excluded container', count(*) = 0, count(*) || ' issues lost'
    from stg.issue i
    where i.key not in (select ticket_key from marts.fact_ticket)
      and i.key not in (select a.key from stg.issue_as_of a join stg.type_map m using (type_raw) where not m.counted)
    union all
    select 'every status, priority, type and resolution on the as-of date has a mapping', count(*) = 0,
           string_agg(distinct coalesce(what, '?'), ', ')
    from (select 'status ' || a.team_key || '/' || a.status what from stg.issue_as_of a
          where (a.team_key, a.status) not in (select team_key, status from stg.status_map)
          union all select 'priority ' || priority_raw from stg.issue_as_of where priority_raw not in (select priority_raw from stg.priority_map)
          union all select 'type ' || type_raw from stg.issue_as_of where type_raw not in (select type_raw from stg.type_map)
          union all select 'resolution ' || resolution from stg.issue_as_of
          where resolution is not null and resolution not in (select resolution from stg.resolution_map))
    union all
    select 'one row per ticket, created inside the window', count(*) = count(distinct ticket_key)
           and min(created_at) >= timestamp '2024-10-01' and max(created_at) <= as_of(), min(created_at) || ' .. ' || max(created_at)
    from marts.fact_ticket
    union all
    select 'resolved tickets: resolved after created, before the as-of date, and in the Done stage', count(*) = 0, count(*) || ' bad'
    from marts.fact_ticket
    where resolved_at is not null and (resolved_at < created_at or resolved_at > as_of() or stage_key <> 4)
    union all
    select 'open tickets have no resolution and are not in the Done stage', count(*) = 0, count(*) || ' bad'
    from marts.fact_ticket where resolved_at is null and (resolution is not null or stage_key = 4)
    union all
    select 'first response, start of work and resolution never precede creation', count(*) = 0, count(*) || ' bad'
    from marts.fact_ticket where first_response_at < created_at or started_at < created_at
    union all
    select 'status intervals tile each ticket''s life without gaps or overlaps', count(*) = 0, count(*) || ' bad'
    from (select key, end_at, lead(start_at) over (partition by key order by start_at, end_at) next_start, is_current
          from stg.status_interval) where not is_current and end_at <> next_start
    union all
    select 'the backlog on the last day equals the open tickets', a = b, a || ' vs ' || b
    from (select sum(open_tickets) a from marts.fact_backlog_daily where "date" = date '2026-09-30'),
         (select count(*) b from marts.fact_ticket where resolved_at is null)
    union all
    select 'open-stage days add up to the lead time of resolved tickets', count(*) = 0, count(*) || ' tickets off by more than rounding'
    from marts.fact_ticket f
    join (select ticket_key, sum(days) d from marts.fact_stage_time group by all) s using (ticket_key)
    where f.resolved_at is not null and f.reopened = 0 and abs(s.d - f.lead_days) > 0.005
    union all
    select 'every ticket maps to a team, type, priority, stage and person', count(*) = 0, count(*) || ' orphans'
    from marts.fact_ticket f
    where team_key not in (select team_key from marts.dim_team) or type_key not in (select type_key from marts.dim_type)
       or priority_key not in (select priority_key from marts.dim_priority)
       or person_key not in (select person_key from marts.dim_person)
    union all
    select 'team access seeds point at real teams', count(*) = 0, count(*) || ' bad'
    from marts.team_access where team_key not in (select team_key from marts.dim_team)
    union all
    select 'every component bridge row points at a real ticket and component', count(*) = 0, count(*) || ' bad'
    from marts.bridge_ticket_component b
    where ticket_key not in (select ticket_key from marts.fact_ticket)
       or component_key not in (select component_key from marts.dim_component)
    union all
    select 'every stage transition changes stage, on a real ticket, by the as-of date', count(*) = 0, count(*) || ' bad'
    from marts.fact_stage_transition
    where from_stage_key = to_stage_key or transition_date > date '2026-09-30'
       or ticket_key not in (select ticket_key from marts.fact_ticket)
)
select * from t;
