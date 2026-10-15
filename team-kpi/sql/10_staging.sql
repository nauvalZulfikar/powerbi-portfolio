-- Staging: one row per issue and one row per field change, cut off at the as-of moment.
-- Each field's value on the as-of date is replayed from the change history, so later edits don't leak in.
create schema stg;
create schema marts;

create macro as_of() as timestamp '2026-09-30 23:59:59';

create table stg.issue as
select key, split_part(key, '-', 1) team_key, summary, type type_now, created,
       status status_now, resolution resolution_now, priority priority_now, assignee.key assignee_now,
       reporter.key reporter, components, history
from read_json('{raw}/*.jsonl.gz', format = 'newline_delimited',
               columns = {key: 'varchar', summary: 'varchar', type: 'varchar', priority: 'varchar', status: 'varchar',
                          resolution: 'varchar', created: 'timestamp', resolved: 'timestamp',
                          assignee: 'struct(key varchar, name varchar, display varchar)', reporter: 'struct(key varchar, name varchar, display varchar)',
                          components: 'varchar[]', labels: 'varchar[]',
                          history: 'struct("at" timestamp, "by" struct(key varchar, name varchar, display varchar), items struct(field varchar, "from" varchar, fromString varchar, "to" varchar, toString varchar)[])[]'});

-- Every field change, in order; seq breaks ties between changes made in the same second.
create table stg.change as
with h as (select key, unnest(history) h, generate_subscripts(history, 1) ord from stg.issue),
     i as (select key, h."at" changed_at, h.by.key author, ord, unnest(h.items) i, generate_subscripts(h.items, 1) ord_i from h)
select key, changed_at, author, i.field, i."from" from_id, i.fromString from_value, i."to" to_id, i.toString to_value,
       row_number() over (partition by key, i.field order by changed_at, ord, ord_i) seq
from i;

-- Value on the as-of date: the last change before it, else the value before the first change.
-- People are compared by Jira's internal user key (logins and display names get renamed); other fields by display value.
create table stg.field_as_of as
select key, field,
       case when count(*) filter (where changed_at <= as_of()) > 0
            then (max({s: seq, v: if(field = 'assignee', to_id, to_value)}) filter (where changed_at <= as_of())).v
            else (min({s: seq, v: if(field = 'assignee', from_id, from_value)})).v end val
from stg.change
group by all;

create table stg.issue_as_of as
select i.key, i.team_key, i.created, i.reporter, i.summary,
       coalesce(s.val, i.status_now) status,
       coalesce(p.val, i.priority_now) priority_raw,
       coalesce(t.val, i.type_now) type_raw,
       if(r.key is null, i.resolution_now, r.val) resolution,   -- null is a real value here: still open
       if(a.key is null, i.assignee_now, a.val) assignee
from stg.issue i
left join stg.field_as_of s on s.key = i.key and s.field = 'status'
left join stg.field_as_of p on p.key = i.key and p.field = 'priority'
left join stg.field_as_of t on t.key = i.key and t.field = 'issuetype'
left join stg.field_as_of r on r.key = i.key and r.field = 'resolution'
left join stg.field_as_of a on a.key = i.key and a.field = 'assignee';

-- Status timeline: one interval per status the issue sat in, from creation to the as-of moment.
create table stg.status_interval as
with first_status as (
    select i.key, i.team_key, i.created, coalesce(f.v, i.status_now) status
    from stg.issue i
    left join (select key, min({s: seq, v: from_value}).v v from stg.change where field = 'status' group by key) f using (key)),
moves as (
    select key, team_key, created changed_at, status, 0 seq from first_status
    union all
    select c.key, i.team_key, c.changed_at, c.to_value, c.seq from stg.change c join stg.issue i using (key)
    where c.field = 'status' and c.changed_at <= as_of())
select key, team_key, status, changed_at start_at,
       coalesce(lead(changed_at) over (partition by key order by changed_at, seq), as_of()) end_at,
       lead(changed_at) over (partition by key order by changed_at, seq) is null is_current
from moves;
