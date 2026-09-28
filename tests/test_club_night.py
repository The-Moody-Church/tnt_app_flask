"""PR3 club-night UX.

(a) tonight redirect + the tnt_team cookie, the date line and the team switcher;
(b) tap-to-toggle attendance chips (POST /api/attendance) and Mark present;
(c) tonight's section card with one-tap credits (POST /api/sections);
(d) Assign team for Unassigned kids (POST /api/kids/<cid>/team, writes MP Group_Participants.Notes);
plus the removed check-in form routes and app.js.

Known app bugs are strict xfails (search for `xfail`).
"""
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest

from models import mp
from models.roster import GP_SELECT
from tests.conftest import (D_0909, D_0916, D_0923, D_0930, TEST_USER, TODAY, FakeMP, FetchClient,
                            add_attendance, add_section, login_session)

D = TODAY.isoformat()            # tonight: 2026-09-23 'Sports Team Night!', sections {1.2}
PAST = D_0916.isoformat()        # a past club night (writable)
FUTURE = D_0930.isoformat()      # a future club night (read-only)
NO_CLUB = '2026-10-07'           # 'NO AWANA - Fall Break'
NOT_SCHEDULED = '2026-09-24'

FLAGS = ('has_bible', 'wearing_shirt', 'has_book', 'did_homework', 'has_dues')
CHIP_LABELS = ('Bible', 'Shirt', 'Book', 'HW', 'Dues')
CREDITS = ('section_complete', 'silver_credit', 'gold_credit')
TEAM_ORDER = ['Red', 'Blue', 'Green', 'Yellow']
COOKIE_MAX_AGE = str(180 * 86400)

GUS = 1007                       # the walk-in: Unassigned, group_participant_id 9007, participant_id 5007
GUS_GP_ROW = {'Group_Participant_ID': 9007, 'Group_ID': 697, 'Participant_ID': 5007,
              'Notes': 'Created by CheckIn', 'End_Date': None}


# =============================================================================
# Helpers
# =============================================================================

class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    @property
    def text(self):
        return ' '.join(''.join(c if isinstance(c, str) else c.text for c in self.children).split())

    @property
    def classes(self):
        return set((self.attrs.get('class') or '').split())

    def walk(self):
        for c in self.children:
            if isinstance(c, _Node):
                yield c
                yield from c.walk()

    def find_all(self, tag=None, cls=None, **attrs):
        """attrs: data_url='x' matches data-url="x"; True = present, False = absent."""
        want = {k.replace('_', '-'): v for k, v in attrs.items()}
        out = []
        for n in self.walk():
            if tag and n.tag != tag:
                continue
            if cls and not set(cls.split()) <= n.classes:
                continue
            if all((k in n.attrs) if v is True else (k not in n.attrs) if v is False else n.attrs.get(k) == v
                   for k, v in want.items()):
                out.append(n)
        return out

    def find(self, *a, **kw):
        found = self.find_all(*a, **kw)
        return found[0] if found else None

    def one(self, *a, **kw):
        found = self.find_all(*a, **kw)
        assert len(found) == 1, f'expected exactly one {a} {kw}, found {len(found)}'
        return found[0]


class _Tree(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = self.cur = _Node('#root', {}, None)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in self.VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def dom(resp):
    assert resp.status_code == 200, (resp.status_code, resp.get_data(as_text=True)[:300])
    tree = _Tree()
    tree.feed(resp.get_data(as_text=True))
    tree.close()
    return tree.root


def _html(resp):
    return resp.get_data(as_text=True)


def _loc(resp):
    parts = urlsplit(resp.headers['Location'])
    return parts.path + (f'?{parts.query}' if parts.query else '')


def _set_cookie(resp, name):
    """The Set-Cookie header for `name` as {'value': ..., 'httponly': True, 'max-age': '...', ...}, or None."""
    for header in resp.headers.getlist('Set-Cookie'):
        if header.startswith(name + '='):
            first, *rest = [p.strip() for p in header.split(';')]
            attrs = {'value': first.split('=', 1)[1]}
            for p in rest:
                k, _, v = p.partition('=')
                attrs[k.lower()] = v or True
            return attrs
    return None


def _ok(resp):
    assert resp.status_code == 200, (resp.status_code, resp.get_data(as_text=True)[:300])
    body = resp.get_json()
    assert body['ok'] is True
    assert resp.headers['Cache-Control'] == 'no-store'
    return body


def _err(resp, status, code):
    assert resp.status_code == status, (resp.status_code, resp.get_data(as_text=True)[:300])
    assert resp.mimetype == 'application/json'
    body = resp.get_json()
    assert body['ok'] is False and body['error'] == code, body
    assert isinstance(body['message'], str) and body['message']
    assert resp.headers['Cache-Control'] == 'no-store'
    return body


def _att(conn, contact_id, d=D_0923):
    return conn.execute('SELECT * FROM attendance WHERE contact_id = %s AND meeting_date = %s',
                        (contact_id, d)).fetchone()


def _sec(conn, contact_id, d, section):
    return conn.execute('SELECT * FROM completed_sections WHERE contact_id = %s AND meeting_date = %s '
                        'AND section = %s', (contact_id, d, section)).fetchone()


def _kid(conn, contact_id):
    return conn.execute('SELECT * FROM kids WHERE contact_id = %s', (contact_id,)).fetchone()


def _count(conn, table):
    return conn.execute(f'SELECT COUNT(*) AS n FROM {table}').fetchone()['n']


def _csrf_meta(resp):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', _html(resp))
    assert m, 'no csrf meta tag on the page'
    return m.group(1)


def att_url(d, cid):
    return f'/api/attendance/{d}/{cid}'


def sec_url(d, cid):
    return f'/api/sections/{d}/{cid}'


def team_url(cid):
    return f'/api/kids/{cid}/team'


@pytest.fixture
def make_client(conn, fake_mp):
    """A signed-in client for an app built with config overrides (e.g. MP_WRITES_ENABLED=True)."""
    from tnt import create_app

    def _make(anon=False, **config):
        app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'TNT_DATE_OVERRIDE': D, **config})
        app.test_client_class = FetchClient
        c = app.test_client()
        if not anon:
            login_session(c)
        return c
    return _make


@pytest.fixture
def night(seeded):
    """Tonight: Alice (Red) checked in at the kiosk with her Bible; Gus the walk-in checked in with no team.
    Bo (Red) and everyone else are not checked in."""
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, has_bible=True)
    add_attendance(seeded, GUS, D_0923, team=None, mp_checked_in=True)
    return seeded


@pytest.fixture
def gus_in_mp(fake_mp):
    fake_mp.tables['Group_Participants'] = [dict(GUS_GP_ROW)]
    return fake_mp


# =============================================================================
# (a) Tonight redirect
# =============================================================================

@pytest.mark.parametrize('path, expected', [
    ('/', f'/home/{D}/team/Blue'),
    ('/attendance', f'/attendance/{D}/team/Blue'),
])
def test_club_night_with_team_cookie_opens_tonights_team(client, seeded, path, expected):
    client.set_cookie('tnt_team', 'Blue')
    resp = client.get(path)
    assert resp.status_code == 302
    assert _loc(resp) == expected
    assert resp.headers['Cache-Control'] == 'no-store'
    assert 'Cookie' in resp.headers.get('Vary', '')


@pytest.mark.parametrize('path, expected', [('/', f'/home/{D}'), ('/attendance', f'/attendance/{D}')])
def test_club_night_without_cookie_opens_tonights_date_page(client, seeded, path, expected):
    resp = client.get(path)
    assert resp.status_code == 302
    assert _loc(resp) == expected
    assert resp.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('path, detail', [('/?all=1', '/home/'), ('/attendance?all=1', '/attendance/')])
def test_all_param_shows_the_date_list(client, seeded, path, detail):
    client.set_cookie('tnt_team', 'Blue')
    root = dom(client.get(path))
    hrefs = [a.attrs['href'] for a in root.find_all('a', cls='schedule-item')]
    assert hrefs == [f'{detail}{d}' for d in ('2026-09-09', '2026-09-16', D, FUTURE, NO_CLUB)]
    today = root.one('a', cls='schedule-item is-today')
    assert today.attrs['href'] == f'{detail}{D}' and 'TODAY' in today.text


