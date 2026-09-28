"""
MinistryPlatform sign-in: the login gate, membership re-check, CSRF ordering, the OIDC callback,
id_token claim validation, logout, security headers and the access queries in models/access.py.

MP is always FakeMP (conftest). OIDC discovery is a static dict patched onto the registered
Authlib client, and id_tokens are real RS256 JWTs signed with a throwaway key, so nothing here
touches the network.
"""
import base64
import hashlib
import logging
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from joserfc import jwt
from joserfc.errors import (ExpiredTokenError, InvalidClaimError, JoseError, MissingClaimError)
from joserfc.jwk import KeySet, RSAKey

import routes.auth as auth
from models import access, mp
from routes.auth import MPCodeIDToken, MPCodeIDTokenNonceRequired, safe_next
from tests.conftest import TEST_USER, TODAY, FetchClient, login_session

MP_BASE = 'https://mp.example.test/ministryplatformapi'
ISS = f'{MP_BASE}/oauth'
BASE = 'http://localhost:5001'           # APP_BASE_URL in tests: the canonical host
CLIENT_ID = 'test-client'
GUID = TEST_USER['guid']
ENDSESSION = f'{MP_BASE}/oauth/connect/endsession'
META = {
    'issuer': ISS,
    'authorization_endpoint': f'{MP_BASE}/oauth/connect/authorize',
    'token_endpoint': f'{MP_BASE}/oauth/connect/token',
    'userinfo_endpoint': f'{MP_BASE}/oauth/connect/userinfo',
    'end_session_endpoint': ENDSESSION,
    'jwks_uri': f'{MP_BASE}/oauth/.well-known/openid-configuration/jwks',
    'id_token_signing_alg_values_supported': ['RS256'],
}
MP_USER_ROW = {'User_ID': 4242, 'Contact_ID': 99999, 'Nickname': 'Tess', 'First_Name': 'Theresa',
               'Last_Name': 'Leader'}
SESSION_KEYS_AFTER_LOGIN = {'user', 'auth_at', 'checked_at', 'aud', 'id_token'}
COOKIE_LIMIT = 4093                     # the smallest per-cookie limit among current browsers
LEADERS_FILTER_1000 = ('Group_Participants.Group_ID = 1000 '
                       'AND Group_Participants.Start_Date <= GETDATE() '
                       'AND (Group_Participants.End_Date IS NULL OR Group_Participants.End_Date >= GETDATE()) '
                       'AND Participant_ID_TABLE_Contact_ID_TABLE.User_Account IS NOT NULL')


# =============================================================================
# helpers and fixtures
# =============================================================================

@pytest.fixture(autouse=True)
def _fresh_access_cache():
    access.clear_cache()
    yield
    access.clear_cache()


@pytest.fixture(scope='module')
def rsa_key():
    return RSAKey.generate_key(2048, parameters={'kid': 'mp-test-kid'})


def _html(resp):
    return resp.get_data(as_text=True)


def _query(url):
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query, keep_blank_values=True).items()}


def _sess(client):
    with client.session_transaction() as s:
        return {k: v for k, v in s.items() if k != '_permanent'}


def _set_session(client, **values):
    """Write session keys. Writing `user` also binds the session to this environment (`aud`), as the
    callback does, so a hand-built user is judged on its own shape rather than rejected for lacking aud."""
    if 'user' in values:
        values.setdefault('aud', BASE)
    with client.session_transaction() as s:
        for k, v in values.items():
            s[k] = v


def _assert_login_redirect(resp, next_path, status=302):
    assert resp.status_code == status, resp.status_code
    loc = urlsplit(resp.headers['Location'])
    assert loc.path == '/login'
    assert _query(resp.headers['Location']) == {'next': next_path}


def _assert_json_error(resp, status, code):
    assert resp.status_code == status, (resp.status_code, resp.get_data(as_text=True)[:200])
    body = resp.get_json()
    assert body['ok'] is False and body['error'] == code, body
    assert resp.headers['Cache-Control'] == 'no-store'
    return body


def _mp_client(app):
    return app.extensions['tnt_oauth'].mp


def _patch_metadata(app, monkeypatch, **extra):
    meta = {**META, **extra}
    monkeypatch.setattr(_mp_client(app), 'load_server_metadata', lambda: meta)
    return meta


def _patch_claims(monkeypatch, claims=None, id_token='hdr.payload.sig', exc=None):
    calls = []

    def fake(client):
        calls.append(client)
        if exc is not None:
            raise exc
        return dict(claims), id_token
    monkeypatch.setattr(auth, '_fetch_verified_claims', fake)
    return calls


def _claims(**overrides):
    now = int(time.time())
    c = {'iss': ISS, 'aud': CLIENT_ID, 'sub': GUID, 'exp': now + 3600, 'iat': now}
    c.update(overrides)
    return {k: v for k, v in c.items() if v is not None}


