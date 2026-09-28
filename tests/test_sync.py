"""MP sync: roster + team assignment (models/roster.py), check-ins (models/checkins.py), bookkeeping (sync_state)."""
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import current_app

from models import checkins, mp, roster, sync_state
from models.errors import Conflict, InvalidInput, NotFound
from models.fields import ATTENDANCE_FLAG_COLUMNS, parse_team
from models.sync_state import Budget, BudgetExceeded, SyncStatus, Unlimited
from tests.conftest import (D_0909, D_0916, D_0923, D_0930, KIDS, TODAY, FakeMP, add_attendance, add_section,
                            mark_synced)

CHICAGO = mp.CHICAGO
TONIGHT_KEY = 'checkins:2026-09-23'
EVENT = [{'Event_ID': 7001, 'Event_Start_Date': '2026-09-23T18:30:00'}]
ACTIVE_PIDS = [pid for _, pid, _, _, _, _, _, active in KIDS if active]
BY = {'user_id': 42, 'contact_id': 3001}
GP_GUS = {'Group_Participant_ID': 9007, 'Group_ID': 697, 'Participant_ID': 5007, 'Notes': 'Created by CheckIn',
          'End_Date': None}
VOLUNTEER = dict(cid=2001, pid=6001, gpid=9101, first='Val', last='Volunteer', notes=None)


# -- helpers ---------------------------------------------------------------------

def gp(cid, pid, gpid, first, last, notes, *, nick=None, role=2, start='2025-08-20T00:00:00', end=None):
    """A Group_Participants row shaped like roster.ROSTER_SELECT."""
    return {'Group_Participant_ID': gpid, 'Participant_ID': pid, 'Group_Role_ID': role, 'Notes': notes,
            'Start_Date': start, 'End_Date': end, 'Contact_ID': cid, 'Nickname': nick,
            'First_Name': first, 'Last_Name': last}


def volunteer_row():
    return gp(*VOLUNTEER.values(), role=15)


def roster_rows(*extra, skip=()):
    """MP's view of the seeded roster (the 7 active kids), plus `extra` rows."""
    rows = [gp(cid, pid, gpid, first, last, team or 'Created by CheckIn', nick=nick)
            for cid, pid, gpid, nick, first, last, team, active in KIDS if active and cid not in skip]
    return rows + list(extra)


def ep(ep_id, pid, time_in='2026-09-23T18:31:02', *, status=3, confirmed=None, group_id=697, event_id=7001):
    """An Event_Participants row shaped like the check-ins select."""
    return {'Event_Participant_ID': ep_id, 'Event_ID': event_id, 'Participant_ID': pid,
            'Participation_Status_ID': status, 'Time_In': time_in, 'Time_Confirmed': confirmed,
            'Group_ID': group_id}


def mp_checkins(rows):
    """Event_Participants stand-in that applies the status / Time_In part of the filter the way MP would."""
    def get(**kw):
        return [r for r in rows if r['Participation_Status_ID'] in (3, 4) and r['Time_In']]
    return get


def raises(err):
    def get(**kw):
        raise err
    return get


def age(conn, key, seconds=3600):
    """Pretend `key` was last synced, and last attempted, `seconds` ago."""
    conn.execute("""UPDATE sync_state SET synced_at = now() - make_interval(secs => %(s)s),
                                          attempted_at = now() - make_interval(secs => %(s)s)
                     WHERE key = %(k)s""", {'s': seconds, 'k': key})


def called(fake, table=None):
    return [t for t, _ in fake.calls if table in (None, t)]


def call_kw(fake, table):
    return [kw for t, kw in fake.calls if t == table]


def kid(conn, cid):
    return conn.execute('SELECT * FROM kids WHERE contact_id = %s', (cid,)).fetchone()


def kids_table(conn):
    return conn.execute('SELECT * FROM kids ORDER BY contact_id').fetchall()


def att(conn, cid, d=D_0923):
    return conn.execute('SELECT * FROM attendance WHERE contact_id = %s AND meeting_date = %s', (cid, d)).fetchone()


def attendance_table(conn):
    return conn.execute('SELECT * FROM attendance ORDER BY id').fetchall()


def assignments(conn):
    return conn.execute('SELECT * FROM team_assignments ORDER BY id').fetchall()


def detail(conn, key=TONIGHT_KEY):
    return sync_state.read(conn, key)['detail']


def fetched(fake_mp, rows):
    """Run rows through fetch_roster to get upsert_roster's input shape."""
    fake_mp.tables['Group_Participants'] = rows
    return roster.fetch_roster()[0]


def now():
    return datetime.now(timezone.utc)


# -- parse_team --------------------------------------------------------------------

@pytest.mark.parametrize('notes, team', [
    ('Red', 'Red'), ('blue', 'Blue'), ('  GREEN ', 'Green'), ('Yellow', 'Yellow'),
    ('Created by CheckIn', None), ('', None), (None, None), ('Red team', None), ('Purple', None),
    ('Unassigned', None),
])
def test_parse_team(notes, team):
    assert parse_team(notes) == team


# -- roster query --------------------------------------------------------------------

def test_roster_filter_active_members():
    assert roster.roster_filter(697) == (
        'Group_Participants.Group_ID = 697 AND Group_Participants.Start_Date <= GETDATE() AND '
        '(Group_Participants.End_Date IS NULL OR Group_Participants.End_Date >= GETDATE())')


def test_roster_filter_since_includes_ended_rows():
    assert roster.roster_filter(697, since=date(2026, 8, 1)) == (
        "Group_Participants.Group_ID = 697 AND Group_Participants.Start_Date >= '2026-08-01'")


@pytest.mark.parametrize('group_id, since', [('697 OR 1=1', None), (0, None), (True, None), (None, None),
                                             (697, '2026-08-01')])
def test_roster_filter_rejects_unsafe_values(group_id, since):
    with pytest.raises(ValueError):
        roster.roster_filter(group_id, since=since)


def test_fetch_roster_query(fake_mp):
    roster.fetch_roster(timeout=(1, 2))
    [(table, kw)] = fake_mp.calls
    assert table == 'Group_Participants'
    assert kw['select'].split(',') == [
        'Group_Participants.Group_Participant_ID', 'Group_Participants.Participant_ID',
        'Group_Participants.Group_Role_ID', 'Group_Participants.Notes', 'Group_Participants.Start_Date',
        'Group_Participants.End_Date', 'Participant_ID_TABLE.Contact_ID',
        'Participant_ID_TABLE_Contact_ID_TABLE.Nickname', 'Participant_ID_TABLE_Contact_ID_TABLE.First_Name',
        'Participant_ID_TABLE_Contact_ID_TABLE.Last_Name']
    assert kw['filter'] == roster.roster_filter(697)
    assert kw['orderby'] == 'Group_Participants.Group_Participant_ID'
    assert kw['timeout'] == (1, 2)


def test_roster_group_id_from_app_config(app_ctx, fake_mp):
    current_app.config['MP_ROSTER_GROUP_ID'] = 698
    roster.fetch_roster()
    assert fake_mp.calls[0][1]['filter'].startswith('Group_Participants.Group_ID = 698 AND ')


def test_roster_group_id_from_env_outside_app(monkeypatch):
    monkeypatch.setenv('MP_ROSTER_GROUP_ID', '701')
    assert roster.roster_group_id() == 701
    monkeypatch.delenv('MP_ROSTER_GROUP_ID')
    assert roster.roster_group_id() == 697


# -- fetch_roster ------------------------------------------------------------------

def test_fetch_roster_keeps_kid_role_and_records_excluded(fake_mp):
    fake_mp.tables['Group_Participants'] = [
        gp(1001, 5001, 9001, 'Alice', 'Anders', 'Red'),
        volunteer_row(),
        gp(1003, 5003, 9003, ' Cara ', ' Cole ', 'blue', nick='  '),
        gp(1002, 5002, 9002, 'Robert', 'Brandt', 'Created by CheckIn', nick='Bo'),
    ]
    kids, excluded = roster.fetch_roster()
    assert excluded == [6001]
    by = {k['contact_id']: k for k in kids}
    assert set(by) == {1001, 1002, 1003}
    assert by[1001] == {'contact_id': 1001, 'participant_id': 5001, 'group_participant_id': 9001,
                        'group_role_id': 2, 'nickname': None, 'first_name': 'Alice', 'last_name': 'Anders',
                        'notes': 'Red', 'team': 'Red', 'start_date': '2025-08-20T00:00:00', 'end_date': None}
    cara = by[1003]
    assert (cara['first_name'], cara['last_name'], cara['nickname'], cara['team']) == ('Cara', 'Cole', None, 'Blue')
    bo = by[1002]
    assert (bo['nickname'], bo['team'], bo['notes']) == ('Bo', None, 'Created by CheckIn')