@pytest.mark.parametrize('path', ['/', '/attendance'])
def test_redirect_follows_the_date_override(make_client, seeded, path):
    c = make_client(TNT_DATE_OVERRIDE=PAST)
    c.set_cookie('tnt_team', 'Green')
    resp = c.get(path)
    assert resp.status_code == 302
    assert _loc(resp) == (f'/home/{PAST}/team/Green' if path == '/' else f'/attendance/{PAST}/team/Green')


@pytest.mark.parametrize('path', ['/', '/attendance'])
@pytest.mark.parametrize('today', [NOT_SCHEDULED, NO_CLUB])
def test_non_club_day_shows_the_list_via_config_override(make_client, seeded, path, today):
    c = make_client(TNT_DATE_OVERRIDE=today)
    c.set_cookie('tnt_team', 'Red')
    resp = c.get(path)
    assert resp.status_code == 200
    assert 'schedule-item' in _html(resp)


@pytest.mark.parametrize('path', ['/', '/attendance'])
@pytest.mark.parametrize('today', [NOT_SCHEDULED, NO_CLUB, '2026-12-25'])
def test_non_club_day_shows_the_list_via_qa_today(client, seeded, path, today):
    client.set_cookie('tnt_team', 'Red')
    resp = client.get(f'{path}?today={today}')
    assert resp.status_code == 200 and 'schedule-item' in _html(resp)
    resp = client.get(path)                          # the QA cookie keeps the override
    assert resp.status_code == 200 and 'schedule-item' in _html(resp)
    resp = client.get(f'{path}?today=clear')         # back to the configured club night
    assert resp.status_code == 302
    assert _loc(resp).endswith(f'{D}/team/Red')


def test_qa_today_on_another_club_night_redirects_there(client, seeded):
    resp = client.get(f'/attendance?today={PAST}')
    assert resp.status_code == 302 and _loc(resp) == f'/attendance/{PAST}'


@pytest.mark.parametrize('value', ['Purple', 'Unassigned', 'red', 'RED', 'Red Team', '', '<script>'])
@pytest.mark.parametrize('path, expected', [('/', f'/home/{D}'), ('/attendance', f'/attendance/{D}')])
def test_invalid_team_cookie_is_ignored(client, seeded, value, path, expected):
    client.set_cookie('tnt_team', value)
    resp = client.get(path)
    assert resp.status_code == 302
    assert _loc(resp) == expected


def test_anonymous_root_goes_to_login_not_tonight(anon_client, seeded):
    anon_client.set_cookie('tnt_team', 'Red')
    resp = anon_client.get('/')
    assert resp.status_code == 302
    assert urlsplit(resp.headers['Location']).path == '/login'


# =============================================================================
# (a) The tnt_team cookie
# =============================================================================

@pytest.mark.parametrize('path', [f'/attendance/{D}/team/Green', f'/home/{D}/team/Green',
                                  f'/attendance/{D}/team/green', f'/home/{PAST}/team/GREEN'])
def test_team_page_remembers_the_team(client, seeded, path):
    cookie = _set_cookie(client.get(path), 'tnt_team')
    assert cookie is not None
    assert cookie['value'] == 'Green'                       # canonical, whatever the URL's case
    assert cookie['httponly'] is True
    assert cookie['samesite'] == 'Lax'
    assert cookie['max-age'] == COOKIE_MAX_AGE
    assert cookie['path'] == '/'
    assert 'secure' not in cookie                           # http test origin


def test_team_cookie_is_secure_when_sessions_are(make_client, seeded):
    c = make_client(SESSION_COOKIE_SECURE=True)
    cookie = _set_cookie(c.get(f'/attendance/{D}/team/Red', base_url='https://localhost'), 'tnt_team')
    assert cookie['value'] == 'Red' and cookie['secure'] is True


def test_remembered_team_is_shared_by_both_tabs(client, seeded):
    client.get(f'/attendance/{D}/team/Yellow')
    assert _loc(client.get('/attendance')) == f'/attendance/{D}/team/Yellow'
    assert _loc(client.get('/')) == f'/home/{D}/team/Yellow'
    client.get(f'/home/{D}/team/Blue')                     # the last team viewed wins
    assert _loc(client.get('/attendance')) == f'/attendance/{D}/team/Blue'


def test_unassigned_page_never_sets_the_cookie(client, night):
    client.set_cookie('tnt_team', 'Red')
    resp = client.get(f'/attendance/{D}/team/Unassigned')
    assert resp.status_code == 200
    assert _set_cookie(resp, 'tnt_team') is None
    assert client.get_cookie('tnt_team').value == 'Red'
    assert _loc(client.get('/attendance')) == f'/attendance/{D}/team/Red'


@pytest.mark.parametrize('path', [f'/attendance/{D}', f'/home/{D}', f'/attendance/{D}/team/Red/kid/1001',
                                  f'/home/{D}/team/Red/record_section',
                                  f'/home/{D}/team/Red/record_section/kid/1001',
                                  f'/attendance/{D}/team/Purple', f'/home/{D}/team/Unassigned'])
def test_only_team_pages_set_the_cookie(client, night, path):
    assert _set_cookie(client.get(path), 'tnt_team') is None


# =============================================================================
# (a) Date line, team switcher, back chain
# =============================================================================

def _pills(root):
    nav = root.one('nav', cls='team-switcher')
    return nav.find_all('a', cls='team-pill')


def test_attendance_team_page_date_line_and_switcher(client, night):
    root = dom(client.get(f'/attendance/{D}/team/Red'))
    line = root.one('div', cls='date-line')
    assert line.one('span', cls='date-line__when').text == 'Tonight · September 23rd'
    assert line.one('a', cls='date-line__all').attrs['href'] == '/attendance?all=1'
    assert line.one('a', cls='date-line__all').text == 'All dates'

    pills = _pills(root)
    names = [p.text.split()[0] for p in pills]
    assert names == TEAM_ORDER + ['Unassigned']
    assert [p.attrs['href'] for p in pills] == [f'/attendance/{D}/team/{t}' for t in names]
    current = [p for p in pills if 'aria-current' in p.attrs]
    assert [p.text.split()[0] for p in current] == ['Red'] and current[0].attrs['aria-current'] == 'page'
    assert [p.text.split()[0] for p in pills if 'is-current' in p.classes] == ['Red']
    counts = {p.text.split()[0]: p.one('span', cls='team-pill__count').text for p in pills}
    assert counts == {'Red': '1', 'Blue': '0', 'Green': '0', 'Yellow': '0', 'Unassigned': '1'}


def test_home_team_page_date_line_and_switcher_has_no_unassigned_pill(client, night):
    root = dom(client.get(f'/home/{D}/team/Blue'))
    line = root.one('div', cls='date-line')
    assert line.one('span', cls='date-line__when').text == 'Tonight · September 23rd'
    assert line.one('a', cls='date-line__all').attrs['href'] == '/?all=1'
    pills = _pills(root)
    assert [p.text for p in pills] == TEAM_ORDER
    assert [p.attrs['href'] for p in pills] == [f'/home/{D}/team/{t}' for t in TEAM_ORDER]
    assert [p.text for p in pills if p.attrs.get('aria-current') == 'page'] == ['Blue']


@pytest.mark.parametrize('path', [f'/attendance/{PAST}/team/Red', f'/home/{PAST}/team/Red'])
def test_past_date_line_has_no_tonight(client, seeded, path):
    root = dom(client.get(path))
    assert root.one('span', cls='date-line__when').text == 'September 16th'
    assert all(PAST in p.attrs['href'] for p in _pills(root))


def test_unassigned_pill_shows_for_unassigned_kids_not_yet_here(client, seeded):
    names = [p.text.split()[0] for p in _pills(dom(client.get(f'/attendance/{D}/team/Red')))]
    assert names[-1] == 'Unassigned'


def test_no_unassigned_pill_without_unassigned_kids(client, seeded):
    seeded.execute("UPDATE kids SET team = 'Green' WHERE contact_id = %s", (GUS,))
    root = dom(client.get(f'/attendance/{D}/team/Red'))
    assert [p.text.split()[0] for p in _pills(root)] == TEAM_ORDER
    assert root.find('a', cls='team-pill unassigned') is None


def test_unassigned_page_marks_its_pill_as_current_class(client, night):
    pill = dom(client.get(f'/attendance/{D}/team/Unassigned')).one('a', cls='team-pill unassigned')
    assert 'is-current' in pill.classes


