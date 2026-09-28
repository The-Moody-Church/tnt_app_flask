"""models/data.py reads and writes against the real test database."""
import inspect
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row

from models import data
from models.data import ALL
from models.errors import InvalidInput, NotFound, NotPresent
from models.fields import ATTENDANCE_FLAG_COLUMNS
from models.utils import get_today
from tests.conftest import (BOOK_SECTIONS, D_0909, D_0916, D_0923, D_0930, D_1007, TODAY, add_attendance,
                            add_section)

LEADER_A = {'contact_id': 7001, 'name': 'Leader Alpha'}
LEADER_B = {'contact_id': 7002, 'name': 'Leader Beta'}
NOT_SCHEDULED = date(2026, 9, 24)


def _att(conn, cid, d):
    return conn.execute('SELECT * FROM attendance WHERE contact_id = %s AND meeting_date = %s',
                        (cid, d)).fetchone()


def _sections(conn, cid=None):
    if cid is None:
        return conn.execute('SELECT * FROM completed_sections ORDER BY id').fetchall()
    return conn.execute('SELECT * FROM completed_sections WHERE contact_id = %s ORDER BY id', (cid,)).fetchall()


def _add_kid(conn, cid, first, last, team=None, *, nickname=None, active=True):
    conn.execute("""INSERT INTO kids (contact_id, participant_id, nickname, first_name, last_name, team, active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""", (cid, cid + 10000, nickname, first, last, team, active))


def _names(rows):
    return [r['name'] for r in rows]


def _ids(rows):
    return [r['id'] for r in rows]


def _by_cid(rows, cid):
    return next(r for r in rows if r['contact_id'] == cid)


# =============================================================================
# Reference data
# =============================================================================

def test_get_schedule_ordered_by_date_with_real_types(seeded):
    """Rows come back in date order with date objects and the sections array as a list."""
    seeded.execute("INSERT INTO schedule (meeting_date, theme) VALUES ('2026-09-02', 'Kickoff')")
    rows = data.get_schedule(conn=seeded)
    assert [r['meeting_date'] for r in rows] == [date(2026, 9, 2), D_0909, D_0916, D_0923, D_0930, D_1007]
    assert all(type(r['meeting_date']) is date for r in rows)
    assert rows[0]['sections'] == []
    assert rows[1]['sections'] == ['Start Zone 1', 'Start Zone 2']
    assert rows[1]['theme'] == 'Welcome!'
    assert set(rows[1]) == {'meeting_date', 'theme', 'sections', 'no_club', 'sequence', 'unit', 'scope',
                            'key_verses', 'details'}


def test_get_schedule_no_club_only_for_no_awana(seeded):
    rows = {r['meeting_date']: r['no_club'] for r in data.get_schedule(conn=seeded)}
    assert rows == {D_0909: False, D_0916: False, D_0923: False, D_0930: False, D_1007: True}


@pytest.mark.parametrize('theme, expected', [
    ('NO AWANA - Fall Break', True),
    ('No Awana tonight', True),
    ('Thanksgiving: no awana', True),
    ('Regular Meeting', False),
    ('Awana Games Night', False),
    ('', False),
])
def test_no_club_is_generated_from_theme(conn, theme, expected):
    conn.execute('INSERT INTO schedule (meeting_date, theme) VALUES (%s, %s)', (D_0909, theme))
    assert data.get_schedule_day(D_0909, conn=conn)['no_club'] is expected


def test_get_schedule_day(seeded):
    day = data.get_schedule_day(D_0923, conn=seeded)
    assert day['meeting_date'] == D_0923
    assert day['theme'] == 'Sports Team Night!'
    assert day['sections'] == ['1.2']
    assert day['no_club'] is False
    assert data.get_schedule_day(D_1007, conn=seeded)['no_club'] is True


def test_get_schedule_day_unknown_date_is_none(seeded):
    assert data.get_schedule_day(NOT_SCHEDULED, conn=seeded) is None


def test_get_schedule_empty(conn):
    assert data.get_schedule(conn=conn) == []


def test_get_book_sections_in_sort_order(seeded):
    """Sort order, not alphabetical ('Start Zone' sorts after '1.x' alphabetically)."""
    assert data.get_book_sections(conn=seeded) == BOOK_SECTIONS


def test_get_book_sections_ignores_insert_order(conn):
    for section, order in (('C', 1), ('A', 3), ('B', 2)):
        conn.execute('INSERT INTO book_sections (section, sort_order) VALUES (%s, %s)', (section, order))
    assert data.get_book_sections(conn=conn) == ['C', 'B', 'A']


def test_get_teams_in_sort_order(conn):
    teams = data.get_teams(conn=conn)
    assert [t['name'] for t in teams] == ['Red', 'Blue', 'Green', 'Yellow']
    assert all(t['emoji'] for t in teams)


def test_reads_default_to_the_request_connection(seeded, app_ctx):
    """With no conn= the data layer uses the app-context connection."""
    assert data.get_book_sections() == BOOK_SECTIONS
    assert data.ping() is True


# =============================================================================
# Kids
# =============================================================================

def test_get_kid_shape_and_name(seeded):
    kid = data.get_kid(contact_id=1001, conn=seeded)
    assert kid == {'contact_id': 1001, 'participant_id': 5001, 'group_participant_id': 9001,
                   'name': 'Alice Anders', 'nickname': None, 'first_name': 'Alice', 'last_name': 'Anders',
                   'team': 'Red', 'active': True}


def test_get_kid_name_prefers_nickname(seeded):
    kid = data.get_kid(contact_id=1002, conn=seeded)
    assert kid['name'] == 'Bo Brandt'
    assert kid['first_name'] == 'Robert'


@pytest.mark.parametrize('nickname, first, last, expected', [
    ('   ', 'Ivy', 'Ito', 'Ivy Ito'),          # blank nickname falls back to first name
    ('', 'Ivy', 'Ito', 'Ivy Ito'),
    (' Ives ', ' Ivy ', ' Ito ', 'Ives Ito'),   # every part is trimmed
    (None, '  Ivy', 'Ito  ', 'Ivy Ito'),
])
def test_kid_name_is_generated(seeded, nickname, first, last, expected):
    _add_kid(seeded, 1100, first, last, 'Red', nickname=nickname)
    assert data.get_kid(contact_id=1100, conn=seeded)['name'] == expected


def test_get_kid_name_follows_nickname_changes(seeded):
    seeded.execute("UPDATE kids SET nickname = 'Ally' WHERE contact_id = 1001")
    assert data.get_kid(contact_id=1001, conn=seeded)['name'] == 'Ally Anders'