def test_fetch_roster_kid_with_helper_row_is_not_excluded(fake_mp):
    fake_mp.tables['Group_Participants'] = [gp(1001, 5001, 9001, 'Alice', 'Anders', 'Red'),
                                            gp(1001, 5001, 9101, 'Alice', 'Anders', None, role=15)]
    kids, excluded = roster.fetch_roster()
    assert [k['group_participant_id'] for k in kids] == [9001]
    assert excluded == []


def test_fetch_roster_kid_role_ids_from_env(fake_mp, monkeypatch):
    monkeypatch.setenv('MP_KID_ROLE_IDS', '2, 3')
    assert roster.kid_role_ids() == (2, 3)
    fake_mp.tables['Group_Participants'] = [gp(1001, 5001, 9001, 'Alice', 'Anders', 'Red', role=3),
                                            volunteer_row()]
    kids, excluded = roster.fetch_roster()
    assert [k['contact_id'] for k in kids] == [1001]
    assert excluded == [6001]


def test_fetch_roster_empty(fake_mp):
    assert roster.fetch_roster() == ([], [])


def test_fetch_roster_dedupe_prefers_open_row(fake_mp):
    """A contact with two active GP rows: an open-ended row beats one with an End_Date, even if it started later."""
    ending = gp(1001, 5001, 9051, 'Alice', 'Anders', 'Blue', start='2026-09-01T00:00:00', end='2026-12-31T00:00:00')
    open_ = gp(1001, 5001, 9001, 'Alice', 'Anders', 'Red', start='2025-08-20T00:00:00')
    for rows in ([ending, open_], [open_, ending]):
        fake_mp.tables['Group_Participants'] = rows
        kids, _ = roster.fetch_roster()
        assert [(k['group_participant_id'], k['team']) for k in kids] == [(9001, 'Red')]


def test_fetch_roster_dedupe_then_latest_start(fake_mp):
    older = gp(1001, 5001, 9001, 'Alice', 'Anders', 'Red', start='2025-08-20T00:00:00')
    newer = gp(1001, 5001, 9051, 'Alice', 'Anders', 'Green', start='2026-09-02T00:00:00')
    for rows in ([older, newer], [newer, older]):
        fake_mp.tables['Group_Participants'] = rows
        kids, _ = roster.fetch_roster()
        assert [(k['group_participant_id'], k['team']) for k in kids] == [(9051, 'Green')]


# -- upsert_roster -----------------------------------------------------------------

def test_upsert_roster_inserts_new_and_updates_existing(seeded, fake_mp):
    rows = roster_rows(gp(1009, 5009, 9009, 'Ivy', 'Irons', 'Green'), skip=(1001, 1002)) + [
        gp(1001, 5001, 9001, 'Alicia', 'Anders', 'Red', nick='Ali'),
        gp(1002, 5002, 9002, 'Robert', 'Brandt', 'Blue', nick='Bo')]
    result = roster.upsert_roster(seeded, fetched(fake_mp, rows), fetched_at=now())
    assert result == {'kids': 8, 'deactivated': 0}
    ivy = kid(seeded, 1009)
    assert (ivy['name'], ivy['team'], ivy['mp_notes'], ivy['participant_id'], ivy['group_participant_id'],
            ivy['group_role_id'], ivy['active']) == ('Ivy Irons', 'Green', 'Green', 5009, 9009, 2, True)
    assert kid(seeded, 1001)['name'] == 'Ali Anders'
    assert kid(seeded, 1002)['team'] == 'Blue'          # a team change made in MP is adopted


def test_upsert_roster_non_team_notes_mean_unassigned(seeded, fake_mp):
    rows = roster_rows(skip=(1003,)) + [gp(1003, 5003, 9003, 'Cara', 'Cole', 'Created by CheckIn')]
    roster.upsert_roster(seeded, fetched(fake_mp, rows), fetched_at=now())
    cara = kid(seeded, 1003)
    assert (cara['team'], cara['mp_notes']) == (None, 'Created by CheckIn')
    gus = kid(seeded, 1007)
    assert (gus['team'], gus['mp_notes']) == (None, 'Created by CheckIn')


def test_upsert_roster_deactivates_missing_kids(seeded, fake_mp):
    result = roster.upsert_roster(seeded, fetched(fake_mp, roster_rows(skip=(1006,))), fetched_at=now())
    assert result == {'kids': 6, 'deactivated': 1}
    fay = kid(seeded, 1006)
    assert (fay['active'], fay['group_participant_id'], fay['team']) == (False, None, 'Yellow')
    assert kid(seeded, 1005)['active'] is True


def test_upsert_roster_reactivates_returning_kid(seeded, fake_mp):
    rows = roster_rows(gp(1008, 5008, 9108, 'Hal', 'Hart', 'Created by CheckIn'))
    roster.upsert_roster(seeded, fetched(fake_mp, rows), fetched_at=now())
    hal = kid(seeded, 1008)
    assert (hal['active'], hal['group_participant_id'], hal['team']) == (True, 9108, None)


def test_upsert_roster_without_deactivation_keeps_missing_active(seeded, fake_mp):
    result = roster.upsert_roster(seeded, fetched(fake_mp, roster_rows()[:1]), fetched_at=now(),
                                  deactivate_missing=False)
    assert result == {'kids': 1, 'deactivated': 0}
    assert seeded.execute('SELECT COUNT(*) AS n FROM kids WHERE active').fetchone()['n'] == 7


def test_upsert_roster_refuses_empty_result(seeded):
    before = kids_table(seeded)
    with pytest.raises(InvalidInput, match='no kids'):
        roster.upsert_roster(seeded, [], fetched_at=now())
    assert kids_table(seeded) == before


def test_upsert_roster_refuses_empty_result_on_first_import(conn):
    with pytest.raises(InvalidInput):
        roster.upsert_roster(conn, [], fetched_at=now())


@pytest.mark.parametrize('keep, ok', [(3, False), (4, True)])
def test_upsert_roster_shrink_guard(seeded, fake_mp, keep, ok):
    """Fewer than half of the 7 active kids coming back looks like a permission problem, not a real roster."""
    kids = fetched(fake_mp, roster_rows()[:keep])
    before = kids_table(seeded)
    if ok:
        assert roster.upsert_roster(seeded, kids, fetched_at=now()) == {'kids': keep, 'deactivated': 7 - keep}
    else:
        with pytest.raises(InvalidInput, match='refusing to shrink'):
            roster.upsert_roster(seeded, kids, fetched_at=now())
        assert kids_table(seeded) == before


def test_upsert_roster_allow_shrink(seeded, fake_mp):
    result = roster.upsert_roster(seeded, fetched(fake_mp, roster_rows()[:1]), fetched_at=now(), allow_shrink=True)
    assert result == {'kids': 1, 'deactivated': 6}


def test_upsert_roster_first_import(conn, fake_mp):
    assert roster.upsert_roster(conn, fetched(fake_mp, roster_rows()), fetched_at=now()) == {'kids': 7,
                                                                                            'deactivated': 0}
    assert conn.execute('SELECT COUNT(*) AS n FROM kids WHERE active').fetchone()['n'] == 7


def test_upsert_roster_keeps_assignment_newer_than_fetch(seeded, fake_mp):
    """A roster read that started before a leader's assignment must not overwrite it."""
    seeded.execute("UPDATE kids SET team = 'Blue', mp_notes = 'Blue', team_assigned_at = now() WHERE contact_id = 1007")
    rows = roster_rows(skip=(1002,)) + [gp(1002, 5002, 9002, 'Robert', 'Brandt', 'Green', nick='Bo')]
    roster.upsert_roster(seeded, fetched(fake_mp, rows), fetched_at=now() - timedelta(minutes=1))
    gus = kid(seeded, 1007)
    assert (gus['team'], gus['mp_notes']) == ('Blue', 'Blue')
    assert kid(seeded, 1002)['team'] == 'Green'         # everyone else still updates