def test_unassigned_page_marks_its_pill_aria_current(client, night):
    pill = dom(client.get(f'/attendance/{D}/team/Unassigned')).one('a', cls='team-pill unassigned')
    assert pill.attrs.get('aria-current') == 'page'


@pytest.mark.parametrize('path, back', [
    (f'/attendance/{D}', '/attendance?all=1'),
    (f'/home/{D}', '/?all=1'),
    (f'/attendance/{D}/team/Red', f'/attendance/{D}'),
    (f'/home/{D}/team/Red', f'/home/{D}'),
    (f'/attendance/{D}/team/Red/kid/1001', f'/attendance/{D}/team/Red'),
])
def test_back_chain_is_team_then_date_then_list(client, night, path, back):
    assert dom(client.get(path)).one('a', cls='back-btn').attrs['href'] == back


def test_summary_bar_show_all_toggle(client, night):
    root = dom(client.get(f'/attendance/{D}/team/Red'))
    toggle = root.one('a', data_show_all_toggle=True, cls='filter-toggle')
    assert toggle.attrs['href'] == f'/attendance/{D}/team/Red?show=all'
    assert toggle.attrs['aria-pressed'] == 'false' and toggle.text == 'Show all'
    assert 'show-all' not in root.one('div', data_kid_list=True).classes

    root = dom(client.get(f'/attendance/{D}/team/Red?show=all'))
    toggle = root.one('a', data_show_all_toggle=True, cls='filter-toggle')
    assert toggle.attrs['href'] == f'/attendance/{D}/team/Red'
    assert toggle.attrs['aria-pressed'] == 'true' and toggle.text == 'Here only'
    assert 'show-all' in root.one('div', data_kid_list=True).classes


# =============================================================================
# (b) POST /api/attendance/<d>/<cid>
# =============================================================================

def _row(**overrides):
    row = {'contact_id': 1001, 'present': True, 'source': 'mp', 'present_label': 'Checked in · 6:12pm',
           **{f: False for f in FLAGS}}
    row.update(overrides)
    return row


def test_set_one_flag_returns_the_row(client, night):
    body = _ok(client.post(att_url(D, 1001), json={'set': {'wearing_shirt': True}}))
    assert body['row'] == _row(has_bible=True, wearing_shirt=True)
    row = _att(night, 1001)
    assert (row['has_bible'], row['wearing_shirt'], row['mp_checked_in']) == (True, True, True)


def test_a_second_set_keeps_the_first_flag(client, night):
    _ok(client.post(att_url(D, 1001), json={'set': {'has_book': True}}))
    body = _ok(client.post(att_url(D, 1001), json={'set': {'did_homework': True}}))
    assert body['row'] == _row(has_bible=True, has_book=True, did_homework=True)
    body = _ok(client.post(att_url(D, 1001), json={'set': {'has_book': False}}))
    assert body['row'] == _row(has_bible=True, did_homework=True)
    assert _count(night, 'attendance') == 2


def test_set_several_fields_at_once(client, night):
    body = _ok(client.post(att_url(D, 1001), json={'set': {'has_book': True, 'has_dues': True,
                                                           'has_bible': False}}))
    assert body['row'] == _row(has_book=True, has_dues=True)


def test_repeating_a_set_is_idempotent(client, night):
    first = _ok(client.post(att_url(D, 1001), json={'set': {'has_dues': True}}))
    again = _ok(client.post(att_url(D, 1001), json={'set': {'has_dues': True}}))
    assert first['row'] == again['row'] == _row(has_bible=True, has_dues=True)


def test_mark_present_then_record_items(client, seeded):
    body = _ok(client.post(att_url(D, 1003), json={'set': {'manual_present': True}}))
    assert body['row'] == _row(contact_id=1003, source='manual', present_label='Marked present by Test Leader')
    row = _att(seeded, 1003)
    assert row['manual_present'] is True and row['present'] is True and row['mp_checked_in'] is False
    assert row['manual_present_by'] == 'Test Leader' and row['manual_present_at'] is not None
    assert row['updated_by_contact_id'] == TEST_USER['contact_id']
    assert row['team'] == 'Blue'
    body = _ok(client.post(att_url(D, 1003), json={'set': {'has_bible': True, 'wearing_shirt': True}}))
    assert body['row'] == _row(contact_id=1003, source='manual', present_label='Marked present by Test Leader',
                               has_bible=True, wearing_shirt=True)


def test_mark_present_and_items_in_one_call(client, seeded):
    body = _ok(client.post(att_url(D, 1003), json={'set': {'manual_present': True, 'has_book': True}}))
    assert body['row']['present'] is True and body['row']['has_book'] is True


def test_mark_present_for_the_unassigned_walk_in(client, seeded):
    _ok(client.post(att_url(D, GUS), json={'set': {'manual_present': True}}))
    assert _att(seeded, GUS)['team'] is None


def test_undo_manual_present_clears_the_items(client, seeded):
    add_attendance(seeded, 1002, D_0923, manual_present=True, has_bible=True, has_dues=True)
    body = _ok(client.post(att_url(D, 1002), json={'set': {'manual_present': False}}))
    assert body['row'] == _row(contact_id=1002, present=False, source=None, present_label='Not checked in')
    row = _att(seeded, 1002)
    assert row['manual_present'] is False and row['manual_present_by'] is None and row['manual_present_at'] is None
    assert not any(row[f] for f in FLAGS)


@pytest.mark.parametrize('manual', [False, True])
def test_undo_present_for_a_kiosk_check_in_is_409(client, seeded, manual):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, manual_present=manual, has_bible=True)
    before = _att(seeded, 1001)
    _err(client.post(att_url(D, 1001), json={'set': {'manual_present': False}}), 409, 'mp_checked_in')
    assert _att(seeded, 1001) == before


def test_undo_present_with_flags_for_a_kiosk_check_in_is_409_and_saves_nothing(client, night):
    before = _att(night, 1001)
    _err(client.post(att_url(D, 1001), json={'set': {'manual_present': False, 'has_book': True}}),
         409, 'mp_checked_in')
    assert _att(night, 1001) == before


@pytest.mark.parametrize('flag', FLAGS)
def test_newly_set_flag_for_a_kid_not_here_is_409_and_saves_nothing(client, seeded, flag):
    _err(client.post(att_url(D, 1003), json={'set': {flag: True}}), 409, 'not_present')
    assert _att(seeded, 1003) is None


def test_newly_set_flag_for_a_kid_with_a_row_but_not_here_is_409(client, seeded):
    add_attendance(seeded, 1003, D_0923)                    # a row, not present
    before = _att(seeded, 1003)
    _err(client.post(att_url(D, 1003), json={'set': {'has_bible': True, 'has_dues': False}}), 409, 'not_present')
    assert _att(seeded, 1003) == before


def test_a_flag_already_on_for_a_kid_not_here_can_be_kept_or_cleared(client, seeded):
    add_attendance(seeded, 1004, D_0923, has_book=True)     # legacy row: item without Present
    body = _ok(client.post(att_url(D, 1004), json={'set': {'has_book': True}}))
    assert body['row']['present'] is False and body['row']['has_book'] is True
    body = _ok(client.post(att_url(D, 1004), json={'set': {'has_book': False}}))
    assert body['row']['has_book'] is False
    _err(client.post(att_url(D, 1004), json={'set': {'has_book': True}}), 409, 'not_present')


def test_clearing_a_flag_for_a_kid_not_here_is_allowed(client, seeded):
    body = _ok(client.post(att_url(D, 1003), json={'set': {'has_bible': False}}))
    assert body['row']['present'] is False and body['row']['has_bible'] is False


@pytest.mark.parametrize('body', [
    {'set': {'has_bible': 'true'}},
    {'set': {'has_bible': 1}},
    {'set': {'has_bible': None}},
    {'set': {'manual_present': 0}},
    {'set': {'manual_present': 'false'}},
    {'set': {'present': True}},
    {'set': {'mp_checked_in': True}},
    {'set': {'team': 'Red'}},
    {'set': {'manual_present_by': 'Someone'}},
    {'set': {'updated_by_contact_id': 1}},
    {'set': {'has_bible': True, 'bogus': True}},
    {'set': {}},
    {'set': []},
    {'set': ['has_bible']},
    {'set': 'has_bible'},
    {'set': None},
    {},
    {'has_bible': True},
    [{'set': {'has_bible': True}}],
    'has_bible',
    5,
    None,
])
def test_bad_attendance_bodies_are_400_and_save_nothing(client, night, body):
    before = _att(night, 1001)
    _err(client.post(att_url(D, 1001), json=body), 400, 'invalid_input')
    assert _att(night, 1001) == before
    assert _count(night, 'attendance') == 2