def test_get_kid_includes_inactive_and_unknown_is_none(seeded):
    assert data.get_kid(contact_id=1008, conn=seeded)['active'] is False
    assert data.get_kid(contact_id=1007, conn=seeded)['team'] is None
    assert data.get_kid(contact_id=424242, conn=seeded) is None


def test_get_roster_all_active_sorted_by_last_name(seeded):
    assert _names(data.get_roster(conn=seeded)) == ['Alice Anders', 'Bo Brandt', 'Cara Cole', 'Dev Dunn',
                                                    'Eli Eames', 'Fay Ford', 'Gus Gray']
    assert data.get_roster(ALL, conn=seeded) == data.get_roster(conn=seeded)


def test_get_roster_include_inactive(seeded):
    assert _names(data.get_roster(include_inactive=True, conn=seeded))[-1] == 'Hal Hart'
    assert len(data.get_roster(include_inactive=True, conn=seeded)) == 8


def test_get_roster_team_filter(seeded):
    assert _names(data.get_roster('Red', conn=seeded)) == ['Alice Anders', 'Bo Brandt']
    assert _names(data.get_roster('Red', include_inactive=True, conn=seeded)) == ['Alice Anders', 'Bo Brandt',
                                                                                  'Hal Hart']
    assert _names(data.get_roster('Blue', conn=seeded)) == ['Cara Cole', 'Dev Dunn']
    assert data.get_roster('Purple', conn=seeded) == []


def test_get_roster_none_means_unassigned(seeded):
    assert _names(data.get_roster(None, conn=seeded)) == ['Gus Gray']
    _add_kid(seeded, 1100, 'Ivy', 'Ito', None, active=False)
    assert _names(data.get_roster(None, conn=seeded)) == ['Gus Gray']
    assert _names(data.get_roster(None, include_inactive=True, conn=seeded)) == ['Gus Gray', 'Ivy Ito']


def test_get_roster_ties_on_last_name_sort_by_name(seeded):
    _add_kid(seeded, 1100, 'Zoe', 'Anders', 'Red')
    _add_kid(seeded, 1101, 'Abe', 'Anders', 'Red')
    assert _names(data.get_roster('Red', conn=seeded)) == ['Abe Anders', 'Alice Anders', 'Zoe Anders',
                                                           'Bo Brandt']


# =============================================================================
# Attendance board
# =============================================================================

def test_board_without_rows_uses_kids_team(seeded):
    assert _names(data.get_board(D_0923, 'Red', conn=seeded)) == ['Alice Anders', 'Bo Brandt']
    assert _names(data.get_board(D_0923, 'Blue', conn=seeded)) == ['Cara Cole', 'Dev Dunn']
    assert _names(data.get_board(D_0923, None, conn=seeded)) == ['Gus Gray']
    assert data.get_board(D_0923, 'Purple', conn=seeded) == []


def test_board_row_team_snapshot_wins_for_that_date(seeded):
    """A kid is listed under the team on that date's attendance row, even when it is NULL."""
    add_attendance(seeded, 1003, D_0923, team='Red', mp_checked_in=True)   # Blue kid, Red that night
    add_attendance(seeded, 1001, D_0923, team=None)                        # Red kid, Unassigned that night
    assert _names(data.get_board(D_0923, 'Red', conn=seeded)) == ['Bo Brandt', 'Cara Cole']
    assert _names(data.get_board(D_0923, 'Blue', conn=seeded)) == ['Dev Dunn']
    assert _names(data.get_board(D_0923, None, conn=seeded)) == ['Alice Anders', 'Gus Gray']
    cara = _by_cid(data.get_board(D_0923, 'Red', conn=seeded), 1003)
    assert (cara['team'], cara['kid_team']) == ('Red', 'Blue')
    alice = _by_cid(data.get_board(D_0923, None, conn=seeded), 1001)
    assert (alice['team'], alice['kid_team']) == (None, 'Red')


def test_board_snapshot_only_affects_its_own_date(seeded):
    add_attendance(seeded, 1003, D_0923, team='Red')
    assert _names(data.get_board(D_0916, 'Red', conn=seeded)) == ['Alice Anders', 'Bo Brandt']
    assert _names(data.get_board(D_0916, 'Blue', conn=seeded)) == ['Cara Cole', 'Dev Dunn']


def test_board_inactive_kid_only_with_a_row(seeded):
    assert 'Hal Hart' not in _names(data.get_board(D_0916, 'Red', conn=seeded))
    add_attendance(seeded, 1008, D_0916, mp_checked_in=True)
    assert _names(data.get_board(D_0916, 'Red', conn=seeded)) == ['Alice Anders', 'Bo Brandt', 'Hal Hart']
    assert 'Hal Hart' not in _names(data.get_board(D_0923, 'Red', conn=seeded))
    add_attendance(seeded, 1008, D_0923, team=None)
    assert _names(data.get_board(D_0923, None, conn=seeded)) == ['Gus Gray', 'Hal Hart']


def test_board_sorted_by_last_name_then_name(seeded):
    _add_kid(seeded, 1100, 'Zed', 'Aaron', 'Red')
    _add_kid(seeded, 1101, 'Abe', 'Anders', 'Blue')
    add_attendance(seeded, 1101, D_0923, team='Red')
    add_attendance(seeded, 1003, D_0923, team='Red')
    assert _names(data.get_board(D_0923, 'Red', conn=seeded)) == ['Zed Aaron', 'Abe Anders', 'Alice Anders',
                                                                  'Bo Brandt', 'Cara Cole']


def test_board_all_teams(seeded):
    add_attendance(seeded, 1008, D_0923)
    assert len(data.get_board(D_0923, ALL, conn=seeded)) == 8


def test_board_row_defaults_without_attendance_row(seeded):
    row = _by_cid(data.get_board(D_0923, 'Red', conn=seeded), 1001)
    assert row['attendance_id'] is None
    assert row['present'] is False
    assert row['mp_checked_in'] is False
    assert row['manual_present'] is False
    assert row['source'] is None
    assert row['mp_time'] is None and row['mp_time_in'] is None and row['mp_time_confirmed'] is None
    assert row['manual_present_by'] is None and row['manual_present_at'] is None
    assert all(row[c] is False for c in ATTENDANCE_FLAG_COLUMNS)
    assert row['team'] == row['kid_team'] == 'Red'
    assert row['name'] == 'Alice Anders' and row['active'] is True


