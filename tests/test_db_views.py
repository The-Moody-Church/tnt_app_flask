"""
Totals views (weekly_attendance_totals, weekly_totals) and their accessors
data.get_attendance_totals / data.get_weekly_totals: exact parity with the Google Sheets formulas.

Sheet semantics being reproduced (see migrations/0001_init.sql):
- one row per schedule date x team, Red/Blue/Green/Yellow, zeros when there is no data
- kids present / item flags: distinct kids (COUNTUNIQUEIFS); flags count without requiring Present
- sections / silvers / golds: ROW counts (COUNTIFS) on the row's own date
- grouping by the team written on each row; NULL-team rows are excluded
- 'Total Points : ' || TRUNC(points_per_kid * 1000) using float math
"""
import math
import random
from datetime import date

import pytest

from models import data
from models.fields import TEAM_STAT_FIELDS
from models.utils import num
from tests.conftest import (BOOK_SECTIONS, D_0909, D_0916, D_0923, D_0930, D_1007, add_attendance,
                            add_section)

TEAMS = ['Red', 'Blue', 'Green', 'Yellow']
EMOJI = {'Red': '🟥', 'Blue': '🟦', 'Green': '🟩', 'Yellow': '🟨'}
DATES = [D_0909, D_0916, D_0923, D_0930, D_1007]
FLAGS = ['has_bible', 'wearing_shirt', 'has_book', 'did_homework', 'has_dues']
FLAG_TOTALS = dict(zip(FLAGS, ['total_bibles', 'total_shirts', 'total_workbooks', 'total_hw_done', 'total_dues']))
CREDITS = ['section_complete', 'silver_credit', 'gold_credit']
CREDIT_TOTALS = dict(zip(CREDITS, ['sections_completed', 'silvers_completed', 'golds_completed']))
ATTENDANCE_COUNTS = ['kids_present', *FLAG_TOTALS.values(), 'attendance_points']
WEEKLY_COUNTS = ['number_of_kids', *CREDIT_TOTALS.values(), 'section_points', 'attendance_points', 'total_points']


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def view_row(conn, view, d, team):
    """Raw view row (unformatted points_per_kid) for one date/team."""
    assert view in ('weekly_totals', 'weekly_attendance_totals')
    return conn.execute(f'SELECT * FROM {view} WHERE meeting_date = %s AND team = %s', (d, team)).fetchone()


def one(rows):
    assert len(rows) == 1, rows
    return rows[0]


def section(conn, cid, d, sec, credits=(), team='__kid__'):
    return add_section(conn, cid, d, sec, team=team, complete='section_complete' in credits,
                       silver='silver_credit' in credits, gold='gold_credit' in credits)


def make_kids(conn, n, team=None):
    """n synthetic kids (Test KidNNNN) with fresh contact ids."""
    start = conn.execute('SELECT COALESCE(MAX(contact_id), 0) + 1 AS n FROM kids').fetchone()['n']
    ids = list(range(start, start + n))
    for cid in ids:
        conn.execute("""INSERT INTO kids (contact_id, participant_id, first_name, last_name, team)
                        VALUES (%s, %s, 'Test', %s, %s)""", (cid, cid + 100000, f'Kid{cid}', team))
    return ids