def _opaque_token(n):
    """n chars of incompressible base64url (the worst case for the session cookie)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(n)).decode().rstrip('=')[:n]


def _start(client, next_path='/progress', **kw):
    return client.get('/login/start', query_string={'next': next_path}, base_url=BASE, **kw)


def _callback(client, **query):
    return client.get('/auth/callback', query_string=query or {'code': 'abc', 'state': 'xyz'}, base_url=BASE)


def _session_cookie(resp, name):
    for header in resp.headers.getlist('Set-Cookie'):
        if header.startswith(name + '='):
            return header.split(';', 1)[0]
    return None


def _csrf_meta(resp):
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', _html(resp))
    assert m, 'no csrf meta tag on the page'
    return m.group(1)


def _mp_calls(fake_mp, table):
    return [kw for t, kw in fake_mp.calls if t == table]


def _leader_calls(fake_mp):
    return [kw for kw in _mp_calls(fake_mp, 'Group_Participants') if kw.get('select') == access.LEADER_SELECT]


def _roster_row(cid, pid, gpid, first, last, notes, role=2):
    return {'Group_Participant_ID': gpid, 'Participant_ID': pid, 'Group_Role_ID': role, 'Notes': notes,
            'Start_Date': '2026-08-15T00:00:00', 'End_Date': None, 'Contact_ID': cid, 'Nickname': None,
            'First_Name': first, 'Last_Name': last}


@pytest.fixture
def leader(fake_mp):
    """MP says user 4242 is an active participant of the leaders group."""
    fake_mp.tables['Group_Participants'] = [{'User_ID': 4242}]
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW)]
    return fake_mp


@pytest.fixture
def not_a_leader(fake_mp):
    """MP lists other leaders but not user 4242 (an EMPTY leader set is an outage, not a denial)."""
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW)]
    return fake_mp


# =============================================================================
# the gate: anonymous requests
# =============================================================================

@pytest.mark.parametrize('path', ['/', '/attendance', '/progress', '/account', '/home/2026-09-23',
                                  '/attendance/2026-09-23/team/Blue?show=all', '/no/such/page'])
def test_anonymous_get_redirects_to_login_with_next(anon_client, path):
    _assert_login_redirect(anon_client.get(path), path)


@pytest.mark.parametrize('method,path', [
    ('post', '/submit_checkin'), ('post', '/edit_attendance'), ('post', '/submit_section'),
    ('post', '/edit_section'), ('post', '/edit_progress_section'), ('post', '/api/sync/roster'),
    ('post', '/no/such/page'), ('get', '/api/sync/roster'), ('get', '/api/nope'),
])
def test_anonymous_post_and_api_get_401_json(anon_client, method, path):
    body = _assert_json_error(getattr(anon_client, method)(path), 401, 'auth_required')
    assert body['login_url'] == '/login?next=/'
    assert body['message']


@pytest.mark.parametrize('headers', [{'X-Requested-With': 'fetch'}, {'Accept': 'application/json'}])
def test_anonymous_fetch_style_get_is_401_json(anon_client, headers):
    _assert_json_error(anon_client.get('/attendance', headers=headers), 401, 'auth_required')


REFERER_NEXT = [
    ('http://localhost/attendance/2026-09-23/team/Blue', '/attendance/2026-09-23/team/Blue'),
    ('http://localhost/attendance/2026-09-23/team/Blue?show=all', '/attendance/2026-09-23/team/Blue?show=all'),
    ('https://evil.example/attendance', '/'),               # cross-origin Referer is ignored
    ('http://localhost//evil.example', '/'),
    ('http://localhost/login?next=/x', '/'),
]


@pytest.mark.parametrize('referer,expected_next', REFERER_NEXT)
def test_401_login_url_uses_same_origin_referer_path(anon_client, referer, expected_next):
    body = _assert_json_error(anon_client.post('/submit_checkin', headers={'Referer': referer}),
                              401, 'auth_required')
    assert _query(body['login_url']) == {'next': expected_next}
    assert urlsplit(body['login_url']).path == '/login'


@pytest.mark.parametrize('referer,expected_next', REFERER_NEXT + [(None, '/')])
def test_anonymous_native_form_post_redirects_to_login_for_the_referring_page(anon_client, referer,
                                                                              expected_next):
    """A plain HTML form post comes back to the page it was on, never to the POST-only URL."""
    headers = {'X-Requested-With': '', **({'Referer': referer} if referer else {})}
    resp = anon_client.post('/submit_checkin', data={'contact_id': '1003'}, headers=headers)
    _assert_login_redirect(resp, expected_next, status=303)


@pytest.mark.parametrize('path', ['/healthz', '/login', '/logged-out', '/static/style.css',
                                  '/static/manifest.json', '/static/app.js', '/static/icon-192.png'])
def test_public_endpoints_are_reachable_anonymously(anon_client, path):
    resp = anon_client.get(path)
    assert resp.status_code == 200, (path, resp.status_code)


def test_healthz_is_json_and_uncached(anon_client):
    resp = anon_client.get('/healthz')
    assert resp.get_json()['ok'] is True and resp.get_json()['db'] is True
    assert resp.headers['Cache-Control'] == 'no-store'


def test_favicon_is_a_permanent_redirect_to_the_icon(anon_client):
    resp = anon_client.get('/favicon.ico')
    assert resp.status_code == 308
    assert resp.headers['Location'] == '/static/icon-192.png'


@pytest.mark.parametrize('path', ['/login', '/logged-out', '/healthz', '/static/app.js'])
def test_public_pages_do_not_start_a_session(anon_client, path):
    resp = anon_client.get(path)
    assert _session_cookie(resp, 'tnt_session') is None


def test_unknown_url_signed_in_is_404_page(client):
    resp = client.get('/no/such/page')
    assert resp.status_code == 404
    assert 'doesn’t exist' in _html(resp)


BAD_USERS = [
    None, 'Test Leader', ['x'], {}, 42,
    {**TEST_USER, 'user_id': '4242'}, {**TEST_USER, 'user_id': 0}, {**TEST_USER, 'user_id': -5},
    {**TEST_USER, 'user_id': 4242.0}, {**TEST_USER, 'user_id': None},
    {**TEST_USER, 'contact_id': None}, {**TEST_USER, 'contact_id': '99999'}, {**TEST_USER, 'contact_id': 0},
    {**TEST_USER, 'guid': 'not-a-guid'}, {**TEST_USER, 'guid': None}, {**TEST_USER, 'guid': 12345},
    {**TEST_USER, 'via': 'superuser'}, {**TEST_USER, 'via': None}, {**TEST_USER, 'via': 'Leader'},
    {**TEST_USER, 'name': None}, {**TEST_USER, 'name': 7},
    {k: v for k, v in TEST_USER.items() if k != 'name'},
    {k: v for k, v in TEST_USER.items() if k != 'user_id'},
]


@pytest.mark.parametrize('bad', BAD_USERS, ids=[f'bad{i}' for i in range(len(BAD_USERS))])
def test_malformed_session_user_is_anonymous(anon_client, fake_mp, bad):
    now = int(time.time())
    _set_session(anon_client, user=bad, auth_at=now, checked_at=now, id_token='tok')
    _assert_login_redirect(anon_client.get('/account'), '/account')
    assert 'user' not in _sess(anon_client)
    _set_session(anon_client, user=bad)
    _assert_json_error(anon_client.post('/submit_checkin'), 401, 'auth_required')
    assert fake_mp.calls == []


@pytest.mark.parametrize('field', ['user_id', 'contact_id'])
def test_bool_ids_in_session_user_are_anonymous(anon_client, field):
    login_session(anon_client, **{field: True})
    _assert_login_redirect(anon_client.get('/account'), '/account')


@pytest.mark.parametrize('aud', [None, 'https://tnt.example.org', 'http://localhost:5002'],
                         ids=['missing', 'production', 'other-port'])
def test_session_from_another_environment_is_anonymous(anon_client, fake_mp, aud):
    """A cookie minted for another deployment (same SECRET_KEY) is never accepted here."""
    login_session(anon_client)
    with anon_client.session_transaction() as s:
        if aud is None:
            del s['aud']
        else:
            s['aud'] = aud
    _assert_login_redirect(anon_client.get('/account'), '/account')
    assert _sess(anon_client) == {}
    login_session(anon_client)
    _set_session(anon_client, aud=aud or '')
    _assert_json_error(anon_client.post('/api/sync/roster'), 401, 'auth_required')
    assert fake_mp.calls == []


def test_malformed_user_sees_the_login_page_not_a_redirect(anon_client):
    _set_session(anon_client, user={**TEST_USER, 'via': 'root'})
    resp = anon_client.get('/login?next=/account')
    assert resp.status_code == 200
    assert 'Sign in with MinistryPlatform' in _html(resp)


def test_cookie_signed_with_another_secret_key_is_anonymous(app, anon_client):
    from tnt import create_app
    other = create_app({'TESTING': True, 'SECRET_KEY': 'another-' + 'k' * 40})
    forger = other.test_client()
    login_session(forger)
    name = app.config['SESSION_COOKIE_NAME']
    forged = forger.get_cookie(name).value
    anon_client.set_cookie(name, forged)
    _assert_login_redirect(anon_client.get('/account'), '/account')
    _assert_json_error(anon_client.post('/api/sync/roster'), 401, 'auth_required')


def test_cookie_signed_with_a_fallback_key_still_works(conn, fake_mp):
    """SECRET_KEY rotation: sessions signed with a key in SECRET_KEY_FALLBACKS stay valid."""
    from tnt import create_app
    old = create_app({'TESTING': True, 'SECRET_KEY': 'old-' + 'o' * 40})
    new = create_app({'TESTING': True, 'SECRET_KEY': 'new-' + 'n' * 40,
                      'SECRET_KEY_FALLBACKS': ['old-' + 'o' * 40]})
    signer = old.test_client()
    login_session(signer)
    name = new.config['SESSION_COOKIE_NAME']
    c = new.test_client()
    c.set_cookie(name, signer.get_cookie(name).value)
    assert c.get('/account').status_code == 200


def test_auth_not_before_newer_than_auth_at_signs_the_session_out(app, client, fake_mp):
    login_session(client, auth_ago=60)
    app.config['AUTH_NOT_BEFORE'] = int(time.time()) - 30
    _set_session(client, id_token='tok')
    _assert_login_redirect(client.get('/account'), '/account')
    assert _sess(client) == {}
    assert fake_mp.calls == []


def test_auth_not_before_rejects_posts_with_401(app, client):
    login_session(client, auth_ago=60)
    app.config['AUTH_NOT_BEFORE'] = int(time.time()) - 30
    _assert_json_error(client.post('/api/sync/roster'), 401, 'auth_required')


def test_auth_not_before_older_than_auth_at_is_fine(app, client):
    login_session(client, auth_ago=60)
    app.config['AUTH_NOT_BEFORE'] = int(time.time()) - 3600
    assert client.get('/account').status_code == 200


def test_login_with_a_killed_session_does_not_loop(app, client):
    login_session(client, auth_ago=60)
    app.config['AUTH_NOT_BEFORE'] = int(time.time()) - 30
    first = client.get('/login?next=/account')
    assert first.status_code == 302 and first.headers['Location'] == '/account'
    _assert_login_redirect(client.get('/account'), '/account')
    assert client.get('/login?next=/account').status_code == 200


def test_current_user_outside_a_signed_in_request_raises(app):
    with app.test_request_context('/'):
        with pytest.raises(RuntimeError):
            auth.current_user()


@pytest.mark.parametrize('name,initials', [('Test Leader', 'TL'), ('cher', 'C'), ('Ana María de la Cruz', 'AM'),
                                           ('', '?'), ('   ', '?')])
def test_current_user_initials(name, initials):
    u = auth.CurrentUser(guid=GUID, user_id=1, contact_id=2, name=name, via='leader')
    assert u.initials == initials
    assert u.role_label == 'T&T Leader'
    assert auth.CurrentUser(guid=GUID, user_id=1, contact_id=2, name=name, via='admin').role_label == 'Staff access'


# =============================================================================
# membership re-check (hourly; POSTs skip it for POST_TRUST_SECONDS after the last good check)
# =============================================================================

def test_recheck_not_due_makes_no_mp_call(client, fake_mp):
    login_session(client, checked_ago=3500)
    assert client.get('/account').status_code == 200
    assert fake_mp.calls == []


def test_recheck_due_and_allowed_updates_checked_at(client, leader):
    login_session(client, checked_ago=3700)
    before = int(time.time())
    resp = client.get('/account')
    assert resp.status_code == 200
    s = _sess(client)
    assert s['checked_at'] >= before
    assert s['user'] == TEST_USER
    assert len(_leader_calls(leader)) == 1
    assert _leader_calls(leader)[0]['timeout'] == mp.HOT_TIMEOUT    # a leader is waiting on this page load
    assert _mp_calls(leader, 'dp_User_User_Groups') == []      # leaders never need the admin query
    assert 'recheck_failed_at' not in s


def test_recheck_uses_the_ten_minute_access_cache(app, fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 4242}, {'User_ID': 5151}]
    a, b = app.test_client(), app.test_client()
    login_session(a, checked_ago=3700)
    login_session(b, checked_ago=3700, user_id=5151)
    assert a.get('/account').status_code == 200
    assert b.get('/account').status_code == 200
    assert len(_leader_calls(fake_mp)) == 1


def test_recheck_leader_to_admin_propagates_via(app, client, fake_mp):
    app.config['ADMIN_USER_GROUP_IDS'] = (29,)
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    fake_mp.tables['dp_User_User_Groups'] = [{'User_ID': 4242}]
    login_session(client, checked_ago=3700)
    resp = client.get('/account')
    assert resp.status_code == 200
    assert 'Staff access' in _html(resp)
    assert _sess(client)['user'] == {**TEST_USER, 'via': 'admin'}
    admin_call = _mp_calls(fake_mp, 'dp_User_User_Groups')
    assert len(admin_call) == 1 and admin_call[0]['filter'] == 'dp_User_User_Groups.User_Group_ID IN (29)'


def test_recheck_admin_to_leader_propagates_via(app, client, leader):
    app.config['ADMIN_USER_GROUP_IDS'] = (29,)
    login_session(client, checked_ago=3700, via='admin')
    resp = client.get('/account')
    assert 'T&amp;T Leader' in _html(resp)
    assert _sess(client)['user']['via'] == 'leader'
    assert _mp_calls(leader, 'dp_User_User_Groups') == []


def test_recheck_denied_get_shows_forbidden_and_keeps_only_the_logout_hint(client, not_a_leader):
    login_session(client, checked_ago=3700)
    _set_session(client, id_token='the-id-token', recheck_failed_at=int(time.time()) - 400)
    resp = client.get('/account')
    assert resp.status_code == 403
    assert 'T&amp;T Leaders list' in _html(resp)
    assert 'action="/logout"' in _html(resp)
    s = _sess(client)
    assert 'user' not in s
    assert s['logout_hint'] == 'the-id-token'
    assert set(s) <= {'logout_hint', 'csrf_token'}
    _assert_login_redirect(client.get('/account'), '/account')


def test_recheck_denied_fetch_get_is_403_json(client, not_a_leader):
    login_session(client, checked_ago=3700)
    body = _assert_json_error(client.get('/account', headers={'X-Requested-With': 'fetch'}), 403, 'not_authorized')
    assert body['message']
    assert 'user' not in _sess(client)


def test_recheck_denied_without_id_token_leaves_an_empty_session(client, not_a_leader):
    login_session(client, checked_ago=3700)
    assert client.get('/account').status_code == 403
    assert set(_sess(client)) <= {'csrf_token'}


def test_recheck_admin_removed_from_config_is_denied(app, client, not_a_leader):
    """An admin whose group was taken out of ADMIN_USER_GROUP_IDS loses access at the next re-check."""
    app.config['ADMIN_USER_GROUP_IDS'] = ()
    not_a_leader.tables['dp_User_User_Groups'] = [{'User_ID': 4242}]
    login_session(client, checked_ago=3700, via='admin')
    assert client.get('/account').status_code == 403
    assert _mp_calls(not_a_leader, 'dp_User_User_Groups') == []


def test_recheck_empty_leader_set_is_an_outage_not_a_denial(client, fake_mp):
    """MP returning no leaders at all is a permissions/config problem: nobody gets signed out over it."""
    fake_mp.tables['Group_Participants'] = []
    login_session(client, checked_ago=3700)
    _set_session(client, id_token='tok')
    assert client.get('/account').status_code == 200
    s = _sess(client)
    assert s['user'] == TEST_USER and s['id_token'] == 'tok' and 'recheck_failed_at' in s


def test_recheck_mp_unavailable_within_grace_keeps_working_and_throttles(client, fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get', table='Group_Participants')
    login_session(client, checked_ago=3 * 86400)
    checked_before = _sess(client)['checked_at']
    before = int(time.time())
    assert client.get('/account').status_code == 200
    s = _sess(client)
    assert s['recheck_failed_at'] >= before
    assert s['checked_at'] == checked_before              # grace runs from the last successful check
    assert len(fake_mp.calls) == 1
    assert client.get('/account').status_code == 200       # within 5 min: no MP call at all
    assert client.get('/attendance').status_code == 200
    assert len(fake_mp.calls) == 1


def test_recheck_retries_after_five_minutes_and_recovers(client, fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 4242}]
    login_session(client, checked_ago=2 * 86400)
    _set_session(client, recheck_failed_at=int(time.time()) - 301)
    assert client.get('/account').status_code == 200
    assert len(fake_mp.calls) == 1
    s = _sess(client)
    assert 'recheck_failed_at' not in s
    assert int(time.time()) - s['checked_at'] < 5


def test_recheck_throttled_still_retries_when_grace_is_over(app, client, fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    grace = app.config['AUTH_OUTAGE_GRACE_SECONDS']
    login_session(client, checked_ago=grace + 60)
    _set_session(client, recheck_failed_at=int(time.time()) - 10)
    assert client.get('/account').status_code == 503
    assert len(fake_mp.calls) == 1


def test_recheck_mp_unavailable_beyond_grace_is_503(app, client, fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    login_session(client, checked_ago=app.config['AUTH_OUTAGE_GRACE_SECONDS'] + 60)
    resp = client.get('/account')
    assert resp.status_code == 503
    assert 'MinistryPlatform unavailable' in _html(resp)
    assert _sess(client)['user'] == TEST_USER               # not signed out: MP is merely down
    _assert_json_error(client.get('/account', headers={'X-Requested-With': 'fetch'}), 503, 'mp_unavailable')


def test_recheck_beyond_grace_with_mp_up_just_rechecks(app, client, leader):
    login_session(client, checked_ago=app.config['AUTH_OUTAGE_GRACE_SECONDS'] + 60)
    assert client.get('/account').status_code == 200


API_CLIENT_ERRORS = [mp.MPForbidden('forbidden', op='get', status=403),
                     mp.MPAuthError('token_rejected', op='token', status=401)]


@pytest.mark.parametrize('error', API_CLIENT_ERRORS, ids=['forbidden', 'auth'])
def test_recheck_api_client_errors_get_the_outage_grace_and_log_an_error(client, fake_mp, caplog, error):
    """A broken API client (credentials, table permissions) must not sign every leader out mid-club-night,
    but it is our misconfiguration, so it is logged at ERROR rather than WARNING."""
    fake_mp.fail_with = error
    login_session(client, checked_ago=3700)
    before = int(time.time())
    with caplog.at_level(logging.WARNING, logger='routes.auth'):
        assert client.get('/account').status_code == 200
    s = _sess(client)
    assert s['user'] == TEST_USER and s['recheck_failed_at'] >= before
    logged = [r for r in caplog.records if 'auth.recheck_unavailable' in r.getMessage()]
    assert [r.levelno for r in logged] == [logging.ERROR]
    assert client.get('/account').status_code == 200        # throttled like an outage
    assert len(fake_mp.calls) == 1


@pytest.mark.parametrize('error', API_CLIENT_ERRORS, ids=['forbidden', 'auth'])
def test_recheck_api_client_errors_beyond_grace_are_503(app, client, fake_mp, error):
    fake_mp.fail_with = error
    login_session(client, checked_ago=app.config['AUTH_OUTAGE_GRACE_SECONDS'] + 60)
    assert client.get('/account').status_code == 503
    assert _sess(client)['user'] == TEST_USER


def test_recheck_other_mp_errors_are_503_even_within_grace(client, fake_mp):
    fake_mp.fail_with = mp.MPBadRequest('bad_request', op='get', status=400)     # a code bug, not an outage
    login_session(client, checked_ago=3700)
    assert client.get('/account').status_code == 503
    s = _sess(client)
    assert 'recheck_failed_at' not in s
    assert s['user'] == TEST_USER
    assert client.get('/account').status_code == 503        # no throttle: every page load retries
    assert len(fake_mp.calls) == 2


def test_post_within_the_post_trust_window_never_waits_on_mp(app, client, seeded, fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')   # would fail loudly if MP were called
    checked_ago = auth.POST_TRUST_SECONDS - 60
    assert checked_ago > app.config['AUTH_RECHECK_SECONDS']      # a GET now would re-check
    login_session(client, checked_ago=checked_ago)
    checked = _sess(client)['checked_at']
    resp = client.post('/api/sync/roster')
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert fake_mp.calls == []
    assert _sess(client)['checked_at'] == checked


def test_post_after_the_post_trust_window_rechecks_like_a_get(client, seeded, leader):
    login_session(client, checked_ago=auth.POST_TRUST_SECONDS + 60)
    before = int(time.time())
    resp = client.post('/api/sync/roster')
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    calls = _leader_calls(leader)
    assert len(calls) == 1 and calls[0]['timeout'] == mp.HOT_TIMEOUT
    assert _sess(client)['checked_at'] >= before


def test_post_after_the_post_trust_window_with_mp_down_uses_the_grace(client, seeded, fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    login_session(client, checked_ago=auth.POST_TRUST_SECONDS + 60)
    resp = client.post('/api/sync/roster')
    assert resp.status_code == 200 and resp.get_json()['ok'] is True
    assert len(fake_mp.calls) == 1 and 'recheck_failed_at' in _sess(client)


def test_post_after_the_post_trust_window_denied_is_403_and_signs_out(client, seeded, not_a_leader):
    login_session(client, checked_ago=auth.POST_TRUST_SECONDS + 60)
    _set_session(client, id_token='the-id-token')
    _assert_json_error(client.post('/api/sync/roster'), 403, 'not_authorized')
    s = _sess(client)
    assert 'user' not in s and s['logout_hint'] == 'the-id-token'


def test_post_beyond_grace_with_mp_down_is_503(app, client, seeded, fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    login_session(client, checked_ago=app.config['AUTH_OUTAGE_GRACE_SECONDS'] + 60)
    _assert_json_error(client.post('/api/sync/roster'), 503, 'mp_unavailable')
    assert len(fake_mp.calls) == 1
    assert _sess(client)['user'] == TEST_USER               # not signed out: MP is merely down


def test_head_request_triggers_the_recheck_like_get(client, leader):
    login_session(client, checked_ago=3700)
    assert client.head('/account').status_code == 200
    assert len(_leader_calls(leader)) == 1


# =============================================================================
# CSRF (checked by the gate, after the sign-in check)
#
# csrf_client is a plain FlaskClient: its POSTs look like native HTML form posts (the logout forms are);
# pass X-Requested-With: fetch, or post to /api/*, for the app's fetch() calls.
# =============================================================================

FETCH = {'X-Requested-With': 'fetch'}

@pytest.fixture
def csrf_app(conn, fake_mp):
    from tnt import create_app
    return create_app({'TESTING': True, 'WTF_CSRF_ENABLED': True, 'TNT_DATE_OVERRIDE': TODAY.isoformat()})


@pytest.fixture
def csrf_client(csrf_app):
    c = csrf_app.test_client()
    login_session(c)
    return c


def test_csrf_post_without_token_is_400_json(csrf_client, seeded):
    body = _assert_json_error(csrf_client.post('/api/sync/roster'), 400, 'csrf_failed')
    assert body['reload'] is True


def test_csrf_post_with_a_bad_token_is_400_json(csrf_client, seeded):
    _csrf_meta(csrf_client.get('/account'))
    resp = csrf_client.post('/api/sync/roster', headers={'X-CSRFToken': 'not-the-token'})
    _assert_json_error(resp, 400, 'csrf_failed')


def test_csrf_failure_on_a_fetch_form_post_is_json(csrf_client, seeded):
    _assert_json_error(csrf_client.post('/submit_checkin', data={'contact_id': '1003'}, headers=FETCH),
                       400, 'csrf_failed')


def test_csrf_failure_on_a_native_form_post_is_the_page_expired_page(csrf_client, seeded):
    resp = csrf_client.post('/submit_checkin', data={'contact_id': '1003'})
    assert resp.status_code == 400 and resp.mimetype == 'text/html'
    assert 'Page expired' in _html(resp)


HTTPS = 'https://localhost'


def test_csrf_missing_referrer_on_https_is_csrf_referrer_without_reload(csrf_client, seeded):
    """Reloading can't fix a browser that strips Referer, so the client isn't told to reload."""
    token = _csrf_meta(csrf_client.get('/account', base_url=HTTPS))
    resp = csrf_client.post('/api/sync/roster', base_url=HTTPS, headers={'X-CSRFToken': token})
    body = _assert_json_error(resp, 400, 'csrf_referrer')
    assert 'reload' not in body and body['message']
    resp = csrf_client.post('/api/sync/roster', base_url=HTTPS,
                            headers={'X-CSRFToken': token, 'Referer': 'https://evil.example/account'})
    _assert_json_error(resp, 400, 'csrf_referrer')
    resp = csrf_client.post('/api/sync/roster', base_url=HTTPS,
                            headers={'X-CSRFToken': token, 'Referer': f'{HTTPS}/account'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True


def test_csrf_token_as_form_field_reaches_the_handler(csrf_client, seeded):
    token = _csrf_meta(csrf_client.get('/account'))
    resp = csrf_client.post('/api/sync/roster', data={'csrf_token': token})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True


@pytest.mark.parametrize('header', ['X-CSRFToken', 'X-CSRF-Token'])
def test_csrf_token_as_header_reaches_the_handler(csrf_client, seeded, header):
    token = _csrf_meta(csrf_client.get('/account'))
    resp = csrf_client.post('/api/sync/roster', headers={header: token, 'X-Requested-With': 'fetch'})
    assert resp.status_code == 200 and resp.get_json()['ok'] is True


def test_csrf_token_from_another_session_is_rejected(csrf_app, csrf_client, seeded):
    other = csrf_app.test_client()
    login_session(other)
    token = _csrf_meta(other.get('/account'))
    _csrf_meta(csrf_client.get('/account'))
    _assert_json_error(csrf_client.post('/api/sync/roster', headers={'X-CSRFToken': token}), 400, 'csrf_failed')


def test_anonymous_post_without_token_is_401_not_400(csrf_app, seeded):
    c = csrf_app.test_client()
    _assert_json_error(c.post('/api/sync/roster'), 401, 'auth_required')
    _assert_json_error(c.post('/submit_checkin', data={'contact_id': '1003'}, headers=FETCH), 401, 'auth_required')
    _assert_login_redirect(c.post('/submit_checkin', data={'contact_id': '1003'}), '/', status=303)   # native


def test_expired_session_post_without_token_is_401_not_400(csrf_app, csrf_client, seeded):
    token = _csrf_meta(csrf_client.get('/account'))
    csrf_app.config['AUTH_NOT_BEFORE'] = int(time.time()) + 10
    _assert_json_error(csrf_client.post('/api/sync/roster'), 401, 'auth_required')
    _assert_json_error(csrf_client.post('/api/sync/roster', headers={'X-CSRFToken': token}), 401, 'auth_required')


def test_malformed_session_post_without_token_is_401_not_400(csrf_client, seeded):
    _set_session(csrf_client, user={**TEST_USER, 'via': 'root'})
    _assert_json_error(csrf_client.post('/api/sync/roster'), 401, 'auth_required')


def test_signed_in_logout_requires_the_csrf_token(csrf_client):
    token = _csrf_meta(csrf_client.get('/account'))
    _set_session(csrf_client, id_token='tok')
    forged = csrf_client.post('/logout')                    # the sign-out form is a native form post
    assert forged.status_code == 400 and 'Page expired' in _html(forged)
    _assert_json_error(csrf_client.post('/logout', headers=FETCH), 400, 'csrf_failed')
    assert _sess(csrf_client)['user'] == TEST_USER          # a forged logout does nothing
    resp = csrf_client.post('/logout', data={'csrf_token': token})
    assert resp.status_code == 303
    assert resp.headers['Location'].startswith(ENDSESSION + '?')
    assert _sess(csrf_client) == {}


def test_anonymous_logout_needs_no_token(csrf_app):
    resp = csrf_app.test_client().post('/logout')
    assert resp.status_code == 303 and resp.headers['Location'] == '/logged-out'


def test_denied_user_can_switch_accounts_with_the_forbidden_page_token(csrf_client, not_a_leader):
    login_session(csrf_client, checked_ago=3700)
    _set_session(csrf_client, id_token='denied-token')
    page = csrf_client.get('/account')
    assert page.status_code == 403
    m = re.search(r'name="csrf_token" value="([^"]+)"', _html(page))
    assert m, 'forbidden page has no csrf field for the logout form'
    assert 'name="switch" value="1"' in _html(page)
    assert csrf_client.post('/logout', data={'switch': '1'}).status_code == 400
    assert _sess(csrf_client)['logout_hint'] == 'denied-token'
    resp = csrf_client.post('/logout', data={'csrf_token': m.group(1), 'switch': '1'})
    assert resp.status_code == 303
    assert resp.headers['Location'].startswith(ENDSESSION + '?')
    assert _query(resp.headers['Location'])['id_token_hint'] == 'denied-token'


def test_login_page_has_no_csrf_meta(csrf_app):
    assert 'csrf-token' not in _html(csrf_app.test_client().get('/login'))


def test_stale_plain_html_logout_form_gets_an_html_error_page(csrf_client):
    token = _csrf_meta(csrf_client.get('/account'))
    _set_session(csrf_client, csrf_token='rotated-by-a-sign-in-in-another-tab')
    resp = csrf_client.post('/logout', data={'csrf_token': token},
                            headers={'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8'})
    assert resp.status_code == 400
    assert resp.mimetype == 'text/html'
    assert 'Page expired' in _html(resp)
    assert _sess(csrf_client)['user'] == TEST_USER


# =============================================================================
# /login and /login/start
# =============================================================================

def test_login_page_links_to_start_with_the_safe_next(anon_client):
    resp = anon_client.get('/login?next=/progress')
    assert resp.status_code == 200
    m = re.search(r'href="(/login/start[^"]*)"', _html(resp))
    assert m and _query(m.group(1).replace('&amp;', '&')) == {'next': '/progress'}
    assert resp.headers['Cache-Control'] == 'no-store'


def test_login_page_drops_an_unsafe_next(anon_client):
    m = re.search(r'href="(/login/start[^"]*)"', _html(anon_client.get('/login?next=//evil.example')))
    assert _query(m.group(1).replace('&amp;', '&')) == {'next': '/'}


@pytest.mark.parametrize('error,status,text', [('mp_unavailable', 503, 'isn’t responding'),
                                               ('state', 200, 'open the site in Safari'),
                                               ('bogus', 200, 'Sign-in didn’t work')])
def test_login_page_error_messages(anon_client, error, status, text):
    resp = anon_client.get(f'/login?error={error}')
    assert resp.status_code == status
    assert text in _html(resp)


@pytest.mark.parametrize('nxt,expected', [('/progress', '/progress'), ('/attendance?show=all', '/attendance?show=all'),
                                          ('//evil.example', '/'), ('/login', '/'), (None, '/')])
def test_login_when_already_signed_in_redirects_to_next(client, nxt, expected):
    resp = client.get('/login', query_string={'next': nxt} if nxt else None)
    assert resp.status_code == 302 and resp.headers['Location'] == expected


def test_login_start_redirects_to_the_authorize_endpoint(app, anon_client, monkeypatch):
    _patch_metadata(app, monkeypatch)
    resp = _start(anon_client, '/progress')
    assert resp.status_code == 302
    loc = resp.headers['Location']
    assert loc.startswith(META['authorization_endpoint'] + '?')
    q = _query(loc)
    assert q['response_type'] == 'code'
    assert q['client_id'] == CLIENT_ID
    assert q['redirect_uri'] == 'http://localhost:5001/auth/callback'
    assert q['scope'] == 'openid http://www.thinkministry.com/dataplatform/scopes/all'
    assert len(q['state']) >= 20 and len(q['nonce']) >= 16
    assert 'code_challenge' not in q and 'code_challenge_method' not in q
    assert 'response_mode' not in q                         # form_post would break SameSite=Lax
    s = _sess(anon_client)
    assert s['login_next'] == '/progress'
    state = s[f"_state_mp_{q['state']}"]['data']
    assert state['nonce'] == q['nonce'] and 'code_verifier' not in state
    assert resp.headers['Cache-Control'] == 'no-store'


def test_login_start_with_pkce_sends_an_s256_challenge(conn, fake_mp, monkeypatch):
    from tnt import create_app
    app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'MP_OIDC_PKCE': True})
    _patch_metadata(app, monkeypatch)
    c = app.test_client()
    q = _query(_start(c).headers['Location'])
    assert q['code_challenge_method'] == 'S256'
    verifier = _sess(c)[f"_state_mp_{q['state']}"]['data']['code_verifier']
    digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    assert q['code_challenge'] == digest


def test_login_start_on_a_non_canonical_host_bounces_to_app_base_url(app, anon_client, monkeypatch):
    _patch_metadata(app, monkeypatch)
    resp = anon_client.get('/login/start', query_string={'next': '/progress'},
                           base_url='https://tnt-git-branch.vercel.app')
    assert resp.status_code == 302
    loc = urlsplit(resp.headers['Location'])
    assert (loc.scheme, loc.netloc, loc.path) == ('http', 'localhost:5001', '/login/start')
    assert _query(resp.headers['Location']) == {'next': '/progress', 'canon': '1'}
    assert 'login_next' not in _sess(anon_client)


def test_login_start_bounce_keeps_only_a_safe_next(app, anon_client, monkeypatch):
    _patch_metadata(app, monkeypatch)
    resp = anon_client.get('/login/start', query_string={'next': '//evil.example'}, base_url='http://other.test')
    assert _query(resp.headers['Location']) == {'next': '/', 'canon': '1'}


def test_login_start_does_not_bounce_twice(app, anon_client, monkeypatch):
    """If the canonical host still doesn't match (proxy Host rewriting), carry on rather than loop."""
    _patch_metadata(app, monkeypatch)
    resp = anon_client.get('/login/start', query_string={'next': '/progress', 'canon': '1'},
                           base_url='https://tnt-git-branch.vercel.app')
    assert resp.status_code == 302
    assert resp.headers['Location'].startswith(META['authorization_endpoint'] + '?')
    assert _query(resp.headers['Location'])['redirect_uri'] == 'http://localhost:5001/auth/callback'