def test_board_row_mp_checkin(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, has_bible=True, has_dues=True)
    row = _by_cid(data.get_board(D_0923, 'Red', conn=seeded), 1001)
    assert row['attendance_id'] is not None
    assert row['present'] is True and row['mp_checked_in'] is True
    assert row['source'] == 'mp'
    assert row['mp_time'] == row['mp_time_in'] == datetime(2026, 9, 23, 23, 12, tzinfo=timezone.utc)
    assert {c: row[c] for c in ATTENDANCE_FLAG_COLUMNS} == {
        'has_bible': True, 'wearing_shirt': False, 'has_book': False, 'did_homework': False, 'has_dues': True}


def test_board_row_mp_time_prefers_confirmed(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    confirmed = datetime(2026, 9, 23, 23, 30, tzinfo=timezone.utc)
    seeded.execute('UPDATE attendance SET mp_time_confirmed = %s WHERE contact_id = 1001', (confirmed,))
    row = _by_cid(data.get_board(D_0923, 'Red', conn=seeded), 1001)
    assert row['mp_time'] == confirmed


def test_board_row_source_manual_and_mp_wins(seeded):
    add_attendance(seeded, 1001, D_0923, manual_present=True)
    add_attendance(seeded, 1002, D_0923, mp_checked_in=True, manual_present=True)
    board = data.get_board(D_0923, 'Red', conn=seeded)
    alice, bo = _by_cid(board, 1001), _by_cid(board, 1002)
    assert alice['present'] is True and alice['source'] == 'manual' and alice['mp_time'] is None
    assert bo['present'] is True and bo['source'] == 'mp'


def test_board_row_not_present_legacy_flags(seeded):
    """A row can carry flags without Present (imported sheet data); present/source stay off."""
    add_attendance(seeded, 1001, D_0923, has_book=True)
    row = _by_cid(data.get_board(D_0923, 'Red', conn=seeded), 1001)
    assert row['attendance_id'] is not None
    assert row['present'] is False and row['source'] is None
    assert row['has_book'] is True


def test_board_counts_default_membership(seeded):
    assert data.get_board_counts(D_0930, conn=seeded) == {
        'Red': {'here': 0, 'total': 2}, 'Blue': {'here': 0, 'total': 2}, 'Green': {'here': 0, 'total': 1},
        'Yellow': {'here': 0, 'total': 1}, None: {'here': 0, 'total': 1}}


def test_board_counts_follow_board_membership_rule(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    add_attendance(seeded, 1003, D_0923, team='Red')                   # Blue kid on Red tonight, not here
    add_attendance(seeded, 1007, D_0923, team=None, mp_checked_in=True)
    add_attendance(seeded, 1008, D_0923, manual_present=True)          # inactive kid with a row
    add_attendance(seeded, 1005, D_0916, mp_checked_in=True)           # other date: ignored
    counts = data.get_board_counts(D_0923, conn=seeded)
    assert counts == {
        'Red': {'here': 2, 'total': 4}, 'Blue': {'here': 0, 'total': 1}, 'Green': {'here': 0, 'total': 1},
        'Yellow': {'here': 0, 'total': 1}, None: {'here': 1, 'total': 1}}
    for team, c in counts.items():
        board = data.get_board(D_0923, team, conn=seeded)
        assert c == {'here': sum(r['present'] for r in board), 'total': len(board)}


def test_board_counts_null_snapshot_goes_to_none_key(seeded):
    add_attendance(seeded, 1001, D_0923, team=None, manual_present=True)
    counts = data.get_board_counts(D_0923, conn=seeded)
    assert counts['Red'] == {'here': 0, 'total': 1}
    assert counts[None] == {'here': 1, 'total': 2}


def test_get_attendance_with_row(seeded):
    add_attendance(seeded, 1003, D_0923, team='Red', mp_checked_in=True, has_dues=True)
    row = data.get_attendance(contact_id=1003, meeting_date=D_0923, conn=seeded)
    assert row['contact_id'] == 1003 and row['name'] == 'Cara Cole'
    assert row['attendance_id'] is not None
    assert row['present'] is True and row['source'] == 'mp' and row['mp_time'] is not None
    assert row['has_dues'] is True and row['has_bible'] is False
    assert (row['team'], row['kid_team']) == ('Red', 'Blue')


def test_get_attendance_without_row_returns_defaults(seeded):
    add_attendance(seeded, 1002, D_0916, mp_checked_in=True)   # other date doesn't leak in
    row = data.get_attendance(contact_id=1002, meeting_date=D_0923, conn=seeded)
    assert row is not None
    assert row['contact_id'] == 1002 and row['name'] == 'Bo Brandt'
    assert row['attendance_id'] is None
    assert row['present'] is False and row['source'] is None and row['mp_time'] is None
    assert all(row[c] is False for c in ATTENDANCE_FLAG_COLUMNS)
    assert row['team'] == 'Red'


def test_get_attendance_inactive_kid_without_row(seeded):
    row = data.get_attendance(contact_id=1008, meeting_date=D_0923, conn=seeded)
    assert row is not None and row['active'] is False and row['attendance_id'] is None


def test_get_attendance_unknown_kid_is_none(seeded):
    assert data.get_attendance(contact_id=424242, meeting_date=D_0923, conn=seeded) is None


def test_get_attendance_matches_board_row(seeded):
    add_attendance(seeded, 1001, D_0923, manual_present=True, wearing_shirt=True)
    board_row = _by_cid(data.get_board(D_0923, 'Red', conn=seeded), 1001)
    assert data.get_attendance(contact_id=1001, meeting_date=D_0923, conn=seeded) == board_row


# =============================================================================
# set_attendance
# =============================================================================

def test_set_attendance_manual_present_creates_row(seeded):
    row = data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': True},
                              by=LEADER_A, conn=seeded)
    assert row['present'] is True and row['source'] == 'manual'
    assert row['contact_id'] == 1003 and row['attendance_id'] is not None
    raw = _att(seeded, 1003, D_0923)
    assert raw['manual_present'] is True and raw['mp_checked_in'] is False
    assert raw['source'] == 'app'
    assert raw['team'] == 'Blue'
    assert raw['updated_by_contact_id'] == 7001
    assert all(raw[c] is False for c in ATTENDANCE_FLAG_COLUMNS)


def test_set_attendance_returns_current_board_row(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    row = data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'has_book': True}, conn=seeded)
    assert row == data.get_attendance(contact_id=1001, meeting_date=D_0923, conn=seeded)
    assert row['has_book'] is True and row['source'] == 'mp'