@pytest.mark.parametrize('kwargs', [
    {'data': 'not json', 'content_type': 'application/json'},
    {'data': '{"set": {"has_bible": true}', 'content_type': 'application/json'},
    {'data': {'set': 'has_bible'}},                          # a form post
    {},
])
def test_non_json_attendance_bodies_are_400(client, night, kwargs):
    _err(client.post(att_url(D, 1001), **kwargs), 400, 'invalid_input')


def test_attendance_for_an_unknown_kid_is_404(client, seeded):
    _err(client.post(att_url(D, 424242), json={'set': {'manual_present': True}}), 404, 'not_found')
    assert _count(seeded, 'attendance') == 0


@pytest.mark.parametrize('d', [NOT_SCHEDULED, 'garbage', '2026-9-23', '2026-02-30', '20260923'])
def test_attendance_for_a_date_with_no_meeting_is_404(client, seeded, d):
    _err(client.post(att_url(d, 1001), json={'set': {'manual_present': True}}), 404, 'not_found')
    assert _count(seeded, 'attendance') == 0


@pytest.mark.parametrize('d', [FUTURE, NO_CLUB])
def test_attendance_for_a_future_date_is_422(client, seeded, d):
    _err(client.post(att_url(d, 1001), json={'set': {'manual_present': True}}), 422, 'future_date')
    assert _count(seeded, 'attendance') == 0


def test_attendance_for_a_past_date_is_writable(client, seeded):
    body = _ok(client.post(att_url(PAST, 1002), json={'set': {'manual_present': True, 'has_bible': True}}))
    assert body['row']['present'] is True
    assert _att(seeded, 1002, D_0916)['has_bible'] is True


def test_attendance_records_the_signed_in_leader_not_the_body(client, seeded):
    login_session(client, contact_id=31337, name='Pat Leader')
    body = {'set': {'manual_present': True}, 'by': {'contact_id': 1, 'name': 'Mallory'},
            'updated_by_contact_id': 1, 'manual_present_by': 'Mallory'}
    resp = _ok(client.post(att_url(D, 1003), json=body))
    assert resp['row']['present_label'] == 'Marked present by Pat Leader'
    row = _att(seeded, 1003)
    assert row['updated_by_contact_id'] == 31337 and row['manual_present_by'] == 'Pat Leader'


def test_a_later_flag_tap_by_another_leader_updates_updated_by(make_client, client, seeded):
    _ok(client.post(att_url(D, 1003), json={'set': {'manual_present': True}}))
    other = make_client()
    login_session(other, contact_id=55555, name='Other Leader')
    _ok(other.post(att_url(D, 1003), json={'set': {'has_bible': True}}))
    row = _att(seeded, 1003)
    assert row['updated_by_contact_id'] == 55555
    assert row['manual_present_by'] == 'Test Leader'          # who marked present is kept


@pytest.mark.parametrize('url, body', [
    (att_url(D, 1003), {'set': {'manual_present': True}}),
    (sec_url(D, 1001), {'section': '1.2', 'field': 'section_complete', 'value': True}),
    (team_url(GUS), {'team': 'Red'}),
])
def test_anonymous_api_posts_are_401_json_and_save_nothing(anon_client, seeded, gus_in_mp, url, body):
    resp = anon_client.post(url, json=body)
    data = _err(resp, 401, 'auth_required')
    assert urlsplit(data['login_url']).path == '/login'
    assert _count(seeded, 'attendance') == 0 and _count(seeded, 'completed_sections') == 0
    assert gus_in_mp.calls == [] and gus_in_mp.puts == []


# -- CSRF (a CSRF-enabled app, like production) --------------------------------

@pytest.fixture
def csrf_client(conn, fake_mp):
    from tnt import create_app
    app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': True, 'TNT_DATE_OVERRIDE': D, 'MP_WRITES_ENABLED': True})
    app.test_client_class = FetchClient
    c = app.test_client()
    login_session(c)
    return c


@pytest.mark.parametrize('page, url, body', [
    (f'/attendance/{D}/team/Blue', att_url(D, 1003), {'set': {'manual_present': True}}),
    (f'/attendance/{D}/team/Blue/kid/1003', att_url(D, 1003), {'set': {'manual_present': True}}),
    (f'/home/{D}/team/Red/record_section/kid/1001', sec_url(D, 1001),
     {'section': '1.2', 'field': 'section_complete', 'value': True}),
])
def test_api_posts_need_the_pages_csrf_token(csrf_client, seeded, page, url, body):
    token = _csrf_meta(csrf_client.get(page))
    data = _err(csrf_client.post(url, json=body), 400, 'csrf_failed')
    assert data['reload'] is True
    _err(csrf_client.post(url, json=body, headers={'X-CSRFToken': 'not-the-token'}), 400, 'csrf_failed')
    assert _count(seeded, 'attendance') == 0 and _count(seeded, 'completed_sections') == 0
    _ok(csrf_client.post(url, json=body, headers={'X-CSRFToken': token}))


def test_assign_team_needs_the_csrf_token(csrf_client, night, gus_in_mp):
    token = _csrf_meta(csrf_client.get(f'/attendance/{D}/team/Unassigned'))
    _err(csrf_client.post(team_url(GUS), json={'team': 'Red'}), 400, 'csrf_failed')
    assert gus_in_mp.puts == [] and _kid(night, GUS)['team'] is None
    body = _ok(csrf_client.post(team_url(GUS), json={'team': 'Red'}, headers={'X-CSRFToken': token}))
    assert body['kid']['team'] == 'Red'


def test_anonymous_api_post_without_csrf_token_is_401_not_400(csrf_client, seeded):
    from tnt import create_app
    app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': True, 'TNT_DATE_OVERRIDE': D})
    app.test_client_class = FetchClient
    _err(app.test_client().post(att_url(D, 1003), json={'set': {'manual_present': True}}), 401, 'auth_required')


# =============================================================================
# (c) POST /api/sections/<d>/<cid>
# =============================================================================

def _states(main='none', silver='none', gold='none', labels=None):
    labels = labels or {}
    return {f: {'state': s, 'label': labels.get(f, '')}
            for f, s in zip(CREDITS, (main, silver, gold))}


def _credit(client, field, value, *, d=D, cid=1001, section='1.2'):
    return client.post(sec_url(d, cid), json={'section': section, 'field': field, 'value': value})


def test_credit_toggle_on_then_off(client, seeded):
    body = _ok(_credit(client, 'section_complete', True))
    assert body['section'] == '1.2'
    assert body['states'] == _states(main='today')
    row = _sec(seeded, 1001, D_0923, '1.2')
    assert (row['section_complete'], row['silver_credit'], row['gold_credit']) == (True, False, False)
    assert row['updated_by_contact_id'] == TEST_USER['contact_id'] and row['team'] == 'Red'

    body = _ok(_credit(client, 'section_complete', False))
    assert body['states'] == _states()
    row = _sec(seeded, 1001, D_0923, '1.2')                 # never deleted: an all-false row stays
    assert row is not None
    assert (row['section_complete'], row['silver_credit'], row['gold_credit']) == (False, False, False)
    assert _count(seeded, 'completed_sections') == 1


def test_credits_are_independent(client, seeded):
    _ok(_credit(client, 'silver_credit', True))
    body = _ok(_credit(client, 'gold_credit', True))
    assert body['states'] == _states(silver='today', gold='today')
    body = _ok(_credit(client, 'silver_credit', False))
    assert body['states'] == _states(gold='today')
    body = _ok(_credit(client, 'gold_credit', True))         # idempotent
    assert body['states'] == _states(gold='today')
    assert _count(seeded, 'completed_sections') == 1


def test_chip_off_only_clears_that_credit_on_a_form_row(client, seeded):
    add_section(seeded, 1001, D_0923, '1.2', complete=True, silver=True, gold=True)
    body = _ok(_credit(client, 'silver_credit', False))
    assert body['states'] == _states(main='today', gold='today')