def test_login_start_host_match_ignores_case(app, anon_client, monkeypatch):
    _patch_metadata(app, monkeypatch)
    resp = anon_client.get('/login/start', base_url='http://LOCALHOST:5001')
    assert resp.headers['Location'].startswith(META['authorization_endpoint'] + '?')


def test_login_start_is_404_when_login_is_unavailable(app, anon_client, monkeypatch):
    app.config['LOGIN_AVAILABLE'] = False
    meta_calls = []
    monkeypatch.setattr(_mp_client(app), 'load_server_metadata', lambda: meta_calls.append(1) or META)
    resp = _start(anon_client)
    assert resp.status_code == 404
    assert 'isn’t available on this preview' in _html(resp)
    assert 'href="/login/start' not in _html(resp)
    assert meta_calls == []


def test_login_page_hides_the_button_when_login_is_unavailable(app, anon_client):
    app.config['LOGIN_AVAILABLE'] = False
    assert 'href="/login/start' not in _html(anon_client.get('/login'))


def test_login_page_explains_when_login_is_unavailable(app, anon_client):
    app.config['LOGIN_AVAILABLE'] = False
    assert 'isn’t available on this preview' in _html(anon_client.get('/login'))


def test_login_start_purges_stale_state_and_logout_hint(app, anon_client, monkeypatch):
    _patch_metadata(app, monkeypatch)
    _set_session(anon_client, _state_mp_old1={'data': {'nonce': 'a'}, 'exp': time.time() + 3600},
                 _state_mp_old2={'data': {'nonce': 'b'}, 'exp': time.time() + 3600}, logout_hint='old-hint')
    q = _query(_start(anon_client).headers['Location'])
    s = _sess(anon_client)
    assert [k for k in s if k.startswith('_state_mp_')] == [f"_state_mp_{q['state']}"]
    assert 'logout_hint' not in s