def split_points(total, kids):
    """(flags, credits) such that kids + flags + 2 * credits == total, spread over both tables."""
    rest = total - kids
    flags = min(5 * kids, rest // 2)
    if (rest - flags) % 2:
        flags -= 1
    assert flags >= 0 and kids + flags + 2 * ((rest - flags) // 2) == total
    return flags, (rest - flags) // 2


def build_week(conn, d, team, *, kids, flags=0, credits=0):
    """`kids` synthetic present kids on `team` for d, with `flags` item flags and `credits` section credits."""
    assert flags <= 5 * kids and credits <= 3 * len(BOOK_SECTIONS) * kids
    ids = make_kids(conn, kids, team)
    kid_flags = [set() for _ in ids]
    for k in range(flags):
        kid_flags[k % kids].add(FLAGS[k // kids])
    rows = {}
    for k in range(credits):
        rest = k // kids
        rows.setdefault((ids[k % kids], BOOK_SECTIONS[rest // 3]), set()).add(CREDITS[rest % 3])
    for cid, fl in zip(ids, kid_flags):
        add_attendance(conn, cid, d, team=team, mp_checked_in=True, **{f: True for f in fl})
    for (cid, sec), crs in rows.items():
        section(conn, cid, d, sec, crs, team=team)
    return ids


# -----------------------------------------------------------------------------
# Grid shape: every schedule date x 4 teams, zeros when empty
# -----------------------------------------------------------------------------

def test_attendance_totals_grid_every_date_and_team_zero_filled(app_ctx, seeded):
    """No data: 5 dates x 4 teams in Red, Blue, Green, Yellow order, all zeros, points_per_kid int 0."""
    rows = data.get_attendance_totals()
    assert [(r['meeting_date'], r['team']) for r in rows] == [(d, t) for d in DATES for t in TEAMS]
    for r in rows:
        assert r['team_color'] == EMOJI[r['team']]
        assert r['team_sort'] == TEAMS.index(r['team']) + 1
        assert all(r[k] == 0 for k in ATTENDANCE_COUNTS), r
        assert r['points_per_kid'] == 0 and type(r['points_per_kid']) is int


def test_weekly_totals_grid_every_date_and_team_zero_filled(app_ctx, seeded):
    """No data: every date/team present with zeros and 'Total Points : 0'."""
    rows = data.get_weekly_totals()
    assert [(r['meeting_date'], r['team']) for r in rows] == [(d, t) for d in DATES for t in TEAMS]
    for r in rows:
        assert r['team_color'] == EMOJI[r['team']]
        assert all(r[k] == 0 for k in WEEKLY_COUNTS), r
        assert r['points_per_kid'] == 0 and type(r['points_per_kid']) is int
        assert r['team_total_points'] == 'Total Points : 0'


def test_grid_includes_dates_with_no_data_among_dates_with_data(app_ctx, seeded):
    """Data on one date doesn't drop the other dates (incl. the NO AWANA date) from the grid."""
    add_attendance(seeded, 1001, D_0916, mp_checked_in=True)
    rows = data.get_weekly_totals()
    assert len(rows) == 20
    nonzero = [(r['meeting_date'], r['team']) for r in rows if r['total_points']]
    assert nonzero == [(D_0916, 'Red')]
    no_club = data.get_weekly_totals(D_1007)
    assert [r['team'] for r in no_club] == TEAMS
    assert all(r['team_total_points'] == 'Total Points : 0' for r in no_club)


def test_grid_follows_schedule(app_ctx, seeded):
    """A new schedule date adds four rows; a date not on the schedule has none."""
    seeded.execute("INSERT INTO schedule (meeting_date, theme) VALUES ('2026-10-14', 'Regular Meeting')")
    assert [r['team'] for r in data.get_attendance_totals(date(2026, 10, 14))] == TEAMS
    assert [r['team'] for r in data.get_weekly_totals(date(2026, 10, 14))] == TEAMS
    assert len(data.get_weekly_totals()) == 24
    assert data.get_weekly_totals(date(2026, 9, 2)) == []
    assert data.get_attendance_totals(date(2026, 9, 2)) == []


def test_empty_schedule_gives_no_rows(app_ctx, conn):
    """The grid is schedule x teams: no schedule, no rows."""
    assert data.get_weekly_totals() == []
    assert data.get_attendance_totals() == []


def test_weekly_row_has_team_stat_fields(app_ctx, seeded):
    """Every key the team page shows (fields.TEAM_STAT_FIELDS) plus the display string is on the row."""
    row = data.get_weekly_totals(D_0923, 'Red')[0]
    for _label, key in TEAM_STAT_FIELDS:
        assert key in row
    assert {'meeting_date', 'team', 'team_sort', 'team_total_points'} <= row.keys()


def test_view_column_types(seeded):
    """Counts are ints, points_per_kid is a float in the view, team_total_points is text."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, has_bible=True)
    add_attendance(seeded, 1002, D_0923, mp_checked_in=True)
    section(seeded, 1001, D_0923, '1.2', ['section_complete'])
    att = view_row(seeded, 'weekly_attendance_totals', D_0923, 'Red')
    wk = view_row(seeded, 'weekly_totals', D_0923, 'Red')
    assert all(type(att[k]) is int for k in ATTENDANCE_COUNTS)
    assert all(type(wk[k]) is int for k in WEEKLY_COUNTS)
    assert type(att['points_per_kid']) is float and att['points_per_kid'] == 1.5
    assert type(wk['points_per_kid']) is float and wk['points_per_kid'] == 2.5
    assert wk['team_total_points'] == 'Total Points : 2500'


# -----------------------------------------------------------------------------
# Weekly Attendance Totals
# -----------------------------------------------------------------------------

def test_kids_present_is_mp_checked_in_or_manual_present(app_ctx, seeded):
    """Present = MP check-in OR manual mark; a kid with both counts once; a row with neither counts zero."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    add_attendance(seeded, 1002, D_0923, manual_present=True)
    add_attendance(seeded, 1003, D_0923, mp_checked_in=True, manual_present=True)
    add_attendance(seeded, 1004, D_0923)
    by_team = {r['team']: r for r in data.get_attendance_totals(D_0923)}
    assert by_team['Red']['kids_present'] == 2
    assert by_team['Blue']['kids_present'] == 1
    assert by_team['Green']['kids_present'] == 0 and by_team['Yellow']['kids_present'] == 0
    assert by_team['Blue']['attendance_points'] == 1


@pytest.mark.parametrize('flag', FLAGS)
def test_each_flag_counts_into_its_own_column(app_ctx, seeded, flag):
    """One present kid with one flag: that total is 1, the other four are 0."""
    add_attendance(seeded, 1005, D_0916, mp_checked_in=True, **{flag: True})
    row = one(data.get_attendance_totals(D_0916, 'Green'))
    for f, col in FLAG_TOTALS.items():
        assert row[col] == (1 if f == flag else 0), col
    assert row['kids_present'] == 1
    assert row['attendance_points'] == 2
    assert row['points_per_kid'] == 2


def test_flags_count_without_present(app_ctx, seeded):
    """Sheet semantics: flags count even if the kid isn't Present; per-kid is 0 (IFERROR) with nobody present."""
    add_attendance(seeded, 1001, D_0923, has_bible=True, wearing_shirt=True)
    add_attendance(seeded, 1002, D_0923, has_bible=True)
    att = one(data.get_attendance_totals(D_0923, 'Red'))
    assert att['kids_present'] == 0
    assert att['total_bibles'] == 2 and att['total_shirts'] == 1
    assert att['attendance_points'] == 3
    assert att['points_per_kid'] == 0
    wk = one(data.get_weekly_totals(D_0923, 'Red'))
    assert wk['number_of_kids'] == 0
    assert wk['attendance_points'] == 3 and wk['total_points'] == 3
    assert wk['points_per_kid'] == 0
    assert wk['team_total_points'] == 'Total Points : 0'


def test_attendance_points_is_sum_of_six_counts(app_ctx, seeded):
    """J = SUM(D:I): 1 per present kid + 1 per flag per kid; K = J / D."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, **{f: True for f in FLAGS})
    add_attendance(seeded, 1002, D_0923, manual_present=True, has_bible=True, has_dues=True)
    row = one(data.get_attendance_totals(D_0923, 'Red'))
    assert row['kids_present'] == 2
    assert [row[c] for c in FLAG_TOTALS.values()] == [2, 1, 1, 1, 2]
    assert row['attendance_points'] == 2 + 2 + 1 + 1 + 1 + 2
    assert row['points_per_kid'] == 9 / 2


def test_attendance_points_per_kid_fraction(app_ctx, seeded):
    """4 points / 3 kids: the view keeps the full float, the accessor shows 10 significant digits."""
    for cid in (1001, 1002, 1008):
        add_attendance(seeded, cid, D_0916, mp_checked_in=True, has_bible=(cid == 1001))
    assert view_row(seeded, 'weekly_attendance_totals', D_0916, 'Red')['points_per_kid'] == 4 / 3
    assert one(data.get_attendance_totals(D_0916, 'Red'))['points_per_kid'] == 1.333333333


def test_inactive_kid_rows_still_count(app_ctx, seeded):
    """Totals count every recorded row, including kids no longer active on the roster."""
    add_attendance(seeded, 1008, D_0916, mp_checked_in=True, has_book=True)
    section(seeded, 1008, D_0916, '1.1', ['section_complete'])
    row = one(data.get_weekly_totals(D_0916, 'Red'))
    assert (row['number_of_kids'], row['attendance_points'], row['section_points']) == (1, 2, 2)


def test_rows_only_count_on_their_own_date(app_ctx, seeded):
    """A row on 9/16 does not leak into 9/23 or any other date."""
    add_attendance(seeded, 1003, D_0916, mp_checked_in=True, has_bible=True)
    section(seeded, 1003, D_0916, '1.1', CREDITS)
    for r in data.get_weekly_totals():
        if (r['meeting_date'], r['team']) == (D_0916, 'Blue'):
            assert r['total_points'] == 2 + 6
        else:
            assert r['total_points'] == 0, r


# -----------------------------------------------------------------------------
# Weekly Totals
# -----------------------------------------------------------------------------

def test_credits_are_row_counts(app_ctx, seeded):
    """COUNTIFS counts rows: the same kid's several section rows each count; all-false rows count 0."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    section(seeded, 1001, D_0923, '1.1', ['section_complete'])
    section(seeded, 1001, D_0923, '1.2', ['section_complete', 'silver_credit'])
    section(seeded, 1001, D_0923, 'Start Zone 1', ['gold_credit'])
    section(seeded, 1002, D_0923, '1.2', [])
    row = one(data.get_weekly_totals(D_0923, 'Red'))
    assert (row['sections_completed'], row['silvers_completed'], row['golds_completed']) == (2, 1, 1)
    assert row['section_points'] == 2 * (2 + 1 + 1)
    assert row['attendance_points'] == 1
    assert row['total_points'] == 9
    assert row['points_per_kid'] == 9
    assert row['team_total_points'] == 'Total Points : 9000'


@pytest.mark.parametrize('credit', CREDITS)
def test_each_credit_counts_into_its_own_column(app_ctx, seeded, credit):
    """A single-credit row adds 1 to its column and 2 section points."""
    section(seeded, 1006, D_0923, '1.2', [credit])
    row = one(data.get_weekly_totals(D_0923, 'Yellow'))
    for c, col in CREDIT_TOTALS.items():
        assert row[col] == (1 if c == credit else 0), col
    assert row['section_points'] == 2


def test_silver_recorded_on_later_date_counts_on_later_date(app_ctx, seeded):
    """Main credit on 9/16, silver for the same section on 9/23: each counts on its own row's date."""
    add_attendance(seeded, 1003, D_0916, mp_checked_in=True)
    add_attendance(seeded, 1003, D_0923, mp_checked_in=True)
    section(seeded, 1003, D_0916, '1.1', ['section_complete'])
    section(seeded, 1003, D_0923, '1.1', ['silver_credit'])
    section(seeded, 1003, D_0930, '1.1', ['gold_credit'])
    wk16 = one(data.get_weekly_totals(D_0916, 'Blue'))
    wk23 = one(data.get_weekly_totals(D_0923, 'Blue'))
    wk30 = one(data.get_weekly_totals(D_0930, 'Blue'))
    assert (wk16['sections_completed'], wk16['silvers_completed'], wk16['golds_completed']) == (1, 0, 0)
    assert (wk23['sections_completed'], wk23['silvers_completed'], wk23['golds_completed']) == (0, 1, 0)
    assert (wk30['sections_completed'], wk30['silvers_completed'], wk30['golds_completed']) == (0, 0, 1)
    assert wk16['total_points'] == 3 and wk23['total_points'] == 3
    # 9/30: section points but nobody present -> per kid 0
    assert wk30['total_points'] == 2 and wk30['number_of_kids'] == 0
    assert wk30['points_per_kid'] == 0 and wk30['team_total_points'] == 'Total Points : 0'


def test_weekly_links_attendance_view(app_ctx, seeded):
    """number_of_kids = kids_present and attendance_points match the attendance view on every row."""
    add_attendance(seeded, 1001, D_0916, mp_checked_in=True, has_bible=True)
    add_attendance(seeded, 1003, D_0923, manual_present=True, has_dues=True, has_book=True)
    add_attendance(seeded, 1006, D_0923, has_bible=True)
    section(seeded, 1003, D_0923, '1.2', ['section_complete'])
    att = data.get_attendance_totals()
    wk = data.get_weekly_totals()
    assert [(r['meeting_date'], r['team']) for r in att] == [(r['meeting_date'], r['team']) for r in wk]
    for a, w in zip(att, wk):
        assert w['number_of_kids'] == a['kids_present']
        assert w['attendance_points'] == a['attendance_points']
        assert w['total_points'] == w['section_points'] + w['attendance_points']


def test_weekly_points_per_kid_uses_total_points(app_ctx, seeded):
    """Weekly K = (section points + attendance points) / kids present."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, has_bible=True)
    add_attendance(seeded, 1002, D_0923, mp_checked_in=True)
    section(seeded, 1002, D_0923, '1.2', ['section_complete', 'silver_credit'])
    row = one(data.get_weekly_totals(D_0923, 'Red'))
    assert (row['attendance_points'], row['section_points'], row['total_points']) == (3, 4, 7)
    assert row['points_per_kid'] == 3.5
    assert row['team_total_points'] == 'Total Points : 3500'
    assert one(data.get_attendance_totals(D_0923, 'Red'))['points_per_kid'] == 1.5


# -----------------------------------------------------------------------------
# Team grouping: the row's team snapshot
# -----------------------------------------------------------------------------

def test_grouping_uses_row_team_not_current_kid_team(app_ctx, seeded):
    """Moving a kid to another team afterwards leaves every past total unchanged."""
    add_attendance(seeded, 1001, D_0916, mp_checked_in=True, has_bible=True)
    add_attendance(seeded, 1003, D_0916, mp_checked_in=True)
    section(seeded, 1001, D_0916, '1.1', ['section_complete', 'gold_credit'])
    before_wk, before_att = data.get_weekly_totals(), data.get_attendance_totals()
    seeded.execute("UPDATE kids SET team = 'Blue' WHERE contact_id = 1001")
    seeded.execute("UPDATE kids SET team = NULL WHERE contact_id = 1003")
    assert data.get_weekly_totals() == before_wk
    assert data.get_attendance_totals() == before_att
    red = one(data.get_weekly_totals(D_0916, 'Red'))
    assert (red['number_of_kids'], red['total_points']) == (1, 2 + 4)


def test_row_team_overrides_kid_team(app_ctx, seeded):
    """Each table groups by its own row's team, even when it differs from kids.team and from each other."""
    add_attendance(seeded, 1001, D_0923, team='Green', mp_checked_in=True, has_bible=True)
    section(seeded, 1001, D_0923, '1.2', ['section_complete'], team='Yellow')
    by_team = {r['team']: r for r in data.get_weekly_totals(D_0923)}
    assert by_team['Red']['total_points'] == 0
    assert (by_team['Green']['number_of_kids'], by_team['Green']['attendance_points'],
            by_team['Green']['section_points']) == (1, 2, 0)
    assert (by_team['Yellow']['number_of_kids'], by_team['Yellow']['section_points'],
            by_team['Yellow']['total_points']) == (0, 2, 2)
    assert by_team['Yellow']['team_total_points'] == 'Total Points : 0'


def test_null_team_rows_excluded(app_ctx, seeded):
    """Unassigned (NULL-team) rows count nowhere, including a rostered kid's row written with no team."""
    add_attendance(seeded, 1007, D_0923, mp_checked_in=True, **{f: True for f in FLAGS})
    section(seeded, 1007, D_0923, '1.2', CREDITS)
    add_attendance(seeded, 1001, D_0923, team=None, mp_checked_in=True, has_bible=True)
    section(seeded, 1001, D_0923, '1.2', CREDITS, team=None)
    assert all(r['total_points'] == 0 and r['number_of_kids'] == 0 for r in data.get_weekly_totals())
    assert all(r['attendance_points'] == 0 for r in data.get_attendance_totals())
    assert data.get_weekly_totals(D_0923, None) == []
    assert data.get_attendance_totals(D_0923, None) == []
    assert seeded.execute('SELECT COUNT(*) AS n FROM weekly_totals WHERE team IS NULL').fetchone()['n'] == 0


# -----------------------------------------------------------------------------
# 'Total Points : <trunc(per_kid * 1000)>' display string
# -----------------------------------------------------------------------------

@pytest.mark.parametrize('total, kids, expected', [
    (166, 16, 'Total Points : 10375'),
    (105, 18, 'Total Points : 5833'),
    (323, 20, 'Total Points : ' + str(math.trunc((323 / 20) * 1000))),
    (7, 3, 'Total Points : 2333'),
    (40, 1, 'Total Points : 40000'),
])
def test_team_total_points_string(app_ctx, seeded, total, kids, expected):
    """The display string is the per-kid average x 1000, truncated, not the total."""
    flags, credits = split_points(total, kids)
    build_week(seeded, D_0916, 'Green', kids=kids, flags=flags, credits=credits)
    row = one(data.get_weekly_totals(D_0916, 'Green'))
    assert row['number_of_kids'] == kids
    assert row['attendance_points'] == kids + flags
    assert row['section_points'] == 2 * credits
    assert row['total_points'] == total
    assert row['team_total_points'] == expected
    assert view_row(seeded, 'weekly_totals', D_0916, 'Green')['points_per_kid'] == total / kids
    others = [r for r in data.get_weekly_totals() if (r['meeting_date'], r['team']) != (D_0916, 'Green')]
    assert all(r['team_total_points'] == 'Total Points : 0' for r in others)


def test_team_total_points_uses_float_math(app_ctx, seeded):
    """323/20: Sheets' IEEE doubles give 16.15*1000 = 16149.999..., so 16149, not integer-exact 16150."""
    assert math.trunc((323 / 20) * 1000) != 323 * 1000 // 20  # the edge case is real
    flags, credits = split_points(323, 20)
    build_week(seeded, D_0923, 'Blue', kids=20, flags=flags, credits=credits)
    row = one(data.get_weekly_totals(D_0923, 'Blue'))
    assert row['total_points'] == 323
    assert row['team_total_points'] == 'Total Points : 16149'
    assert row['points_per_kid'] == 16.15


# -----------------------------------------------------------------------------
# Accessors: filtering and points_per_kid display (num)
# -----------------------------------------------------------------------------

@pytest.mark.parametrize('getter', [data.get_weekly_totals, data.get_attendance_totals])
def test_filter_by_date_and_team(app_ctx, seeded, getter):
    """(d, team) -> one row; (None, team) -> that team on every date; (d) -> 4 teams; () -> whole grid."""
    add_attendance(seeded, 1003, D_0923, mp_checked_in=True)
    row = one(getter(D_0923, 'Blue'))
    assert (row['meeting_date'], row['team'], row['team_color']) == (D_0923, 'Blue', '🟦')
    assert row['attendance_points'] == 1
    assert [(r['meeting_date'], r['team']) for r in getter(None, 'Blue')] == [(d, 'Blue') for d in DATES]
    assert [r['team'] for r in getter(D_0923)] == TEAMS
    assert len(getter()) == 20
    assert getter(D_0923, 'Purple') == []


@pytest.mark.parametrize('getter', [data.get_weekly_totals, data.get_attendance_totals])
def test_explicit_conn_outside_app_context(seeded, getter):
    """conn= works without an app context (scripts)."""
    add_attendance(seeded, 1006, D_0930, mp_checked_in=True, has_bible=True)
    row = one(getter(D_0930, 'Yellow', conn=seeded))
    assert row['attendance_points'] == 2 and row['points_per_kid'] == 2


@pytest.mark.parametrize('points, kids, shown', [
    (4, 2, 2),
    (166, 16, 10.375),
    (84, 17, 4.941176471),
    (21, 4, 5.25),
    (100, 3, 33.33333333),
])
def test_points_per_kid_display(app_ctx, seeded, points, kids, shown):
    """num(): integer results come back as int; fractions round to the sheet's 10 significant digits."""
    flags, credits = split_points(points, kids)
    build_week(seeded, D_0916, 'Red', kids=kids, flags=flags, credits=credits)
    wk = one(data.get_weekly_totals(D_0916, 'Red'))
    assert wk['points_per_kid'] == shown
    assert type(wk['points_per_kid']) is type(shown)
    assert view_row(seeded, 'weekly_totals', D_0916, 'Red')['points_per_kid'] == points / kids


def test_attendance_points_per_kid_display(app_ctx, seeded):
    """84 attendance points / 17 kids -> 4.941176471 (raw 4.9411764705882...); 34/17 -> int 2."""
    build_week(seeded, D_0923, 'Yellow', kids=17, flags=84 - 17)
    build_week(seeded, D_0923, 'Green', kids=17, flags=17)
    by_team = {r['team']: r for r in data.get_attendance_totals(D_0923)}
    assert by_team['Yellow']['points_per_kid'] == 4.941176471
    assert view_row(seeded, 'weekly_attendance_totals', D_0923, 'Yellow')['points_per_kid'] == 84 / 17
    assert by_team['Green']['points_per_kid'] == 2 and type(by_team['Green']['points_per_kid']) is int


# -----------------------------------------------------------------------------
# Randomized parity against a Python transcription of the sheet formulas
# -----------------------------------------------------------------------------

def sheet_totals(att_rows, sec_rows):
    """Reference: the sheet formulas over RAW rows, keyed by (date, team)."""
    out = {}
    for d in DATES:
        for t in TEAMS:
            att = [r for r in att_rows if r['d'] == d and r['team'] == t]
            sec = [r for r in sec_rows if r['d'] == d and r['team'] == t]
            present = len({r['cid'] for r in att if r['mp'] or r['manual']})                   # D (COUNTUNIQUEIFS)
            flags = {FLAG_TOTALS[f]: len({r['cid'] for r in att if r[f]}) for f in FLAGS}      # E:I
            ap = present + sum(flags.values())                                                 # J = SUM(D:I)
            credits = {CREDIT_TOTALS[c]: sum(1 for r in sec if r[c]) for c in CREDITS}         # E:G (COUNTIFS)
            sp = 2 * sum(credits.values())                                                     # H
            tp = sp + ap                                                                       # J
            ppk = tp / present if present else 0                                               # K = IFERROR(J/D, 0)
            out[(d, t)] = {
                'att': {'kids_present': present, **flags, 'attendance_points': ap,
                        'points_per_kid': ap / present if present else 0},
                'wk': {'number_of_kids': present, **credits, 'section_points': sp, 'attendance_points': ap,
                       'total_points': tp, 'points_per_kid': ppk,
                       'team_total_points': f'Total Points : {math.trunc(ppk * 1000)}'},
            }
    return out


@pytest.mark.parametrize('seed', [1, 7, 2026])
def test_views_match_sheet_formulas_on_random_season(app_ctx, seeded, seed):
    """Random rows (mixed teams incl. NULL, flags without Present, all-false credits): views == sheet formulas."""
    rng = random.Random(seed)
    kid_ids = [k for k in range(1001, 1009)] + make_kids(seeded, 16)
    choices = TEAMS + [None]
    att_rows, sec_rows = [], []
    for d in DATES:
        for cid in kid_ids:
            home = rng.choice(choices)
            if rng.random() < 0.7:
                r = {'cid': cid, 'd': d, 'team': home, 'mp': rng.random() < 0.5, 'manual': rng.random() < 0.3,
                     **{f: rng.random() < 0.4 for f in FLAGS}}
                add_attendance(seeded, cid, d, team=r['team'], mp_checked_in=r['mp'], manual_present=r['manual'],
                               **{f: r[f] for f in FLAGS})
                att_rows.append(r)
            for sec in rng.sample(BOOK_SECTIONS, rng.randint(0, 3)):
                team = home if rng.random() < 0.8 else rng.choice(choices)
                r = {'cid': cid, 'd': d, 'team': team, **{c: rng.random() < 0.5 for c in CREDITS}}
                section(seeded, cid, d, sec, [c for c in CREDITS if r[c]], team=team)
                sec_rows.append(r)
    expected = sheet_totals(att_rows, sec_rows)
    assert any(e['wk']['total_points'] for e in expected.values())

    raw_att = seeded.execute('SELECT * FROM weekly_attendance_totals ORDER BY meeting_date, team_sort').fetchall()
    raw_wk = seeded.execute('SELECT * FROM weekly_totals ORDER BY meeting_date, team_sort').fetchall()
    assert [(r['meeting_date'], r['team']) for r in raw_wk] == list(expected)
    for r in raw_att:
        exp = expected[(r['meeting_date'], r['team'])]['att']
        assert {k: r[k] for k in exp} == exp, (r['meeting_date'], r['team'])
    for r in raw_wk:
        exp = expected[(r['meeting_date'], r['team'])]['wk']
        assert {k: r[k] for k in exp} == exp, (r['meeting_date'], r['team'])

    for r in data.get_weekly_totals():
        exp = expected[(r['meeting_date'], r['team'])]['wk']
        assert r['points_per_kid'] == num(exp['points_per_kid'])
        assert r['team_total_points'] == exp['team_total_points']
    for r in data.get_attendance_totals():
        assert r['points_per_kid'] == num(expected[(r['meeting_date'], r['team'])]['att']['points_per_kid'])