def test_upsert_roster_newer_fetch_overrides_old_assignment(seeded, fake_mp):
    seeded.execute("""UPDATE kids SET team = 'Blue', mp_notes = 'Blue', team_assigned_at = now() - interval '1 hour'
                       WHERE contact_id = 1007""")
    rows = roster_rows(skip=(1007,)) + [gp(1007, 5007, 9007, 'Gus', 'Gray', 'Yellow')]
    roster.upsert_roster(seeded, fetched(fake_mp, rows), fetched_at=now())
    gus = kid(seeded, 1007)
    assert (gus['team'], gus['mp_notes']) == ('Yellow', 'Yellow')


# -- sync_roster -------------------------------------------------------------------

@pytest.fixture
def stale_roster(seeded, fake_mp):
    """The roster sync is due and MP returns the seeded roster plus a volunteer."""
    age(seeded, 'roster')
    fake_mp.tables['Group_Participants'] = roster_rows(volunteer_row())
    return seeded


def test_sync_roster_syncs_and_records_detail(stale_roster, fake_mp):
    status = roster.sync_roster(conn=stale_roster)
    assert status.state == 'synced' and status.ok
    row = sync_state.read(stale_roster, 'roster')
    assert row['synced_at'] == status.synced_at
    assert row['last_error'] is None
    assert row['detail'] == {'kids': 7, 'deactivated': 0, 'excluded_pids': [6001]}
    assert roster.excluded_participant_ids(stale_roster) == {6001}
    assert called(fake_mp) == ['Group_Participants']
    connect, read = fake_mp.calls[0][1]['timeout']
    assert 0 < connect <= mp.HOT_TIMEOUT[0] and 0 < read <= mp.HOT_TIMEOUT[1]
    assert sync_state.memo_fresh('roster')


def test_sync_roster_single_flight(stale_roster, fake_mp):
    first = roster.sync_roster(conn=stale_roster)
    sync_state.memo_clear()                            # another instance: no memo, and the DB row is fresh
    second = roster.sync_roster(conn=stale_roster)
    assert (first.state, second.state) == ('synced', 'fresh')
    assert second.synced_at == first.synced_at
    assert len(fake_mp.calls) == 1
    assert sync_state.memo_fresh('roster')             # the loser memoizes the remaining freshness


def test_sync_roster_memo_skips_db_and_mp(stale_roster, fake_mp):
    roster.sync_roster(conn=stale_roster)
    age(stale_roster, 'roster')                        # even a stale-looking row: the warm-instance memo wins
    before = sync_state.read(stale_roster, 'roster')
    memo = roster.sync_roster(conn=stale_roster)
    assert memo.state == 'fresh' and memo.synced_at is not None   # the memo keeps its time for the banner
    assert len(fake_mp.calls) == 1
    assert sync_state.read(stale_roster, 'roster')['attempted_at'] == before['attempted_at']


def test_sync_roster_in_flight_claim_serves_db(stale_roster, fake_mp):
    """Another request holds the claim: serve Postgres, flagged stale, without calling MP."""
    stale_roster.execute("UPDATE sync_state SET attempted_at = now() WHERE key = 'roster'")
    status = roster.sync_roster(conn=stale_roster)
    assert status.state == 'stale' and status.synced_at is not None
    assert fake_mp.calls == []


def test_sync_roster_pending_before_first_sync(conn, fake_mp):
    conn.execute("INSERT INTO sync_state (key, attempted_at) VALUES ('roster', now())")
    assert roster.sync_roster(conn=conn).state == 'pending'
    assert fake_mp.calls == []


def test_sync_roster_first_ever_sync(conn, fake_mp):
    fake_mp.tables['Group_Participants'] = roster_rows()
    assert roster.sync_roster(conn=conn).state == 'synced'
    assert conn.execute('SELECT COUNT(*) AS n FROM kids WHERE active').fetchone()['n'] == 7


def test_sync_roster_force_skips_ttl_not_retry_window(seeded, fake_mp):
    fake_mp.tables['Group_Participants'] = roster_rows()
    assert roster.sync_roster(force=True, conn=seeded).state == 'fresh'    # seeded attempted moments ago
    assert fake_mp.calls == []
    age(seeded, 'roster', 30)                                                # a double-tapped Refresh
    assert roster.sync_roster(force=True, conn=seeded).state == 'fresh'
    assert fake_mp.calls == []
    age(seeded, 'roster', 61)                                                # still fresh, 60 s window passed
    assert roster.sync_roster(force=True, conn=seeded).state == 'synced'
    assert len(fake_mp.calls) == 1


def test_ensure_roster_fresh_never_forces(seeded, fake_mp):
    age(seeded, 'roster', 6)
    assert roster.ensure_roster_fresh(conn=seeded).state == 'fresh'
    assert fake_mp.calls == []


def test_sync_roster_mp_unavailable_trips_breaker(stale_roster, fake_mp):
    prev = sync_state.read(stale_roster, 'roster')['synced_at']
    before = kids_table(stale_roster)
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    status = roster.sync_roster(conn=stale_roster)
    assert (status.state, status.error, status.synced_at) == ('error', 'mp_unavailable', prev)
    assert not status.ok
    row = sync_state.read(stale_roster, 'roster')
    assert (row['last_error'], row['synced_at']) == ('MP get failed: timeout', prev)
    assert sync_state.mp_is_down(stale_roster)
    assert kids_table(stale_roster) == before
    assert not sync_state.memo_fresh('roster')

    fake_mp.fail_with = None
    age(stale_roster, 'roster')                        # the claim is open again; only the breaker holds it off
    status = roster.sync_roster(conn=stale_roster)
    assert (status.state, status.error) == ('error', 'MP get failed: timeout')
    assert len(fake_mp.calls) == 1


def test_sync_roster_force_bypasses_breaker_and_success_clears_it(stale_roster, fake_mp):
    sync_state.mark_mp_down(stale_roster, 'MP get failed: timeout')
    down = roster.sync_roster(conn=stale_roster)
    assert down.state == 'error' and down.synced_at is not None     # breaker open: stale data is reported as such
    assert fake_mp.calls == []
    assert roster.sync_roster(force=True, conn=stale_roster).state == 'synced'
    assert not sync_state.mp_is_down(stale_roster)


def test_sync_roster_no_kids_guard(stale_roster, fake_mp):
    """MP answering with only a volunteer (e.g. a permissions problem) leaves the roster alone."""
    fake_mp.tables['Group_Participants'] = [volunteer_row()]
    before = kids_table(stale_roster)
    status = roster.sync_roster(conn=stale_roster)
    assert status.state == 'error' and 'no kids' in status.error
    assert kids_table(stale_roster) == before
    assert 'no kids' in sync_state.read(stale_roster, 'roster')['last_error']
    assert not sync_state.mp_is_down(stale_roster)


def test_sync_roster_shrink_guard(stale_roster, fake_mp):
    fake_mp.tables['Group_Participants'] = roster_rows()[:2]
    before = kids_table(stale_roster)
    status = roster.sync_roster(conn=stale_roster)
    assert status.state == 'error' and 'refusing to shrink' in status.error
    assert kids_table(stale_roster) == before


def test_sync_roster_other_mp_error_skips_breaker(stale_roster, fake_mp):
    fake_mp.fail_with = mp.MPForbidden('forbidden', op='get', table='Group_Participants', status=403)
    status = roster.sync_roster(conn=stale_roster)
    assert (status.state, status.error) == ('error', 'MP get Group_Participants failed: 403')
    assert not sync_state.mp_is_down(stale_roster)


def test_sync_roster_tiny_budget_skips_without_mp_call(stale_roster, fake_mp):
    status = roster.sync_roster(budget=Budget(0.1), conn=stale_roster)
    assert (status.state, status.error) == ('skipped', 'budget')
    assert status.synced_at is not None
    assert fake_mp.calls == []
    assert sync_state.read(stale_roster, 'roster')['last_error'] == 'skipped: request MP budget spent'


def test_sync_roster_keeps_assignment_made_during_fetch(stale_roster, fake_mp):
    """A leader assigns Gus while the roster request is in flight; the older MP read must not undo it."""
    def rows(**kw):
        # Both fetched_at and team_assigned_at come from the Postgres clock, so they compare cleanly.
        stale_roster.execute("""UPDATE kids SET team = 'Blue', mp_notes = 'Blue', team_assigned_at = clock_timestamp()
                                 WHERE contact_id = 1007""")
        return roster_rows()                           # read before the assignment reached MP
    fake_mp.tables['Group_Participants'] = rows
    assert roster.sync_roster(conn=stale_roster).state == 'synced'
    gus = kid(stale_roster, 1007)
    assert (gus['team'], gus['mp_notes']) == ('Blue', 'Blue')


