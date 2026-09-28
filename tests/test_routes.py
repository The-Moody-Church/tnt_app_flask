"""Route behavior: URL resolution, attendance/record-section/progress flows, write contract, errors, QA date."""
import re
from urllib.parse import urlsplit

import psycopg
import pytest

from models import data, mp
from tests.conftest import FetchClient, login_session, D_0909, D_0916, D_0923, D_0930, add_attendance, add_section

D = '2026-09-23'   # tonight (TODAY)
FUTURE = '2026-09-30'
MP_DOWN_NOTE = 'Can’t reach MinistryPlatform'

SUMMARY_RE = re.compile(r'<strong>(\d+)</strong> here · <strong>(\d+)</strong> not checked in')


def _html(resp):
    return resp.get_data(as_text=True)


def _loc(resp):
    """Redirect target as path[?query]: the Location header for GETs, the JSON `redirect` for POSTs."""
    target = resp.headers.get('Location') or (resp.get_json(silent=True) or {}).get('redirect')
    parts = urlsplit(target)
    return parts.path + (f'?{parts.query}' if parts.query else '')


def _schedule_item(body, href):
    m = re.search(rf'href="{re.escape(href)}">(.*?)</a>', body, re.S)
    return m and m.group(1)


def _summary(body):
    m = SUMMARY_RE.search(body)
    return (int(m[1]), int(m[2])) if m else None


def _groups(body, marker='Not checked in ('):
    """Split a list page into (here part, not-checked-in part)."""
    here, _, absent = body.partition(marker)
    return here, absent


def _att(conn, contact_id, d=D_0923):
    return conn.execute('SELECT * FROM attendance WHERE contact_id = %s AND meeting_date = %s',
                        (contact_id, d)).fetchone()


def _sec(conn, contact_id, d, section):
    return conn.execute('SELECT * FROM completed_sections WHERE contact_id = %s AND meeting_date = %s '
                        'AND section = %s', (contact_id, d, section)).fetchone()


def _entry(conn, entry_id):
    return conn.execute('SELECT * FROM completed_sections WHERE id = %s', (entry_id,)).fetchone()


def _credits(row):
    return (row['section_complete'], row['silver_credit'], row['gold_credit'])


FLAGS = ('has_bible', 'wearing_shirt', 'has_book', 'did_homework', 'has_dues')


def _flags(row):
    return {f: row[f] for f in FLAGS}


@pytest.fixture
def night(seeded):
    """Tonight: Alice checked in at the kiosk (with her Bible); Gus the walk-in checked in with no team."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, has_bible=True)
    add_attendance(seeded, 1007, D_0923, team=None, mp_checked_in=True)
    return seeded


# =============================================================================
# URL resolution: dates, teams, kids
# =============================================================================

@pytest.mark.parametrize('path, expected', [
    ('/home/not-a-date', '/'),
    ('/home/2026-9-23', '/'),
    ('/home/20260923', '/'),
    ('/home/2026-02-30', '/'),
    ('/home/2026-09-24', '/'),                      # valid date, not a meeting
    ('/home/2026-09-24/team/Red', '/'),
    ('/home/2026-09-24/team/Purple', '/'),          # the date is checked before the team
    ('/home/2026-09-24/team/Red/record_section', '/'),
    ('/home/2026-09-24/team/Red/record_section/kid/1001', '/'),
    ('/home/2026-09-24/team/Red/record_section/kid/1001/section/1.2', '/'),
    ('/home/2026-09-24/team/Red/kid/1001/section/1.2', '/'),
    ('/attendance/garbage', '/attendance'),
    ('/attendance/2026-09-24', '/attendance'),
    ('/attendance/2026-09-24/team/Red', '/attendance'),
    ('/attendance/2026-09-24/team/Red/kid/1001', '/attendance'),
    ('/attendance/2026-09-24/team/Red/checkin/1001', '/attendance'),
])
def test_unknown_or_malformed_date_redirects_to_date_list(client, seeded, path, expected):
    """Dates that aren't a YYYY-MM-DD meeting go back to the list."""
    resp = client.get(path)
    assert resp.status_code == 302
    assert _loc(resp) == expected


TEAM_PAGES = [
    ('/home/{d}/team/{t}', '/home/{d}'),
    ('/home/{d}/team/{t}/record_section', '/home/{d}'),
    ('/home/{d}/team/{t}/record_section/kid/1001', '/home/{d}'),
    ('/home/{d}/team/{t}/record_section/kid/1001/section/1.2', '/home/{d}'),
    ('/home/{d}/team/{t}/kid/1001/section/1.2', '/home/{d}'),
    ('/attendance/{d}/team/{t}', '/attendance/{d}'),
    ('/attendance/{d}/team/{t}/kid/1001', '/attendance/{d}'),
    ('/attendance/{d}/team/{t}/checkin/1001', '/attendance/{d}'),
]


@pytest.mark.parametrize('team', ['Purple', 'Redd', 'Red%20Team', "Red'%3Balert(1)",
                                  '%3Cscript%3Ealert(1)', 'Red%22%3E%3Cimg'])
@pytest.mark.parametrize('page, expected', TEAM_PAGES)
def test_unknown_team_redirects_to_date_page(client, seeded, page, expected, team):
    """Anything but a real team (or Unassigned where allowed) redirects; the value is never reflected."""
    resp = client.get(page.format(d=D, t=team))
    assert resp.status_code == 302
    assert _loc(resp) == expected.format(d=D)
    assert 'alert' not in resp.headers['Location'] and 'script' not in resp.headers['Location']


def test_team_names_are_case_insensitive(client, seeded):
    resp = client.get(f'/attendance/{D}/team/red')
    assert resp.status_code == 200
    assert 'Red Team' in _html(resp)


@pytest.mark.parametrize('path', [
    f'/attendance/{D}/team/Unassigned',
    f'/attendance/{D}/team/unassigned',
    f'/attendance/{D}/team/Unassigned/kid/1007',
    f'/attendance/{D}/team/Unassigned/checkin/1007',
    f'/home/{D}/team/Unassigned/record_section',
    f'/home/{D}/team/Unassigned/record_section/kid/1007',
    f'/home/{D}/team/Unassigned/record_section/kid/1007/section/1.2',
    f'/home/{D}/team/Unassigned/kid/1007/section/1.2',
])
def test_unassigned_allowed_on_attendance_and_record_pages(client, night, path):
    assert client.get(path).status_code == 200


def test_home_team_page_rejects_unassigned(client, night):
    """There are no team points for Unassigned, so the home team page bounces to the date."""
    resp = client.get(f'/home/{D}/team/Unassigned')
    assert resp.status_code == 302
    assert _loc(resp) == f'/home/{D}'


