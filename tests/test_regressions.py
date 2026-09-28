"""Regression tests for issues found while writing the PR1 suites."""
from datetime import datetime, timezone

import pytest

from config import load_config
from models import checkins, data, mp, roster, sync_state
from tests.conftest import D_0916, D_0923, TODAY, add_attendance, add_section, mark_synced

LEADER = {'contact_id': 77, 'user_id': 7, 'name': 'Pat Leader'}


def roster_row(cid, pid, gpid, first, last, notes, role=2, end=None, start='2026-08-15T00:00:00', nick=None):
    return {'Group_Participant_ID': gpid, 'Participant_ID': pid, 'Group_Role_ID': role, 'Notes': notes,
            'Start_Date': start, 'End_Date': end, 'Contact_ID': cid, 'Nickname': nick,
            'First_Name': first, 'Last_Name': last}


def allow_forced_retry(conn):
    """Forced syncs are rate-limited to one per 5 s per key; pretend the last attempt was a minute ago."""
    conn.execute("UPDATE sync_state SET attempted_at = now() - interval '1 minute'")


def seeded_roster_rows(conn):
    return [roster_row(r['contact_id'], r['participant_id'], r['group_participant_id'] or 9999 + r['contact_id'],
                       r['first_name'], r['last_name'], r['team'] or 'Created by CheckIn')
            for r in conn.execute('SELECT * FROM kids WHERE active ORDER BY contact_id')]


# -- data layer -------------------------------------------------------------------

def test_section_on_a_null_team_night_stays_unassigned(seeded, app_ctx):
    """A kid who was Unassigned that night keeps sections off every team, even after being assigned later."""
    add_attendance(seeded, 1007, D_0916, team=None, mp_checked_in=True)
    seeded.execute("UPDATE kids SET team = 'Green' WHERE contact_id = 1007")
    row = data.upsert_completed_section(contact_id=1007, meeting_date=D_0916, section='1.1',
                                        credits={'section_complete': True})
    assert row['team'] is None


def test_section_without_attendance_row_uses_current_team(seeded, app_ctx):
    row = data.set_section_credit(contact_id=1003, meeting_date=D_0923, section='1.2', field='silver_credit',
                                  value=True)
    assert row['team'] == 'Blue'


def test_progress_hides_inactive_kid_with_only_cleared_credits(seeded, app_ctx):
    add_section(seeded, 1008, D_0916, '1.1')                   # all credits false (a toggled-off tap)
    assert 1008 not in {r['contact_id'] for r in data.get_progress_summary()}
    add_section(seeded, 1008, D_0923, '1.2', complete=True)
    assert 1008 in {r['contact_id'] for r in data.get_progress_summary()}


def test_unmarking_manual_present_clears_items(seeded, app_ctx):
    data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': True, 'has_bible': True},
                        by=LEADER)
    row = data.set_attendance(contact_id=1003, meeting_date=D_0923, fields={'manual_present': False}, by=LEADER)
    assert not row['present'] and not row['has_bible']
    assert row['manual_present_by'] is None


def test_legacy_items_without_present_survive_an_edit(seeded, app_ctx):
    """Imported rows can have items without Present; saving the kid page with Present off must not wipe them."""
    add_attendance(seeded, 1004, D_0916, has_bible=True, has_book=True)
    row = data.set_attendance(contact_id=1004, meeting_date=D_0916,
                              fields={'manual_present': False, 'has_bible': True, 'has_book': False})
    assert row['has_bible'] and not row['has_book']


def test_by_without_contact_id_is_tolerated(seeded, app_ctx):
    row = data.set_attendance(contact_id=1001, meeting_date=D_0923, fields={'manual_present': True},
                              by={'name': 'No Id'})
    assert row['manual_present_by'] == 'No Id'


# -- roster / check-in sync ---------------------------------------------------------

def test_participant_moved_to_merged_contact_does_not_wedge_sync(seeded, fake_mp):
    rows = seeded_roster_rows(seeded)
    rows = [r for r in rows if r['Contact_ID'] != 1001]
    rows.append(roster_row(1101, 5001, 9101, 'Alice', 'Anders', 'Red'))   # MP merged 1001 into 1101
    fake_mp.tables['Group_Participants'] = rows
    allow_forced_retry(seeded)
    status = roster.sync_roster(force=True, conn=seeded)
    assert status.state == 'synced'
    old = seeded.execute('SELECT participant_id, active FROM kids WHERE contact_id = 1001').fetchone()
    assert old == {'participant_id': None, 'active': False}
    assert seeded.execute('SELECT participant_id FROM kids WHERE contact_id = 1101').fetchone()['participant_id'] == 5001


def test_team_set_in_mp_fills_tonights_unassigned_rows_only(seeded, fake_mp, app_ctx):
    add_attendance(seeded, 1007, D_0916, team=None, mp_checked_in=True)
    add_attendance(seeded, 1007, TODAY, team=None, mp_checked_in=True)
    add_section(seeded, 1007, TODAY, '1.2', team=None, complete=True)
    rows = seeded_roster_rows(seeded)
    for r in rows:
        if r['Contact_ID'] == 1007:
            r['Notes'] = 'Yellow'
    fake_mp.tables['Group_Participants'] = rows
    allow_forced_retry(seeded)
    assert roster.sync_roster(force=True).state == 'synced'
    teams = {r['meeting_date']: r['team'] for r in seeded.execute(
        'SELECT meeting_date, team FROM attendance WHERE contact_id = 1007')}
    assert teams == {D_0916: None, TODAY: 'Yellow'}
    assert seeded.execute('SELECT team FROM completed_sections WHERE contact_id = 1007').fetchone()['team'] == 'Yellow'