def test_sync_roster_uses_request_connection(stale_roster, app_ctx, fake_mp):
    assert roster.sync_roster().state == 'synced'
    assert sync_state.read(stale_roster, 'roster')['detail']['kids'] == 7


def test_sync_roster_needs_conn_outside_app(fake_mp):
    with pytest.raises(RuntimeError):
        roster.sync_roster()


# -- sync_state: Budget, SyncStatus, claim, memo, circuit breaker --------------------

def test_budget_timeout_capped_by_remaining_time():
    assert Budget(10).timeout() == mp.HOT_TIMEOUT
    assert Budget(10, base_timeout=(1, 2)).timeout() == (1, 2)
    connect, read = Budget(1.0).timeout()
    assert connect == read and 0.5 <= read <= 1.0


def test_budget_exceeded():
    with pytest.raises(BudgetExceeded):
        Budget(0.4).timeout()
    spent = Budget(3.0)
    spent.deadline = time.monotonic() - 1
    with pytest.raises(BudgetExceeded):
        spent.timeout()
    assert spent.remaining() < 0


def test_unlimited_budget_uses_script_timeouts():
    assert Unlimited().timeout() == mp.SCRIPT_TIMEOUT
    assert Unlimited(timeout=(1, 5)).timeout() == (1, 5)


@pytest.mark.parametrize('state, ok', [('fresh', True), ('synced', True), ('no_event', True), ('frozen', True),
                                       ('stale', False), ('pending', False), ('error', False), ('skipped', False)])
def test_sync_status_ok(state, ok):
    assert SyncStatus('k', state).ok is ok


def test_claim_single_flight(conn):
    assert sync_state.claim(conn, 'k', ttl=60, retry_after=20) == {'synced_at': None, 'detail': {}}
    assert sync_state.claim(conn, 'k', ttl=60, retry_after=20) is None               # in flight
    assert sync_state.claim(conn, 'k', ttl=60, retry_after=20, force=True) is None   # force still waits


def test_claim_after_success_waits_for_ttl_unless_forced(conn):
    sync_state.claim(conn, 'k', ttl=600, retry_after=20)
    synced = now()
    sync_state.complete(conn, 'k', synced_at=synced, detail={'n': 1})
    conn.execute("UPDATE sync_state SET attempted_at = now() - interval '1 minute' WHERE key = 'k'")
    assert sync_state.claim(conn, 'k', ttl=600, retry_after=20) is None
    assert sync_state.claim(conn, 'k', ttl=600, retry_after=20, force=True) == {'synced_at': synced,
                                                                                 'detail': {'n': 1}}


def test_claim_stale_row_returns_prior_state(conn):
    conn.execute("""INSERT INTO sync_state (key, synced_at, attempted_at, detail)
                    VALUES ('k', now() - interval '2 hours', now() - interval '2 hours', '{"a": 1}')""")
    claimed = sync_state.claim(conn, 'k', ttl=3600, retry_after=20)
    assert claimed['detail'] == {'a': 1}
    row = sync_state.read(conn, 'k')
    assert row['synced_at'] == claimed['synced_at']                                  # untouched until complete()
    assert row['attempted_at'] > claimed['synced_at'] + timedelta(hours=1)


def test_complete_fail_and_read(conn):
    assert sync_state.read(conn, 'k') is None
    sync_state.claim(conn, 'k', ttl=60, retry_after=20)
    sync_state.fail(conn, 'k', 'x' * 500)
    row = sync_state.read(conn, 'k')
    assert (row['last_error'], row['synced_at']) == ('x' * 300, None)
    synced = now()
    sync_state.complete(conn, 'k', synced_at=synced, detail={'at': datetime(2026, 9, 23, tzinfo=timezone.utc)})
    row = sync_state.read(conn, 'k')
    assert (row['last_error'], row['synced_at']) == (None, synced)
    assert row['detail'] == {'at': '2026-09-23 00:00:00+00:00'}


def test_status_from_row():
    t = now()
    assert sync_state.status_from_row('k', None, 60) == SyncStatus('k', 'pending')
    assert sync_state.status_from_row('k', {'synced_at': None, 'last_error': None}, 60) == SyncStatus('k', 'pending')
    assert sync_state.status_from_row('k', {'synced_at': None, 'last_error': 'boom'}, 60) == SyncStatus(
        'k', 'error', None, 'boom')
    fresh = t - timedelta(seconds=10)
    assert sync_state.status_from_row('k', {'synced_at': fresh, 'last_error': None}, 60) == SyncStatus(
        'k', 'fresh', fresh)
    old = t - timedelta(seconds=120)
    assert sync_state.status_from_row('k', {'synced_at': old, 'last_error': None}, 60) == SyncStatus('k', 'stale', old)
    assert sync_state.status_from_row('k', {'synced_at': fresh, 'last_error': 'boom'}, 60) == SyncStatus(
        'k', 'error', fresh, 'boom')


def test_memo(fake_mp):
    assert not sync_state.memo_fresh('k')
    sync_state.memo_set('k', 60)
    assert sync_state.memo_fresh('k')
    for secs in (0, -5):
        sync_state.memo_set('j', secs)
        assert not sync_state.memo_fresh('j')
    sync_state.memo_clear()
    assert not sync_state.memo_fresh('k')


def test_circuit_breaker(conn):
    assert not sync_state.mp_is_down(conn)
    sync_state.mark_mp_up(conn)                        # no row yet: a no-op
    assert sync_state.read(conn, 'mp_health') is None
    sync_state.mark_mp_down(conn, 'MP get failed: timeout')
    assert sync_state.mp_is_down(conn)
    row = sync_state.read(conn, 'mp_health')
    assert row['last_error'] == 'MP get failed: timeout'
    until = datetime.fromisoformat(row['detail']['down_until'])
    assert timedelta(seconds=55) < until - now() <= timedelta(seconds=sync_state.MP_DOWN_SECONDS)
    sync_state.mark_mp_up(conn)
    assert not sync_state.mp_is_down(conn)
    row = sync_state.read(conn, 'mp_health')
    assert (row['last_error'], row['detail']) == (None, {})
    sync_state.mark_mp_down(conn, 'boom', seconds=-1)  # already expired
    assert not sync_state.mp_is_down(conn)


# -- check-in TTLs and queries -----------------------------------------------------

@pytest.mark.parametrize('d, ttl', [
    (TODAY + timedelta(days=1), None), (D_0930, None),
    (TODAY, 45),
    (TODAY - timedelta(days=1), 3600), (D_0916, 3600),
    (TODAY - timedelta(days=8), None), (D_0909, None),
])
def test_checkin_ttl(d, ttl):
    assert checkins.checkin_ttl(d, today=TODAY) == ttl


def test_checkin_ttl_uses_app_today(app_ctx):
    assert checkins.checkin_ttl(D_0923) == checkins.TTL_LIVE
    assert checkins.checkin_ttl(D_0930) is None


def test_key_for():
    assert checkins.key_for(D_0923) == TONIGHT_KEY


def test_event_filter():
    assert checkins.event_filter(697, D_0923, date(2026, 9, 24)) == (
        "Event_Groups.Group_ID = 697 AND Event_ID_TABLE.[Allow_Check-in] = 1 AND Event_ID_TABLE.Cancelled = 0 "
        "AND Event_ID_TABLE.Event_Start_Date >= '2026-09-23' AND Event_ID_TABLE.Event_Start_Date < '2026-09-24'")


def test_checkins_filter():
    assert checkins.checkins_filter([7001, 7002], [5001, 5002]) == (
        'Event_ID IN (7001,7002) AND Participation_Status_ID IN (3,4) AND Time_In IS NOT NULL '
        'AND (Group_ID = 697 OR Group_ID IS NULL OR Participant_ID IN (5001,5002))')


def test_checkins_filter_without_participants():
    assert checkins.checkins_filter([7001], []) == (
        'Event_ID IN (7001) AND Participation_Status_ID IN (3,4) AND Time_In IS NOT NULL '
        'AND (Group_ID = 697 OR Group_ID IS NULL)')