def test_set_attendance_sequential_flags_both_kept(seeded):
    """Each call upserts only its own columns, so earlier taps survive."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'has_bible': True}, conn=seeded)
    data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'has_book': True}, conn=seeded)
    raw = _att(seeded, 1001, D_0923)
    assert raw['has_bible'] is True and raw['has_book'] is True
    assert raw['wearing_shirt'] is False and raw['did_homework'] is False and raw['has_dues'] is False


def test_set_attendance_second_leader_does_not_clobber(seeded):
    add_attendance(seeded, 1002, D_0923, mp_checked_in=True)
    data.set_attendance(contact_id=1002, meeting_date=D_0923, fields={'has_bible': True, 'has_dues': True},
                        by=LEADER_A, conn=seeded)
    data.set_attendance(contact_id=1002, meeting_date=D_0923, fields={'wearing_shirt': True},
                        by=LEADER_B, conn=seeded)
    raw = _att(seeded, 1002, D_0923)
    assert (raw['has_bible'], raw['has_dues'], raw['wearing_shirt']) == (True, True, True)
    assert raw['updated_by_contact_id'] == 7002


def test_set_attendance_same_field_last_write_wins(seeded):
    add_attendance(seeded, 1002, D_0923, mp_checked_in=True)
    data.set_attendance(contact_id=1002, meeting_date=D_0923, fields={'has_bible': True}, by=LEADER_A,
                        conn=seeded)
    data.set_attendance(contact_id=1002, meeting_date=D_0923, fields={'has_bible': False}, by=LEADER_B,
                        conn=seeded)
    assert _att(seeded, 1002, D_0923)['has_bible'] is False


def test_set_attendance_concurrent_leaders_both_land(seeded, database):
    """Leader B's tap waits on leader A's open write and then merges into it instead of overwriting it."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    errors = []
    with psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn_a, \
            psycopg.connect(database, autocommit=True, row_factory=dict_row) as conn_b:

        def leader_b():
            try:
                data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'wearing_shirt': True},
                                    by=LEADER_B, conn=conn_b)
            except Exception as e:  # noqa: BLE001 - surfaced by the assert below
                errors.append(e)

        with conn_a.transaction():
            data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'has_bible': True},
                                by=LEADER_A, conn=conn_a)
            thread = threading.Thread(target=leader_b)
            thread.start()
            thread.join(0.5)
            assert thread.is_alive(), 'leader B should wait on the row lock'
        thread.join(10)
    assert not thread.is_alive()
    assert errors == []
    raw = _att(seeded, 1001, D_0923)
    assert raw['has_bible'] is True and raw['wearing_shirt'] is True
    assert raw['updated_by_contact_id'] == 7002


def test_set_attendance_flag_without_present_raises_and_rolls_back(seeded):
    with pytest.raises(NotPresent) as exc:
        data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'has_bible': True}, conn=seeded)
    assert exc.value.code == 'not_present'
    assert _att(seeded, 1003, D_0923) is None


def test_set_attendance_flag_on_existing_absent_row_raises_and_rolls_back(seeded):
    add_attendance(seeded, 1003, D_0923)
    before = _att(seeded, 1003, D_0923)
    with pytest.raises(NotPresent):
        data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'has_bible': True, 'has_book': True},
                            by=LEADER_A, conn=seeded)
    assert _att(seeded, 1003, D_0923) == before


def test_set_attendance_unmark_present_with_new_flag_rolls_back_both(seeded):
    add_attendance(seeded, 1002, D_0923, manual_present=True)
    with pytest.raises(NotPresent):
        data.set_attendance(contact_id=1002, meeting_date=D_0923,
                            fields={'manual_present': False, 'has_bible': True}, conn=seeded)
    raw = _att(seeded, 1002, D_0923)
    assert raw['manual_present'] is True and raw['has_bible'] is False


def test_set_attendance_mark_present_and_flags_in_one_call(seeded):
    row = data.set_attendance(contact_id=1003, meeting_date=D_0923,
                              fields={'manual_present': True, 'has_bible': True, 'has_book': True},
                              by=LEADER_A, conn=seeded)
    assert row['present'] is True
    assert row['has_bible'] is True and row['has_book'] is True and row['has_dues'] is False


def test_set_attendance_flag_allowed_for_mp_checked_in_kid(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    row = data.set_attendance(contact_id=1001, meeting_date=D_0923,
                              fields={c: True for c in ATTENDANCE_FLAG_COLUMNS}, conn=seeded)
    assert all(row[c] is True for c in ATTENDANCE_FLAG_COLUMNS)


def test_set_attendance_false_flag_does_not_need_present(seeded):
    row = data.set_attendance(contact_id=1004, meeting_date=D_0923, fields={'has_bible': False}, conn=seeded)
    assert row['present'] is False and row['has_bible'] is False


def test_set_attendance_legacy_flags_can_be_resent_or_cleared(seeded):
    """Imported rows may carry flags without Present; re-sending or clearing them is not a new set."""
    add_attendance(seeded, 1004, D_0916, has_bible=True, has_dues=True)
    row = data.set_attendance(contact_id=1004, meeting_date=D_0916, fields={'has_bible': True, 'has_dues': True},
                              conn=seeded)
    assert row['has_bible'] is True and row['present'] is False
    row = data.set_attendance(contact_id=1004, meeting_date=D_0916, fields={'has_bible': False}, conn=seeded)
    assert row['has_bible'] is False and row['has_dues'] is True
    with pytest.raises(NotPresent):
        data.set_attendance(contact_id=1004, meeting_date=D_0916, fields={'has_book': True}, conn=seeded)
    with pytest.raises(NotPresent):   # cleared a moment ago, so setting it again is new
        data.set_attendance(contact_id=1004, meeting_date=D_0916, fields={'has_bible': True}, conn=seeded)
    raw = _att(seeded, 1004, D_0916)
    assert (raw['has_bible'], raw['has_book'], raw['has_dues']) == (False, False, True)


def test_set_attendance_manual_present_records_by_and_at(seeded):
    before = seeded.execute('SELECT now() AS t').fetchone()['t']
    row = data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': True},
                              by=LEADER_A, conn=seeded)
    assert row['manual_present_by'] == 'Leader Alpha'
    raw = _att(seeded, 1003, D_0923)
    assert raw['manual_present_by'] == 'Leader Alpha'
    assert raw['manual_present_at'] is not None and raw['manual_present_at'] >= before
    assert row['manual_present_at'] == raw['manual_present_at']


def test_set_attendance_manual_present_false_clears_by_and_at(seeded):
    data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': True}, by=LEADER_A,
                        conn=seeded)
    row = data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': False},
                              by=LEADER_B, conn=seeded)
    assert row['present'] is False and row['source'] is None
    raw = _att(seeded, 1003, D_0923)
    assert raw['manual_present'] is False
    assert raw['manual_present_by'] is None and raw['manual_present_at'] is None
    assert raw['updated_by_contact_id'] == 7002