def test_double_tapped_sign_in_keeps_one_state(app, anon_client, monkeypatch):
    _patch_metadata(app, monkeypatch)
    _start(anon_client)
    _start(anon_client)
    _start(anon_client)
    assert len([k for k in _sess(anon_client) if k.startswith('_state_mp_')]) == 1


def test_login_start_with_discovery_down_goes_back_to_login(app, anon_client, monkeypatch):
    def down():
        raise requests.ConnectionError('discovery down')
    monkeypatch.setattr(_mp_client(app), 'load_server_metadata', down)
    resp = _start(anon_client)
    assert resp.status_code == 302
    assert resp.headers['Location'] == '/login?error=mp_unavailable'
    follow = anon_client.get(resp.headers['Location'])
    assert follow.status_code == 503 and 'isn’t responding' in _html(follow)


BAD_DISCOVERY = [{'issuer': 'https://evil.example/oauth'}, {'issuer': ISS + '/'}, {'issuer': None},
                 {'authorization_endpoint': None}, {'token_endpoint': ''}, {'jwks_uri': None}]


@pytest.mark.parametrize('bad', BAD_DISCOVERY, ids=['evil-issuer', 'issuer-slash', 'no-issuer', 'no-authorize',
                                                    'no-token', 'no-jwks'])
def test_login_start_rejects_a_bad_discovery_document(app, anon_client, monkeypatch, bad):
    """A maintenance page or wrong-tenant document must not be used, or kept as cached metadata."""
    client = _mp_client(app)
    _patch_metadata(app, monkeypatch, **bad)
    monkeypatch.setattr(client, 'server_metadata', {'issuer': 'cached-junk'})
    resp = _start(anon_client)
    assert resp.status_code == 302 and resp.headers['Location'] == '/login?error=mp_unavailable'
    assert client.server_metadata == {}
    assert not [k for k in _sess(anon_client) if k.startswith('_state_mp_')]