@pytest.mark.parametrize('event_ids, pids', [(['7001) OR (1=1'], [5001]), ([], [5001]), ([7001], ['5001 OR 1=1'])])
def test_checkins_filter_rejects_unsafe_ids(event_ids, pids):
    with pytest.raises(ValueError):
        checkins.checkins_filter(event_ids, pids)


def test_fetch_event_ids_range_groups_by_date(fake_mp):
    fake_mp.tables['Event_Groups'] = [
        {'Event_ID': 7001, 'Event_Start_Date': '2026-09-16T18:30:00'},
        {'Event_ID': 7002, 'Event_Start_Date': '2026-09-23T18:30:00'},
        {'Event_ID': 7002, 'Event_Start_Date': '2026-09-23T18:30:00'},   # the event linked to the group twice
        {'Event_ID': 7003, 'Event_Start_Date': '2026-09-23T19:00:00'},
    ]
    assert checkins.fetch_event_ids_range(D_0916, D_0923, timeout=(1, 2)) == {D_0916: [7001], D_0923: [7002, 7003]}
    [(table, kw)] = fake_mp.calls
    assert table == 'Event_Groups'
    assert kw['select'] == 'Event_Groups.Event_ID,Event_ID_TABLE.Event_Start_Date'
    assert kw['filter'] == checkins.event_filter(697, D_0916, date(2026, 9, 24))
    assert (kw['orderby'], kw['timeout']) == ('Event_Groups.Event_ID', (1, 2))


def test_fetch_event_ids_single_date(fake_mp):
    fake_mp.tables['Event_Groups'] = EVENT
    assert checkins.fetch_event_ids(D_0923) == [7001]
    assert checkins.fetch_event_ids(D_0916) == []


def test_fetch_checkins_earliest_time_in_wins(fake_mp):
    fake_mp.tables['Event_Participants'] = [
        ep(8002, 5001, '2026-09-23T18:40:00'),
        ep(8001, 5001, '2026-09-23T18:31:02', status=4, confirmed='2026-09-23T18:35:00', event_id=7002),
        ep(8003, 5003, '2026-09-23T18:45:00', group_id=None),
        ep(8004, 5004, None),                          # no Time_In is not a check-in
    ]
    result = checkins.fetch_checkins([7001, 7002], [5001, 5003], timeout=(1, 2))
    assert result == {
        5001: {'ep_id': 8001, 'time_in': datetime(2026, 9, 23, 18, 31, 2, tzinfo=CHICAGO),
               'time_confirmed': datetime(2026, 9, 23, 18, 35, tzinfo=CHICAGO), 'group_id': 697},
        5003: {'ep_id': 8003, 'time_in': datetime(2026, 9, 23, 18, 45, tzinfo=CHICAGO),
               'time_confirmed': None, 'group_id': None}}
    [(table, kw)] = fake_mp.calls
    assert table == 'Event_Participants'
    assert kw['select'] == ('Event_Participant_ID,Event_ID,Participant_ID,Participation_Status_ID,'
                            'Time_In,Time_Confirmed,Group_ID')
    assert kw['filter'] == checkins.checkins_filter([7001, 7002], [5001, 5003])
    assert (kw['orderby'], kw['timeout']) == ('Event_Participant_ID', (1, 2))


# -- sync_checkins -----------------------------------------------------------------

@pytest.fixture
def tonight(seeded, app_ctx, fake_mp):
    """App 'today' is 2026-09-23, tonight's check-in sync is due, and MP has one Awana event."""
    seeded.execute('DELETE FROM sync_state WHERE key = %s', (TONIGHT_KEY,))
    fake_mp.tables['Event_Groups'] = EVENT
    return seeded


def test_sync_checkins_upserts_attendance(tonight, fake_mp):
    fake_mp.tables['Event_Participants'] = mp_checkins([
        ep(8001, 5001, '2026-09-23T18:31:02', confirmed='2026-09-23T18:35:00'),
        ep(8003, 5003, '2026-09-23T18:33:00', status=4, group_id=None),
        ep(8007, 5007, '2026-09-23T18:50:00')])
    status = checkins.sync_checkins(D_0923, conn=tonight)
    assert status.state == 'synced' and status.synced_at is not None
    alice = att(tonight, 1001)
    assert (alice['mp_checked_in'], alice['present']) == (True, True)
    assert alice['mp_time_in'] == datetime(2026, 9, 23, 18, 31, 2, tzinfo=CHICAGO)
    assert alice['mp_time_confirmed'] == datetime(2026, 9, 23, 18, 35, tzinfo=CHICAGO)
    assert alice['mp_event_participant_id'] == 8001 and alice['mp_synced_at'] is not None
    assert (alice['team'], alice['source'], alice['manual_present']) == ('Red', 'mp_sync', False)
    assert not any(alice[c] for c in ATTENDANCE_FLAG_COLUMNS)
    assert att(tonight, 1003)['team'] == 'Blue'
    assert att(tonight, 1007)['team'] is None          # Unassigned snapshot
    assert att(tonight, 1002) is None
    row = sync_state.read(tonight, TONIGHT_KEY)
    assert (row['synced_at'], row['last_error']) == (status.synced_at, None)
    assert row['detail']['event_ids'] == [7001]
    assert row['detail']['counts'] == {'checked_in': 3, 'upserted': 3, 'cleared': 0}
    assert called(fake_mp) == ['Event_Groups', 'Event_Participants']
    assert call_kw(fake_mp, 'Event_Groups')[0]['filter'] == checkins.event_filter(697, D_0923, date(2026, 9, 24))
    assert sync_state.memo_fresh(TONIGHT_KEY)


def test_sync_checkins_query_filter(tonight, fake_mp):
    checkins.sync_checkins(D_0923, conn=tonight)
    f = call_kw(fake_mp, 'Event_Participants')[0]['filter']
    assert f.startswith('Event_ID IN (7001) AND ')
    assert 'Participation_Status_ID IN (3,4)' in f and 'Time_In IS NOT NULL' in f
    assert 'Group_ID = 697 OR Group_ID IS NULL' in f
    pids = re.search(r'Participant_ID IN \(([\d,]+)\)', f).group(1)
    assert [int(p) for p in pids.split(',')] == ACTIVE_PIDS    # the inactive kid (5008) isn't asked about


def test_sync_checkins_queries_inactive_kid_checked_in_tonight(tonight, fake_mp):
    """A kid who left the roster but was checked in tonight is still asked about, so a cancel clears them."""
    add_attendance(tonight, 1008, D_0923, mp_checked_in=True)
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001), ep(8008, 5008, status=5)])
    checkins.sync_checkins(D_0923, conn=tonight)
    f = call_kw(fake_mp, 'Event_Participants')[0]['filter']
    assert '5008' in re.search(r'Participant_ID IN \(([\d,]+)\)', f).group(1).split(',')
    assert att(tonight, 1008)['mp_checked_in'] is False


def test_sync_checkins_never_touches_leader_fields(tonight, fake_mp):
    add_attendance(tonight, 1001, D_0923, team='Green', manual_present=True, has_bible=True, has_dues=True)
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001)])
    checkins.sync_checkins(D_0923, conn=tonight)
    alice = att(tonight, 1001)
    assert (alice['mp_checked_in'], alice['mp_event_participant_id']) == (True, 8001)
    assert (alice['team'], alice['source'], alice['manual_present']) == ('Green', 'app', True)   # snapshot kept
    assert (alice['has_bible'], alice['has_dues'], alice['wearing_shirt'], alice['has_book'],
            alice['did_homework']) == (True, True, False, False, False)


def test_sync_checkins_clears_cancelled_kids(tonight, fake_mp):
    add_attendance(tonight, 1002, D_0923, mp_checked_in=True, has_book=True)
    add_attendance(tonight, 1004, D_0923, mp_checked_in=True, manual_present=True)
    add_attendance(tonight, 1002, D_0916, mp_checked_in=True)
    fake_mp.tables['Event_Participants'] = mp_checkins([
        ep(8001, 5001), ep(8002, 5002, status=5), ep(8004, 5004, status=5)])
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    bo = att(tonight, 1002)
    assert (bo['mp_checked_in'], bo['mp_time_in'], bo['mp_time_confirmed'], bo['mp_event_participant_id']) == (
        False, None, None, None)
    assert (bo['has_book'], bo['present']) == (True, False)
    dev = att(tonight, 1004)
    assert (dev['mp_checked_in'], dev['manual_present'], dev['present']) == (False, True, True)
    assert att(tonight, 1002, D_0916)['mp_checked_in'] is True     # other dates untouched
    assert detail(tonight)['counts'] == {'checked_in': 1, 'upserted': 1, 'cleared': 2}