def test_set_attendance_manual_present_false_on_new_row_leaves_by_and_at_empty(seeded):
    data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': False}, by=LEADER_A,
                        conn=seeded)
    raw = _att(seeded, 1003, D_0923)
    assert raw['manual_present'] is False
    assert raw['manual_present_by'] is None and raw['manual_present_at'] is None


def test_set_attendance_flag_write_keeps_manual_present_by(seeded):
    data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': True}, by=LEADER_A,
                        conn=seeded)
    marked = _att(seeded, 1003, D_0923)
    data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'has_bible': True}, by=LEADER_B,
                        conn=seeded)
    raw = _att(seeded, 1003, D_0923)
    assert raw['manual_present_by'] == 'Leader Alpha'
    assert raw['manual_present_at'] == marked['manual_present_at']
    assert raw['updated_by_contact_id'] == 7002


def test_set_attendance_unmark_manual_keeps_mp_present(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, manual_present=True)
    row = data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'manual_present': False},
                              conn=seeded)
    assert row['present'] is True and row['source'] == 'mp'


def test_set_attendance_leaves_mp_columns_and_source_alone(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, source='mp_sync')
    before = _att(seeded, 1001, D_0923)
    data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'has_bible': True}, by=LEADER_A,
                        conn=seeded)
    raw = _att(seeded, 1001, D_0923)
    for col in ('source', 'mp_checked_in', 'mp_time_in', 'mp_time_confirmed', 'mp_event_participant_id',
                'mp_synced_at', 'manual_present', 'created_at', 'team', 'id'):
        assert raw[col] == before[col], col
    assert raw['updated_at'] > before['updated_at']


@pytest.mark.parametrize('by, expected', [
    (LEADER_A, 7001),
    (SimpleNamespace(contact_id=7003, name='Leader Gamma'), 7003),
    (None, None),
])
def test_set_attendance_records_updated_by(seeded, by, expected):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    seeded.execute('UPDATE attendance SET updated_by_contact_id = 1 WHERE contact_id = 1001')
    data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'has_bible': True}, by=by, conn=seeded)
    assert _att(seeded, 1001, D_0923)['updated_by_contact_id'] == expected


def test_set_attendance_manual_present_by_accepts_object(seeded):
    data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': True},
                        by=SimpleNamespace(contact_id=7003, name='Leader Gamma'), conn=seeded)
    raw = _att(seeded, 1003, D_0923)
    assert raw['manual_present_by'] == 'Leader Gamma' and raw['updated_by_contact_id'] == 7003


def test_set_attendance_unknown_kid_not_found(seeded):
    with pytest.raises(NotFound):
        data.set_attendance(contact_id=424242, meeting_date=D_0923, fields={'manual_present': True},
                            conn=seeded)
    with pytest.raises(NotFound):   # NotFound, not NotPresent
        data.set_attendance(contact_id=424242, meeting_date=D_0923, fields={'has_bible': True}, conn=seeded)
    assert seeded.execute('SELECT count(*) AS n FROM attendance').fetchone()['n'] == 0


def test_set_attendance_date_not_in_schedule_invalid(seeded):
    with pytest.raises(InvalidInput):
        data.set_attendance(contact_id=1001, meeting_date=NOT_SCHEDULED, fields={'manual_present': True},
                            conn=seeded)
    assert seeded.execute('SELECT count(*) AS n FROM attendance').fetchone()['n'] == 0


@pytest.mark.parametrize('fields', [
    {},
    {'present': True},
    {'mp_checked_in': True},
    {'mp_time_in': True},
    {'manual_present_by': True},
    {'team': True},
    {'source': True},
    {'Has Bible': True},
    {'has_bible': 1},
    {'has_bible': 'true'},
    {'has_bible': None},
    {'manual_present': 0},
    {'has_bible': True, 'bogus': True},
])
def test_set_attendance_rejects_bad_fields(seeded, fields):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    before = _att(seeded, 1001, D_0923)
    with pytest.raises(InvalidInput):
        data.set_attendance(contact_id=1001, meeting_date=D_0923, fields=fields, conn=seeded)
    assert _att(seeded, 1001, D_0923) == before
    with pytest.raises(InvalidInput):
        data.set_attendance(contact_id=1003, meeting_date=D_0923, fields=fields, conn=seeded)
    assert _att(seeded, 1003, D_0923) is None


def test_set_attendance_team_snapshot_taken_on_insert_only(seeded):
    data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'manual_present': True}, conn=seeded)
    assert _att(seeded, 1001, D_0923)['team'] == 'Red'
    seeded.execute("UPDATE kids SET team = 'Blue' WHERE contact_id = 1001")
    data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'has_bible': True}, conn=seeded)
    data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'manual_present': False}, conn=seeded)
    assert _att(seeded, 1001, D_0923)['team'] == 'Red'
    data.set_attendance(contact_id=1001, meeting_date=D_0916, fields={'manual_present': True}, conn=seeded)
    assert _att(seeded, 1001, D_0916)['team'] == 'Blue'


def test_set_attendance_unassigned_snapshot_stays_null(seeded):
    data.set_attendance(contact_id=1007, meeting_date=D_0923, fields={'manual_present': True}, conn=seeded)
    assert _att(seeded, 1007, D_0923)['team'] is None
    seeded.execute("UPDATE kids SET team = 'Green' WHERE contact_id = 1007")
    data.set_attendance(contact_id=1007, meeting_date=D_0923, fields={'has_bible': True}, conn=seeded)
    assert _att(seeded, 1007, D_0923)['team'] is None


def test_set_attendance_existing_row_snapshot_not_overwritten(seeded):
    add_attendance(seeded, 1003, D_0923, team='Red', mp_checked_in=True)
    data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'has_dues': True}, conn=seeded)
    assert _att(seeded, 1003, D_0923)['team'] == 'Red'


# =============================================================================
# upsert_completed_section (form path)
# =============================================================================

def test_upsert_completed_section_inserts(seeded):
    entry = data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                          credits={'section_complete': True}, by=LEADER_A, conn=seeded)
    assert {k: entry[k] for k in ('contact_id', 'name', 'meeting_date', 'section', 'team', 'section_complete',
                                  'silver_credit', 'gold_credit')} == {
        'contact_id': 1001, 'name': 'Alice Anders', 'meeting_date': D_0923, 'section': '1.2', 'team': 'Red',
        'section_complete': True, 'silver_credit': False, 'gold_credit': False}
    [raw] = _sections(seeded, 1001)
    assert raw['id'] == entry['id']
    assert raw['source'] == 'app' and raw['updated_by_contact_id'] == 7001