def test_callback_with_a_bad_discovery_document_fails_closed(app, anon_client, leader, monkeypatch):
    _patch_metadata(app, monkeypatch)
    q = _query(_start(anon_client).headers['Location'])
    _patch_metadata(app, monkeypatch, issuer='https://evil.example/oauth')    # MP's document changed mid-flow
    token_calls = []
    monkeypatch.setattr(_mp_client(app), 'fetch_access_token', lambda **kw: token_calls.append(kw) or {})
    resp = _callback(anon_client, code='c', state=q['state'])
    assert resp.status_code == 503 and 'isn’t responding' in _html(resp)
    assert token_calls == [] and leader.calls == []
    assert 'user' not in _sess(anon_client)


# =============================================================================
# /auth/callback (verification patched out)
# =============================================================================

def test_callback_signs_in_a_leader_and_returns_to_next(app, anon_client, leader, monkeypatch):
    _patch_metadata(app, monkeypatch)
    _set_session(anon_client, csrf_token='old-csrf', junk='x', recheck_failed_at=1, logout_hint='h',
                 user={**TEST_USER, 'user_id': 1})
    _start(anon_client, '/progress')
    _patch_claims(monkeypatch, _claims(sub=GUID.upper()), id_token='the.id.token')
    before = int(time.time())
    resp = _callback(anon_client)
    assert resp.status_code == 302 and resp.headers['Location'] == '/progress'
    s = _sess(anon_client)
    assert set(s) == SESSION_KEYS_AFTER_LOGIN                # login_next, state, csrf_token, junk all gone
    assert s['user'] == {'guid': GUID, 'user_id': 4242, 'contact_id': 99999, 'name': 'Tess Leader',
                         'via': 'leader'}
    assert s['auth_at'] == s['checked_at'] >= before
    assert s['aud'] == BASE                                  # bound to this environment
    assert s['id_token'] == 'the.id.token'
    with anon_client.session_transaction() as raw:
        assert raw.permanent
    lookup = _mp_calls(leader, 'dp_Users')
    assert lookup == [{'select': access.USER_SELECT, 'filter': f"dp_Users.User_GUID = '{GUID}'",
                       'timeout': mp.DEFAULT_TIMEOUT}]
    assert anon_client.get('/account').status_code == 200


def test_callback_without_login_start_goes_home(anon_client, leader, monkeypatch):
    _patch_claims(monkeypatch, _claims())
    resp = _callback(anon_client)
    assert resp.status_code == 302 and resp.headers['Location'] == '/'


def test_callback_refetches_membership_even_with_a_warm_cache(app, anon_client, leader, monkeypatch):
    leader.tables['Group_Participants'] = [{'User_ID': 1}]
    assert access.leader_user_ids(app.config['MP_LEADER_GROUP_ID'], max_age=600) == frozenset({1})  # cached
    leader.tables['Group_Participants'] = [{'User_ID': 1}, {'User_ID': 4242}]                        # just added in MP
    _patch_claims(monkeypatch, _claims())
    assert _callback(anon_client).status_code == 302
    assert _sess(anon_client)['user']['via'] == 'leader'


def test_callback_admin_via_admin_user_groups(app, anon_client, fake_mp, monkeypatch):
    app.config['ADMIN_USER_GROUP_IDS'] = (29, 31)
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW, Nickname=None)]
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    fake_mp.tables['dp_User_User_Groups'] = [{'User_ID': 4242}]
    _patch_claims(monkeypatch, _claims())
    assert _callback(anon_client).status_code == 302
    user = _sess(anon_client)['user']
    assert user['via'] == 'admin' and user['name'] == 'Theresa Leader'
    assert 'Staff access' in _html(anon_client.get('/account'))


def test_callback_not_a_leader_is_forbidden_with_a_logout_hint(anon_client, fake_mp, monkeypatch, caplog):
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW)]
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    _set_session(anon_client, junk='x', login_next='/progress')
    _patch_claims(monkeypatch, _claims(), id_token='denied.id.token')
    with caplog.at_level(logging.INFO, logger='routes.auth'):
        resp = _callback(anon_client)
    assert resp.status_code == 403
    assert 'T&amp;T Leaders list' in _html(resp)
    # what the leader reads out to the director: which MP login and contact they actually signed in as
    assert re.search(r'Signed in as <strong>Tess Leader</strong> \(MP user 4242,\s+contact 99999\)', _html(resp))
    assert any('auth.denied reason=not_member user_id=4242 contact_id=99999' in r.getMessage()
               for r in caplog.records)
    s = _sess(anon_client)
    assert 'user' not in s and 'junk' not in s and 'login_next' not in s
    assert s['logout_hint'] == 'denied.id.token'
    assert _mp_calls(fake_mp, 'dp_User_User_Groups') == []   # no admin groups configured -> no query
    _assert_login_redirect(anon_client.get('/account'), '/account')


def test_callback_not_a_leader_with_a_huge_id_token_stores_no_hint(anon_client, not_a_leader, monkeypatch):
    _patch_claims(monkeypatch, _claims(), id_token=_opaque_token(5000))
    assert _callback(anon_client).status_code == 403
    assert 'logout_hint' not in _sess(anon_client)


@pytest.mark.parametrize('row', [None, {'User_ID': 4242, 'Contact_ID': None}, {'User_ID': None, 'Contact_ID': 5}])
def test_callback_unknown_mp_user_is_forbidden(anon_client, fake_mp, monkeypatch, caplog, row):
    fake_mp.tables['dp_Users'] = [row] if row else []
    fake_mp.tables['Group_Participants'] = [{'User_ID': 4242}]
    _patch_claims(monkeypatch, _claims())
    with caplog.at_level(logging.INFO, logger='routes.auth'):
        resp = _callback(anon_client)
    assert resp.status_code == 403
    assert 'Signed in as' not in _html(resp)                  # no MP user to describe
    assert any('auth.denied reason=no_dp_user' in r.getMessage() for r in caplog.records)
    assert 'user' not in _sess(anon_client)
    assert _mp_calls(fake_mp, 'Group_Participants') == []


def test_callback_with_an_empty_leader_set_is_503_not_forbidden(anon_client, fake_mp, monkeypatch):
    """No leaders at all means MP permissions/config broke: say MP is unavailable, don't tell a leader they
    aren't one."""
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW)]
    fake_mp.tables['Group_Participants'] = []
    _patch_claims(monkeypatch, _claims(), id_token='the.id.token')
    resp = _callback(anon_client)
    assert resp.status_code == 503 and 'isn’t responding' in _html(resp)
    assert _sess(anon_client) == {}


@pytest.mark.parametrize('sub', ['not-a-guid', '', None, 12345, GUID + 'x', '{' + GUID + '}'])
def test_callback_invalid_sub_is_400(anon_client, leader, monkeypatch, sub):
    claims = _claims()
    if sub is None:
        claims.pop('sub')
    else:
        claims['sub'] = sub
    _patch_claims(monkeypatch, claims)
    resp = _callback(anon_client)
    assert resp.status_code == 400
    assert 'Sign-in didn’t work' in _html(resp)
    assert 'user' not in _sess(anon_client)
    assert leader.calls == []


def _raiser(error):
    def rows(**kw):
        raise error
    return rows


@pytest.mark.parametrize('table,error', [
    ('dp_Users', mp.MPUnavailable('timeout', op='get')),
    ('dp_Users', mp.MPForbidden('forbidden', op='get', status=403)),
    ('Group_Participants', mp.MPUnavailable('timeout', op='get')),
    ('dp_User_User_Groups', mp.MPAuthError('token_rejected', op='token')),
], ids=['lookup-down', 'lookup-forbidden', 'leaders-down', 'admins-auth'])
def test_callback_mp_error_fails_closed_with_503(app, client, fake_mp, monkeypatch, table, error):
    app.config['ADMIN_USER_GROUP_IDS'] = (29,)
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW)]
    fake_mp.tables['Group_Participants'] = []
    fake_mp.tables[table] = _raiser(error)
    _patch_claims(monkeypatch, _claims())
    resp = _callback(client)
    assert resp.status_code == 503
    assert 'isn’t responding' in _html(resp)
    assert _sess(client) == {}                                # fail closed: even the old session is gone


@pytest.mark.parametrize('exc,status,text', [
    (MismatchingStateError(), 400, 'open the site in Safari'),
    (OAuthError(error='access_denied'), 400, 'Sign-in was cancelled.'),
    (OAuthError(error='invalid_grant'), 400, 'Sign-in took too long'),
    (OAuthError(error='server_error'), 400, 'Sign-in didn’t work'),
    (InvalidClaimError('nonce'), 400, 'couldn’t verify'),
    (ExpiredTokenError('exp'), 400, 'couldn’t verify'),
    (JoseError('bad'), 400, 'couldn’t verify'),
    (auth.LoginFailure('no_id_token'), 400, 'Sign-in didn’t work'),
    (requests.ConnectionError('down'), 503, 'isn’t responding'),
    (requests.Timeout('slow'), 503, 'isn’t responding'),
], ids=['state', 'cancelled', 'invalid_grant', 'oauth_other', 'nonce', 'expired', 'jose', 'no_id_token',
        'connection', 'timeout'])
def test_callback_verification_failures(anon_client, leader, monkeypatch, exc, status, text):
    _patch_claims(monkeypatch, exc=exc)
    resp = _callback(anon_client)
    assert resp.status_code == status
    assert text in _html(resp)
    assert 'user' not in _sess(anon_client)
    assert leader.calls == []
    assert resp.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('login_next,expected', [(None, '/'), ('/progress', '/progress'), ('//evil.com', '/')])
def test_replayed_callback_when_already_signed_in_goes_to_next(client, leader, monkeypatch, login_next, expected):
    """Back button / double-loaded callback after a good sign-in: no scary state error, no MP calls."""
    if login_next:
        _set_session(client, login_next=login_next)
    _patch_claims(monkeypatch, exc=MismatchingStateError())
    resp = _callback(client)
    assert resp.status_code == 302 and resp.headers['Location'] == expected
    assert _sess(client)['user'] == TEST_USER
    assert leader.calls == []


def test_state_mismatch_with_a_malformed_session_user_is_still_400(anon_client, leader, monkeypatch):
    _set_session(anon_client, user={**TEST_USER, 'via': 'root'})
    _patch_claims(monkeypatch, exc=MismatchingStateError())
    assert _callback(anon_client).status_code == 400


NEXT_ATTACKS = ['//evil.com', 'https://evil.com', '/\\evil.com', '/.//evil.com', '/login?x', '/auth/callback',
                '/logout', '/logged-out', '\\\\evil.com', 'evil.com', 'javascript:alert(1)', '/\t/evil.com',
                '/%09/x\n', '/./x', '/a/../../evil', ' /x', '/' + 'a' * 600, 'http:/evil.com', '///evil.com']