def test_sync_checkins_empty_result_never_clears(tonight, fake_mp):
    add_attendance(tonight, 1001, D_0923, mp_checked_in=True)
    add_attendance(tonight, 1002, D_0923, mp_checked_in=True)
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001, status=5), ep(8002, 5002, status=5)])
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert att(tonight, 1001)['mp_checked_in'] and att(tonight, 1002)['mp_checked_in']
    d = detail(tonight)
    assert d['warning'] == 'empty MP result; kept existing check-ins'
    assert d['counts'] == {'checked_in': 0, 'upserted': 0, 'cleared': 0}


def test_sync_checkins_only_non_kids_counts_as_empty(tonight, fake_mp):
    """A result holding only a volunteer maps to no kids, so it can't clear anyone either."""
    mark_synced(tonight, 'roster', detail=json.dumps({'excluded_pids': [6001]}))
    add_attendance(tonight, 1002, D_0923, mp_checked_in=True)
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8101, 6001)])
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert att(tonight, 1002)['mp_checked_in'] is True


def test_sync_checkins_empty_result_with_nothing_checked_in(tonight, fake_mp):
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert 'warning' not in detail(tonight)
    assert attendance_table(tonight) == []


def test_sync_checkins_earliest_time_in_wins(tonight, fake_mp):
    fake_mp.tables['Event_Groups'] = EVENT + [{'Event_ID': 7002, 'Event_Start_Date': '2026-09-23T19:00:00'}]
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8002, 5001, '2026-09-23T19:02:00', event_id=7002),
                                                        ep(8001, 5001, '2026-09-23T18:31:02')])
    checkins.sync_checkins(D_0923, conn=tonight)
    alice = att(tonight, 1001)
    assert (alice['mp_event_participant_id'], alice['mp_time_in']) == (
        8001, datetime(2026, 9, 23, 18, 31, 2, tzinfo=CHICAGO))
    assert call_kw(fake_mp, 'Event_Participants')[0]['filter'].startswith('Event_ID IN (7001,7002) AND ')
    assert detail(tonight)['counts']['checked_in'] == 1


def test_sync_checkins_caches_event_lookup(tonight, fake_mp):
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001)])
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    checked_at = detail(tonight)['events_checked_at']

    age(tonight, TONIGHT_KEY, 60)
    sync_state.memo_clear()
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert called(fake_mp) == ['Event_Groups', 'Event_Participants', 'Event_Participants']
    assert detail(tonight)['events_checked_at'] == checked_at

    expired = (now() - timedelta(hours=7)).isoformat()
    tonight.execute("""UPDATE sync_state SET detail = jsonb_set(detail, '{events_checked_at}', to_jsonb(%s::text))
                        WHERE key = %s""", (expired, TONIGHT_KEY))
    age(tonight, TONIGHT_KEY, 60)
    sync_state.memo_clear()
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert called(fake_mp, 'Event_Groups') == ['Event_Groups'] * 2
    assert detail(tonight)['events_checked_at'] != expired


def test_sync_checkins_no_event(tonight, fake_mp):
    fake_mp.tables['Event_Groups'] = []
    add_attendance(tonight, 1001, D_0923, mp_checked_in=True)
    status = checkins.sync_checkins(D_0923, conn=tonight)
    assert status.state == 'no_event' and status.ok and status.synced_at is not None
    assert called(fake_mp) == ['Event_Groups']
    assert att(tonight, 1001)['mp_checked_in'] is True           # nothing cleared
    row = sync_state.read(tonight, TONIGHT_KEY)
    assert (row['synced_at'], row['detail']['event_ids']) == (status.synced_at, [])
    checkins.sync_checkins(D_0923, conn=tonight)                  # memoized: no MP call
    assert called(fake_mp) == ['Event_Groups']


def test_sync_checkins_no_event_survives_lost_claim(tonight, fake_mp):
    fake_mp.tables['Event_Groups'] = []
    checkins.sync_checkins(D_0923, conn=tonight)
    sync_state.memo_clear()                            # another instance: no memo, the row is fresh
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'no_event'


def test_sync_checkins_walk_in_forces_one_roster_sync(tonight, fake_mp):
    """A kid MP checked in before the roster knew them: one forced roster sync picks them up."""
    age(tonight, 'roster')
    fake_mp.tables['Group_Participants'] = roster_rows(gp(1010, 5010, 9010, 'Jo', 'Jett', 'Created by CheckIn'))
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001),
                                                        ep(8010, 5010, '2026-09-23T18:55:00', group_id=None)])
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert called(fake_mp) == ['Event_Groups', 'Event_Participants', 'Group_Participants']
    jo = kid(tonight, 1010)
    assert (jo['name'], jo['active'], jo['team']) == ('Jo Jett', True, None)
    row = att(tonight, 1010)
    assert (row['mp_checked_in'], row['team'], row['mp_event_participant_id']) == (True, None, 8010)
    d = detail(tonight)
    assert d['tried_pids'] == [5010]
    assert d['counts']['checked_in'] == 2
    assert sync_state.read(tonight, 'roster')['detail']['kids'] == 8


def test_sync_checkins_forces_roster_once_per_unknown_pid(tonight, fake_mp):
    """A pid the roster still doesn't know after its forced sync isn't retried later that night."""
    age(tonight, 'roster')
    fake_mp.tables['Group_Participants'] = roster_rows()      # 5011 never shows up on the roster
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001), ep(8011, 5011, group_id=None)])
    checkins.sync_checkins(D_0923, conn=tonight)
    assert len(called(fake_mp, 'Group_Participants')) == 1
    assert detail(tonight)['tried_pids'] == [5011]
    assert [r['contact_id'] for r in attendance_table(tonight)] == [1001]

    for key in (TONIGHT_KEY, 'roster'):
        age(tonight, key, 60)
    sync_state.memo_clear()
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert len(called(fake_mp, 'Group_Participants')) == 1
    assert len(called(fake_mp, 'Event_Participants')) == 2

    # a different new walk-in the same night still gets its own forced sync
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001), ep(8011, 5011, group_id=None),
                                                        ep(8012, 5012, group_id=None)])
    for key in (TONIGHT_KEY, 'roster'):
        age(tonight, key, 60)
    sync_state.memo_clear()
    checkins.sync_checkins(D_0923, conn=tonight)
    assert len(called(fake_mp, 'Group_Participants')) == 2
    assert detail(tonight)['tried_pids'] == [5011, 5012]


def test_sync_checkins_volunteer_never_forces_roster_sync(tonight, fake_mp):
    mark_synced(tonight, 'roster', detail=json.dumps({'excluded_pids': [6001]}))
    age(tonight, 'roster')
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001), ep(8101, 6001)])
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert called(fake_mp, 'Group_Participants') == []
    assert 'tried_pids' not in detail(tonight)
    assert [r['contact_id'] for r in attendance_table(tonight)] == [1001]


def test_sync_checkins_walk_in_roster_failure_still_applies_known_kids(tonight, fake_mp):
    age(tonight, 'roster')
    fake_mp.tables['Group_Participants'] = raises(mp.MPUnavailable('timeout', op='get', table='Group_Participants'))
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001), ep(8010, 5010, group_id=None)])
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'synced'
    assert att(tonight, 1001)['mp_checked_in'] is True
    assert kid(tonight, 1010) is None
    assert detail(tonight)['tried_pids'] == [5010]
    assert sync_state.read(tonight, 'roster')['last_error'] == 'MP get Group_Participants failed: timeout'


def test_sync_checkins_skips_mp_while_down(tonight, fake_mp):
    sync_state.mark_mp_down(tonight, 'MP get failed: timeout')
    assert checkins.sync_checkins(D_0923, conn=tonight).state == 'error'       # never synced tonight, MP down
    mark_synced(tonight, TONIGHT_KEY)
    age(tonight, TONIGHT_KEY, 120)
    status = checkins.sync_checkins(D_0923, conn=tonight)
    assert status.state == 'error' and status.synced_at is not None
    assert fake_mp.calls == []
    assert checkins.sync_checkins(D_0923, force=True, conn=tonight).state == 'synced'   # refresh bypasses it
    assert not sync_state.mp_is_down(tonight)