def test_upsert_completed_section_or_merge_never_downgrades(seeded):
    first = data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                          credits={'section_complete': True, 'silver_credit': True},
                                          conn=seeded)
    second = data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                           credits={'section_complete': False, 'silver_credit': False,
                                                    'gold_credit': False}, conn=seeded)
    assert second['id'] == first['id']
    assert (second['section_complete'], second['silver_credit'], second['gold_credit']) == (True, True, False)
    third = data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                          credits={'gold_credit': True}, conn=seeded)
    assert (third['section_complete'], third['silver_credit'], third['gold_credit']) == (True, True, True)
    assert len(_sections(seeded, 1001)) == 1


def test_upsert_completed_section_merges_into_existing_row(seeded):
    entry_id = add_section(seeded, 1001, D_0923, '1.2', silver=True)
    entry = data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                          credits={'section_complete': True}, by=LEADER_B, conn=seeded)
    assert entry['id'] == entry_id
    assert (entry['section_complete'], entry['silver_credit'], entry['gold_credit']) == (True, True, False)
    assert _sections(seeded, 1001)[0]['updated_by_contact_id'] == 7002


def test_upsert_completed_section_separate_rows_per_date_and_section(seeded):
    kw = dict(contact_id=1001, credits={'section_complete': True}, conn=seeded)
    a = data.upsert_completed_section(meeting_date=D_0916, section='1.1', **kw)
    b = data.upsert_completed_section(meeting_date=D_0923, section='1.1', **kw)
    c = data.upsert_completed_section(meeting_date=D_0923, section='1.2', **kw)
    assert len({a['id'], b['id'], c['id']}) == 3


def test_upsert_completed_section_team_from_same_date_attendance(seeded):
    add_attendance(seeded, 1003, D_0923, team='Red', mp_checked_in=True)   # Blue kid on Red tonight
    entry = data.upsert_completed_section(contact_id=1003, meeting_date=D_0923, section='1.2',
                                          credits={'section_complete': True}, conn=seeded)
    assert entry['team'] == 'Red'


def test_upsert_completed_section_team_falls_back_to_kids_team(seeded):
    add_attendance(seeded, 1003, D_0916, team='Red')   # a different date doesn't count
    entry = data.upsert_completed_section(contact_id=1003, meeting_date=D_0923, section='1.2',
                                          credits={'section_complete': True}, conn=seeded)
    assert entry['team'] == 'Blue'
    unassigned = data.upsert_completed_section(contact_id=1007, meeting_date=D_0923, section='1.2',
                                               credits={'section_complete': True}, conn=seeded)
    assert unassigned['team'] is None


def test_upsert_completed_section_team_snapshot_kept_on_merge(seeded):
    data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                  credits={'section_complete': True}, conn=seeded)
    seeded.execute("UPDATE kids SET team = 'Green' WHERE contact_id = 1001")
    entry = data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                          credits={'silver_credit': True}, conn=seeded)
    assert entry['team'] == 'Red'


def test_upsert_completed_section_unknown_section_invalid(seeded):
    with pytest.raises(InvalidInput):
        data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='9.9',
                                      credits={'section_complete': True}, conn=seeded)
    assert _sections(seeded) == []


def test_upsert_completed_section_unknown_date_invalid(seeded):
    with pytest.raises(InvalidInput):
        data.upsert_completed_section(contact_id=1001, meeting_date=NOT_SCHEDULED, section='1.2',
                                      credits={'section_complete': True}, conn=seeded)
    assert _sections(seeded) == []


def test_upsert_completed_section_unknown_kid_not_found(seeded):
    with pytest.raises(NotFound):
        data.upsert_completed_section(contact_id=424242, meeting_date=D_0923, section='1.2',
                                      credits={'section_complete': True}, conn=seeded)
    assert _sections(seeded) == []


@pytest.mark.parametrize('credits', [
    {}, {'section': True}, {'team': True}, {'Silver Credit': True}, {'silver_credit': 'TRUE'},
    {'gold_credit': 1}, {'section_complete': None}, {'section_complete': True, 'has_bible': True},
])
def test_upsert_completed_section_rejects_bad_credits(seeded, credits):
    with pytest.raises(InvalidInput):
        data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2', credits=credits,
                                      conn=seeded)
    assert _sections(seeded) == []


# =============================================================================
# set_section_credit (chip path)
# =============================================================================

def test_set_section_credit_sets_exact_values_both_ways(seeded):
    kw = dict(contact_id=1001, meeting_date=D_0923, section='1.2', conn=seeded)
    first = data.set_section_credit(field='silver_credit', value=True, **kw)
    assert (first['section_complete'], first['silver_credit'], first['gold_credit']) == (False, True, False)
    assert first['team'] == 'Red' and first['name'] == 'Alice Anders'
    entry = data.set_section_credit(field='section_complete', value=True, **kw)
    assert (entry['section_complete'], entry['silver_credit'], entry['gold_credit']) == (True, True, False)
    entry = data.set_section_credit(field='silver_credit', value=False, **kw)
    assert (entry['section_complete'], entry['silver_credit'], entry['gold_credit']) == (True, False, False)
    entry = data.set_section_credit(field='section_complete', value=False, **kw)
    assert (entry['section_complete'], entry['silver_credit'], entry['gold_credit']) == (False, False, False)
    assert entry['id'] == first['id']


def test_set_section_credit_never_deletes_rows(seeded):
    kw = dict(contact_id=1001, meeting_date=D_0923, section='1.2', conn=seeded)
    entry = data.set_section_credit(field='gold_credit', value=True, **kw)
    data.set_section_credit(field='gold_credit', value=False, **kw)
    rows = _sections(seeded, 1001)
    assert _ids(rows) == [entry['id']]
    assert not (rows[0]['section_complete'] or rows[0]['silver_credit'] or rows[0]['gold_credit'])
    assert data.get_completed_sections(contact_id=1001, conn=seeded) == []
    assert _ids(data.get_completed_sections(contact_id=1001, include_empty=True, conn=seeded)) == [entry['id']]
    again = data.set_section_credit(field='gold_credit', value=True, **kw)
    assert again['id'] == entry['id'] and again['gold_credit'] is True


def test_set_section_credit_false_on_new_row_creates_empty_row(seeded):
    entry = data.set_section_credit(contact_id=1001, meeting_date=D_0923, section='1.2', field='gold_credit',
                                    value=False, conn=seeded)
    assert not (entry['section_complete'] or entry['silver_credit'] or entry['gold_credit'])
    assert len(_sections(seeded, 1001)) == 1