@pytest.mark.parametrize('nxt', NEXT_ATTACKS, ids=[f'n{i}' for i in range(len(NEXT_ATTACKS))])
def test_callback_next_attacks_all_land_on_home(app, anon_client, leader, monkeypatch, nxt):
    _patch_metadata(app, monkeypatch)
    _start(anon_client, nxt)
    _patch_claims(monkeypatch, _claims())
    resp = _callback(anon_client)
    assert resp.status_code == 302 and resp.headers['Location'] == '/'


def test_callback_ignores_a_tampered_login_next(anon_client, leader, monkeypatch):
    _set_session(anon_client, login_next='//evil.com')
    _patch_claims(monkeypatch, _claims())
    assert _callback(anon_client).headers['Location'] == '/'


@pytest.mark.parametrize('value,expected', [
    ('/', '/'), ('/progress', '/progress'), ('/attendance/2026-09-23/team/Blue?show=all',
                                             '/attendance/2026-09-23/team/Blue?show=all'),
    ('/home/2026-09-23/team/Red%20Team', '/home/2026-09-23/team/Red%20Team'),
    ('/loginx', '/'), ('/login/start', '/'), ('/authx', '/authx'),
    (None, '/'), ('', '/'), (42, '/'), (['/x'], '/'),
] + [(v, '/') for v in NEXT_ATTACKS])
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_1300_char_id_token_keeps_the_cookie_under_the_browser_limit(app, anon_client, leader, monkeypatch):
    _patch_claims(monkeypatch, _claims(), id_token=_opaque_token(1300))
    resp = _callback(anon_client)
    cookie = _session_cookie(resp, app.config['SESSION_COOKIE_NAME'])
    assert cookie and len(cookie) < COOKIE_LIMIT
    page = anon_client.get('/account')                       # adds csrf_token
    assert len(_session_cookie(page, app.config['SESSION_COOKIE_NAME'])) < COOKIE_LIMIT
    assert len(_sess(anon_client)['id_token']) == 1300


def test_max_size_id_token_still_fits_with_every_other_session_key(app, anon_client, fake_mp, monkeypatch):
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW, Nickname='Maximiliana-Alexandrina',
                                       Last_Name='Montgomery-Wellington-Smythe')]
    fake_mp.tables['Group_Participants'] = [{'User_ID': 4242}]
    token = _opaque_token(auth.ID_TOKEN_MAX_CHARS)
    _patch_claims(monkeypatch, _claims(), id_token=token)
    _callback(anon_client)
    _set_session(anon_client, recheck_failed_at=int(time.time()))
    page = anon_client.get('/account')
    assert _sess(anon_client)['id_token'] == token
    assert len(_session_cookie(page, app.config['SESSION_COOKIE_NAME'])) < COOKIE_LIMIT


@pytest.mark.parametrize('length', [auth.ID_TOKEN_MAX_CHARS + 1, 5000])
def test_oversized_id_token_is_not_stored_but_sign_in_works(app, anon_client, leader, monkeypatch, length):
    _patch_claims(monkeypatch, _claims(), id_token=_opaque_token(length))
    resp = _callback(anon_client)
    assert resp.status_code == 302
    assert len(_session_cookie(resp, app.config['SESSION_COOKIE_NAME'])) < COOKIE_LIMIT
    s = _sess(anon_client)
    assert 'id_token' not in s and s['user']['user_id'] == 4242


# =============================================================================
# the real Authlib code path: state, nonce and RS256 id_token verification
# =============================================================================

def _run_real_flow(app, client, monkeypatch, key, make_claims, *, sign_with=None, token_extra=None,
                   callback_query=None):
    """/login/start then /auth/callback through Authlib, with only discovery and the token POST faked."""
    meta = _patch_metadata(app, monkeypatch, jwks=KeySet([key]).as_dict(private=False))
    q = _query(_start(client, '/progress').headers['Location'])
    captured = {}

    def fake_fetch_access_token(**kw):
        captured.update(kw)
        claims = make_claims(q)
        token = {'access_token': 'mp-access-token', 'token_type': 'Bearer', 'expires_in': 3600}
        if claims is not None:
            token['id_token'] = jwt.encode({'alg': 'RS256', 'kid': key.kid}, claims, sign_with or key)
        token.update(token_extra or {})
        return token
    monkeypatch.setattr(_mp_client(app), 'fetch_access_token', fake_fetch_access_token)
    query = callback_query(q) if callback_query else {'code': 'the-code', 'state': q['state']}
    return _callback(client, **query), captured, q, meta


def test_real_flow_with_echoed_nonce_signs_in(app, anon_client, leader, monkeypatch, rsa_key):
    resp, captured, q, _ = _run_real_flow(app, anon_client, monkeypatch, rsa_key,
                                          lambda q: _claims(nonce=q['nonce']))
    assert resp.status_code == 302 and resp.headers['Location'] == '/progress'
    assert captured['code'] == 'the-code'
    assert captured['redirect_uri'] == 'http://localhost:5001/auth/callback'
    assert 'code_verifier' not in captured
    s = _sess(anon_client)
    assert set(s) == SESSION_KEYS_AFTER_LOGIN
    assert s['user']['guid'] == GUID and s['id_token'].count('.') == 2
    assert 'mp-access-token' not in repr(s)                   # the all-data access token is never kept


def test_real_flow_without_nonce_in_the_id_token_signs_in(app, anon_client, leader, monkeypatch, rsa_key):
    resp, *_ = _run_real_flow(app, anon_client, monkeypatch, rsa_key, lambda q: _claims())
    assert resp.status_code == 302 and _sess(anon_client)['user']['user_id'] == 4242


@pytest.mark.parametrize('make_claims', [
    lambda q: _claims(nonce='not-' + q['nonce']),
    lambda q: _claims(nonce=q['nonce'], aud='some-other-client'),
    lambda q: _claims(nonce=q['nonce'], iss='https://evil.example/oauth'),
    lambda q: _claims(nonce=q['nonce'], exp=int(time.time()) - 600),
    lambda q: _claims(nonce=q['nonce'], sub=None),
    lambda q: _claims(nonce=q['nonce'], iat=None),
], ids=['wrong-nonce', 'wrong-aud', 'wrong-iss', 'expired', 'no-sub', 'no-iat'])
def test_real_flow_rejects_bad_id_tokens(app, anon_client, leader, monkeypatch, rsa_key, make_claims):
    resp, *_ = _run_real_flow(app, anon_client, monkeypatch, rsa_key, make_claims)
    assert resp.status_code == 400
    assert 'couldn’t verify' in _html(resp)
    assert 'user' not in _sess(anon_client)
    assert leader.calls == []


def test_real_flow_rejects_a_token_signed_by_another_key(app, anon_client, leader, monkeypatch, rsa_key):
    impostor = RSAKey.generate_key(2048, parameters={'kid': rsa_key.kid})
    resp, *_ = _run_real_flow(app, anon_client, monkeypatch, rsa_key, lambda q: _claims(nonce=q['nonce']),
                              sign_with=impostor)
    assert resp.status_code == 400 and 'user' not in _sess(anon_client)


def test_real_flow_rejects_a_wrong_state(app, anon_client, leader, monkeypatch, rsa_key):
    resp, captured, *_ = _run_real_flow(app, anon_client, monkeypatch, rsa_key, lambda q: _claims(),
                                        callback_query=lambda q: {'code': 'c', 'state': 'forged-state'})
    assert resp.status_code == 400 and 'open the site in Safari' in _html(resp)
    assert captured == {}                                     # never reached the token endpoint


def test_real_flow_state_is_single_use(app, anon_client, leader, monkeypatch, rsa_key):
    resp, captured, q, _ = _run_real_flow(app, anon_client, monkeypatch, rsa_key,
                                          lambda q: _claims(nonce=q['nonce']))
    assert resp.status_code == 302
    user = _sess(anon_client)['user']
    captured.clear()
    replay = _callback(anon_client, code='the-code', state=q['state'])
    assert replay.status_code == 302 and replay.headers['Location'] == '/'   # already signed in: just go on
    assert captured == {}                                     # the code is never exchanged twice
    assert _sess(anon_client)['user'] == user
    with anon_client.session_transaction() as s:
        del s['user']
    replay = _callback(anon_client, code='the-code', state=q['state'])
    assert replay.status_code == 400 and captured == {}


def test_real_flow_cancelled_at_mp(app, anon_client, leader, monkeypatch, rsa_key):
    resp, captured, *_ = _run_real_flow(
        app, anon_client, monkeypatch, rsa_key, lambda q: _claims(),
        callback_query=lambda q: {'error': 'access_denied', 'state': q['state']})
    assert resp.status_code == 400 and 'Sign-in was cancelled.' in _html(resp)
    assert captured == {}


def test_real_flow_without_an_id_token_is_rejected(app, anon_client, leader, monkeypatch, rsa_key):
    resp, *_ = _run_real_flow(app, anon_client, monkeypatch, rsa_key, lambda q: None)
    assert resp.status_code == 400 and 'Sign-in didn’t work' in _html(resp)
    assert 'user' not in _sess(anon_client)


def test_real_flow_with_pkce_sends_the_verifier(conn, leader, monkeypatch, rsa_key):
    from tnt import create_app
    app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'MP_OIDC_PKCE': True})
    c = app.test_client()
    resp, captured, q, _ = _run_real_flow(app, c, monkeypatch, rsa_key, lambda q: _claims(nonce=q['nonce']))
    assert resp.status_code == 302
    digest = base64.urlsafe_b64encode(hashlib.sha256(captured['code_verifier'].encode()).digest()).decode()
    assert digest.rstrip('=') == q['code_challenge']


# =============================================================================
# MPCodeIDToken claim validation (real RS256 tokens)
# =============================================================================

def _validate(key, claims, *, sent_nonce='sent-nonce', cls=MPCodeIDToken):
    token = jwt.encode({'alg': 'RS256', 'kid': key.kid}, claims, key)
    decoded = jwt.decode(token, key)
    options = {'iss': {'essential': True, 'value': ISS}, 'aud': {'essential': True, 'values': [CLIENT_ID]},
               'sub': {'essential': True}}
    cls(decoded.claims, decoded.header, options,
        {'nonce': sent_nonce, 'client_id': CLIENT_ID}).validate(leeway=120)


@pytest.mark.parametrize('claims', [
    lambda: _claims(),                                               # MP omits nonce
    lambda: _claims(nonce='sent-nonce'),
    lambda: _claims(aud=[CLIENT_ID]),
    lambda: _claims(aud=[CLIENT_ID, 'other-api'], azp=CLIENT_ID),
    lambda: _claims(exp=int(time.time()) - 60),                      # within the 120 s leeway
    lambda: _claims(iat=int(time.time()) + 60),
    lambda: _claims(sub=GUID.upper(), auth_time=int(time.time())),
], ids=['no-nonce', 'nonce', 'aud-list', 'aud-list-azp', 'exp-in-leeway', 'iat-in-leeway', 'extras'])
def test_id_token_claims_accepted(rsa_key, claims):
    _validate(rsa_key, claims())