def test_sync_checkins_mp_unavailable(tonight, fake_mp):
    add_attendance(tonight, 1002, D_0923, mp_checked_in=True)
    fake_mp.tables['Event_Participants'] = raises(mp.MPUnavailable('timeout', op='get', table='Event_Participants'))
    status = checkins.sync_checkins(D_0923, conn=tonight)
    assert (status.state, status.error, status.synced_at) == ('error', 'mp_unavailable', None)
    assert att(tonight, 1002)['mp_checked_in'] is True
    assert sync_state.mp_is_down(tonight)
    row = sync_state.read(tonight, TONIGHT_KEY)
    assert (row['last_error'], row['synced_at']) == ('MP get Event_Participants failed: timeout', None)
    assert not sync_state.memo_fresh(TONIGHT_KEY)


def test_sync_checkins_other_mp_error(tonight, fake_mp):
    fake_mp.tables['Event_Participants'] = raises(mp.MPBadRequest('bad_request', op='get',
                                                                  table='Event_Participants', status=400))
    status = checkins.sync_checkins(D_0923, conn=tonight)
    assert (status.state, status.error) == ('error', 'MP get Event_Participants failed: 400')
    assert not sync_state.mp_is_down(tonight)


def test_sync_checkins_older_and_future_dates_frozen(tonight, fake_mp):
    status = checkins.sync_checkins(D_0909, conn=tonight)
    assert status.state == 'frozen' and status.ok
    assert status.synced_at == sync_state.read(tonight, 'checkins:2026-09-09')['synced_at']
    future = checkins.sync_checkins(D_0930, conn=tonight)            # future dates never sync
    assert (future.key, future.state) == ('checkins:2026-09-30', 'frozen')
    assert fake_mp.calls == []


def test_sync_checkins_force_syncs_frozen_date(tonight, fake_mp):
    age(tonight, 'checkins:2026-09-09', 60)
    fake_mp.tables['Event_Groups'] = [{'Event_ID': 6901, 'Event_Start_Date': '2026-09-09T18:30:00'}]
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001, '2026-09-09T18:31:00', event_id=6901)])
    assert checkins.sync_checkins(D_0909, force=True, conn=tonight).state == 'synced'
    assert att(tonight, 1001, D_0909)['mp_checked_in'] is True
    assert call_kw(fake_mp, 'Event_Groups')[0]['filter'].endswith(
        "Event_Start_Date >= '2026-09-09' AND Event_ID_TABLE.Event_Start_Date < '2026-09-10'")


def test_sync_checkins_last_week_refreshes_hourly(tonight, fake_mp):
    assert checkins.ensure_checkins_fresh(D_0916, conn=tonight).state == 'fresh'
    age(tonight, 'checkins:2026-09-16', 600)            # 10 minutes old: still fresh for a past date
    sync_state.memo_clear()
    assert checkins.sync_checkins(D_0916, conn=tonight).state == 'fresh'
    assert fake_mp.calls == []
    age(tonight, 'checkins:2026-09-16', 3700)
    sync_state.memo_clear()
    fake_mp.tables['Event_Groups'] = [{'Event_ID': 6902, 'Event_Start_Date': '2026-09-16T18:30:00'}]
    assert checkins.sync_checkins(D_0916, conn=tonight).state == 'synced'


def test_sync_checkins_budget_spent_mid_sync(tonight, fake_mp):
    budget = Budget(3.0)

    def events(**kw):
        budget.deadline = time.monotonic()           # the event lookup used up the request's MP time
        return EVENT
    fake_mp.tables['Event_Groups'] = events
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001)])
    status = checkins.sync_checkins(D_0923, budget=budget, conn=tonight)
    assert (status.state, status.error) == ('skipped', 'budget')
    assert called(fake_mp) == ['Event_Groups']
    assert att(tonight, 1001) is None
    assert sync_state.read(tonight, TONIGHT_KEY)['last_error'] == 'skipped: request MP budget spent'


# -- live_sync ---------------------------------------------------------------------

def test_live_sync_roster_then_checkins_within_budget(tonight, fake_mp):
    age(tonight, 'roster')
    fake_mp.tables['Group_Participants'] = roster_rows()
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001)])
    status = checkins.live_sync(D_0923, conn=tonight)
    assert (status.key, status.state) == (TONIGHT_KEY, 'synced')
    assert called(fake_mp) == ['Group_Participants', 'Event_Groups', 'Event_Participants']
    for _, kw in fake_mp.calls:
        connect, read = kw['timeout']
        assert 0 < connect <= mp.HOT_TIMEOUT[0] and 0 < read <= 3.0
    assert att(tonight, 1001)['mp_checked_in'] is True
    assert checkins.live_sync(D_0923, conn=tonight).state == 'fresh'    # inside both TTLs: no MP calls
    assert len(fake_mp.calls) == 3


def test_live_sync_tiny_budget_skips_without_mp_calls(tonight, fake_mp):
    age(tonight, 'roster')
    status = checkins.live_sync(D_0923, seconds=0.1, conn=tonight)
    assert (status.state, status.error) == ('skipped', 'budget')
    assert fake_mp.calls == []
    for key in ('roster', TONIGHT_KEY):
        assert sync_state.read(tonight, key)['last_error'] == 'skipped: request MP budget spent'


def test_live_sync_roster_spends_the_budget(tonight, fake_mp, monkeypatch):
    """A slow roster call eats the request's MP time: check-ins are skipped rather than overrunning it."""
    budgets = []

    def make_budget(seconds):
        budgets.append(Budget(seconds))
        return budgets[-1]
    monkeypatch.setattr(checkins, 'Budget', make_budget)

    def slow_roster(**kw):
        budgets[0].deadline = time.monotonic() + 0.2
        return roster_rows()
    age(tonight, 'roster')
    fake_mp.tables['Group_Participants'] = slow_roster
    status = checkins.live_sync(D_0923, conn=tonight)
    assert (status.state, status.error) == ('skipped', 'budget')
    assert called(fake_mp) == ['Group_Participants']
    assert sync_state.read(tonight, 'roster')['last_error'] is None


def test_live_sync_reports_stale_when_roster_errors(tonight, fake_mp):
    age(tonight, 'roster')
    fake_mp.tables['Group_Participants'] = roster_rows()[:2]            # shrink guard trips
    fake_mp.tables['Event_Participants'] = mp_checkins([ep(8001, 5001)])
    status = checkins.live_sync(D_0923, conn=tonight)
    assert (status.key, status.state) == (TONIGHT_KEY, 'stale')
    assert 'refusing to shrink' in status.error and status.synced_at is not None
    assert att(tonight, 1001)['mp_checked_in'] is True                 # check-ins still synced


def test_live_sync_mp_down_skips_checkins_call(tonight, fake_mp):
    """MP times out on the roster: the breaker trips and check-ins aren't tried again in the same request."""
    age(tonight, 'roster')
    mark_synced(tonight, TONIGHT_KEY)
    age(tonight, TONIGHT_KEY, 60)
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    status = checkins.live_sync(D_0923, conn=tonight)
    assert status.state == 'error' and status.synced_at is not None
    assert called(fake_mp) == ['Group_Participants']


def test_live_sync_fresh_checkins_with_roster_outage_is_stale(tonight, fake_mp):
    age(tonight, 'roster')
    mark_synced(tonight, TONIGHT_KEY)                                   # synced moments ago by another instance
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    status = checkins.live_sync(D_0923, conn=tonight)
    assert (status.state, status.error) == ('stale', 'mp_unavailable')
    assert called(fake_mp) == ['Group_Participants']


# -- assign_team -------------------------------------------------------------------

@pytest.fixture
def gus(seeded, app_ctx, fake_mp):
    """Gus (1007) is an unassigned walk-in; MP's GP row still says 'Created by CheckIn'."""
    fake_mp.tables['Group_Participants'] = [dict(GP_GUS)]
    return seeded


