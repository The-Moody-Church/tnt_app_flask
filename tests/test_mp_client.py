"""models/mp.py: MPClient token cache, retries, error mapping, paging, writes, and the $filter literal helpers."""
import threading
import time as real_time
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from models import mp
from models.mp import MPClient

BASE = 'https://mp.example.test/ministryplatformapi'
SECRET = 'sekrit-client-secret'
GUID = '0b8c9d2e-1f3a-4b5c-8d6e-7f8091a2b3c4'


class FakeResponse:
    def __init__(self, status_code=200, body=None, *, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else ''
        self._invalid = text is not None

    def json(self):
        if self._invalid:
            raise requests.exceptions.JSONDecodeError('Expecting value', self.text, 0)
        return self._body


class FakeSession:
    """Scripted requests.Session. Queue responses (or exceptions) in `token_queue` / `queue`.

    With an empty token queue each POST issues a fresh token 'tok-N'; with an empty data queue
    each request returns 200 [].
    """

    def __init__(self):
        self.headers = {}
        self.token_queue = []
        self.queue = []
        self.posts = []
        self.requests = []
        self.issued = 0

    def post(self, url, **kw):
        self.posts.append({'url': url, **kw})
        if self.token_queue:
            item = self.token_queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        self.issued += 1
        return FakeResponse(200, {'access_token': f'tok-{self.issued}', 'expires_in': 3600})

    def request(self, method, url, **kw):
        self.requests.append({'method': method, 'url': url, **kw})
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item(method, url, kw) if callable(item) else item
        return FakeResponse(200, [])

    def bearer(self, i):
        return self.requests[i]['headers']['Authorization']


class Clock:
    """Stands in for the `time` module inside models.mp."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def monotonic(self):
        return self.now


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def mpc(session):
    return MPClient(BASE, 'client-id', SECRET, writes_enabled=True, session_factory=lambda: session)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mp, 'time', c)
    return c


def _no_http(session):
    assert session.posts == [] and session.requests == []


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def test_token_post_sends_client_credentials_form(mpc, session):
    """The token POST is a client_credentials form with the all-data scope and the client id/secret."""
    assert mpc.token(timeout=(1, 2)) == 'tok-1'
    assert len(session.posts) == 1
    post = session.posts[0]
    assert post['url'] == f'{BASE}/oauth/connect/token'
    assert post['data'] == {'grant_type': 'client_credentials',
                            'scope': 'http://www.thinkministry.com/dataplatform/scopes/all',
                            'client_id': 'client-id', 'client_secret': SECRET}
    assert post['headers'] == {'Content-Type': 'application/x-www-form-urlencoded'}
    assert post['timeout'] == (1, 2)


def test_token_cached_across_calls(mpc, session, clock):
    """Several data calls share one token POST, and every call carries the bearer token."""
    mpc.get('Contacts', select='Contact_ID', top=1)
    clock.now += 60
    mpc.get('Contacts', select='Contact_ID', top=1)
    mpc.get_one('Contacts', select='Contact_ID', filter='Contact_ID = 1')
    assert len(session.posts) == 1
    assert [session.bearer(i) for i in range(3)] == ['Bearer tok-1'] * 3


def test_token_refreshed_five_minutes_before_expiry(mpc, session, clock):
    """expires_in=3600 -> reused until 3300 s, refreshed at 3300 s (the 5-minute early margin)."""
    assert mpc.token() == 'tok-1'
    clock.now += 3299.9
    assert mpc.token() == 'tok-1'
    clock.now += 0.1
    assert mpc.token() == 'tok-2'
    assert len(session.posts) == 2


def test_token_short_lifetime_uses_30_second_floor(mpc, session, clock):
    """expires_in below the margin still caches for 30 s rather than refreshing on every call."""
    session.token_queue.append(FakeResponse(200, {'access_token': 'short', 'expires_in': 60}))
    assert mpc.token() == 'short'
    clock.now += 29.9
    assert mpc.token() == 'short'
    clock.now += 0.1
    assert mpc.token() == 'tok-1'


def test_token_missing_expires_in_defaults_to_an_hour(mpc, session, clock):
    session.token_queue.append(FakeResponse(200, {'access_token': 'no-exp'}))
    assert mpc.token() == 'no-exp'
    clock.now += 3299
    assert mpc.token() == 'no-exp'
    clock.now += 1
    assert mpc.token() == 'tok-1'


def test_token_force_and_invalidate_fetch_a_new_token(mpc, session):
    assert mpc.token() == 'tok-1'
    assert mpc.token(force=True) == 'tok-2'
    mpc.invalidate_token()
    assert mpc.token() == 'tok-3'
    assert mpc.token() == 'tok-3'


@pytest.mark.parametrize('status', [400, 401])
def test_token_rejected_raises_auth_error(mpc, session, status):
    session.token_queue.append(FakeResponse(status, {'error': 'invalid_client'}))
    with pytest.raises(mp.MPAuthError) as exc:
        mpc.token()
    assert exc.value.status == status
    assert exc.value.kind == 'token_rejected'
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize('status', [302, 403, 500, 503])
def test_token_other_http_errors_are_unavailable(mpc, session, status):
    session.token_queue.append(FakeResponse(status, {}))
    with pytest.raises(mp.MPUnavailable) as exc:
        mpc.token()
    assert exc.value.status == status


@pytest.mark.parametrize('error', [requests.ConnectionError('conn refused'), requests.Timeout('slow'),
                                   requests.exceptions.SSLError('bad cert')])
def test_token_network_error_is_unavailable(mpc, session, error):
    session.token_queue.append(error)
    with pytest.raises(mp.MPUnavailable) as exc:
        mpc.token()
    assert exc.value.kind == type(error).__name__
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.parametrize('response', [FakeResponse(200, text='<html>oops</html>'),
                                      FakeResponse(200, {'token_type': 'Bearer'})])
def test_token_bad_body_is_unavailable(mpc, session, response):
    session.token_queue.append(response)
    with pytest.raises(mp.MPUnavailable) as exc:
        mpc.token()
    assert exc.value.kind == 'bad_body'


@pytest.mark.parametrize('body', [None, ['access_token'], {'access_token': 't', 'expires_in': 'soon'}])
def test_token_malformed_json_body_is_unavailable(mpc, session, body):
    session.token_queue.append(FakeResponse(200, body))
    with pytest.raises(mp.MPUnavailable):
        mpc.token()


def test_token_failure_is_not_cached(mpc, session):
    """A failed token POST leaves nothing cached; the next call tries again."""
    session.token_queue.append(FakeResponse(503, {}))
    with pytest.raises(mp.MPUnavailable):
        mpc.token()
    assert mpc.token() == 'tok-1'
    assert len(session.posts) == 2


def test_token_without_credentials_is_auth_error_without_http(session):
    client = MPClient(BASE, '', '', session_factory=lambda: session)
    with pytest.raises(mp.MPAuthError) as exc:
        client.get('Contacts', select='Contact_ID', top=1)
    assert exc.value.kind == 'not_configured'
    _no_http(session)


def test_concurrent_token_requests_post_once(session):
    """The lock + double-check means racing threads share one token POST."""
    original_post = session.post

    def slow_post(url, **kw):
        real_time.sleep(0.05)
        return original_post(url, **kw)

    session.post = slow_post
    client = MPClient(BASE, 'client-id', SECRET, session_factory=lambda: session)
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.token())) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == ['tok-1'] * 5
    assert len(session.posts) == 1


def test_one_session_per_thread_with_default_headers():
    """Each thread gets its own session (created once, with Accept and User-Agent); the token is shared."""
    sessions = []

    def factory():
        s = FakeSession()
        s.issued = len(sessions) * 100
        sessions.append(s)
        return s

    client = MPClient(BASE, 'client-id', SECRET, session_factory=factory)
    client.get('Contacts', select='Contact_ID', top=1)
    client.get('Contacts', select='Contact_ID', top=1)
    worker = threading.Thread(target=lambda: client.get('Contacts', select='Contact_ID', top=1))
    worker.start()
    worker.join()
    assert len(sessions) == 2
    assert len(sessions[0].requests) == 2 and len(sessions[1].requests) == 1
    assert len(sessions[0].posts) == 1 and sessions[1].posts == []
    assert sessions[1].bearer(0) == 'Bearer tok-1'
    for s in sessions:
        assert s.headers['Accept'] == 'application/json'
        assert s.headers['User-Agent'].startswith('tnt-tracker/')


# ---------------------------------------------------------------------------
# Data calls: retries and error mapping
# ---------------------------------------------------------------------------

def test_401_refreshes_token_once_and_retries(mpc, session):
    session.queue += [FakeResponse(401, {}), FakeResponse(200, [{'Contact_ID': 1}])]
    assert mpc.get('Contacts', select='Contact_ID', top=1) == [{'Contact_ID': 1}]
    assert len(session.posts) == 2
    assert [session.bearer(0), session.bearer(1)] == ['Bearer tok-1', 'Bearer tok-2']
    assert session.requests[0]['params'] == session.requests[1]['params']


def test_two_401s_raise_auth_error(mpc, session):
    session.queue += [FakeResponse(401, {}), FakeResponse(401, {})]
    with pytest.raises(mp.MPAuthError) as exc:
        mpc.get('Contacts', select='Contact_ID', top=1)
    assert exc.value.status == 401
    assert exc.value.table == 'Contacts'
    assert len(session.requests) == 2
    assert len(session.posts) == 2


def test_refreshed_token_is_reused_after_401(mpc, session):
    session.queue.append(FakeResponse(401, {}))
    mpc.get('Contacts', select='Contact_ID', top=1)
    mpc.get('Contacts', select='Contact_ID', top=1)
    assert session.bearer(2) == 'Bearer tok-2'
    assert len(session.posts) == 2


def test_token_rejected_during_401_retry_is_auth_error(mpc, session):
    mpc.token()
    session.queue.append(FakeResponse(401, {}))
    session.token_queue.append(FakeResponse(400, {}))
    with pytest.raises(mp.MPAuthError):
        mpc.get('Contacts', select='Contact_ID', top=1)
    assert len(session.requests) == 1


@pytest.mark.parametrize('status, error', [
    (400, mp.MPBadRequest), (403, mp.MPForbidden), (404, mp.MPNotFound), (429, mp.MPUnavailable),
    (500, mp.MPUnavailable), (502, mp.MPUnavailable), (503, mp.MPUnavailable), (504, mp.MPUnavailable),
    (302, mp.MPUnavailable),
])
def test_http_status_maps_to_typed_error_without_retry(mpc, session, status, error):
    session.queue.append(FakeResponse(status, {'Message': 'nope'}))
    with pytest.raises(error) as exc:
        mpc.get('Contacts', select='Contact_ID', top=1)
    assert type(exc.value) is error
    assert (exc.value.status, exc.value.op, exc.value.table) == (status, 'get', 'Contacts')
    assert len(session.requests) == 1
    assert len(session.posts) == 1


def test_all_mp_errors_share_a_base_class():
    for cls in (mp.MPUnavailable, mp.MPAuthError, mp.MPForbidden, mp.MPBadRequest, mp.MPNotFound,
                mp.MPWritesDisabled):
        assert issubclass(cls, mp.MPError)


def test_non_json_200_is_unavailable(mpc, session):
    session.queue.append(FakeResponse(200, text='<html>maintenance</html>'))
    with pytest.raises(mp.MPUnavailable) as exc:
        mpc.get('Contacts', select='Contact_ID', top=1)
    assert exc.value.kind == 'bad_body'
    assert exc.value.status == 200


@pytest.mark.parametrize('error', [requests.ConnectionError('refused'), requests.Timeout('slow'),
                                   requests.exceptions.ReadTimeout('read'),
                                   requests.exceptions.ChunkedEncodingError('cut off')])
def test_request_exceptions_are_unavailable(mpc, session, error):
    session.queue.append(error)
    with pytest.raises(mp.MPUnavailable) as exc:
        mpc.get('Contacts', select='Contact_ID', top=1)
    assert exc.value.kind == type(error).__name__
    assert exc.value.table == 'Contacts'
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert len(session.requests) == 1


SECRET_FILTER = f"User_GUID = '{GUID}'"
LEAKY_BODY = f"{{\"Message\": \"Invalid filter {SECRET_FILTER} with tok-1\"}}"


@pytest.mark.parametrize('responses', [
    [FakeResponse(400, text=LEAKY_BODY)],
    [FakeResponse(403, text=LEAKY_BODY)],
    [FakeResponse(404, text=LEAKY_BODY)],
    [FakeResponse(429, text=LEAKY_BODY)],
    [FakeResponse(500, text=LEAKY_BODY)],
    [FakeResponse(200, text=LEAKY_BODY)],
    [FakeResponse(401, text=LEAKY_BODY), FakeResponse(401, text=LEAKY_BODY)],
    [requests.ConnectionError(f"Max retries exceeded with url: /tables/dp_Users?$filter={SECRET_FILTER}")],
])
def test_error_messages_never_include_filter_body_or_token(mpc, session, responses):
    session.queue += responses
    with pytest.raises(mp.MPError) as exc:
        mpc.get('dp_Users', select='User_ID', filter=SECRET_FILTER, top=1)
    text = ' '.join([str(exc.value), repr(exc.value), *map(str, exc.value.args)])
    for secret in (GUID, 'User_GUID', 'Invalid filter', 'tok-', SECRET, 'Max retries'):
        assert secret not in text
    assert 'dp_Users' in str(exc.value)


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('select', [None, ''])
def test_get_requires_select(mpc, session, select):
    with pytest.raises(ValueError, match='select'):
        mpc.get('Contacts', select=select, top=1)
    _no_http(session)


@pytest.mark.parametrize('table', ['', '1Contacts', 'Con tacts', 'Contacts/1', '../dp_Users', 'Contacts?$top=1',
                                   'dp_Users;', 'Contacts$', 'Contacts.Contact_ID', "Contacts'"])
def test_get_rejects_bad_table_names(mpc, session, table):
    with pytest.raises(ValueError, match='table'):
        mpc.get(table, select='Contact_ID', top=1)
    _no_http(session)


def test_get_rejects_table_name_with_trailing_newline(mpc, session):
    with pytest.raises(ValueError):
        mpc.get('Contacts\n', select='Contact_ID', top=1)


def test_get_accepts_mp_style_table_names(mpc, session):
    for table in ('Contacts', 'Group_Participants', 'dp_Users', '_Private2'):
        mpc.get(table, select='x', top=1)
    assert [r['url'] for r in session.requests] == [
        f'{BASE}/tables/Contacts', f'{BASE}/tables/Group_Participants', f'{BASE}/tables/dp_Users',
        f'{BASE}/tables/_Private2']


def test_get_sends_query_params_and_timeout(mpc, session):
    mpc.get('Group_Participants', select='Group_Participant_ID,Notes', filter='Group_ID = 697',
            orderby='Group_Participant_ID', distinct=True, timeout=(2, 4))
    req = session.requests[0]
    assert req['method'] == 'GET'
    assert req['url'] == f'{BASE}/tables/Group_Participants'
    assert req['params'] == {'$select': 'Group_Participant_ID,Notes', '$filter': 'Group_ID = 697',
                             '$orderby': 'Group_Participant_ID', '$distinct': 'true', '$top': 1000, '$skip': 0}
    assert req['json'] is None
    assert req['timeout'] == (2, 4)
    assert session.posts[0]['timeout'] == (2, 4)


def test_get_omits_unset_options(mpc, session):
    mpc.get('Contacts', select='Contact_ID', top=10)
    assert session.requests[0]['params'] == {'$select': 'Contact_ID', '$top': 10}


def test_get_paginates_with_top_and_skip(mpc, session):
    """1000 + 5 rows -> two calls, rows concatenated in order."""
    session.queue += [FakeResponse(200, [{'n': i} for i in range(1000)]),
                      FakeResponse(200, [{'n': i} for i in range(1000, 1005)])]
    rows = mpc.get('Contacts', select='Contact_ID', orderby='Contact_ID')
    assert [r['n'] for r in rows] == list(range(1005))
    assert [(r['params']['$top'], r['params']['$skip']) for r in session.requests] == [(1000, 0), (1000, 1000)]
    assert all(r['params']['$orderby'] == 'Contact_ID' for r in session.requests)
    assert len(session.posts) == 1


def test_get_exact_page_multiple_fetches_one_empty_page(mpc, session):
    session.queue += [FakeResponse(200, [{'n': i} for i in range(1000)]), FakeResponse(200, [])]
    assert len(mpc.get('Contacts', select='Contact_ID', orderby='Contact_ID')) == 1000
    assert len(session.requests) == 2


def test_get_short_first_page_is_one_call(mpc, session):
    session.queue.append(FakeResponse(200, [{'n': 1}]))
    assert mpc.get('Contacts', select='Contact_ID', orderby='Contact_ID') == [{'n': 1}]
    assert len(session.requests) == 1


def test_get_pagination_requires_orderby(mpc, session):
    with pytest.raises(ValueError, match='orderby'):
        mpc.get('Contacts', select='Contact_ID')
    with pytest.raises(ValueError, match='orderby'):
        mpc.get('Contacts', select='Contact_ID', top=5, paginate=True)
    _no_http(session)


def test_get_top_disables_pagination(mpc, session):
    """top= means one call with $top and no $skip, and no orderby needed."""
    session.queue.append(FakeResponse(200, [{'n': i} for i in range(5)]))
    assert len(mpc.get('Contacts', select='Contact_ID', top=5)) == 5
    assert len(session.requests) == 1
    assert session.requests[0]['params'] == {'$select': 'Contact_ID', '$top': 5}


def test_get_paginate_false_sends_no_paging_params(mpc, session):
    mpc.get('Contacts', select='Contact_ID', paginate=False)
    assert session.requests[0]['params'] == {'$select': 'Contact_ID'}


def test_get_stops_after_max_pages(mpc, session, monkeypatch):
    monkeypatch.setattr(mp, 'PAGE_SIZE', 2)
    monkeypatch.setattr(mp, 'MAX_PAGES', 3)
    session.queue += [FakeResponse(200, [{}, {}]) for _ in range(5)]
    with pytest.raises(mp.MPError) as exc:
        mpc.get('Contacts', select='Contact_ID', orderby='Contact_ID')
    assert exc.value.kind == 'too_many_rows'
    assert [r['params']['$skip'] for r in session.requests] == [0, 2, 4]


def test_get_error_on_later_page_propagates(mpc, session):
    session.queue += [FakeResponse(200, [{}] * 1000), FakeResponse(503, {})]
    with pytest.raises(mp.MPUnavailable):
        mpc.get('Contacts', select='Contact_ID', orderby='Contact_ID')


def test_get_one_returns_first_row_or_none(mpc, session):
    session.queue += [FakeResponse(200, [{'User_ID': 7}]), FakeResponse(200, [])]
    assert mpc.get_one('dp_Users', select='User_ID', filter='User_ID = 7') == {'User_ID': 7}
    assert mpc.get_one('dp_Users', select='User_ID', filter='User_ID = 8') is None
    assert session.requests[0]['params'] == {'$select': 'User_ID', '$filter': 'User_ID = 7', '$top': 1}


def test_base_url_trailing_slash_is_stripped(session):
    client = MPClient(BASE + '/', 'client-id', SECRET, session_factory=lambda: session)
    client.get('Contacts', select='Contact_ID', top=1)
    assert session.posts[0]['url'] == f'{BASE}/oauth/connect/token'
    assert session.requests[0]['url'] == f'{BASE}/tables/Contacts'


# ---------------------------------------------------------------------------
# put()
# ---------------------------------------------------------------------------

RECORDS = [{'Group_Participant_ID': 9007, 'Notes': 'Blue'}]


def test_put_blocked_when_writes_disabled(session):
    client = MPClient(BASE, 'client-id', SECRET, writes_enabled=False, session_factory=lambda: session)
    with pytest.raises(mp.MPWritesDisabled) as exc:
        client.put('Group_Participants', RECORDS, user_id=42)
    assert (exc.value.op, exc.value.table) == ('put', 'Group_Participants')
    _no_http(session)


def test_writes_default_to_disabled(session):
    client = MPClient(BASE, 'client-id', SECRET, session_factory=lambda: session)
    assert client.writes_enabled is False
    with pytest.raises(mp.MPWritesDisabled):
        client.put('Group_Participants', RECORDS, user_id=42)
    _no_http(session)


@pytest.mark.parametrize('user_id', [True, False, 0, -1, '42', 42.0, None])
def test_put_requires_positive_int_user_id(mpc, session, user_id):
    with pytest.raises(ValueError, match='user_id'):
        mpc.put('Group_Participants', RECORDS, user_id=user_id)
    _no_http(session)


@pytest.mark.parametrize('records', [[], [{}], [{'Notes': 'Red'}, {}], ['Notes'], [None],
                                     {'Group_Participant_ID': 9007, 'Notes': 'Blue'}])
def test_put_requires_non_empty_dict_records(mpc, session, records):
    with pytest.raises(ValueError, match='records'):
        mpc.put('Group_Participants', records, user_id=42)
    _no_http(session)


def test_put_rejects_bad_table_name(mpc, session):
    with pytest.raises(ValueError, match='table'):
        mpc.put('Group_Participants?$userId=1', RECORDS, user_id=42)
    _no_http(session)


def test_put_sends_user_id_and_json_body(mpc, session):
    session.queue.append(FakeResponse(200, [{'Group_Participant_ID': 9007, 'Notes': 'Blue'}]))
    result = mpc.put('Group_Participants', RECORDS, user_id=42, timeout=(2, 4))
    assert result == [{'Group_Participant_ID': 9007, 'Notes': 'Blue'}]
    req = session.requests[0]
    assert req['method'] == 'PUT'
    assert req['url'] == f'{BASE}/tables/Group_Participants'
    assert req['params'] == {'$userId': 42}
    assert req['json'] == RECORDS
    assert req['headers'] == {'Authorization': 'Bearer tok-1'}
    assert req['timeout'] == (2, 4)


def test_put_passes_select(mpc, session):
    mpc.put('Group_Participants', RECORDS, user_id=42, select='Group_Participant_ID,Notes')
    assert session.requests[0]['params'] == {'$userId': 42, '$select': 'Group_Participant_ID,Notes'}


def test_put_retries_once_on_401_with_same_body(mpc, session):
    session.queue += [FakeResponse(401, {}), FakeResponse(200, RECORDS)]
    assert mpc.put('Group_Participants', RECORDS, user_id=42) == RECORDS
    assert [r['json'] for r in session.requests] == [RECORDS, RECORDS]
    assert session.bearer(1) == 'Bearer tok-2'


@pytest.mark.parametrize('status, error', [(400, mp.MPBadRequest), (403, mp.MPForbidden), (500, mp.MPUnavailable)])
def test_put_errors_are_typed(mpc, session, status, error):
    session.queue.append(FakeResponse(status, {}))
    with pytest.raises(error) as exc:
        mpc.put('Group_Participants', RECORDS, user_id=42)
    assert exc.value.op == 'put'


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def test_module_helpers_delegate_to_installed_client(mpc, session, monkeypatch):
    monkeypatch.setattr(mp, '_client', mpc)
    session.queue += [FakeResponse(200, [{'a': 1}]), FakeResponse(200, [{'a': 2}]), FakeResponse(200, RECORDS)]
    assert mp.get_client() is mpc
    assert mp.mp_get('Contacts', select='a', top=1) == [{'a': 1}]
    assert mp.mp_get_one('Contacts', select='a', filter='a = 2') == {'a': 2}
    assert mp.mp_put('Group_Participants', RECORDS, user_id=42) == RECORDS
    assert mp.writes_enabled() is True


# ---------------------------------------------------------------------------
# $filter literal helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', [(5, '5'), (697, '697'), ('42', '42'), ('007', '7')])
def test_q_int_accepts_positive_ints(value, expected):
    assert mp.q_int(value) == expected


@pytest.mark.parametrize('value', [True, False, 0, -1, '0', '-1', '1 OR 1=1', '1)', '1.5', 1.5, '', ' 7', '7 ',
                                   None, [1], "1' OR '1'='1"])
def test_q_int_rejects_non_ids(value):
    with pytest.raises(ValueError):
        mp.q_int(value)


def test_q_ids_joins_validated_ids():
    assert mp.q_ids([1, '2', 3]) == '1,2,3'
    assert mp.q_ids(n for n in (5001, 5002)) == '5001,5002'
    assert mp.q_ids({9}) == '9'


@pytest.mark.parametrize('values', [[], (), iter([]), [1, 'x'], [1, 0], [True], ['1 OR 1=1']])
def test_q_ids_rejects_empty_or_bad_lists(values):
    with pytest.raises(ValueError):
        mp.q_ids(values)


def test_q_date_quotes_iso_date():
    assert mp.q_date(date(2026, 9, 23)) == "'2026-09-23'"


@pytest.mark.parametrize('value', [datetime(2026, 9, 23, 18, 0), datetime(2026, 9, 23, tzinfo=timezone.utc),
                                   '2026-09-23', None, 20260923])
def test_q_date_rejects_non_dates(value):
    with pytest.raises(ValueError):
        mp.q_date(value)


@pytest.mark.parametrize('value, expected', [
    (GUID, GUID), (GUID.upper(), GUID), (f'  {GUID.upper()}\n', GUID),
    ('0B8C9D2E-1f3a-4B5C-8d6e-7F8091A2B3C4', GUID),
])
def test_normalize_guid_lowercases_and_strips(value, expected):
    assert mp.normalize_guid(value) == expected


@pytest.mark.parametrize('value', [None, 123, '', 'not-a-guid', '{' + GUID + '}', GUID.replace('-', ''),
                                   GUID[:-1], GUID + '0', GUID[:-1] + 'g', f"{GUID}' OR '1'='1",
                                   GUID.replace('-', ' ')])
def test_normalize_guid_rejects_invalid(value):
    assert mp.normalize_guid(value) is None


def test_q_guid_quotes_normalized_guid():
    assert mp.q_guid(GUID.upper()) == f"'{GUID}'"


@pytest.mark.parametrize('value', [None, '', 'abc', f"{GUID}' OR 1=1 --", "' OR ''='", GUID.replace('-', '')])
def test_q_guid_rejects_invalid(value):
    with pytest.raises(ValueError):
        mp.q_guid(value)


# ---------------------------------------------------------------------------
# parse_mp_datetime
# ---------------------------------------------------------------------------

def test_parse_mp_datetime_attaches_chicago_to_naive_values():
    dt = mp.parse_mp_datetime('2026-09-23T18:31:02')
    assert dt.tzinfo == mp.CHICAGO
    assert (dt.hour, dt.minute, dt.second) == (18, 31, 2)
    assert dt.utcoffset() == timedelta(hours=-5)
    assert dt == datetime(2026, 9, 23, 23, 31, 2, tzinfo=timezone.utc)


def test_parse_mp_datetime_uses_standard_time_in_winter():
    dt = mp.parse_mp_datetime('2026-01-14T18:05:00')
    assert dt.utcoffset() == timedelta(hours=-6)


def test_parse_mp_datetime_handles_fractional_seconds():
    dt = mp.parse_mp_datetime('2026-09-23T18:31:02.873')
    assert dt.microsecond == 873000 and dt.tzinfo == mp.CHICAGO


def test_parse_mp_datetime_converts_aware_values_to_chicago():
    dt = mp.parse_mp_datetime('2026-09-23T23:31:02+00:00')
    assert dt.tzinfo == mp.CHICAGO
    assert (dt.day, dt.hour, dt.minute) == (23, 18, 31)


def test_parse_mp_datetime_accepts_datetime_objects():
    assert mp.parse_mp_datetime(datetime(2026, 9, 23, 18, 0)).tzinfo == mp.CHICAGO


@pytest.mark.parametrize('value', [None, ''])
def test_parse_mp_datetime_empty_is_none(value):
    assert mp.parse_mp_datetime(value) is None


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('env, expected', [
    ({}, False),
    ({'VERCEL_ENV': 'production'}, True),
    ({'VERCEL_ENV': 'preview'}, False),
    ({'VERCEL_ENV': 'development'}, False),
    ({'VERCEL_ENV': 'production', 'MP_WRITES_ENABLED': ''}, True),
    ({'VERCEL_ENV': 'production', 'MP_WRITES_ENABLED': '0'}, False),
    ({'VERCEL_ENV': 'production', 'MP_WRITES_ENABLED': 'false'}, False),
    ({'VERCEL_ENV': 'preview', 'MP_WRITES_ENABLED': 'true'}, True),
    ({'MP_WRITES_ENABLED': '1'}, True),
    ({'MP_WRITES_ENABLED': ' YES '}, True),
    ({'MP_WRITES_ENABLED': 'on'}, True),
    ({'MP_WRITES_ENABLED': 'off'}, False),
    ({'MP_WRITES_ENABLED': 'no'}, False),
])
def test_from_env_writes_enabled(env, expected):
    assert MPClient.from_env(env).writes_enabled is expected


def test_from_env_reads_base_url_and_credentials():
    client = MPClient.from_env({'MINISTRY_PLATFORM_BASE_URL': 'https://mp.example.test/api/',
                                'MP_CLIENT_ID': 'cid', 'MP_CLIENT_SECRET': 'sec'})
    assert client.base_url == 'https://mp.example.test/api'
    assert (client.client_id, client.client_secret) == ('cid', 'sec')


@pytest.mark.parametrize('env', [{}, {'MINISTRY_PLATFORM_BASE_URL': ''}])
def test_from_env_defaults_to_moody_host(env):
    client = MPClient.from_env(env)
    assert client.base_url == 'https://moody.ministryplatform.com/ministryplatformapi'
    assert (client.client_id, client.client_secret) == ('', '')