def test_mp_already_assigned_adopts_team_for_tonight(seeded, fake_mp, app_ctx):
    add_attendance(seeded, 1007, TODAY, team=None, mp_checked_in=True)
    fake_mp.tables['Group_Participants'] = [{'Group_Participant_ID': 9007, 'Group_ID': 697, 'Participant_ID': 5007,
                                             'Notes': 'Blue', 'End_Date': None}]
    with pytest.raises(Exception) as exc:
        roster.assign_team(contact_id=1007, team='Red', by=LEADER)
    assert getattr(exc.value, 'code', None) == 'already_assigned'
    assert seeded.execute('SELECT team FROM attendance WHERE contact_id = 1007').fetchone()['team'] == 'Blue'
    assert fake_mp.puts == []


def test_empty_event_lookup_is_rechecked_on_force(seeded, fake_mp):
    seeded.execute("DELETE FROM sync_state WHERE key = %s", (checkins.key_for(TODAY),))
    fake_mp.tables['Event_Groups'] = []
    assert checkins.sync_checkins(TODAY, conn=seeded).state == 'no_event'
    fake_mp.tables['Event_Groups'] = [{'Event_ID': 17940, 'Event_Start_Date': '2026-09-23T18:30:00'}]
    fake_mp.tables['Event_Participants'] = []
    allow_forced_retry(seeded)
    status = checkins.sync_checkins(TODAY, force=True, conn=seeded)
    assert status.state == 'synced'
    assert sync_state.read(seeded, checkins.key_for(TODAY))['detail']['event_ids'] == [17940]


def test_empty_result_warning_is_not_sticky(seeded, fake_mp):
    add_attendance(seeded, 1001, TODAY, mp_checked_in=True)
    mark_synced(seeded, checkins.key_for(TODAY),
                detail='{"event_ids": [17940], "events_checked_at": "%s"}' % datetime.now(timezone.utc).isoformat())
    fake_mp.tables['Event_Groups'] = [{'Event_ID': 17940, 'Event_Start_Date': '2026-09-23T18:30:00'}]
    fake_mp.tables['Event_Participants'] = []
    allow_forced_retry(seeded)
    checkins.sync_checkins(TODAY, force=True, conn=seeded)
    assert 'warning' in sync_state.read(seeded, checkins.key_for(TODAY))['detail']
    fake_mp.tables['Event_Participants'] = [{'Event_Participant_ID': 1, 'Event_ID': 17940, 'Participant_ID': 5001,
                                             'Participation_Status_ID': 4, 'Time_In': '2026-09-23T18:01:00',
                                             'Time_Confirmed': None, 'Group_ID': 697}]
    allow_forced_retry(seeded)
    checkins.sync_checkins(TODAY, force=True, conn=seeded)
    assert 'warning' not in sync_state.read(seeded, checkins.key_for(TODAY))['detail']


# -- MP client ------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Session:
    def __init__(self, body):
        self.headers, self.body = {}, body

    def post(self, *a, **kw):
        return _Resp(200, {'access_token': 't', 'expires_in': 3600})

    def request(self, *a, **kw):
        return _Resp(200, self.body)


@pytest.mark.parametrize('body', [{'Message': 'maintenance'}, 'oops', None])
def test_table_get_requires_a_list_body(body):
    client = mp.MPClient('https://mp.example.test/api', 'id', 'secret', session_factory=lambda: _Session(body))
    with pytest.raises(mp.MPUnavailable):
        client.get('Contacts', select='Contact_ID', orderby='Contact_ID')
    with pytest.raises(mp.MPUnavailable):
        client.get('Contacts', select='Contact_ID', top=1)


def test_parse_mp_datetime_converts_real_utc():
    assert mp.parse_mp_datetime('2026-09-23T23:31:02Z').strftime('%H:%M') == '18:31'
    assert mp.parse_mp_datetime('2026-09-23T18:31:02').strftime('%H:%M %Z') == '18:31 CDT'


# -- config ---------------------------------------------------------------------------

def test_preview_without_secret_key_disables_login():
    cfg = load_config({'VERCEL_ENV': 'preview', 'APP_BASE_URL': 'https://staging.example', 'MP_CLIENT_ID': 'a',
                       'MP_CLIENT_SECRET': 'b'})
    assert cfg['LOGIN_AVAILABLE'] is False
    cfg = load_config({'VERCEL_ENV': 'preview', 'APP_BASE_URL': 'https://staging.example', 'MP_CLIENT_ID': 'a',
                       'MP_CLIENT_SECRET': 'b', 'SECRET_KEY': 'k' * 40})
    assert cfg['LOGIN_AVAILABLE'] is True


@pytest.mark.parametrize('value', ['09/23/2026', '2026-13-01', '2026-W39-3', 'soon'])
def test_malformed_date_override_is_ignored(value):
    assert load_config({'TNT_DATE_OVERRIDE': value})['TNT_DATE_OVERRIDE'] is None


def test_secret_key_fallbacks_are_stripped():
    assert load_config({'SECRET_KEY_FALLBACKS': 'old1, old2 ,'})['SECRET_KEY_FALLBACKS'] == ['old1', 'old2']


# -- routes ---------------------------------------------------------------------------

def test_section_for_unassigned_kid_returns_to_the_unassigned_picker(client, seeded):
    add_attendance(seeded, 1007, TODAY, team=None, mp_checked_in=True)
    resp = client.post('/submit_section', data={'date_str': TODAY.isoformat(), 'team': 'Unassigned',
                                                'contact_id': '1007', 'section': '1.2', 'section_complete': 'on'})
    assert resp.status_code == 302
    assert resp.location.endswith('/home/2026-09-23/team/Unassigned/record_section')