@pytest.mark.parametrize('claims,sent,error', [
    (lambda: _claims(nonce='other-nonce'), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(nonce='sent-nonce'), None, InvalidClaimError),        # nonce present but none was sent
    (lambda: _claims(nonce='sent-nonce'), '', InvalidClaimError),
    (lambda: _claims(aud='other-client'), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(aud=['other-client']), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(aud=[CLIENT_ID, 'other-api']), 'sent-nonce', MissingClaimError),   # multi-aud needs azp
    (lambda: _claims(azp='other-client'), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(iss='https://evil.example/oauth'), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(iss=ISS + '/'), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(exp=int(time.time()) - 300), 'sent-nonce', ExpiredTokenError),
    (lambda: _claims(iat=int(time.time()) + 600), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(nbf=int(time.time()) + 600), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(sub=None), 'sent-nonce', MissingClaimError),
    (lambda: _claims(sub=''), 'sent-nonce', InvalidClaimError),
    (lambda: _claims(exp=None), 'sent-nonce', MissingClaimError),
    (lambda: _claims(aud=None), 'sent-nonce', MissingClaimError),
], ids=['wrong-nonce', 'nonce-not-sent', 'nonce-empty-sent', 'wrong-aud', 'wrong-aud-list', 'multi-aud-no-azp',
        'wrong-azp', 'wrong-iss', 'iss-trailing-slash', 'expired', 'iat-future', 'nbf-future', 'no-sub',
        'empty-sub', 'no-exp', 'no-aud'])
def test_id_token_claims_rejected(rsa_key, claims, sent, error):
    with pytest.raises(error):
        _validate(rsa_key, claims(), sent_nonce=sent)


def test_missing_nonce_rejected_when_required(rsa_key):
    with pytest.raises(MissingClaimError):
        _validate(rsa_key, _claims(), cls=MPCodeIDTokenNonceRequired)
    _validate(rsa_key, _claims(nonce='sent-nonce'), cls=MPCodeIDTokenNonceRequired)
    with pytest.raises(InvalidClaimError):
        _validate(rsa_key, _claims(nonce='other-nonce'), cls=MPCodeIDTokenNonceRequired)


def test_require_nonce_is_wired_from_config_per_app(conn, leader, monkeypatch, rsa_key):
    """The policy is read from each app's config at sign-in; creating another app never changes it."""
    from tnt import create_app
    strict = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'MP_OIDC_REQUIRE_NONCE': True})
    lax = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False})      # created last, on purpose
    assert MPCodeIDToken.REQUIRE_NONCE is False                          # the shared class is not mutated
    resp, *_ = _run_real_flow(strict, strict.test_client(), monkeypatch, rsa_key, lambda q: _claims())
    assert resp.status_code == 400 and 'couldn’t verify' in _html(resp)
    resp, *_ = _run_real_flow(strict, strict.test_client(), monkeypatch, rsa_key,
                              lambda q: _claims(nonce=q['nonce']))
    assert resp.status_code == 302
    resp, *_ = _run_real_flow(lax, lax.test_client(), monkeypatch, rsa_key, lambda q: _claims())
    assert resp.status_code == 302


# =============================================================================
# logout
# =============================================================================

def test_logout_sends_the_id_token_hint_to_mp_endsession(client):
    _set_session(client, id_token='the.id.token')
    resp = client.post('/logout')
    assert resp.status_code == 303
    loc = resp.headers['Location']
    assert loc.startswith(ENDSESSION + '?')
    assert _query(loc) == {'id_token_hint': 'the.id.token',
                           'post_logout_redirect_uri': 'http://localhost:5001/logged-out'}
    assert _sess(client) == {}
    cookie = [h for h in resp.headers.getlist('Set-Cookie') if h.startswith('tnt_session=')]
    assert cookie and ('Max-Age=0' in cookie[0] or '1970' in cookie[0])
    _assert_login_redirect(client.get('/account'), '/account')


def test_logout_without_an_id_token_signs_out_here(client):
    """MP ignores post_logout_redirect_uri without id_token_hint and strands the user on its page."""
    resp = client.post('/logout')
    assert resp.status_code == 303 and resp.headers['Location'] == '/logged-out'
    assert _sess(client) == {}
    _assert_login_redirect(client.get('/account'), '/account')


@pytest.mark.parametrize('signed_in', [True, False])
def test_switch_account_without_a_hint_still_ends_the_mp_session(anon_client, signed_in):
    """'Use a different account' must end the MP session, or MP silently signs the same login back in."""
    if signed_in:
        login_session(anon_client)
    resp = anon_client.post('/logout', data={'switch': '1'})
    assert resp.status_code == 303
    assert resp.headers['Location'].startswith(ENDSESSION + '?')
    assert _query(resp.headers['Location']) == {'post_logout_redirect_uri': 'http://localhost:5001/logged-out'}
    assert _sess(anon_client) == {}


def test_logout_with_nothing_to_end_clears_nothing(anon_client):
    """A drive-by POST /logout from another site mustn't wipe a sign-in that is in progress."""
    state = {'data': {'nonce': 'n'}, 'exp': time.time() + 3600}
    _set_session(anon_client, login_next='/progress', _state_mp_abc=state)
    resp = anon_client.post('/logout')
    assert resp.status_code == 303 and resp.headers['Location'] == '/logged-out'
    assert _sess(anon_client) == {'login_next': '/progress', '_state_mp_abc': state}


def test_logout_with_only_a_logout_hint_uses_it(anon_client):
    _set_session(anon_client, logout_hint='denied.id.token')
    resp = anon_client.post('/logout')
    assert resp.status_code == 303
    assert _query(resp.headers['Location'])['id_token_hint'] == 'denied.id.token'
    assert _sess(anon_client) == {}


def test_anonymous_logout_goes_to_logged_out(anon_client):
    resp = anon_client.post('/logout')
    assert resp.status_code == 303 and resp.headers['Location'] == '/logged-out'
    page = anon_client.get('/logged-out')
    assert page.status_code == 200 and 'signed out' in _html(page)


def test_logout_works_for_a_session_beyond_grace_while_mp_is_down(app, client, fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    login_session(client, checked_ago=app.config['AUTH_OUTAGE_GRACE_SECONDS'] + 60)
    resp = client.post('/logout')
    assert resp.status_code == 303 and fake_mp.calls == []


def test_get_logout_is_405(client):
    assert client.get('/logout').status_code == 405
    assert _sess(client)['user'] == TEST_USER


def test_anonymous_get_logout_goes_to_login_not_back_to_logout(anon_client):
    """The 405 comes after the gate, so an anonymous GET is just sent to sign in (with a safe next)."""
    _assert_login_redirect(anon_client.get('/logout'), '/')


# =============================================================================
# security headers
# =============================================================================

CSP = "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; form-action 'self' https://mp.example.test"


@pytest.mark.parametrize('who,path', [('anon', '/login'), ('anon', '/logged-out'), ('anon', '/healthz'),
                                      ('anon', '/static/style.css'), ('anon', '/account'),
                                      ('user', '/account'), ('user', '/attendance'), ('anon', '/favicon.ico')])
def test_security_headers_on_every_response(anon_client, client, who, path):
    resp = (client if who == 'user' else anon_client).get(path)
    assert resp.headers['X-Content-Type-Options'] == 'nosniff'
    assert resp.headers['X-Frame-Options'] == 'DENY'
    assert resp.headers['Referrer-Policy'] == 'strict-origin-when-cross-origin'
    assert resp.headers['Content-Security-Policy'] == CSP


@pytest.mark.parametrize('path', ['/login', '/login?next=/progress', '/logged-out'])
def test_auth_pages_are_no_store(anon_client, path):
    resp = anon_client.get(path)
    assert resp.headers['Cache-Control'] == 'no-store'
    assert 'Cookie' in resp.headers.get('Vary', '')


def test_auth_callback_and_logout_are_no_store(client, leader, monkeypatch):
    _patch_claims(monkeypatch, _claims())
    assert _callback(client).headers['Cache-Control'] == 'no-store'
    assert client.post('/logout').headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('path', ['/account', '/attendance', '/no/such/page'])
def test_signed_in_pages_are_private_and_vary_on_cookie(client, path):
    resp = client.get(path)
    assert resp.headers['Cache-Control'] == 'private, no-cache'
    assert 'Cookie' in resp.headers['Vary']


def test_gate_redirect_varies_on_cookie(anon_client):
    resp = anon_client.get('/attendance')
    assert 'Cookie' in resp.headers['Vary']
    assert resp.headers['Cache-Control'] == 'private, no-cache'


def test_json_errors_stay_no_store(anon_client):
    assert anon_client.post('/api/sync/roster').headers['Cache-Control'] == 'no-store'


def test_session_cookie_flags(client):
    resp = client.get('/account')
    header = [h for h in resp.headers.getlist('Set-Cookie') if h.startswith('tnt_session=')][0]
    assert 'HttpOnly' in header and 'SameSite=Lax' in header and 'Path=/' in header
    assert 'Domain=' not in header                            # __Host- prefix rules in production
    assert 'Expires=' in header                               # permanent (rolling 35-day) session


# =============================================================================
# account page and the JSON API
# =============================================================================

def test_account_page_shows_the_user_and_a_sign_out_form(client):
    resp = client.get('/account')
    body = _html(resp)
    assert resp.status_code == 200
    assert '<h1>Test Leader</h1>' in body
    assert 'T&amp;T Leader · signed in with MinistryPlatform' in body
    assert '>TL<' in body
    assert re.search(r'<form method="post" action="/logout">\s*<input type="hidden" name="csrf_token" value="[^"]+">',
                     body)
    assert '<meta name="csrf-token" content="' in body
    assert 'action="/api/sync/roster" data-json-form' in body


def test_account_page_escapes_the_name(client):
    login_session(client, name='<script>alert(1)</script>')
    body = _html(client.get('/account'))
    assert '<script>alert(1)</script>' not in body
    assert '&lt;script&gt;' in body


def test_account_avatar_in_the_header_of_app_pages(client):
    body = _html(client.get('/attendance'))
    assert 'href="/account"' in body and 'aria-label="Account: Test Leader"' in body


def test_api_sync_roster_refreshes_kids(client, seeded, fake_mp):
    seeded.execute("UPDATE sync_state SET attempted_at = now() - interval '1 minute'")
    rows = [_roster_row(r['contact_id'], r['participant_id'], r['group_participant_id'] or 9999 + r['contact_id'],
                        r['first_name'], r['last_name'], r['team'] or 'Created by CheckIn')
            for r in seeded.execute('SELECT * FROM kids WHERE active ORDER BY contact_id')]
    rows.append(_roster_row(1010, 5010, 9010, 'Ivy', 'Irwin', 'Red'))
    fake_mp.tables['Group_Participants'] = rows
    resp = client.post('/api/sync/roster')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'message': 'Kids refreshed from MinistryPlatform'}
    assert resp.headers['Cache-Control'] == 'no-store'
    assert seeded.execute('SELECT first_name FROM kids WHERE contact_id = 1010').fetchone()['first_name'] == 'Ivy'
    assert _leader_calls(fake_mp) == []


