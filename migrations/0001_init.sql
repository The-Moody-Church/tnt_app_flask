-- 0001_init: TNT schema. Applied by scripts/migrate.py as the database owner over the DIRECT url.
-- Grants live in scripts/neon/grants.sql (re-runnable), not here.

CREATE TABLE teams (
    name        text PRIMARY KEY,
    emoji       text NOT NULL,
    sort_order  smallint NOT NULL UNIQUE
);
INSERT INTO teams (name, emoji, sort_order) VALUES
    ('Red', '🟥', 1), ('Blue', '🟦', 2), ('Green', '🟩', 3), ('Yellow', '🟨', 4);

CREATE TABLE book_sections (
    section     text PRIMARY KEY CHECK (section <> '' AND section = btrim(section)),
    sort_order  smallint NOT NULL UNIQUE
);

CREATE TABLE schedule (
    meeting_date date PRIMARY KEY,
    theme        text NOT NULL DEFAULT '',
    sections     text[] NOT NULL DEFAULT '{}',   -- tonight's section(s), e.g. {1.5,1.6}
    sequence     text,
    unit         text,
    scope        text,
    key_verses   text,
    details      text,
    no_club      boolean GENERATED ALWAYS AS (theme ILIKE '%no awana%') STORED,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Snapshot of MP Group 697 (T&T) kids. team comes from Group_Participants.Notes; NULL = Unassigned.
CREATE TABLE kids (
    contact_id           integer PRIMARY KEY,
    participant_id       integer UNIQUE,           -- NULL if MP moved it to another (merged) contact
    group_participant_id integer,
    nickname             text,
    first_name           text NOT NULL,
    last_name            text NOT NULL,
    name                 text GENERATED ALWAYS AS
                           (COALESCE(NULLIF(btrim(nickname), ''), btrim(first_name)) || ' ' || btrim(last_name)) STORED,
    team                 text REFERENCES teams(name),
    mp_notes             text,
    group_role_id        integer,
    active               boolean NOT NULL DEFAULT true,
    team_assigned_at     timestamptz,      -- set by assign_team; a roster fetch started earlier must not overwrite it
    first_seen_at        timestamptz NOT NULL DEFAULT now(),
    synced_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX kids_active_team_idx ON kids (team, last_name) WHERE active;

-- One row per kid per meeting date. team is a snapshot taken when the row is first written.
CREATE TABLE attendance (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contact_id              integer NOT NULL REFERENCES kids(contact_id),
    meeting_date            date    NOT NULL REFERENCES schedule(meeting_date),
    team                    text REFERENCES teams(name),
    mp_checked_in           boolean NOT NULL DEFAULT false,
    mp_time_in              timestamptz,
    mp_time_confirmed       timestamptz,
    mp_event_participant_id integer,
    mp_synced_at            timestamptz,
    manual_present          boolean NOT NULL DEFAULT false,
    manual_present_by       text,
    manual_present_at       timestamptz,
    present                 boolean GENERATED ALWAYS AS (mp_checked_in OR manual_present) STORED,
    has_bible               boolean NOT NULL DEFAULT false,
    wearing_shirt           boolean NOT NULL DEFAULT false,
    has_book                boolean NOT NULL DEFAULT false,
    did_homework            boolean NOT NULL DEFAULT false,
    has_dues                boolean NOT NULL DEFAULT false,
    source                  text NOT NULL DEFAULT 'app' CHECK (source IN ('app', 'mp_sync', 'import', 'backfill')),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    updated_by_contact_id   integer,
    CONSTRAINT attendance_kid_date_uq UNIQUE (contact_id, meeting_date)
);
CREATE INDEX attendance_date_team_idx ON attendance (meeting_date, team);

-- Rows are never deleted; a row with every credit false scores 0 and is hidden from lists.
CREATE TABLE completed_sections (
    id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contact_id            integer NOT NULL REFERENCES kids(contact_id),
    meeting_date          date    NOT NULL REFERENCES schedule(meeting_date),
    section               text    NOT NULL REFERENCES book_sections(section) ON UPDATE CASCADE,
    team                  text REFERENCES teams(name),
    section_complete      boolean NOT NULL DEFAULT false,
    silver_credit         boolean NOT NULL DEFAULT false,
    gold_credit           boolean NOT NULL DEFAULT false,
    source                text NOT NULL DEFAULT 'app' CHECK (source IN ('app', 'import')),
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by_contact_id integer,
    CONSTRAINT completed_sections_kid_date_section_uq UNIQUE (contact_id, meeting_date, section)
);
CREATE INDEX completed_sections_date_team_idx ON completed_sections (meeting_date, team);
CREATE INDEX completed_sections_kid_idx ON completed_sections (contact_id);

-- MP sync bookkeeping: 'roster', 'checkins:YYYY-MM-DD', 'mp_health' (circuit breaker).
CREATE TABLE sync_state (
    key          text PRIMARY KEY,
    synced_at    timestamptz,     -- start time of the last SUCCESSFUL fetch
    attempted_at timestamptz,     -- last claim (success or failure)
    last_error   text,
    detail       jsonb NOT NULL DEFAULT '{}'
);

-- Audit of team assignments written back to MP.
CREATE TABLE team_assignments (
    id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contact_id             integer NOT NULL REFERENCES kids(contact_id),
    group_participant_id   integer NOT NULL,
    old_notes              text,
    new_team               text NOT NULL REFERENCES teams(name),
    assigned_by_contact_id integer NOT NULL,
    assigned_by_user_id    integer NOT NULL,
    assigned_at            timestamptz NOT NULL DEFAULT now()
);

-- Weekly Attendance Totals. Sheet: COUNTUNIQUEIFS(Name, Team, Date, <flag>=TRUE) per flag,
-- grouped by the team recorded on each row. Flags count without requiring Present (as the sheet did).
CREATE VIEW weekly_attendance_totals AS
WITH per_team AS (
    SELECT a.meeting_date, a.team,
           COUNT(DISTINCT a.contact_id) FILTER (WHERE a.present)       AS kids_present,
           COUNT(DISTINCT a.contact_id) FILTER (WHERE a.has_bible)     AS total_bibles,
           COUNT(DISTINCT a.contact_id) FILTER (WHERE a.wearing_shirt) AS total_shirts,
           COUNT(DISTINCT a.contact_id) FILTER (WHERE a.has_book)      AS total_workbooks,
           COUNT(DISTINCT a.contact_id) FILTER (WHERE a.did_homework)  AS total_hw_done,
           COUNT(DISTINCT a.contact_id) FILTER (WHERE a.has_dues)      AS total_dues
    FROM attendance a
    WHERE a.team IS NOT NULL
    GROUP BY a.meeting_date, a.team
), grid AS (
    SELECT s.meeting_date, t.name AS team, t.emoji AS team_color, t.sort_order AS team_sort,
           COALESCE(p.kids_present, 0)::int    AS kids_present,
           COALESCE(p.total_bibles, 0)::int    AS total_bibles,
           COALESCE(p.total_shirts, 0)::int    AS total_shirts,
           COALESCE(p.total_workbooks, 0)::int AS total_workbooks,
           COALESCE(p.total_hw_done, 0)::int   AS total_hw_done,
           COALESCE(p.total_dues, 0)::int      AS total_dues
    FROM schedule s CROSS JOIN teams t
    LEFT JOIN per_team p ON p.meeting_date = s.meeting_date AND p.team = t.name
)
SELECT g.meeting_date, g.team, g.team_color, g.team_sort,
       g.kids_present, g.total_bibles, g.total_shirts, g.total_workbooks, g.total_hw_done, g.total_dues,
       (g.kids_present + g.total_bibles + g.total_shirts + g.total_workbooks + g.total_hw_done + g.total_dues)
           AS attendance_points,                                                 -- =SUM(D:I)
       CASE WHEN g.kids_present = 0 THEN 0::float8                               -- =IFERROR(J/D,0)
            ELSE (g.kids_present + g.total_bibles + g.total_shirts + g.total_workbooks
                  + g.total_hw_done + g.total_dues)::float8 / g.kids_present END AS points_per_kid
FROM grid g;

-- Weekly Totals. Sheet: COUNTIFS row counts (not distinct) per credit, 2 points per credit,
-- per-kid average, and the 'Total Points : <trunc(per_kid*1000)>' display string.
CREATE VIEW weekly_totals AS
WITH per_team AS (
    SELECT c.meeting_date, c.team,
           COUNT(*) FILTER (WHERE c.section_complete) AS sections_completed,
           COUNT(*) FILTER (WHERE c.silver_credit)    AS silvers_completed,
           COUNT(*) FILTER (WHERE c.gold_credit)      AS golds_completed
    FROM completed_sections c
    WHERE c.team IS NOT NULL
    GROUP BY c.meeting_date, c.team
), base AS (
    SELECT w.meeting_date, w.team, w.team_color, w.team_sort,
           w.kids_present                          AS number_of_kids,        -- ='Weekly Attendance Totals'!D
           COALESCE(p.sections_completed, 0)::int  AS sections_completed,
           COALESCE(p.silvers_completed, 0)::int   AS silvers_completed,
           COALESCE(p.golds_completed, 0)::int     AS golds_completed,
           w.attendance_points                                               -- ='Weekly Attendance Totals'!J
    FROM weekly_attendance_totals w
    LEFT JOIN per_team p ON p.meeting_date = w.meeting_date AND p.team = w.team
), pts AS (
    SELECT b.*, 2 * (b.sections_completed + b.silvers_completed + b.golds_completed) AS section_points
    FROM base b
)
SELECT p.meeting_date, p.team, p.team_color, p.team_sort, p.number_of_kids,
       p.sections_completed, p.silvers_completed, p.golds_completed,
       p.section_points, p.attendance_points,
       p.section_points + p.attendance_points AS total_points,
       CASE WHEN p.number_of_kids = 0 THEN 0::float8
            ELSE (p.section_points + p.attendance_points)::float8 / p.number_of_kids END AS points_per_kid,
       -- =CONCATENATE("Total Points : ", TRUNC(K*1000)). Float math on purpose: it matches Sheets'
       -- IEEE doubles, which differ from exact integer division in a few edge cases (e.g. 323/20).
       'Total Points : ' || CASE WHEN p.number_of_kids = 0 THEN 0
            ELSE trunc(((p.section_points + p.attendance_points)::float8 / p.number_of_kids) * 1000)::bigint END
           AS team_total_points
FROM pts p;