def test_set_section_credit_downgrades_form_row(seeded):
    """Unlike the form path, the chip path can un-toggle a credit."""
    data.upsert_completed_section(contact_id=1001, meeting_date=D_0923, section='1.2',
                                  credits={'section_complete': True, 'silver_credit': True}, conn=seeded)
    entry = data.set_section_credit(contact_id=1001, meeting_date=D_0923, section='1.2', field='silver_credit',
                                    value=False, conn=seeded)
    assert (entry['section_complete'], entry['silver_credit']) == (True, False)


def test_set_section_credit_team_and_updated_by(seeded):
    add_attendance(seeded, 1003, D_0923, team='Red', mp_checked_in=True)
    entry = data.set_section_credit(contact_id=1003, meeting_date=D_0923, section='1.2',
                                    field='section_complete', value=True, by=LEADER_A, conn=seeded)
    assert entry['team'] == 'Red'
    assert _sections(seeded, 1003)[0]['updated_by_contact_id'] == 7001
    assert _sections(seeded, 1003)[0]['source'] == 'app'
    seeded.execute("UPDATE kids SET team = 'Green' WHERE contact_id = 1003")
    seeded.execute("UPDATE attendance SET team = 'Yellow' WHERE contact_id = 1003")
    entry = data.set_section_credit(contact_id=1003, meeting_date=D_0923, section='1.2',
                                    field='silver_credit', value=True, by=LEADER_B, conn=seeded)
    assert entry['team'] == 'Red'
    assert _sections(seeded, 1003)[0]['updated_by_contact_id'] == 7002


def test_set_section_credit_team_falls_back_to_kids_team(seeded):
    entry = data.set_section_credit(contact_id=1005, meeting_date=D_0923, section='1.2',
                                    field='section_complete', value=True, conn=seeded)
    assert entry['team'] == 'Green'


@pytest.mark.parametrize('field, value', [
    ('team', True), ('section', True), ('id', True), ('has_bible', True), ('gold_credit; --', True),
    ('silver_credit', 1), ('silver_credit', 'false'), ('silver_credit', None),
])
def test_set_section_credit_rejects_bad_field_or_value(seeded, field, value):
    with pytest.raises(InvalidInput):
        data.set_section_credit(contact_id=1001, meeting_date=D_0923, section='1.2', field=field, value=value,
                                conn=seeded)
    assert _sections(seeded) == []


def test_set_section_credit_unknown_section_date_kid(seeded):
    with pytest.raises(InvalidInput):
        data.set_section_credit(contact_id=1001, meeting_date=D_0923, section='9.9', field='gold_credit',
                                value=True, conn=seeded)
    with pytest.raises(InvalidInput):
        data.set_section_credit(contact_id=1001, meeting_date=NOT_SCHEDULED, section='1.2', field='gold_credit',
                                value=True, conn=seeded)
    with pytest.raises(NotFound):
        data.set_section_credit(contact_id=424242, meeting_date=D_0923, section='1.2', field='gold_credit',
                                value=True, conn=seeded)
    assert _sections(seeded) == []


# =============================================================================
# update_completed_section (edit pages)
# =============================================================================

def test_update_completed_section_sets_exact_values(seeded):
    entry_id = add_section(seeded, 1001, D_0916, '1.1', complete=True, silver=True)
    entry = data.update_completed_section(entry_id=entry_id, credits={'silver_credit': False, 'gold_credit': True},
                                          by=LEADER_A, conn=seeded)
    assert entry['id'] == entry_id and entry['name'] == 'Alice Anders'
    assert (entry['section_complete'], entry['silver_credit'], entry['gold_credit']) == (True, False, True)
    raw = _sections(seeded, 1001)[0]
    assert raw['updated_by_contact_id'] == 7001
    assert (raw['meeting_date'], raw['section'], raw['team']) == (D_0916, '1.1', 'Red')


def test_update_completed_section_can_clear_everything(seeded):
    entry_id = add_section(seeded, 1001, D_0916, '1.1', complete=True, silver=True, gold=True)
    entry = data.update_completed_section(
        entry_id=entry_id, credits={'section_complete': False, 'silver_credit': False, 'gold_credit': False},
        conn=seeded)
    assert (entry['section_complete'], entry['silver_credit'], entry['gold_credit']) == (False, False, False)
    assert _ids(_sections(seeded, 1001)) == [entry_id]


def test_update_completed_section_leaves_other_rows_alone(seeded):
    target = add_section(seeded, 1001, D_0916, '1.1', complete=True)
    other = add_section(seeded, 1001, D_0923, '1.1', complete=True)
    data.update_completed_section(entry_id=target, credits={'section_complete': False}, conn=seeded)
    assert data.get_section_entry(other, conn=seeded)['section_complete'] is True


def test_update_completed_section_team_snapshot_unchanged(seeded):
    entry_id = add_section(seeded, 1001, D_0916, '1.1', complete=True)
    seeded.execute("UPDATE kids SET team = 'Blue' WHERE contact_id = 1001")
    entry = data.update_completed_section(entry_id=entry_id, credits={'gold_credit': True}, conn=seeded)
    assert entry['team'] == 'Red'


def test_update_completed_section_unknown_id_is_none(seeded):
    assert data.update_completed_section(entry_id=424242, credits={'gold_credit': True}, conn=seeded) is None
    assert _sections(seeded) == []


@pytest.mark.parametrize('credits', [{}, {'team': True}, {'id': True}, {'gold_credit': 'yes'}, {'gold_credit': 0}])
def test_update_completed_section_rejects_bad_credits(seeded, credits):
    entry_id = add_section(seeded, 1001, D_0916, '1.1', complete=True)
    before = _sections(seeded, 1001)
    with pytest.raises(InvalidInput):
        data.update_completed_section(entry_id=entry_id, credits=credits, conn=seeded)
    assert _sections(seeded, 1001) == before


def test_get_section_entry(seeded):
    entry_id = add_section(seeded, 1003, D_0916, '1.1', team='Red', silver=True)
    entry = data.get_section_entry(entry_id, conn=seeded)
    assert entry['contact_id'] == 1003 and entry['name'] == 'Cara Cole' and entry['team'] == 'Red'
    assert data.get_section_entry(424242, conn=seeded) is None


# =============================================================================
# Completed-section reads
# =============================================================================

@pytest.fixture
def sections(seeded):
    ids = {
        'f': None,
        'a': add_section(seeded, 1001, D_0916, '1.1', complete=True),
        'b': add_section(seeded, 1001, D_0923, '1.2', complete=True, silver=True),
        'c': add_section(seeded, 1003, D_0923, '1.2', gold=True),                     # Blue
        'd': add_section(seeded, 1007, D_0923, '1.2', team=None, complete=True),      # Unassigned
        'e': add_section(seeded, 1002, D_0909, 'Start Zone 1'),                       # all false
        'g': add_section(seeded, 1003, D_0916, '1.1', team='Red', silver=True),       # Blue kid, Red row
    }
    ids['f'] = add_section(seeded, 1001, D_0909, 'Start Zone 1', complete=True)       # inserted last, dated first
    return ids