def test_assign_team(gus, fake_mp):
    add_attendance(gus, 1007, D_0923, team=None, mp_checked_in=True)
    add_attendance(gus, 1007, D_0916, team=None, mp_checked_in=True)
    add_attendance(gus, 1001, D_0923, team=None)                 # someone else's NULL row stays put
    add_section(gus, 1007, D_0923, '1.2', team=None, complete=True)
    add_section(gus, 1007, D_0923, '1.3', team='Red', silver=True)  # only NULL-team rows are filled
    add_section(gus, 1007, D_0916, '1.1', team=None, complete=True)
    result = roster.assign_team(contact_id=1007, team='blue', by=BY, conn=gus)
    assert result == {'contact_id': 1007, 'name': 'Gus Gray', 'team': 'Blue'}
    [(table, kw)] = fake_mp.calls
    assert table == 'Group_Participants'
    assert kw['filter'] == 'Group_Participant_ID = 9007'
    assert kw['select'] == 'Group_Participant_ID,Group_ID,Participant_ID,Notes,End_Date'
    assert fake_mp.puts == [('Group_Participants', [{'Group_Participant_ID': 9007, 'Notes': 'Blue'}],
                             {'user_id': 42, 'select': 'Group_Participant_ID,Notes'})]
    k = kid(gus, 1007)
    assert (k['team'], k['mp_notes']) == ('Blue', 'Blue') and k['team_assigned_at'] is not None
    assert att(gus, 1007, D_0923)['team'] == 'Blue'
    assert att(gus, 1007, D_0916)['team'] is None                # past totals never shift
    assert att(gus, 1001, D_0923)['team'] is None
    sections = gus.execute('SELECT meeting_date, section, team FROM completed_sections WHERE contact_id = 1007')
    assert {(r['meeting_date'], r['section']): r['team'] for r in sections} == {
        (D_0923, '1.2'): 'Blue', (D_0923, '1.3'): 'Red', (D_0916, '1.1'): None}
    [a] = assignments(gus)
    assert (a['contact_id'], a['group_participant_id'], a['old_notes'], a['new_team'], a['assigned_by_contact_id'],
            a['assigned_by_user_id']) == (1007, 9007, 'Created by CheckIn', 'Blue', 3001, 42)


def test_assign_team_accepts_user_object(gus, fake_mp):
    roster.assign_team(contact_id=1007, team='Green', by=SimpleNamespace(user_id=42, contact_id=3001), conn=gus)
    assert fake_mp.puts[0][2]['user_id'] == 42
    assert assignments(gus)[0]['assigned_by_contact_id'] == 3001


def test_assign_team_uses_request_connection(gus, fake_mp):
    assert roster.assign_team(contact_id=1007, team='Red', by=BY)['team'] == 'Red'
    assert kid(gus, 1007)['team'] == 'Red'


def test_assign_team_already_assigned_locally(gus, fake_mp):
    with pytest.raises(Conflict) as exc:
        roster.assign_team(contact_id=1001, team='Blue', by=BY, conn=gus)
    assert (exc.value.code, exc.value.extra) == ('already_assigned', {'team': 'Red'})
    assert fake_mp.calls == [] and fake_mp.puts == []
    assert kid(gus, 1001)['team'] == 'Red'


def test_assign_team_adopts_team_already_set_in_mp(gus, fake_mp):
    fake_mp.tables['Group_Participants'] = [{**GP_GUS, 'Notes': 'green '}]
    with pytest.raises(Conflict) as exc:
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert (exc.value.code, exc.value.extra) == ('already_assigned', {'team': 'Green'})
    assert fake_mp.puts == []
    k = kid(gus, 1007)
    assert (k['team'], k['mp_notes'], k['team_assigned_at']) == ('Green', 'green ', None)
    assert assignments(gus) == []


@pytest.mark.parametrize('gp_row', [None, {**GP_GUS, 'Group_ID': 700}, {**GP_GUS, 'Participant_ID': 5999}],
                         ids=['missing', 'other-group', 'other-participant'])
def test_assign_team_mp_row_not_in_group(gus, fake_mp, gp_row):
    fake_mp.tables['Group_Participants'] = [gp_row] if gp_row else []
    before = kids_table(gus)
    with pytest.raises(NotFound):
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert fake_mp.puts == []
    assert kids_table(gus) == before and assignments(gus) == []


def test_assign_team_ended_mp_row_is_not_found(gus, fake_mp):
    fake_mp.tables['Group_Participants'] = [{**GP_GUS, 'End_Date': '2026-09-20T00:00:00'}]
    with pytest.raises(NotFound):
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert fake_mp.puts == []


def test_assign_team_future_end_date_is_still_active(gus, fake_mp):
    fake_mp.tables['Group_Participants'] = [{**GP_GUS, 'End_Date': '2027-06-01T00:00:00'}]
    assert roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)['team'] == 'Blue'


@pytest.mark.parametrize('team', ['Purple', '', None, 'Unassigned', 'Red team'])
def test_assign_team_invalid_team(gus, fake_mp, team):
    with pytest.raises(InvalidInput):
        roster.assign_team(contact_id=1007, team=team, by=BY, conn=gus)
    assert fake_mp.calls == []


@pytest.mark.parametrize('by', [{'user_id': None, 'contact_id': 3001}, {'user_id': 42, 'contact_id': None},
                                SimpleNamespace(user_id=42), None])
def test_assign_team_requires_signed_in_leader(gus, fake_mp, by):
    with pytest.raises(InvalidInput):
        roster.assign_team(contact_id=1007, team='Blue', by=by, conn=gus)
    assert fake_mp.calls == []


@pytest.mark.parametrize('cid', [9999, 1008], ids=['unknown', 'inactive'])
def test_assign_team_kid_not_on_roster(gus, fake_mp, cid):
    with pytest.raises(NotFound):
        roster.assign_team(contact_id=cid, team='Blue', by=BY, conn=gus)
    assert fake_mp.calls == []


def test_assign_team_kid_without_group_participant_row(gus, fake_mp):
    gus.execute('UPDATE kids SET group_participant_id = NULL WHERE contact_id = 1007')
    with pytest.raises(NotFound):
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert fake_mp.calls == []


def test_assign_team_writes_disabled(gus, fake_mp):
    ro = FakeMP(writes_enabled=False)
    ro.tables['Group_Participants'] = [dict(GP_GUS)]
    mp.set_client(ro)
    add_attendance(gus, 1007, D_0923, team=None, mp_checked_in=True)
    before = (kids_table(gus), attendance_table(gus))
    with pytest.raises(mp.MPWritesDisabled):
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert ro.puts == []
    assert (kids_table(gus), attendance_table(gus)) == before
    assert assignments(gus) == []


def test_assign_team_mp_put_fails(gus, fake_mp):
    def pre_read(**kw):
        fake_mp.fail_with = mp.MPUnavailable('timeout', op='put', table='Group_Participants')   # the PUT times out
        return [dict(GP_GUS)]
    fake_mp.tables['Group_Participants'] = pre_read
    add_attendance(gus, 1007, D_0923, team=None, mp_checked_in=True)
    before = (kids_table(gus), attendance_table(gus))
    with pytest.raises(mp.MPUnavailable):
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert len(fake_mp.puts) == 1
    assert (kids_table(gus), attendance_table(gus)) == before
    assert assignments(gus) == []


@pytest.mark.parametrize('echo', [[], [{'Group_Participant_ID': 9007, 'Notes': 'Created by CheckIn'}]],
                         ids=['empty', 'old-notes'])
def test_assign_team_unconfirmed_write(gus, fake_mp, monkeypatch, echo):
    monkeypatch.setattr(fake_mp, 'put', lambda table, records, **kw: echo)
    with pytest.raises(mp.MPUnavailable) as exc:
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert exc.value.kind == 'write_not_confirmed'
    assert kid(gus, 1007)['team'] is None and assignments(gus) == []


def test_assign_team_lost_local_race_is_conflict(gus, fake_mp, monkeypatch):
    """Another leader's assignment lands locally while this request's PUT is in flight."""
    real_put = fake_mp.put

    def put(table, records, **kw):
        gus.execute("UPDATE kids SET team = 'Red', mp_notes = 'Red', team_assigned_at = now() WHERE contact_id = 1007")
        return real_put(table, records, **kw)
    monkeypatch.setattr(fake_mp, 'put', put)
    add_attendance(gus, 1007, D_0923, team=None, mp_checked_in=True)
    with pytest.raises(Conflict) as exc:
        roster.assign_team(contact_id=1007, team='Blue', by=BY, conn=gus)
    assert (exc.value.code, exc.value.extra) == ('already_assigned', {'team': 'Red'})
    assert kid(gus, 1007)['team'] == 'Red'
    assert att(gus, 1007)['team'] is None              # the losing request backfills nothing
    assert assignments(gus) == []