def test_api_sync_roster_just_synced_is_ok(client, seeded, fake_mp):
    resp = client.post('/api/sync/roster')
    assert resp.get_json() == {'ok': True, 'message': 'Kids were refreshed moments ago'}
    assert fake_mp.calls == []


def test_api_sync_roster_mp_down_is_503_json(client, seeded, fake_mp):
    seeded.execute("UPDATE sync_state SET attempted_at = now() - interval '1 minute'")
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get', table='Group_Participants')
    _assert_json_error(client.post('/api/sync/roster'), 503, 'mp_unavailable')


# =============================================================================
# models/access.py
# =============================================================================

def test_lookup_mp_user_query_and_result(fake_mp):
    fake_mp.tables['dp_Users'] = [dict(MP_USER_ROW)]
    user = access.lookup_mp_user('  ' + GUID.upper() + ' ')
    assert fake_mp.calls == [('dp_Users', {
        'select': 'dp_Users.User_ID,dp_Users.Contact_ID,Contact_ID_TABLE.Nickname AS Nickname,'
                  'Contact_ID_TABLE.First_Name AS First_Name,Contact_ID_TABLE.Last_Name AS Last_Name',
        'filter': "dp_Users.User_GUID = '11111111-2222-3333-4444-555555555555'",
        'timeout': mp.DEFAULT_TIMEOUT})]
    assert user == access.MPUser(user_id=4242, contact_id=99999, guid=GUID, nickname='Tess',
                                 first_name='Theresa', last_name='Leader')
    assert user.display_name == 'Tess Leader'


@pytest.mark.parametrize('row,name', [
    ({'Nickname': None, 'First_Name': 'Theresa', 'Last_Name': 'Leader'}, 'Theresa Leader'),
    ({'Nickname': '', 'First_Name': 'Theresa', 'Last_Name': None}, 'Theresa'),
    ({'Nickname': None, 'First_Name': None, 'Last_Name': None}, 'Leader'),
])
def test_display_name_fallbacks(row, name):
    assert access.MPUser(user_id=1, contact_id=2, guid=GUID, nickname=row['Nickname'],
                         first_name=row['First_Name'], last_name=row['Last_Name']).display_name == name


def test_lookup_rejects_an_invalid_guid_without_calling_mp(fake_mp):
    with pytest.raises(ValueError):
        access.lookup_mp_user("x' OR 1=1 --")
    assert fake_mp.calls == []


def test_leader_user_ids_query(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 4242}, {'User_ID': None}, {'User_ID': 7}, {}]
    assert access.leader_user_ids(1000, max_age=0) == frozenset({4242, 7})
    assert fake_mp.calls == [('Group_Participants', {
        'distinct': True,
        'select': 'Participant_ID_TABLE_Contact_ID_TABLE.User_Account AS User_ID',
        'filter': LEADERS_FILTER_1000,
        'orderby': 'Participant_ID_TABLE_Contact_ID_TABLE.User_Account',
        'timeout': mp.DEFAULT_TIMEOUT})]


def test_admin_user_ids_query(fake_mp):
    fake_mp.tables['dp_User_User_Groups'] = [{'User_ID': 9}, {'User_ID': 4242}]
    assert access.admin_user_ids((29, 31), max_age=0) == frozenset({9, 4242})
    assert fake_mp.calls == [('dp_User_User_Groups', {
        'distinct': True, 'select': 'dp_User_User_Groups.User_ID',
        'filter': 'dp_User_User_Groups.User_Group_ID IN (29,31)',
        'orderby': 'dp_User_User_Groups.User_ID', 'timeout': mp.DEFAULT_TIMEOUT})]


def test_admin_user_ids_empty_config_makes_no_call(fake_mp):
    assert access.admin_user_ids((), max_age=0) == frozenset()
    assert access.admin_user_ids([], max_age=0) == frozenset()
    assert fake_mp.calls == []


def test_check_access_leader_first_admin_only_for_non_leaders(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    fake_mp.tables['dp_User_User_Groups'] = [{'User_ID': 2}]
    assert access.check_access(1, leader_group_id=1000, admin_group_ids=(29,), max_age=600) == \
        access.AccessDecision(True, 'leader')
    assert [t for t, _ in fake_mp.calls] == ['Group_Participants']
    assert access.check_access(2, leader_group_id=1000, admin_group_ids=(29,), max_age=600) == \
        access.AccessDecision(True, 'admin')
    assert access.check_access(3, leader_group_id=1000, admin_group_ids=(29,), max_age=600) == \
        access.AccessDecision(False, None)
    assert [t for t, _ in fake_mp.calls] == ['Group_Participants', 'dp_User_User_Groups']   # both cached


def test_check_access_with_no_admin_groups_queries_leaders_only(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    fake_mp.tables['dp_User_User_Groups'] = [{'User_ID': 3}]
    assert access.check_access(3, leader_group_id=1000, admin_group_ids=(), max_age=0) == \
        access.AccessDecision(False, None)
    assert [t for t, _ in fake_mp.calls] == ['Group_Participants']


def test_access_cache_ttl_is_respected(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    access.leader_user_ids(1000, max_age=600)
    fake_mp.tables['Group_Participants'] = [{'User_ID': 2}]
    assert access.leader_user_ids(1000, max_age=600) == frozenset({1})     # still cached
    assert len(fake_mp.calls) == 1
    key = ('leaders', 1000)
    fetched, value = access._cache[key]
    access._cache[key] = (fetched - 601, value)                             # age it past the TTL
    assert access.leader_user_ids(1000, max_age=600) == frozenset({2})
    assert len(fake_mp.calls) == 2


def test_access_cache_is_per_group(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    access.leader_user_ids(1000, max_age=600)
    access.leader_user_ids(1001, max_age=600)
    access.admin_user_ids((29,), max_age=600)
    access.admin_user_ids((29, 31), max_age=600)
    assert len(fake_mp.calls) == 4


def test_max_age_zero_always_refetches(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    access.leader_user_ids(1000, max_age=600)
    access.leader_user_ids(1000, max_age=0)
    access.leader_user_ids(1000, max_age=0)
    assert len(fake_mp.calls) == 3


def test_mp_errors_propagate_and_stale_values_are_not_served(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    access.leader_user_ids(1000, max_age=600)
    key = ('leaders', 1000)
    fetched, value = access._cache[key]
    access._cache[key] = (fetched - 601, value)
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    with pytest.raises(mp.MPUnavailable):
        access.check_access(1, leader_group_id=1000, admin_group_ids=(), max_age=600)


def test_clear_cache_forces_a_refetch(fake_mp):
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    access.leader_user_ids(1000, max_age=600)
    access.clear_cache()
    access.leader_user_ids(1000, max_age=600)
    assert len(fake_mp.calls) == 2


def test_access_cache_ages_by_wall_clock(fake_mp, monkeypatch):
    """Serverless instances get paused (which freezes monotonic clocks), so the TTL uses time.time()."""
    now = [1_800_000_000.0]
    monkeypatch.setattr(access.time, 'time', lambda: now[0])
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    access.leader_user_ids(1000, max_age=600)
    now[0] += 599
    access.leader_user_ids(1000, max_age=600)
    assert len(fake_mp.calls) == 1
    now[0] += 2
    access.leader_user_ids(1000, max_age=600)
    assert len(fake_mp.calls) == 2


def test_empty_leader_set_is_an_outage_not_nobody(fake_mp, caplog):
    """An empty T&T Leaders result is almost certainly an MP permissions/config change: raise
    MPUnavailable (so sessions ride the outage grace) instead of denying every leader."""
    fake_mp.tables['Group_Participants'] = []
    with caplog.at_level(logging.ERROR, logger='models.access'):
        with pytest.raises(mp.MPUnavailable) as e:
            access.check_access(4242, leader_group_id=1000, admin_group_ids=(29,), max_age=600)
    assert e.value.kind == 'empty_leader_set'
    assert ('leaders', 1000) not in access._cache              # never remembered as "nobody leads T&T"
    assert [t for t, _ in fake_mp.calls] == ['Group_Participants']
    assert any('auth.leader_set_empty group_id=1000' in r.getMessage() for r in caplog.records)
    fake_mp.tables['Group_Participants'] = [{'User_ID': 4242}]
    assert access.check_access(4242, leader_group_id=1000, admin_group_ids=(29,), max_age=0) == \
        access.AccessDecision(True, 'leader')


def test_mp_outage_is_remembered_for_a_minute(fake_mp):
    """While MP is down, later requests fail fast instead of each waiting on MP's timeout."""
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    for _ in range(3):
        with pytest.raises(mp.MPUnavailable):
            access.leader_user_ids(1000, max_age=600)
    assert len(fake_mp.calls) == 1
    fake_mp.fail_with = None
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    key = ('failed', 'leaders', 1000)
    failed_at, error = access._cache[key]
    access._cache[key] = (failed_at - access.FAILURE_CACHE_SECONDS - 1, error)
    assert access.leader_user_ids(1000, max_age=600) == frozenset({1})
    assert len(fake_mp.calls) == 2
    assert key not in access._cache                             # success clears the remembered failure


def test_remembered_outage_is_per_key_and_reset_by_clear_cache(fake_mp):
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    with pytest.raises(mp.MPUnavailable):
        access.leader_user_ids(1000, max_age=600)
    with pytest.raises(mp.MPUnavailable):
        access.leader_user_ids(1001, max_age=600)             # another key still asks MP
    assert len(fake_mp.calls) == 2
    access.clear_cache()
    fake_mp.fail_with = None
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    assert access.leader_user_ids(1000, max_age=600) == frozenset({1})
    assert len(fake_mp.calls) == 3


def test_sign_in_lookups_skip_the_remembered_outage(fake_mp):
    """max_age=0 (the callback) always asks MP, so sign-in works as soon as MP is back."""
    fake_mp.fail_with = mp.MPUnavailable('timeout', op='get')
    with pytest.raises(mp.MPUnavailable):
        access.leader_user_ids(1000, max_age=600)
    fake_mp.fail_with = None
    fake_mp.tables['Group_Participants'] = [{'User_ID': 1}]
    assert access.leader_user_ids(1000, max_age=0) == frozenset({1})


@pytest.mark.parametrize('error', API_CLIENT_ERRORS + [mp.MPBadRequest('bad_request', op='get', status=400)],
                         ids=['forbidden', 'auth', 'bad_request'])
def test_non_outage_mp_errors_are_not_remembered(fake_mp, error):
    fake_mp.fail_with = error
    for _ in range(2):
        with pytest.raises(type(error)):
            access.leader_user_ids(1000, max_age=600)
    assert len(fake_mp.calls) == 2