def test_a_cleared_row_is_hidden_from_lists(client, seeded):
    _ok(_credit(client, 'section_complete', True))
    root = dom(client.get(f'/home/{D}/team/Red'))
    assert any(n.text == '1.2' for n in root.find_all('span', cls='section-name'))
    _ok(_credit(client, 'section_complete', False))
    body = _html(client.get(f'/home/{D}/team/Red'))
    assert 'No completed sections found for this team on this date.' in body
    root = dom(client.get(f'/home/{D}/team/Red/record_section/kid/1001'))
    chips = {c.attrs['data-field']: c.attrs['data-state'] for c in root.find_all('button', cls='credit-chip')}
    assert chips == {f: 'none' for f in CREDITS}
    body = _html(client.get('/progress/student/1001'))
    assert '1.2' not in body


def test_credit_earned_on_an_earlier_date_is_409(client, seeded):
    add_section(seeded, 1001, D_0916, '1.2', complete=True)
    for value in (True, False):                              # can't redo or undo another night's credit
        data = _err(_credit(client, 'section_complete', value), 409, 'already_earned')
        assert data['message'] == 'Already earned 9/16'
        assert data['states'] == _states(main='earlier', labels={'section_complete': '✓ 9/16'})
    assert _sec(seeded, 1001, D_0923, '1.2') is None
    body = _ok(_credit(client, 'silver_credit', True))       # the other credits are still open tonight
    assert body['states'] == _states(main='earlier', silver='today', labels={'section_complete': '✓ 9/16'})


def test_credit_earned_tonight_can_be_undone_even_with_other_earlier_credits(client, seeded):
    add_section(seeded, 1001, D_0909, '1.2', gold=True)
    add_section(seeded, 1001, D_0923, '1.2', complete=True)
    body = _ok(_credit(client, 'section_complete', False))
    assert body['states'] == _states(gold='earlier', labels={'gold_credit': '✓ 9/9'})


@pytest.mark.parametrize('body', [
    {'section': 'Z9', 'field': 'section_complete', 'value': True},
    {'section': '', 'field': 'section_complete', 'value': True},
    {'section': ' 1.2', 'field': 'section_complete', 'value': True},
    {'section': 1.2, 'field': 'section_complete', 'value': True},
    {'section': None, 'field': 'section_complete', 'value': True},
    {'field': 'section_complete', 'value': True},
    {'section': '1.2', 'field': 'platinum', 'value': True},
    {'section': '1.2', 'field': 'has_bible', 'value': True},
    {'section': '1.2', 'field': 'team', 'value': True},
    {'section': '1.2', 'value': True},
    {'section': '1.2', 'field': 'section_complete', 'value': 'true'},
    {'section': '1.2', 'field': 'section_complete', 'value': 1},
    {'section': '1.2', 'field': 'section_complete', 'value': None},
    {'section': '1.2', 'field': 'section_complete'},
    {},
    [{'section': '1.2', 'field': 'section_complete', 'value': True}],
    'section_complete',
    None,
])
def test_bad_credit_bodies_are_400_and_save_nothing(client, seeded, body):
    _err(client.post(sec_url(D, 1001), json=body), 400, 'invalid_input')
    assert _count(seeded, 'completed_sections') == 0


def test_credit_for_an_unknown_kid_is_404(client, seeded):
    _err(_credit(client, 'section_complete', True, cid=424242), 404, 'not_found')
    assert _count(seeded, 'completed_sections') == 0


@pytest.mark.parametrize('d, status, code', [
    (NOT_SCHEDULED, 404, 'not_found'), ('garbage', 404, 'not_found'),
    (FUTURE, 422, 'future_date'), (NO_CLUB, 422, 'future_date'),
])
def test_credit_for_a_bad_date_is_rejected(client, seeded, d, status, code):
    _err(_credit(client, 'section_complete', True, d=d), status, code)
    assert _count(seeded, 'completed_sections') == 0


def test_credit_for_a_past_date_is_writable(client, seeded):
    body = _ok(_credit(client, 'section_complete', True, d=PAST, section='1.1'))
    assert body['states'] == _states(main='today')
    assert _sec(seeded, 1001, D_0916, '1.1')['section_complete'] is True


def test_credit_takes_the_team_on_tonights_attendance_row(client, seeded):
    add_attendance(seeded, 1003, D_0923, team='Green', mp_checked_in=True)   # Blue kid, Green that night
    add_attendance(seeded, GUS, D_0923, team=None, mp_checked_in=True)
    for cid in (1003, GUS, 1004):
        _ok(_credit(client, 'section_complete', True, cid=cid))
    assert _sec(seeded, 1003, D_0923, '1.2')['team'] == 'Green'
    assert _sec(seeded, GUS, D_0923, '1.2')['team'] is None
    assert _sec(seeded, 1004, D_0923, '1.2')['team'] == 'Blue'           # no attendance row: current team


def test_credit_team_snapshot_does_not_change_on_toggle(client, seeded):
    _ok(_credit(client, 'section_complete', True, cid=1003))
    seeded.execute("UPDATE kids SET team = 'Yellow' WHERE contact_id = 1003")
    _ok(_credit(client, 'silver_credit', True, cid=1003))
    assert _sec(seeded, 1003, D_0923, '1.2')['team'] == 'Blue'


# =============================================================================
# (d) POST /api/kids/<cid>/team
# =============================================================================

def test_assign_team_writes_notes_to_mp_then_locally(client, night, gus_in_mp):
    add_attendance(night, GUS, D_0916, team=None, mp_checked_in=True)
    add_section(night, GUS, D_0923, '1.2', team=None, complete=True)
    body = _ok(client.post(team_url(GUS), json={'team': 'Green'}))
    assert body['kid'] == {'contact_id': GUS, 'name': 'Gus Gray', 'team': 'Green'}

    assert gus_in_mp.calls == [('Group_Participants', {'select': GP_SELECT, 'filter': 'Group_Participant_ID = 9007'})]
    assert gus_in_mp.puts == [('Group_Participants', [{'Group_Participant_ID': 9007, 'Notes': 'Green'}],
                               {'user_id': TEST_USER['user_id'], 'select': 'Group_Participant_ID,Notes'})]
    kid = _kid(night, GUS)
    assert (kid['team'], kid['mp_notes']) == ('Green', 'Green') and kid['team_assigned_at'] is not None
    assert _att(night, GUS)['team'] == 'Green'                   # tonight's rows move to the team
    assert _sec(night, GUS, D_0923, '1.2')['team'] == 'Green'
    assert _att(night, GUS, D_0916)['team'] is None              # past totals never shift
    audit = night.execute('SELECT * FROM team_assignments').fetchall()
    assert len(audit) == 1
    assert {k: audit[0][k] for k in ('contact_id', 'group_participant_id', 'old_notes', 'new_team',
                                     'assigned_by_contact_id', 'assigned_by_user_id')} == {
        'contact_id': GUS, 'group_participant_id': 9007, 'old_notes': 'Created by CheckIn', 'new_team': 'Green',
        'assigned_by_contact_id': TEST_USER['contact_id'], 'assigned_by_user_id': TEST_USER['user_id']}


def test_assigned_kid_moves_off_the_unassigned_page(client, night, gus_in_mp):
    _ok(client.post(team_url(GUS), json={'team': 'Yellow'}))
    assert 'Gus Gray' not in _html(client.get(f'/attendance/{D}/team/Unassigned'))
    assert 'Gus Gray' in _html(client.get(f'/attendance/{D}/team/Yellow'))


@pytest.mark.parametrize('value, team', [('yellow', 'Yellow'), (' Blue ', 'Blue'), ('RED', 'Red')])
def test_assign_team_accepts_any_case(client, seeded, gus_in_mp, value, team):
    assert _ok(client.post(team_url(GUS), json={'team': value}))['kid']['team'] == team
    assert gus_in_mp.puts[0][1] == [{'Group_Participant_ID': 9007, 'Notes': team}]


def test_assign_team_writes_as_the_signed_in_leader(client, seeded, gus_in_mp):
    login_session(client, user_id=777, contact_id=888)
    _ok(client.post(team_url(GUS), json={'team': 'Red', 'user_id': 1, 'by': {'user_id': 1}}))
    assert gus_in_mp.puts[0][2]['user_id'] == 777
    audit = seeded.execute('SELECT assigned_by_user_id, assigned_by_contact_id FROM team_assignments').fetchone()
    assert audit == {'assigned_by_user_id': 777, 'assigned_by_contact_id': 888}