def _pick(ids, keys):
    return [ids[k] for k in keys]


def test_get_completed_sections_order_and_hidden_empty_rows(seeded, sections):
    rows = data.get_completed_sections(conn=seeded)
    assert _ids(rows) == _pick(sections, 'fagbcd')
    assert _ids(data.get_completed_sections(team=ALL, conn=seeded)) == _ids(rows)
    assert _ids(data.get_completed_sections(include_empty=True, conn=seeded)) == _pick(sections, 'efagbcd')


def test_get_completed_sections_row_shape(seeded, sections):
    row = next(r for r in data.get_completed_sections(conn=seeded) if r['id'] == sections['b'])
    assert {k: row[k] for k in ('contact_id', 'name', 'meeting_date', 'section', 'team', 'section_complete',
                                'silver_credit', 'gold_credit')} == {
        'contact_id': 1001, 'name': 'Alice Anders', 'meeting_date': D_0923, 'section': '1.2', 'team': 'Red',
        'section_complete': True, 'silver_credit': True, 'gold_credit': False}
    assert row['updated_at'] is not None


@pytest.mark.parametrize('kw, expected', [
    ({'contact_id': 1001}, 'fab'),
    ({'contact_id': 1002}, ''),
    ({'contact_id': 1002, 'include_empty': True}, 'e'),
    ({'meeting_date': D_0923}, 'bcd'),
    ({'meeting_date': D_0930}, ''),
    ({'team': 'Red'}, 'fagb'),            # the row's team, not the kid's current team
    ({'team': 'Blue'}, 'c'),
    ({'team': None}, 'd'),
    ({'team': 'Red', 'include_empty': True}, 'efagb'),
    ({'section': '1.2'}, 'bcd'),
    ({'section': 'Start Zone 1', 'include_empty': True}, 'ef'),
    ({'meeting_date': D_0923, 'team': 'Red', 'section': '1.2'}, 'b'),
    ({'contact_id': 1003, 'team': 'Blue'}, 'c'),
])
def test_get_completed_sections_filters(seeded, sections, kw, expected):
    assert _ids(data.get_completed_sections(conn=seeded, **kw)) == _pick(sections, expected)


def test_get_section_history_first_date_per_credit(seeded):
    add_section(seeded, 1001, D_0923, '1.1', complete=True, silver=True)
    add_section(seeded, 1001, D_0916, '1.1', complete=True)      # earlier date inserted later still wins
    add_section(seeded, 1001, D_0930, '1.1', gold=True)
    add_section(seeded, 1001, D_0923, '1.2', silver=True)
    add_section(seeded, 1001, D_0909, 'Start Zone 1')            # all false: omitted
    add_section(seeded, 1002, D_0909, '1.1', complete=True, silver=True, gold=True)   # another kid
    assert data.get_section_history(contact_id=1001, conn=seeded) == {
        '1.1': {'main_date': D_0916, 'silver_date': D_0923, 'gold_date': D_0930},
        '1.2': {'main_date': None, 'silver_date': D_0923, 'gold_date': None},
    }


def test_get_section_history_ignores_cleared_credits(seeded):
    add_section(seeded, 1001, D_0916, '1.1', silver=True)
    add_section(seeded, 1001, D_0923, '1.1', complete=True)
    data.set_section_credit(contact_id=1001, meeting_date=D_0916, section='1.1', field='silver_credit',
                            value=False, conn=seeded)
    assert data.get_section_history(contact_id=1001, conn=seeded) == {
        '1.1': {'main_date': D_0923, 'silver_date': None, 'gold_date': None}}


def test_get_section_history_empty(seeded):
    assert data.get_section_history(contact_id=1001, conn=seeded) == {}
    add_section(seeded, 1001, D_0909, 'Start Zone 1')
    assert data.get_section_history(contact_id=1001, conn=seeded) == {}
    assert data.get_section_history(contact_id=424242, conn=seeded) == {}


def test_get_progress_summary_distinct_counts(seeded):
    add_section(seeded, 1001, D_0916, '1.1', complete=True, silver=True)
    add_section(seeded, 1001, D_0923, '1.1', complete=True, silver=True)   # same section again: counted once
    add_section(seeded, 1001, D_0923, '1.2', complete=True, gold=True)
    add_section(seeded, 1002, D_0923, '1.2', silver=True)
    add_section(seeded, 1003, D_0909, 'Start Zone 1')                      # all false: zero counts
    rows = data.get_progress_summary(conn=seeded)
    assert _names(rows) == ['Alice Anders', 'Bo Brandt', 'Cara Cole', 'Dev Dunn', 'Eli Eames', 'Fay Ford',
                            'Gus Gray']
    counts = {r['contact_id']: (r['total_sections'], r['total_silver'], r['total_gold']) for r in rows}
    assert counts == {1001: (2, 1, 1), 1002: (0, 1, 0), 1003: (0, 0, 0), 1004: (0, 0, 0), 1005: (0, 0, 0),
                      1006: (0, 0, 0), 1007: (0, 0, 0)}
    alice = _by_cid(rows, 1001)
    assert (alice['team'], alice['active']) == ('Red', True)


def test_get_progress_summary_inactive_kid_only_with_history(seeded):
    assert 1008 not in [r['contact_id'] for r in data.get_progress_summary(conn=seeded)]
    add_section(seeded, 1008, D_0909, 'Start Zone 1', complete=True)
    add_section(seeded, 1008, D_0916, '1.1', complete=True, gold=True)
    rows = data.get_progress_summary(conn=seeded)
    assert _names(rows)[-1] == 'Hal Hart'
    hal = _by_cid(rows, 1008)
    assert hal['active'] is False
    assert (hal['total_sections'], hal['total_silver'], hal['total_gold']) == (2, 0, 1)


# =============================================================================
# Misc
# =============================================================================

def test_is_writable_date(app_ctx):
    assert get_today() == TODAY
    assert data.is_writable_date(TODAY) is True
    assert data.is_writable_date(TODAY - timedelta(days=7)) is True
    assert data.is_writable_date(TODAY + timedelta(days=1)) is False
    assert data.is_writable_date(D_0930) is False


def test_data_layer_never_deletes():
    """The app's DB role has no DELETE (or TRUNCATE) right, so the data layer must never issue one."""
    source = inspect.getsource(data)
    assert 'DELETE' not in source
    assert 'TRUNCATE' not in source