@pytest.mark.parametrize('path', [
    f'/attendance/{D}/team/Red/kid/9999',
    f'/attendance/{D}/team/Red/checkin/9999',
    f'/attendance/{D}/team/Red/kid/abc',
    f'/home/{D}/team/Red/record_section/kid/9999',
    f'/home/{D}/team/Red/record_section/kid/9999/section/1.2',
    f'/home/{D}/team/Red/kid/9999/section/1.2',
    '/progress/student/9999',
    '/progress/student/9999/section/1.2',
    '/progress/student/9999/record/1',
])
def test_unknown_kid_is_404(client, seeded, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert 'That page doesn’t exist.' in _html(resp)


def test_record_section_form_unknown_section_redirects_to_section_list(client, seeded):
    resp = client.get(f'/home/{D}/team/Red/record_section/kid/1001/section/9.9')
    assert resp.status_code == 302
    assert _loc(resp) == f'/home/{D}/team/Red/record_section/kid/1001'


# =============================================================================
# Home tab
# =============================================================================

@pytest.mark.parametrize('path, detail', [('/', '/home/'), ('/attendance', '/attendance/')])
def test_date_lists_link_every_meeting_and_mark_today(client, seeded, path, detail):
    body = _html(client.get(path))
    for d in ('2026-09-09', '2026-09-16', '2026-09-23', '2026-09-30', '2026-10-07'):
        assert f'href="{detail}{d}"' in body
    assert re.search(rf'id="current-date"\s+href="{detail}2026-09-23"', body)
    assert 'TODAY' in _schedule_item(body, f'{detail}2026-09-23')
    assert 'today-badge' not in _schedule_item(body, f'{detail}2026-09-30')
    assert body.count('is-no-club') == 1          # 'NO AWANA - Fall Break'


def test_home_details_shows_team_points_and_no_unassigned_card(client, night):
    add_section(night, 1001, D_0923, '1.2', complete=True, silver=True)
    body = _html(client.get(f'/home/{D}'))
    for team in ('Red', 'Blue', 'Green', 'Yellow'):
        assert f'href="/home/{D}/team/{team}"' in body
    # Red: 1 present + 1 Bible = 2 attendance points; (1 section + 1 silver) * 2 = 4 -> 6 per kid.
    assert 'Total Points - Total Points : 6000' in body
    assert 'Total Points - Total Points : 0' in body
    assert f'/home/{D}/team/Unassigned' not in body


def test_home_team_details_lists_tonights_non_empty_sections_for_the_team(client, night):
    add_section(night, 1001, D_0923, '1.2', complete=True)
    add_section(night, 1001, D_0916, '1.1', complete=True)      # another date
    add_section(night, 1002, D_0923, '1.3')                     # all credits false: hidden
    add_section(night, 1003, D_0923, '1.2', complete=True)      # Blue
    body = _html(client.get(f'/home/{D}/team/Red'))
    assert f'href="/home/{D}/team/Red/kid/1001/section/1.2"' in body
    assert '/section/1.1' not in body
    assert 'Bo Brandt' not in body and '/kid/1002/' not in body
    assert 'Cara Cole' not in body
    assert f'href="/home/{D}/team/Red/record_section"' in body
    assert 'Total Points : ' in body


def test_home_section_details_shows_entry_or_empty_note(client, night):
    add_section(night, 1001, D_0923, '1.2', complete=True, gold=True)
    body = _html(client.get(f'/home/{D}/team/Red/kid/1001/section/1.2'))
    assert 'name="section_complete" checked' in body
    assert re.search(r'name="silver_credit"\s*>', body)
    assert 'name="gold_credit" checked' in body
    assert 'class="edit-button"' in body
    empty = _html(client.get(f'/home/{D}/team/Red/kid/1001/section/1.3'))
    assert 'No completion data found' in empty
    assert 'id="editForm"' not in empty


# =============================================================================
# Attendance tab: team list
# =============================================================================

def test_team_attendance_shows_only_present_kids_by_default(client, night):
    add_attendance(night, 1002, D_0923, manual_present=True)
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert _summary(body) == (2, 0)
    assert 'Alice Anders' in body and 'Bo Brandt' in body
    assert '✓ Checked in · 6:12pm' in body
    assert '✋ Marked present' in body
    assert 'kid-status is-manual' in body
    assert f'href="/attendance/{D}/team/Red/kid/1001"' in body
    assert 'Gus Gray' not in body                       # the walk-in is Unassigned, not Red


def test_team_attendance_hides_not_checked_in_kids_until_show_all(client, night):
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert _summary(body) == (1, 1)
    assert 'Bo Brandt' not in body
    assert 'Not checked in (' not in body
    assert f'href="/attendance/{D}/team/Red?show=all"' in body

    body = _html(client.get(f'/attendance/{D}/team/Red?show=all'))
    assert _summary(body) == (1, 1)
    here, absent = _groups(body)
    assert 'Alice Anders' in here and 'Bo Brandt' not in here
    assert 'Not checked in (1)' in body
    assert 'Bo Brandt' in absent
    assert f'href="/attendance/{D}/team/Red/checkin/1002"' in absent
    assert 'Tap to mark present' in absent
    assert 'Here only' in body


def test_team_attendance_empty_note_when_nobody_is_here(client, seeded):
    body = _html(client.get(f'/attendance/{D}/team/Blue'))
    assert _summary(body) == (0, 2)
    assert 'No one from Blue is checked in yet' in body


def test_team_attendance_future_date_is_read_only(client, seeded):
    body = _html(client.get(f'/attendance/{FUTURE}/team/Red?show=all'))
    _, absent = _groups(body)
    assert 'Alice Anders' in absent
    assert 'Tap to mark present' not in absent


def test_inactive_kid_only_listed_when_they_have_a_row(client, seeded):
    body = _html(client.get(f'/attendance/{D}/team/Red?show=all'))
    assert 'Hal Hart' not in body
    add_attendance(seeded, 1008, D_0923, mp_checked_in=True)
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert 'Hal Hart' in _groups(body)[0]


def test_board_uses_the_team_on_that_nights_row(client, seeded):
    """A kid whose row for the date says Blue is on Blue's list that night, even though kids.team is Red."""
    add_attendance(seeded, 1001, D_0923, team='Blue', mp_checked_in=True)
    assert 'Alice Anders' not in _html(client.get(f'/attendance/{D}/team/Red?show=all'))
    blue = _html(client.get(f'/attendance/{D}/team/Blue'))
    assert 'Alice Anders' in _groups(blue)[0]
    assert _summary(blue) == (1, 2)


def test_unassigned_banner_on_team_page_when_an_unassigned_kid_is_here(client, night):
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert 'banner-warning' in body
    assert f'href="/attendance/{D}/team/Unassigned"' in body
    assert '1 checked-in kid has no team' in body

    add_attendance(night, 1006, D_0923, team=None, mp_checked_in=True)   # tonight's row has no team
    assert '2 checked-in kids have no team' in _html(client.get(f'/attendance/{D}/team/Blue'))


def test_no_unassigned_banner_when_the_unassigned_kid_is_not_here(client, seeded):
    """Gus is on the roster with no team, but hasn't checked in: nothing to fix yet."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    assert 'banner-warning' not in _html(client.get(f'/attendance/{D}/team/Red'))


def test_unassigned_page_lists_the_walk_in(client, night):
    body = _html(client.get(f'/attendance/{D}/team/Unassigned'))
    assert '<h1 class="header-title">Unassigned</h1>' in body
    assert _summary(body) == (1, 0)
    assert 'Gus Gray' in body
    assert f'href="/attendance/{D}/team/Unassigned/kid/1007"' in body
    assert 'Alice Anders' not in body
    assert 'banner-warning' not in body


def test_unassigned_page_show_all_links_to_checkin_form(client, seeded):
    body = _html(client.get(f'/attendance/{D}/team/Unassigned?show=all'))
    assert _summary(body) == (0, 1)
    _, absent = _groups(body)
    assert f'href="/attendance/{D}/team/Unassigned/checkin/1007"' in absent


def test_kid_name_is_escaped(client, seeded):
    seeded.execute("UPDATE kids SET nickname = '<img src=x onerror=alert(1)>' WHERE contact_id = 1001")
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True)
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert '&lt;img src=x onerror=alert(1)&gt;' in body
    assert '<img src=x' not in body


# =============================================================================
# Attendance tab: date page (team cards)
# =============================================================================

def _card(body, href):
    m = re.search(rf'<a class="team-card" href="{re.escape(href)}">(.*?)</a>', body, re.S)
    return m and ' '.join(m.group(1).split())


def test_attendance_details_shows_team_cards_and_unassigned_card(client, night):
    body = _html(client.get(f'/attendance/{D}'))
    assert 'Kids Present - 1' in _card(body, f'/attendance/{D}/team/Red')
    assert 'Kids Present - 0' in _card(body, f'/attendance/{D}/team/Blue')
    unassigned = _card(body, f'/attendance/{D}/team/Unassigned')
    assert unassigned and 'Kids Present - 1' in unassigned


def test_attendance_details_unassigned_card_counts_roster_kids_not_yet_here(client, seeded):
    unassigned = _card(_html(client.get(f'/attendance/{D}')), f'/attendance/{D}/team/Unassigned')
    assert unassigned and 'Kids Present - 0' in unassigned


def test_attendance_details_no_unassigned_card_without_unassigned_kids(client, seeded):
    seeded.execute("UPDATE kids SET team = 'Green' WHERE contact_id = 1007")
    body = _html(client.get(f'/attendance/{D}'))
    assert f'/attendance/{D}/team/Unassigned' not in body
    assert _card(body, f'/attendance/{D}/team/Green')


# =============================================================================
# Record-section kid picker
# =============================================================================

def test_kid_picker_shows_only_present_kids_by_default(client, night):
    body = _html(client.get(f'/home/{D}/team/Red/record_section'))
    assert _summary(body) == (1, 1)
    assert f'href="/home/{D}/team/Red/record_section/kid/1001"' in body
    assert 'Bo Brandt' not in body
    assert f'href="/home/{D}/team/Red/record_section?show=all"' in body
    assert f'href="/home/{D}/team/Red"' in body                  # back to the team page


def test_kid_picker_show_all_lists_absent_kids(client, night):
    body = _html(client.get(f'/home/{D}/team/Red/record_section?show=all'))
    here, absent = _groups(body)
    assert 'Alice Anders' in here and 'Bo Brandt' not in here
    assert 'Not checked in (1)' in body
    assert f'href="/home/{D}/team/Red/record_section/kid/1002"' in absent


def test_kid_picker_unassigned(client, night):
    body = _html(client.get(f'/home/{D}/team/Unassigned/record_section'))
    assert _summary(body) == (1, 0)
    assert f'href="/home/{D}/team/Unassigned/record_section/kid/1007"' in body
    assert f'<a class="back-btn" href="/home/{D}">' in body       # no Unassigned team page to go back to
    assert 'Alice Anders' not in body


# =============================================================================
# Kid attendance details and check-in form
# =============================================================================

def test_kid_details_mp_checkin_is_read_only_present(client, night):
    body = _html(client.get(f'/attendance/{D}/team/Red/kid/1001'))
    assert '✓ Checked in · 6:12pm' in body
    assert 'name="manual_present"' not in body
    assert 'can_mark_present' not in body
    assert 'name="has_bible" checked' in body
    assert 'class="edit-button"' in body


def test_kid_details_offers_manual_present_toggle_when_not_mp_checked_in(client, seeded):
    body = _html(client.get(f'/attendance/{D}/team/Red/kid/1002'))
    assert 'name="can_mark_present" value="1"' in body
    assert re.search(r'name="manual_present"\s*>', body)          # unchecked
    assert 'present-status is-absent' in body
    assert 'Not checked in' in body


def test_kid_details_manual_present_shows_who(client, seeded):
    add_attendance(seeded, 1002, D_0923, manual_present=True)
    seeded.execute("UPDATE attendance SET manual_present_by = 'Leader Lee' WHERE contact_id = 1002")
    body = _html(client.get(f'/attendance/{D}/team/Red/kid/1002'))
    assert '✋ Marked present by Leader Lee' in body
    assert 'name="manual_present" checked' in body
    assert 'name="can_mark_present" value="1"' in body


def test_kid_details_future_date_has_no_edit_button(client, seeded):
    body = _html(client.get(f'/attendance/{FUTURE}/team/Red/kid/1001'))
    assert 'class="edit-button"' not in body


def test_checkin_form(client, seeded):
    body = _html(client.get(f'/attendance/{D}/team/Blue/checkin/1003'))
    assert 'name="manual_present" checked' in body
    assert 'name="team" value="Blue"' in body
    assert 'name="contact_id" value="1003"' in body
    assert f'href="/attendance/{D}/team/Blue?show=all"' in body
    future = _html(client.get(f'/attendance/{FUTURE}/team/Blue/checkin/1003'))
    assert 'hasn’t happened yet' in future
    assert 'name="manual_present"' not in future


# =============================================================================
# POST /submit_checkin and /edit_attendance
# =============================================================================

def test_submit_checkin_marks_present_with_items(client, seeded, fake_mp):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1003', 'team': 'Blue',
                                                'manual_present': 'on', 'has_bible': 'on', 'has_dues': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _loc(resp) == f'/attendance/{D}/team/Blue'
    row = _att(seeded, 1003)
    assert row['manual_present'] and row['present'] and not row['mp_checked_in']
    assert row['manual_present_at'] is not None
    assert row['team'] == 'Blue' and row['source'] == 'app'
    assert _flags(row) == {'has_bible': True, 'wearing_shirt': False, 'has_book': False,
                           'did_homework': False, 'has_dues': True}
    body = _html(client.get(f'/attendance/{D}/team/Blue'))
    assert 'Cara Cole' in _groups(body)[0]
    assert '✋ Marked present' in body
    assert fake_mp.puts == []


def test_submit_checkin_present_only(client, seeded):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1003', 'team': 'Blue',
                                                'manual_present': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    row = _att(seeded, 1003)
    assert row['present'] and not any(_flags(row).values())


NATIVE = {'X-Requested-With': ''}      # a plain HTML form post (no JavaScript)


def test_submit_checkin_native_form_post_redirects_after_saving(client, seeded):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1003', 'team': 'Blue',
                                                'manual_present': 'on'}, headers=NATIVE)
    assert resp.status_code == 303
    assert urlsplit(resp.headers['Location']).path == f'/attendance/{D}/team/Blue'
    assert _att(seeded, 1003)['present']


def test_submit_checkin_native_form_post_error_is_an_html_page(client, seeded):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1003', 'team': 'Blue',
                                                'has_bible': 'on'}, headers=NATIVE)
    assert resp.status_code == 409 and resp.mimetype == 'text/html'
    assert resp.get_json(silent=True) is None
    assert _att(seeded, 1003) is None


@pytest.mark.parametrize('accept', ['application/json', 'application/json, text/html;q=0.5'])
def test_submit_checkin_json_accept_gets_json(client, seeded, accept):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1003', 'team': 'Blue',
                                                'manual_present': 'on'}, headers={**NATIVE, 'Accept': accept})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True


def test_submit_checkin_items_without_present_is_409_and_saves_nothing(client, seeded):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1003', 'team': 'Blue',
                                                'has_bible': 'on'})
    assert resp.status_code == 409
    body = resp.get_json()
    assert body['ok'] is False and body['error'] == 'not_present'
    assert _att(seeded, 1003) is None


def test_submit_checkin_items_without_present_keeps_existing_row(client, seeded):
    add_attendance(seeded, 1003, D_0923)
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1003', 'team': 'Blue',
                                                'has_book': 'on'})
    assert resp.status_code == 409
    assert not any(_flags(_att(seeded, 1003)).values())


def test_submit_checkin_items_for_mp_checked_in_kid(client, night):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1001', 'team': 'Red',
                                                'wearing_shirt': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    row = _att(night, 1001)
    assert row['mp_checked_in'] and not row['manual_present']
    assert row['wearing_shirt'] and not row['has_bible']      # the form sends every flag


def test_submit_checkin_unassigned_redirects_to_unassigned_list(client, seeded):
    resp = client.post('/submit_checkin', data={'date_str': D, 'contact_id': '1007', 'team': 'Unassigned',
                                                'manual_present': 'on'})
    assert _loc(resp) == f'/attendance/{D}/team/Unassigned'
    assert _att(seeded, 1007)['team'] is None


@pytest.mark.parametrize('form, status, code', [
    ({'contact_id': '1003', 'manual_present': 'on'}, 400, 'invalid_input'),                          # no date
    ({'date_str': '2026-9-23', 'contact_id': '1003', 'manual_present': 'on'}, 400, 'invalid_input'),
    ({'date_str': D, 'manual_present': 'on'}, 400, 'invalid_input'),                                # no kid
    ({'date_str': D, 'contact_id': 'abc', 'manual_present': 'on'}, 400, 'invalid_input'),
    ({'date_str': D, 'contact_id': '-1', 'manual_present': 'on'}, 400, 'invalid_input'),
    ({'date_str': '2026-09-22', 'contact_id': '1003', 'manual_present': 'on'}, 400, 'invalid_input'),  # no meeting
    ({'date_str': D, 'contact_id': '9999', 'manual_present': 'on'}, 404, 'not_found'),
])
def test_submit_checkin_rejects_bad_input(client, seeded, form, status, code):
    resp = client.post('/submit_checkin', data={'team': 'Blue', **form})
    assert resp.status_code == status
    body = resp.get_json()
    assert body['ok'] is False and body['error'] == code and body['message']
    assert resp.headers['Cache-Control'] == 'no-store'
    assert seeded.execute('SELECT COUNT(*) AS n FROM attendance').fetchone()['n'] == 0


def test_edit_attendance_sets_flags_exactly(client, night):
    resp = client.post('/edit_attendance', data={'date_str': D, 'team_name': 'Red', 'contact_id': '1001',
                                                 'has_book': 'on', 'did_homework': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _loc(resp) == f'/attendance/{D}/team/Red/kid/1001'
    row = _att(night, 1001)
    assert _flags(row) == {'has_bible': False, 'wearing_shirt': False, 'has_book': True,
                           'did_homework': True, 'has_dues': False}
    assert row['mp_checked_in'] and not row['manual_present']


def test_edit_attendance_ignores_manual_present_without_can_mark_present(client, night):
    """The MP-checked-in form has no Present toggle; a stray manual_present is ignored."""
    client.post('/edit_attendance', data={'date_str': D, 'team_name': 'Red', 'contact_id': '1001',
                                          'manual_present': 'on', 'has_bible': 'on'})
    row = _att(night, 1001)
    assert not row['manual_present'] and row['has_bible']


def test_edit_attendance_mark_present_and_items(client, seeded):
    resp = client.post('/edit_attendance', data={'date_str': D, 'team_name': 'Blue', 'contact_id': '1003',
                                                 'can_mark_present': '1', 'manual_present': 'on',
                                                 'wearing_shirt': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    row = _att(seeded, 1003)
    assert row['manual_present'] and row['present'] and row['wearing_shirt']


def test_edit_attendance_unmarking_manual_present_clears_items(client, seeded):
    add_attendance(seeded, 1002, D_0923, manual_present=True, has_bible=True, has_dues=True)
    seeded.execute("UPDATE attendance SET manual_present_by = 'Leader Lee', manual_present_at = now() "
                   "WHERE contact_id = 1002")
    resp = client.post('/edit_attendance', data={'date_str': D, 'team_name': 'Red', 'contact_id': '1002',
                                                 'can_mark_present': '1', 'has_bible': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    row = _att(seeded, 1002)
    assert not row['manual_present'] and not row['present']
    assert not any(_flags(row).values())
    assert row['manual_present_by'] is None and row['manual_present_at'] is None
    body = _html(client.get(f'/attendance/{D}/team/Red?show=all'))
    assert 'Bo Brandt' in _groups(body)[1]


def test_edit_attendance_items_for_absent_kid_is_409(client, seeded):
    add_attendance(seeded, 1002, D_0923)
    resp = client.post('/edit_attendance', data={'date_str': D, 'team_name': 'Red', 'contact_id': '1002',
                                                 'has_dues': 'on'})
    assert resp.status_code == 409
    assert resp.get_json()['error'] == 'not_present'
    assert not _att(seeded, 1002)['has_dues']


def test_edit_attendance_missing_team_name_redirects_to_unassigned(client, night):
    resp = client.post('/edit_attendance', data={'date_str': D, 'contact_id': '1007', 'has_bible': 'on'})
    assert _loc(resp) == f'/attendance/{D}/team/Unassigned/kid/1007'


# =============================================================================
# Future dates are never writable
# =============================================================================

ENTRY = object()   # replaced by a future-dated completed_sections id

FUTURE_WRITES = [
    ('/submit_checkin', {'date_str': FUTURE, 'contact_id': '1003', 'team': 'Blue', 'manual_present': 'on',
                         'has_bible': 'on'}),
    ('/submit_checkin', {'date_str': '2027-01-06', 'contact_id': '1003', 'team': 'Blue',
                         'manual_present': 'on'}),     # future and not a meeting: still future_date
    ('/edit_attendance', {'date_str': FUTURE, 'contact_id': '1003', 'team_name': 'Blue',
                          'can_mark_present': '1', 'manual_present': 'on'}),
    ('/submit_section', {'date_str': FUTURE, 'contact_id': '1003', 'team': 'Blue', 'section': '1.3',
                         'section_complete': 'on'}),
    ('/edit_section', {'date_str': FUTURE, 'entry_id': ENTRY, 'team_name': 'Blue', 'gold_credit': 'on'}),
    ('/edit_progress_section', {'entry_id': ENTRY, 'gold_credit': 'on'}),
]


@pytest.mark.parametrize('path, form', FUTURE_WRITES)
def test_future_date_writes_are_rejected(client, seeded, path, form):
    entry_id = add_section(seeded, 1003, D_0930, '1.3', silver=True)   # e.g. an import typo
    form = {k: (str(entry_id) if v is ENTRY else v) for k, v in form.items()}
    resp = client.post(path, data=form)
    assert resp.status_code == 422
    body = resp.get_json()
    assert body == {'ok': False, 'error': 'future_date', 'message': body['message']}
    assert body['message']
    assert seeded.execute('SELECT COUNT(*) AS n FROM attendance').fetchone()['n'] == 0
    assert _credits(_entry(seeded, entry_id)) == (False, True, False)


def test_edit_section_future_check_uses_the_entrys_date(client, seeded):
    entry_id = add_section(seeded, 1003, D_0930, '1.3', silver=True)
    resp = client.post('/edit_section', data={'date_str': D, 'entry_id': str(entry_id), 'team_name': 'Blue',
                                              'gold_credit': 'on'})
    assert resp.status_code == 422
    assert _credits(_entry(seeded, entry_id)) == (False, True, False)


def test_record_section_form_future_date_is_read_only(client, seeded):
    body = _html(client.get(f'/home/{FUTURE}/team/Red/record_section/kid/1001/section/1.3'))
    assert 'hasn’t happened yet' in body
    assert 'name="section_complete"' not in body


def test_past_meeting_dates_are_writable(client, seeded):
    resp = client.post('/submit_checkin', data={'date_str': '2026-09-16', 'contact_id': '1003', 'team': 'Blue',
                                                'manual_present': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _att(seeded, 1003, D_0916)['present']


# =============================================================================
# Record a completed section
# =============================================================================

def _submit(client, **form):
    base = {'date_str': D, 'contact_id': '1001', 'team': 'Red', 'section': '1.2'}
    return client.post('/submit_section', data={**base, **form})


def test_submit_section_creates_row_and_redirects_to_team(client, night):
    resp = _submit(client, section_complete='on')
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _loc(resp) == f'/home/{D}/team/Red'
    row = _sec(night, 1001, D_0923, '1.2')
    assert _credits(row) == (True, False, False)
    assert row['team'] == 'Red' and row['source'] == 'app'


def test_submit_section_or_merges_credits(client, night):
    _submit(client, section_complete='on')
    _submit(client, silver_credit='on')
    assert _credits(_sec(night, 1001, D_0923, '1.2')) == (True, True, False)
    _submit(client, gold_credit='on')
    assert _credits(_sec(night, 1001, D_0923, '1.2')) == (True, True, True)
    n = night.execute('SELECT COUNT(*) AS n FROM completed_sections WHERE contact_id = 1001').fetchone()['n']
    assert n == 1


def test_submit_section_without_a_credit_is_400(client, night):
    resp = _submit(client)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body['ok'] is False and body['error'] == 'invalid_input'
    assert _sec(night, 1001, D_0923, '1.2') is None


def test_submit_section_derives_team_on_the_server(client, night):
    """The posted team only picks the redirect; the row takes the kid's team."""
    resp = _submit(client, team='Blue', section_complete='on')
    assert _loc(resp) == f'/home/{D}/team/Blue'
    assert _sec(night, 1001, D_0923, '1.2')['team'] == 'Red'


def test_submit_section_takes_the_team_on_tonights_attendance_row(client, seeded):
    add_attendance(seeded, 1001, D_0923, team='Blue', mp_checked_in=True)
    _submit(client, section_complete='on')
    assert _sec(seeded, 1001, D_0923, '1.2')['team'] == 'Blue'


def test_submit_section_for_unassigned_kid(client, night):
    resp = _submit(client, contact_id='1007', team='Unassigned', section_complete='on')
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _sec(night, 1007, D_0923, '1.2')['team'] is None
    assert client.get(_loc(resp), follow_redirects=True).status_code == 200


def test_submit_section_with_spaces_in_section_name(client, seeded):
    resp = _submit(client, date_str='2026-09-09', section='Start Zone 1', gold_credit='on')
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _credits(_sec(seeded, 1001, D_0909, 'Start Zone 1')) == (False, False, True)


@pytest.mark.parametrize('form, status, code', [
    ({'section': '9.9'}, 400, 'invalid_input'),
    ({'section': ''}, 400, 'invalid_input'),
    ({'date_str': '2026-09-22'}, 400, 'invalid_input'),     # not a meeting date
    ({'date_str': 'yesterday'}, 400, 'invalid_input'),
    ({'contact_id': ''}, 400, 'invalid_input'),
    ({'contact_id': '9999'}, 404, 'not_found'),
])
def test_submit_section_rejects_bad_input(client, seeded, form, status, code):
    resp = _submit(client, section_complete='on', **form)
    assert resp.status_code == status
    body = resp.get_json()
    assert body['ok'] is False and body['error'] == code
    assert seeded.execute('SELECT COUNT(*) AS n FROM completed_sections').fetchone()['n'] == 0


def test_record_section_list_shows_coalesced_status(client, seeded):
    add_section(seeded, 1001, D_0909, 'Start Zone 1', complete=True, silver=True)
    add_section(seeded, 1001, D_0916, 'Start Zone 1', gold=True)
    add_section(seeded, 1001, D_0916, '1.1', complete=True)
    body = _html(client.get(f'/home/{D}/team/Red/record_section/kid/1001'))
    sz1 = f'/home/{D}/team/Red/record_section/kid/1001/section/Start%20Zone%201'
    assert re.search(rf'class="section-row complete"\s+href="{re.escape(sz1)}"', body)
    assert '✓ 9/9' in body and '✓ 9/16' in body
    assert body.count('class="section-row complete"') == 1
    for s in ('Start%20Zone%202', '1.1', '1.2', '1.6'):
        assert f'/record_section/kid/1001/section/{s}"' in body


def test_record_section_form_only_offers_missing_credits(client, seeded):
    add_section(seeded, 1001, D_0916, '1.2', complete=True)
    body = _html(client.get(f'/home/{D}/team/Red/record_section/kid/1001/section/1.2'))
    assert 'name="section_complete"' not in body
    assert 'name="silver_credit"' in body and 'name="gold_credit"' in body
    assert re.search(r'Main &mdash; <span[^>]*>9/16<', body)
    assert 'name="team" value="Red"' in body and 'name="section" value="1.2"' in body


def test_record_section_form_all_done(client, seeded):
    add_section(seeded, 1001, D_0916, '1.2', complete=True, silver=True)
    add_section(seeded, 1001, D_0923, '1.2', gold=True)
    body = _html(client.get(f'/home/{D}/team/Red/record_section/kid/1001/section/1.2'))
    assert 'All credits already recorded!' in body
    assert 'id="submitBtn"' not in body


def test_record_section_form_with_spaces_in_section_name(client, seeded):
    resp = client.get(f'/home/{D}/team/Red/record_section/kid/1001/section/Start%20Zone%201')
    assert resp.status_code == 200
    assert 'name="section" value="Start Zone 1"' in _html(resp)


# =============================================================================
# Edit a section entry (home tab)
# =============================================================================

def test_edit_section_clears_credits_and_redirects_to_details(client, night):
    entry_id = add_section(night, 1001, D_0923, '1.2', complete=True, silver=True)
    resp = client.post('/edit_section', data={'date_str': D, 'team_name': 'Red', 'entry_id': str(entry_id)})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _loc(resp) == f'/home/{D}/team/Red/kid/1001/section/1.2'
    assert _credits(_entry(night, entry_id)) == (False, False, False)      # kept, never deleted
    assert '/kid/1001/section/1.2' not in _html(client.get(f'/home/{D}/team/Red'))
    assert client.get(_loc(resp)).status_code == 200


def test_edit_section_sets_exact_values(client, night):
    entry_id = add_section(night, 1001, D_0923, '1.2', complete=True, silver=True)
    client.post('/edit_section', data={'date_str': D, 'team_name': 'Red', 'entry_id': str(entry_id),
                                       'section_complete': 'on', 'gold_credit': 'on'})
    assert _credits(_entry(night, entry_id)) == (True, False, True)


def test_edit_section_with_spaces_redirects_to_encoded_url(client, seeded):
    entry_id = add_section(seeded, 1001, D_0909, 'Start Zone 1', complete=True)
    resp = client.post('/edit_section', data={'date_str': '2026-09-09', 'team_name': 'Red',
                                              'entry_id': str(entry_id), 'silver_credit': 'on'})
    assert _loc(resp) == '/home/2026-09-09/team/Red/kid/1001/section/Start%20Zone%201'
    assert client.get(_loc(resp)).status_code == 200


@pytest.mark.parametrize('entry, status, code', [('999999', 404, 'not_found'), ('', 400, 'invalid_input'),
                                                 ('x', 400, 'invalid_input')])
def test_edit_section_bad_entry(client, seeded, entry, status, code):
    resp = client.post('/edit_section', data={'date_str': D, 'team_name': 'Red', 'entry_id': entry,
                                              'gold_credit': 'on'})
    assert resp.status_code == status
    assert resp.get_json()['error'] == code


# =============================================================================
# Progress tab
# =============================================================================

@pytest.fixture
def history(seeded):
    """Alice: Start Zone 1 all credits, 1.1 twice (two dates), 1.2 main+silver, 1.3 cleared. Hal: 1.1."""
    ids = {
        'sz1': add_section(seeded, 1001, D_0909, 'Start Zone 1', complete=True, silver=True, gold=True),
        '1.1a': add_section(seeded, 1001, D_0916, '1.1', complete=True),
        '1.2': add_section(seeded, 1001, D_0923, '1.2', complete=True, silver=True),
        '1.1b': add_section(seeded, 1001, D_0923, '1.1', complete=True),
        '1.3': add_section(seeded, 1001, D_0923, '1.3'),
        'bo': add_section(seeded, 1002, D_0923, '1.2', complete=True),
        'hal': add_section(seeded, 1008, D_0916, '1.1', complete=True),
    }
    return ids


def _student(body, contact_id):
    m = re.search(rf'<a class="schedule-item student-item" href="/progress/student/{contact_id}">(.*?)</a>',
                  body, re.S)
    return m and ' '.join(m.group(1).split())


def test_progress_list_counts_and_teams(client, history):
    body = _html(client.get('/progress'))
    alice = _student(body, 1001)
    assert 'Alice Anders' in alice and 'Red Team' in alice
    assert '📖 3' in alice and '🥈 2' in alice and '🥇 1' in alice
    gus = _student(body, 1007)
    assert '>Unassigned<' in gus and '📖 0' in gus
    hal = _student(body, 1008)
    assert hal and 'Hal Hart' in hal and '· inactive' in hal
    assert 'inactive' not in alice


def test_progress_list_hides_inactive_kid_without_history(client, seeded):
    body = _html(client.get('/progress'))
    assert _student(body, 1008) is None
    assert _student(body, 1001)


def test_student_progress_coalesces_in_book_order(client, history):
    body = _html(client.get('/progress/student/1001'))
    order = [body.index(f'Section {s}<') for s in ('Start Zone 1', '1.1', '1.2')]
    assert order == sorted(order)
    assert 'Section 1.3<' not in body                       # the cleared row
    assert 'href="/progress/student/1001/section/Start%20Zone%201"' in body
    assert body.count('class="section-row complete"') == 1
    assert '✓ 9/16' in body                                  # 1.1: first date wins
    stats = dict((label, int(n)) for n, label in
                 re.findall(r'>(\d+)</div>\s*<div[^>]*>(Sections|Silver|Gold)</div>', body))
    assert stats == {'Sections': 3, 'Silver': 2, 'Gold': 1}


def test_student_progress_unassigned_and_inactive(client, history):
    assert '<div class="team-badge unassigned">Unassigned</div>' in _html(client.get('/progress/student/1007'))
    body = _html(client.get('/progress/student/1008'))
    assert 'Hal Hart' in body and 'href="/progress/student/1008/section/1.1"' in body


def test_student_section_log_lists_each_record(client, history):
    body = _html(client.get('/progress/student/1001/section/1.1'))
    first = body.index(f'href="/progress/student/1001/record/{history["1.1a"]}"')
    second = body.index(f'href="/progress/student/1001/record/{history["1.1b"]}"')
    assert first < second
    assert '9/16' in body and '9/23' in body


def test_student_section_log_with_spaces_in_section_name(client, history):
    resp = client.get('/progress/student/1001/section/Start%20Zone%201')
    assert resp.status_code == 200
    body = _html(resp)
    assert 'Section Start Zone 1' in body
    assert f'href="/progress/student/1001/record/{history["sz1"]}"' in body


def test_student_section_log_hides_cleared_rows(client, history):
    body = _html(client.get('/progress/student/1001/section/1.3'))
    assert 'No entries found.' in body
    assert '/record/' not in body


def test_student_section_details(client, history):
    body = _html(client.get(f'/progress/student/1001/record/{history["1.2"]}'))
    assert 'name="section_complete" checked' in body and 'name="silver_credit" checked' in body
    assert 'class="edit-button"' in body
    assert 'href="/progress/student/1001/section/1.2"' in body


@pytest.mark.parametrize('entry', ['bo', 'missing'])
def test_student_section_details_for_another_kids_entry_redirects(client, history, entry):
    entry_id = history.get(entry, 999999)
    resp = client.get(f'/progress/student/1001/record/{entry_id}')
    assert resp.status_code == 302
    assert _loc(resp) == '/progress/student/1001'


def test_student_section_details_future_entry_is_read_only(client, seeded):
    entry_id = add_section(seeded, 1001, D_0930, '1.3', complete=True)
    body = _html(client.get(f'/progress/student/1001/record/{entry_id}'))
    assert 'class="edit-button"' not in body


def test_edit_progress_section_sets_exact_values(client, seeded, history):
    entry_id = history['1.2']
    resp = client.post('/edit_progress_section', data={'entry_id': str(entry_id), 'gold_credit': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert _loc(resp) == f'/progress/student/1001/record/{entry_id}'
    assert _credits(_entry(seeded, entry_id)) == (False, False, True)


@pytest.mark.parametrize('entry, status, code', [('999999', 404, 'not_found'), ('', 400, 'invalid_input')])
def test_edit_progress_section_bad_entry(client, seeded, entry, status, code):
    resp = client.post('/edit_progress_section', data={'entry_id': entry, 'gold_credit': 'on'})
    assert resp.status_code == status
    assert resp.get_json()['error'] == code


@pytest.mark.parametrize('path', ['/progress/student/Alice%20Anders', '/progress/student/Alice%20Anders/section/1.1',
                                  '/progress/student/Alice%20Anders/record/1'])
def test_old_name_based_progress_urls_are_404(client, history, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert 'That page doesn’t exist.' in _html(resp)


# =============================================================================
# Removed endpoints, error pages
# =============================================================================

@pytest.mark.parametrize('path', ['/metrics', '/test/reset', '/test/clear_cache', '/test'])
def test_debug_endpoints_are_gone(client, conn, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.mimetype == 'text/html'
    assert client.post(path).get_json() == {'ok': False, 'error': 'not_found', 'message': 'Not found'}


def test_404_json_for_fetch_and_json_clients(client, conn):
    assert client.get('/nope', headers={'Accept': 'application/json'}).get_json()['error'] == 'not_found'
    assert client.get('/nope', headers={'X-Requested-With': 'fetch'}).get_json()['error'] == 'not_found'


def _db_down(*_args, **_kwargs):
    raise psycopg.OperationalError('connection refused')


@pytest.mark.parametrize('path, target', [
    ('/', 'get_schedule'),
    ('/attendance', 'get_schedule'),
    (f'/home/{D}', 'get_schedule_day'),
    (f'/attendance/{D}/team/Red', 'get_schedule_day'),
    (f'/home/{D}/team/Red/record_section', 'get_schedule_day'),
])
def test_db_down_on_get_renders_503_page(client, seeded, monkeypatch, path, target):
    monkeypatch.setattr(data, target, _db_down)
    resp = client.get(path)
    assert resp.status_code == 503
    assert resp.mimetype == 'text/html'
    body = _html(resp)
    assert 'Database unavailable' in body and 'Try again in a moment' in body


def test_db_down_on_get_returns_json_to_fetch(client, seeded, monkeypatch):
    monkeypatch.setattr(data, 'get_schedule_day', _db_down)
    resp = client.get(f'/home/{D}', headers={'Accept': 'application/json'})
    assert resp.status_code == 503
    assert resp.get_json()['error'] == 'db_unavailable'


@pytest.mark.parametrize('path, target, form', [
    ('/submit_checkin', 'set_attendance', {'date_str': D, 'contact_id': '1003', 'manual_present': 'on'}),
    ('/edit_attendance', 'set_attendance', {'date_str': D, 'contact_id': '1003', 'has_bible': 'on'}),
    ('/submit_section', 'upsert_completed_section', {'date_str': D, 'contact_id': '1001', 'section': '1.2',
                                                     'section_complete': 'on'}),
    ('/edit_section', 'get_section_entry', {'date_str': D, 'entry_id': '1', 'gold_credit': 'on'}),
    ('/edit_progress_section', 'get_section_entry', {'entry_id': '1', 'gold_credit': 'on'}),
])
def test_db_down_on_post_returns_503_json(client, seeded, monkeypatch, path, target, form):
    monkeypatch.setattr(data, target, _db_down)
    resp = client.post(path, data=form)
    assert resp.status_code == 503
    body = resp.get_json()
    assert body['ok'] is False and body['error'] == 'db_unavailable'
    assert 'Nothing was saved' in body['message']
    assert resp.headers['Cache-Control'] == 'no-store'


# =============================================================================
# QA date override (?today=)
# =============================================================================

def test_qa_ribbon_shows_configured_override(client, seeded):
    for path in ('/', f'/home/{D}/team/Red/record_section'):     # base and fullscreen layouts
        body = _html(client.get(path))
        assert 'class="qa-ribbon"' in body
        assert 'Testing as September 23, 2026' in body


def test_qa_today_param_sets_cookie_and_moves_today(client, seeded):
    resp = client.get('/?today=2026-09-30')
    assert resp.status_code == 200
    set_cookie = resp.headers['Set-Cookie']
    assert 'tnt_today=2026-09-30' in set_cookie and 'HttpOnly' in set_cookie
    body = _html(resp)
    assert 'Testing as September 30, 2026' in body
    assert re.search(r'id="current-date"\s+href="/home/2026-09-30"', body)
    assert 'TODAY' in _schedule_item(body, '/home/2026-09-30')

    body = _html(client.get('/attendance'))                         # the cookie carries it
    assert 'Testing as September 30, 2026' in body
    resp = client.post('/submit_checkin', data={'date_str': FUTURE, 'contact_id': '1003', 'team': 'Blue',
                                                'manual_present': 'on'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True                                   # 9/30 is "today" now
    assert _att(seeded, 1003, D_0930)['present']


def test_qa_today_between_meetings_shows_next(client, seeded):
    body = _html(client.get('/?today=2026-09-24'))
    assert re.search(r'id="current-date"\s+href="/home/2026-09-30"', body)
    assert 'NEXT' in _schedule_item(body, '/home/2026-09-30')


def test_qa_today_clear_removes_cookie(client, seeded):
    client.get('/?today=2026-09-30')
    resp = client.get('/?today=clear')
    assert 'tnt_today=;' in resp.headers['Set-Cookie']
    assert client.get_cookie('tnt_today') is None
    assert 'Testing as September 23, 2026' in _html(resp)
    resp = client.post('/submit_checkin', data={'date_str': FUTURE, 'contact_id': '1003', 'manual_present': 'on'})
    assert resp.status_code == 422


def test_qa_today_ignores_garbage(client, seeded):
    resp = client.get('/?today=2026-13-01')
    assert 'tnt_today' not in resp.headers.get('Set-Cookie', '')
    assert 'Testing as September 23, 2026' in _html(resp)


@pytest.fixture
def locked_client(seeded, fake_mp):
    """An app with the QA override disabled (as in production)."""
    from tnt import create_app
    seeded.execute("INSERT INTO schedule (meeting_date, theme) VALUES ('2099-01-07', 'Far Future')")
    app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'ALLOW_DATE_OVERRIDE': False,
                      'TNT_DATE_OVERRIDE': None})
    app.test_client_class = FetchClient
    c = app.test_client()
    login_session(c)
    return c


def test_qa_today_ignored_when_override_disallowed(locked_client, seeded):
    resp = locked_client.get('/?today=2099-01-07')
    assert resp.status_code == 200
    assert 'tnt_today' not in resp.headers.get('Set-Cookie', '')
    body = _html(resp)
    assert 'class="qa-ribbon"' not in body
    assert 'TODAY' not in _schedule_item(body, '/home/2099-01-07')

    locked_client.set_cookie('tnt_today', '2099-01-07')
    resp = locked_client.post('/submit_checkin', data={'date_str': '2099-01-07', 'contact_id': '1003',
                                                       'manual_present': 'on'})
    assert resp.status_code == 422
    assert resp.get_json()['error'] == 'future_date'
    assert 'class="qa-ribbon"' not in _html(locked_client.get('/attendance'))


def test_qa_ribbon_hidden_when_override_disallowed(seeded, fake_mp):
    from tnt import create_app
    app = create_app({'TESTING': True, 'ALLOW_DATE_OVERRIDE': False, 'TNT_DATE_OVERRIDE': D})
    assert 'class="qa-ribbon"' not in _html(app.test_client().get('/'))


# =============================================================================
# MP sync on the request path
# =============================================================================

@pytest.mark.parametrize('path', [
    f'/home/{D}', f'/home/{D}/team/Red', f'/home/{D}/team/Red/record_section',
    f'/attendance/{D}', f'/attendance/{D}/team/Red', f'/attendance/{D}/team/Unassigned',
    '/attendance/2026-09-16/team/Red', '/attendance/2026-09-09/team/Red', f'/attendance/{FUTURE}/team/Red',
    f'/attendance/{D}/team/Red/kid/1001', '/progress', '/progress/student/1001',
])
def test_fresh_sync_makes_no_mp_calls(client, night, fake_mp, path):
    assert client.get(path).status_code == 200
    assert fake_mp.calls == []


def test_fresh_sync_note(client, night):
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert 'Check-ins updated' in body
    assert f'href="/attendance/{D}/team/Red?refresh=1"' in body
    assert 'sync-note is-stale' not in body
    show_all = _html(client.get(f'/attendance/{D}/team/Red?show=all'))
    assert f'href="/attendance/{D}/team/Red?show=all&amp;refresh=1"' in show_all


def test_fresh_sync_note_keeps_its_time_on_the_next_request(client, night):
    client.get(f'/attendance/{D}/team/Red')
    assert 'Check-ins updated' in _html(client.get(f'/attendance/{D}/team/Red'))


@pytest.fixture
def mp_down(fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get', table='Group_Participants')
    return fake_mp


STALE_PAGES = [f'/home/{D}', f'/home/{D}/team/Red', f'/home/{D}/team/Red/record_section',
               f'/attendance/{D}', f'/attendance/{D}/team/Red', f'/attendance/{D}/team/Unassigned',
               '/attendance/2026-09-16/team/Red']


@pytest.mark.parametrize('path', STALE_PAGES)
def test_pages_render_from_postgres_when_mp_is_unreachable(client, night, mp_down, path):
    night.execute('DELETE FROM sync_state')
    resp = client.get(path)
    assert resp.status_code == 200
    assert mp_down.calls                                      # it tried MP once...
    calls = len(mp_down.calls)
    assert client.get(path).status_code == 200
    assert len(mp_down.calls) == calls                        # ...then the circuit breaker held


def test_team_list_still_shows_stored_checkins_when_mp_is_unreachable(client, night, mp_down):
    night.execute('DELETE FROM sync_state')
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert 'Alice Anders' in body and _summary(body) == (1, 1)


def test_mp_unreachable_note_when_checkins_never_synced(client, night, mp_down):
    night.execute("DELETE FROM sync_state WHERE key LIKE 'checkins:%'")
    for path in (f'/attendance/{D}/team/Red', f'/attendance/{D}', f'/home/{D}'):
        resp = client.get(path)
        assert resp.status_code == 200
        body = _html(resp)
        assert MP_DOWN_NOTE in body, path
        assert 'sync-note is-stale' in body


def test_stale_checkins_marked_stale_when_mp_is_unreachable(client, night, mp_down):
    night.execute("UPDATE sync_state SET synced_at = now() - interval '2 hours', "
                  "attempted_at = now() - interval '2 hours'")
    resp = client.get(f'/attendance/{D}/team/Red')
    assert resp.status_code == 200
    assert 'sync-note is-stale' in _html(resp)


def test_mp_unreachable_note_when_nothing_ever_synced(client, night, mp_down):
    night.execute('DELETE FROM sync_state')
    body = _html(client.get(f'/attendance/{D}/team/Red'))
    assert MP_DOWN_NOTE in body
    assert 'Loading check-ins' not in body