def test_assign_team_already_assigned_locally_is_409(client, seeded, gus_in_mp):
    data = _err(client.post(team_url(1001), json={'team': 'Blue'}), 409, 'already_assigned')
    assert data['team'] == 'Red'
    assert gus_in_mp.calls == [] and gus_in_mp.puts == []
    assert _kid(seeded, 1001)['team'] == 'Red'


def test_assign_team_already_assigned_in_mp_is_409_and_adopted(client, night, fake_mp):
    fake_mp.tables['Group_Participants'] = [{**GUS_GP_ROW, 'Notes': 'Blue'}]
    data = _err(client.post(team_url(GUS), json={'team': 'Red'}), 409, 'already_assigned')
    assert data['team'] == 'Blue'
    assert fake_mp.puts == []
    assert _kid(night, GUS)['team'] == 'Blue' and _att(night, GUS)['team'] == 'Blue'
    assert _count(night, 'team_assignments') == 0


def _assert_nothing_assigned(conn):
    assert _kid(conn, GUS)['team'] is None and _kid(conn, GUS)['mp_notes'] == 'Created by CheckIn'
    assert _kid(conn, GUS)['team_assigned_at'] is None
    assert _att(conn, GUS)['team'] is None
    assert _count(conn, 'team_assignments') == 0


def test_assign_team_with_mp_writes_disabled_is_503(client, night):
    disabled = FakeMP(writes_enabled=False)
    disabled.tables['Group_Participants'] = [dict(GUS_GP_ROW)]
    mp.set_client(disabled)                                  # the fake_mp fixture resets the client afterwards
    _err(client.post(team_url(GUS), json={'team': 'Red'}), 503, 'disabled')
    assert disabled.puts == []
    _assert_nothing_assigned(night)


@pytest.mark.parametrize('error', [
    mp.MPUnavailable('timeout', op='put', table='Group_Participants'),
    mp.MPForbidden('forbidden', op='put', table='Group_Participants', status=403),
    mp.MPBadRequest('bad_request', op='put', table='Group_Participants', status=400),
])
def test_assign_team_mp_error_on_put_is_502_and_changes_nothing(client, night, fake_mp, error):
    def pre_read(**kw):
        fake_mp.fail_with = error                            # the pre-read succeeds; the PUT then fails
        return [dict(GUS_GP_ROW)]
    fake_mp.tables['Group_Participants'] = pre_read
    _err(client.post(team_url(GUS), json={'team': 'Red'}), 502, 'mp_unavailable')
    assert len(fake_mp.puts) == 1                            # it did try
    _assert_nothing_assigned(night)


@pytest.mark.parametrize('result', [[], [{'Group_Participant_ID': 9007, 'Notes': 'Created by CheckIn'}],
                                    [{'Group_Participant_ID': 9007, 'Notes': 'Blue'}]])
def test_assign_team_unconfirmed_mp_write_is_502(client, night, gus_in_mp, monkeypatch, result):
    monkeypatch.setattr(gus_in_mp, 'put', lambda table, records, **kw: result)
    _err(client.post(team_url(GUS), json={'team': 'Red'}), 502, 'mp_unavailable')
    _assert_nothing_assigned(night)


def test_assign_team_mp_error_on_pre_read_is_502(client, night, gus_in_mp):
    gus_in_mp.fail_with = mp.MPUnavailable('timeout', op='get', table='Group_Participants')
    _err(client.post(team_url(GUS), json={'team': 'Red'}), 502, 'mp_unavailable')
    assert gus_in_mp.puts == []
    _assert_nothing_assigned(night)


@pytest.mark.parametrize('body', [
    {'team': 'Purple'}, {'team': 'Unassigned'}, {'team': ''}, {'team': None}, {'team': 5}, {'team': True},
    {'team': ['Red']}, {'team': 'Red Team'}, {}, [{'team': 'Red'}], 'Red', None,
])
def test_assign_team_invalid_team_is_400(client, night, gus_in_mp, body):
    _err(client.post(team_url(GUS), json=body), 400, 'invalid_input')
    assert gus_in_mp.calls == [] and gus_in_mp.puts == []
    _assert_nothing_assigned(night)


@pytest.mark.parametrize('cid', [424242, 1008])             # unknown; last season's (inactive) kid
def test_assign_team_for_a_kid_not_on_the_roster_is_404(client, seeded, gus_in_mp, cid):
    _err(client.post(team_url(cid), json={'team': 'Red'}), 404, 'not_found')
    assert gus_in_mp.calls == [] and gus_in_mp.puts == []


@pytest.mark.parametrize('rows', [
    [],
    [{**GUS_GP_ROW, 'Group_ID': 1000}],
    [{**GUS_GP_ROW, 'Participant_ID': 5001}],
    [{**GUS_GP_ROW, 'End_Date': '2026-09-01T00:00:00'}],
])
def test_assign_team_for_a_kid_no_longer_in_the_mp_group_is_404(client, night, fake_mp, rows):
    fake_mp.tables['Group_Participants'] = rows
    _err(client.post(team_url(GUS), json={'team': 'Red'}), 404, 'not_found')
    assert fake_mp.puts == []
    _assert_nothing_assigned(night)


def test_assign_team_with_a_future_end_date_still_works(client, night, fake_mp):
    fake_mp.tables['Group_Participants'] = [{**GUS_GP_ROW, 'End_Date': '2027-06-01T00:00:00'}]
    assert _ok(client.post(team_url(GUS), json={'team': 'Red'}))['kid']['team'] == 'Red'


# =============================================================================
# (b) Templates: team page chips and Mark present
# =============================================================================

def _kid_row(root, name):
    rows = [li for li in root.find_all('li', cls='kid-row') if li.attrs.get('data-name') == name]
    assert len(rows) == 1, f'{name}: {len(rows)} rows'
    return rows[0]


def _chips(node):
    return node.find_all('button', cls='chip-toggle')


def test_team_page_chips_for_a_kid_who_is_here(client, night):
    root = dom(client.get(f'/attendance/{D}/team/Red'))
    alice = _kid_row(root, 'Alice Anders')
    assert alice.attrs['data-url'] == att_url(D, 1001)
    assert 'is-absent' not in alice.classes
    group = alice.one('div', cls='chip-row')
    assert group.attrs['role'] == 'group' and group.attrs['aria-label'] == 'Attendance for Alice Anders'
    chips = _chips(alice)
    assert [c.attrs['data-field'] for c in chips] == list(FLAGS)
    assert [c.text for c in chips] == list(CHIP_LABELS)
    assert [c.attrs['data-label'] for c in chips] == list(CHIP_LABELS)
    assert all(c.attrs['type'] == 'button' for c in chips)
    assert [c.attrs['aria-pressed'] for c in chips] == ['true', 'false', 'false', 'false', 'false']
    assert not any('disabled' in c.attrs for c in chips)
    assert alice.find('button', data_mark_present=True) is None


def test_team_page_chips_reflect_every_flag(client, seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, wearing_shirt=True, did_homework=True, has_dues=True)
    chips = _chips(_kid_row(dom(client.get(f'/attendance/{D}/team/Red')), 'Alice Anders'))
    assert [c.attrs['aria-pressed'] for c in chips] == ['false', 'true', 'false', 'true', 'true']


def test_team_page_manual_present_kid_has_chips_and_status(client, seeded):
    seeded.execute("""INSERT INTO attendance (contact_id, meeting_date, team, manual_present, manual_present_by)
                      VALUES (1002, '2026-09-23', 'Red', true, 'Pat Leader')""")
    bo = _kid_row(dom(client.get(f'/attendance/{D}/team/Red')), 'Bo Brandt')
    assert len(_chips(bo)) == 5 and not any('disabled' in c.attrs for c in _chips(bo))
    status = bo.one('span', cls='kid-status')
    assert 'is-manual' in status.classes and status.text == '✋ Marked present by Pat Leader'


def test_team_page_kids_not_here_get_mark_present_not_chips(client, night):
    root = dom(client.get(f'/attendance/{D}/team/Red?show=all'))
    bo = _kid_row(root, 'Bo Brandt')
    assert 'is-absent' in bo.classes
    assert bo.attrs['data-url'] == att_url(D, 1002)
    assert _chips(bo) == []
    btn = bo.one('button', cls='mark-present')
    assert btn.attrs['data-mark-present'] == 'on' and btn.attrs['type'] == 'button' and btn.text == 'Mark present'
    # the not-checked-in group is in the page even without ?show=all (hidden by CSS until Show all)
    root = dom(client.get(f'/attendance/{D}/team/Red'))
    assert _kid_row(root, 'Bo Brandt').find('button', cls='mark-present') is not None


def test_past_team_page_is_writable(client, seeded):
    add_attendance(seeded, 1001, D_0916, mp_checked_in=True)
    root = dom(client.get(f'/attendance/{PAST}/team/Red?show=all'))
    alice = _kid_row(root, 'Alice Anders')
    assert alice.attrs['data-url'] == att_url(PAST, 1001)
    assert len(_chips(alice)) == 5 and not any('disabled' in c.attrs for c in _chips(alice))
    assert _kid_row(root, 'Bo Brandt').find('button', cls='mark-present') is not None


def test_future_team_page_is_read_only(client, seeded):
    add_attendance(seeded, 1001, D_0930, manual_present=True, has_bible=True)
    root = dom(client.get(f'/attendance/{FUTURE}/team/Red?show=all'))
    chips = _chips(_kid_row(root, 'Alice Anders'))
    assert len(chips) == 5 and all('disabled' in c.attrs for c in chips)
    assert chips[0].attrs['aria-pressed'] == 'true'
    assert root.find_all('button', cls='mark-present') == []


# =============================================================================
# (b) Templates: kid page (instant toggles + Mark present / Undo)
# =============================================================================

def _kid_page(client, cid, team='Red', d=D):
    root = dom(client.get(f'/attendance/{d}/team/{team}/kid/{cid}'))
    live = root.one('div', cls='kid-live')
    return root, live


def test_kid_page_for_a_kiosk_check_in(client, night):
    root, live = _kid_page(client, 1001)
    assert live.attrs['data-url'] == att_url(D, 1001) and live.attrs['data-name'] == 'Alice Anders'
    chips = _chips(live)
    assert [c.attrs['data-field'] for c in chips] == list(FLAGS)
    assert all('chip-toggle--wide' in c.classes for c in chips)
    assert [c.attrs['aria-pressed'] for c in chips] == ['true', 'false', 'false', 'false', 'false']
    assert not any('disabled' in c.attrs for c in chips)
    assert live.find('button', data_mark_present=True) is None
    assert root.one('span', cls='present-status').text == '✓ Checked in · 6:12pm'
    assert root.find('form') is None                          # no more check-in form


def test_kid_page_for_a_kid_not_here(client, seeded):
    root, live = _kid_page(client, 1003, team='Blue')
    btn = live.one('button', data_mark_present=True)
    assert btn.attrs['data-mark-present'] == 'on' and btn.text == 'Mark present'
    assert 'is-undo' not in btn.classes
    assert all('disabled' in c.attrs for c in _chips(live))
    assert 'Mark Cara present to record their items.' in live.text
    assert 'is-absent' in root.one('span', cls='present-status').classes


def test_kid_page_for_a_manual_present_kid_offers_undo(client, seeded):
    seeded.execute("""INSERT INTO attendance (contact_id, meeting_date, team, manual_present, manual_present_by)
                      VALUES (1002, '2026-09-23', 'Red', true, 'Pat Leader')""")
    root, live = _kid_page(client, 1002)
    btn = live.one('button', data_mark_present=True)
    assert btn.attrs['data-mark-present'] == 'off' and btn.text == 'Undo' and 'is-undo' in btn.classes
    assert not any('disabled' in c.attrs for c in _chips(live))
    assert root.one('span', cls='present-status').text == '✋ Marked present by Pat Leader'


def test_kid_page_legacy_item_without_present_can_be_cleared(client, seeded):
    add_attendance(seeded, 1004, D_0923, has_book=True)
    _, live = _kid_page(client, 1004, team='Blue')
    disabled = {c.attrs['data-field']: 'disabled' in c.attrs for c in _chips(live)}
    assert disabled == {'has_bible': True, 'wearing_shirt': True, 'has_book': False, 'did_homework': True,
                        'has_dues': True}
    assert live.one('button', data_mark_present='on') is not None


def test_kid_page_for_a_future_date_is_read_only(client, seeded):
    add_attendance(seeded, 1002, D_0930, manual_present=True)
    _, live = _kid_page(client, 1002, d=FUTURE)
    assert live.find('button', data_mark_present=True) is None
    assert all('disabled' in c.attrs for c in _chips(live))
    assert 'This date hasn’t happened yet' in live.text


def test_kid_page_for_an_unknown_kid_is_404(client, seeded):
    assert client.get(f'/attendance/{D}/team/Red/kid/424242').status_code == 404


# =============================================================================
# (d) Templates: Assign team bottom sheet
# =============================================================================

def test_assign_ui_on_the_unassigned_page_when_mp_writes_are_enabled(make_client, night):
    c = make_client(MP_WRITES_ENABLED=True)
    root = dom(c.get(f'/attendance/{D}/team/Unassigned'))
    gus = _kid_row(root, 'Gus Gray')
    assert gus.attrs['data-assign-url'] == team_url(GUS)
    btn = gus.one('button', cls='assign-btn')
    assert 'data-assign' in btn.attrs and btn.attrs['type'] == 'button' and btn.text == 'Assign team'
    dialog = root.one('dialog', id='assign-dialog')
    assert 'sheet' in dialog.classes
    assert dialog.attrs['aria-labelledby'] == 'assign-title' and root.one('h2', id='assign-title')
    teams = dialog.find_all('button', data_team=True)
    assert [b.attrs['data-team'] for b in teams] == TEAM_ORDER
    assert [b.text for b in teams] == TEAM_ORDER
    assert all(b.attrs['aria-pressed'] == 'false' and b.attrs['type'] == 'button' for b in teams)
    confirm = dialog.one('button', data_assign_confirm=True)
    assert 'disabled' in confirm.attrs
    assert dialog.one('button', data_assign_cancel=True)
    assert 'Tap Assign team to set one.' in root.one('p', cls='kid-note').text


def test_assign_ui_hidden_when_mp_writes_are_disabled(make_client, night):
    c = make_client(MP_WRITES_ENABLED=False)
    root = dom(c.get(f'/attendance/{D}/team/Unassigned'))
    assert root.find('dialog') is None
    assert root.find_all('button', cls='assign-btn') == []
    assert 'data-assign-url' not in _kid_row(root, 'Gus Gray').attrs
    assert 'Ask staff to set a team in MinistryPlatform.' in root.one('p', cls='kid-note').text


@pytest.mark.parametrize('team', TEAM_ORDER)
def test_assign_ui_never_on_team_pages(make_client, night, team):
    c = make_client(MP_WRITES_ENABLED=True)
    body = _html(c.get(f'/attendance/{D}/team/{team}?show=all'))
    assert 'assign-dialog' not in body and 'data-assign' not in body and 'Assign team' not in body


def test_assign_button_hidden_on_a_future_unassigned_page(make_client, seeded):
    add_attendance(seeded, GUS, D_0930, team=None, manual_present=True)
    c = make_client(MP_WRITES_ENABLED=True)
    root = dom(c.get(f'/attendance/{FUTURE}/team/Unassigned'))
    assert root.find_all('button', cls='assign-btn') == []


# =============================================================================
# (c) Templates: tonight's section card
# =============================================================================

def _section_list(client, cid=1001, d=D, team='Red'):
    return dom(client.get(f'/home/{d}/team/{team}/record_section/kid/{cid}'))


def _card_chips(card, section):
    return {c.attrs['data-field']: c for c in card.find_all('button', cls='credit-chip', data_section=section)}


def test_tonight_card_pins_the_scheduled_section(client, seeded):
    root = _section_list(client)
    card = root.one('div', cls='tonight-card')
    assert card.one('p', cls='tonight-card__title').text == 'Tonight’s section'
    blocks = card.find_all('div', cls='tonight-section')
    assert len(blocks) == 1
    name = blocks[0].one('a', cls='tonight-section__name')
    assert name.text == '1.2'
    assert name.attrs['href'] == f'/home/{D}/team/Red/record_section/kid/1001/section/1.2'
    chips = _card_chips(card, '1.2')
    assert list(chips) == list(CREDITS)
    for field, chip in chips.items():
        assert f'credit-chip--{field}' in chip.classes
        assert chip.attrs['data-url'] == sec_url(D, 1001)
        assert chip.attrs['data-state'] == 'none' and chip.attrs['aria-pressed'] == 'false'
        assert 'disabled' not in chip.attrs and chip.attrs['type'] == 'button'
    assert [chips[f].attrs['data-label'] for f in CREDITS] == ['Main', 'Silver', 'Gold']


def test_tonight_card_shows_tonights_credits_as_undoable(client, seeded):
    add_section(seeded, 1001, D_0923, '1.2', silver=True)
    chips = _card_chips(_section_list(client).one('div', cls='tonight-card'), '1.2')
    assert chips['silver_credit'].attrs['data-state'] == 'today'
    assert chips['silver_credit'].attrs['aria-pressed'] == 'true'
    assert 'disabled' not in chips['silver_credit'].attrs
    assert chips['section_complete'].attrs['data-state'] == 'none'


def test_tonight_card_credit_from_another_night_is_disabled_with_its_date(client, seeded):
    add_section(seeded, 1001, D_0916, '1.2', complete=True)
    chips = _card_chips(_section_list(client).one('div', cls='tonight-card'), '1.2')
    main = chips['section_complete']
    assert main.attrs['data-state'] == 'earlier' and main.attrs['aria-pressed'] == 'false'
    assert 'disabled' in main.attrs
    assert main.one('span', cls='credit-chip__when').text.endswith('9/16')
    for field in ('silver_credit', 'gold_credit'):
        assert chips[field].attrs['data-state'] == 'none' and 'disabled' not in chips[field].attrs
        assert chips[field].one('span', cls='credit-chip__when').text == ''


def test_tonight_card_credit_from_another_night_reads_check_and_date(client, seeded):
    add_section(seeded, 1001, D_0916, '1.2', complete=True)
    main = _card_chips(_section_list(client).one('div', cls='tonight-card'), '1.2')['section_complete']
    css = _html(client.get('/static/style.css'))
    css_check = re.search(r'credit-chip[^{]*earlier[^{]*::before\s*\{[^}]*content:\s*"✓', css)
    assert '✓ 9/16' in main.text or css_check


def test_tonight_tag_marks_the_pinned_section_in_the_full_list(client, seeded):
    root = _section_list(client)
    rows = root.find_all('a', cls='section-row')
    assert len(rows) == 8
    tagged = [r.one('span', cls='section-name').text for r in rows if r.find('span', cls='tonight-tag')]
    assert tagged == ['1.2']
    assert root.one('span', cls='tonight-tag').text == 'Tonight'


def test_tonight_card_lists_every_scheduled_section(client, seeded):
    seeded.execute("UPDATE schedule SET sections = '{1.2,1.3,9.9}' WHERE meeting_date = '2026-09-23'")
    root = _section_list(client)
    card = root.one('div', cls='tonight-card')
    assert card.one('p', cls='tonight-card__title').text == 'Tonight’s sections'
    assert [b.one('a', cls='tonight-section__name').text for b in card.find_all('div', cls='tonight-section')] == [
        '1.2', '1.3']                                        # 9.9 isn't a book section
    assert len(_card_chips(card, '1.3')) == 3
    tagged = [r.one('span', cls='section-name').text for r in root.find_all('a', cls='section-row')
              if r.find('span', cls='tonight-tag')]
    assert tagged == ['1.2', '1.3']


@pytest.mark.parametrize('d, setup', [
    (D, "UPDATE schedule SET sections = '{}' WHERE meeting_date = '2026-09-23'"),
    (D, "UPDATE schedule SET sections = '{9.9}' WHERE meeting_date = '2026-09-23'"),
    (NO_CLUB, None),
])
def test_no_tonight_card_when_the_date_has_no_sections(client, seeded, d, setup):
    if setup:
        seeded.execute(setup)
    root = _section_list(client, d=d)
    assert root.find('div', cls='tonight-card') is None
    assert root.find('span', cls='tonight-tag') is None
    assert len(root.find_all('a', cls='section-row')) == 8


def test_past_date_card_is_titled_with_the_date(client, seeded):
    card = _section_list(client, d=PAST).one('div', cls='tonight-card')
    assert card.one('p', cls='tonight-card__title').text == 'Section for 9/16'
    chips = _card_chips(card, '1.1')
    assert all(c.attrs['data-url'] == sec_url(PAST, 1001) and 'disabled' not in c.attrs for c in chips.values())


def test_past_date_card_title_with_several_sections(client, seeded):
    title = _section_list(client, d=D_0909.isoformat()).one('p', cls='tonight-card__title').text
    assert '9/9s' not in title


def test_past_date_full_list_does_not_say_tonight(client, seeded):
    root = _section_list(client, d=PAST)
    assert not any('Tonight' in t.text for t in root.find_all('span', cls='tonight-tag'))


def test_future_date_card_chips_are_disabled(client, seeded):
    card = _section_list(client, d=FUTURE).one('div', cls='tonight-card')
    chips = _card_chips(card, '1.3')
    assert len(chips) == 3 and all('disabled' in c.attrs for c in chips.values())


def test_section_list_for_an_unassigned_kid_has_the_card(client, night):
    card = _section_list(client, cid=GUS, team='Unassigned').one('div', cls='tonight-card')
    assert all(c.attrs['data-url'] == sec_url(D, GUS) for c in _card_chips(card, '1.2').values())


# =============================================================================
# Removed routes, assets
# =============================================================================

OLD_CHECKIN_FORM = {'date_str': D, 'contact_id': '1003', 'team': 'Blue', 'manual_present': 'on',
                    'has_bible': 'on', 'can_mark_present': '1'}


@pytest.mark.parametrize('method, path', [
    ('post', '/submit_checkin'),
    ('post', '/edit_attendance'),
    ('get', '/submit_checkin'),
    ('get', '/edit_attendance'),
    ('get', f'/attendance/{D}/team/Blue/checkin/1003'),
    ('get', f'/attendance/{D}/team/Unassigned/checkin/1007'),
])
@pytest.mark.parametrize('fetch', [True, False])
def test_removed_checkin_routes_are_gone(client, seeded, method, path, fetch):
    headers = {} if fetch else {'X-Requested-With': ''}
    if method == 'post':
        resp = client.post(path, data=OLD_CHECKIN_FORM, headers=headers)
    else:
        resp = client.get(path, headers=headers)
    assert resp.status_code in (404, 405)
    if fetch and method == 'post':
        assert resp.get_json()['ok'] is False
    assert _count(seeded, 'attendance') == 0


def test_no_page_links_to_the_old_checkin_form(client, night):
    for path in (f'/attendance/{D}/team/Red?show=all', f'/attendance/{D}/team/Unassigned?show=all',
                 f'/attendance/{D}/team/Blue/kid/1003'):
        body = _html(client.get(path))
        assert '/checkin/' not in body and 'submit_checkin' not in body and 'edit_attendance' not in body


def test_app_js_is_served_with_the_club_night_handlers(anon_client, conn):
    resp = anon_client.get('/static/app.js')
    assert resp.status_code == 200
    assert resp.mimetype in ('text/javascript', 'application/javascript')
    js = _html(resp)
    for name in ('initAttendanceChips', 'initMarkPresent', 'initCreditChips', 'initAssignDialog', 'initShowAll'):
        assert re.search(rf'function {name}\s*\(', js), name
        assert re.search(rf'^\s*(document\.addEventListener\(\'DOMContentLoaded\', )?{name}(\(\))?[;)]', js, re.M), \
            f'{name} is never called'
    assert "'X-CSRFToken'" in js and "'X-Requested-With': 'fetch'" in js
    assert 'window.tnt' in js


@pytest.mark.parametrize('path', [f'/attendance/{D}/team/Red', f'/attendance/{D}/team/Red/kid/1001',
                                  f'/home/{D}/team/Red/record_section/kid/1001'])
def test_club_night_pages_load_app_js_and_the_csrf_meta(client, night, path):
    root = dom(client.get(path))
    assert [s.attrs['src'] for s in root.find_all('script', src=True)] == ['/static/app.js?v=dev']
    assert root.one('meta', name='csrf-token').attrs['content']
