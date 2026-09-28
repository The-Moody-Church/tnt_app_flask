"""config.load_config(env): fail-fast rules, deployed-context defaults, cookies, and env parsing."""
from datetime import timedelta

import pytest

from config import DEFAULT_MP_BASE_URL, load_config
from models.mp import MPClient

PROD_SECRET = 'p' * 32


def prod_env(**extra):
    env = {'VERCEL_ENV': 'production', 'SECRET_KEY': PROD_SECRET, 'APP_BASE_URL': 'https://tnt.example.org',
           'MP_CLIENT_ID': 'cid', 'MP_CLIENT_SECRET': 'csecret'}
    env.update(extra)
    return {k: v for k, v in env.items() if v is not None}


# ---------------------------------------------------------------------------
# Production fail-fast
# ---------------------------------------------------------------------------

def test_production_with_full_env():
    cfg = load_config(prod_env())
    assert cfg['VERCEL_ENV'] == 'production'
    assert cfg['PRODUCTION'] is True and cfg['DEPLOYED'] is True
    assert cfg['SECRET_KEY'] == PROD_SECRET
    assert cfg['APP_BASE_URL'] == 'https://tnt.example.org'
    assert (cfg['MP_CLIENT_ID'], cfg['MP_CLIENT_SECRET']) == ('cid', 'csecret')
    assert cfg['LOGIN_AVAILABLE'] is True


@pytest.mark.parametrize('secret', [None, '', 'p' * 31])
def test_production_requires_32_char_secret_key(secret):
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        load_config(prod_env(SECRET_KEY=secret))


@pytest.mark.parametrize('secret', ['short', 'p' * 31])
def test_preview_rejects_a_short_secret_key(secret):
    """A guessable key on a preview could forge sessions; set a real one or none (login then stays off)."""
    with pytest.raises(RuntimeError, match='SECRET_KEY'):
        load_config({'VERCEL_ENV': 'preview', 'SECRET_KEY': secret})


def test_production_accepts_exactly_32_char_secret_key():
    assert load_config(prod_env(SECRET_KEY='q' * 32))['SECRET_KEY'] == 'q' * 32


@pytest.mark.parametrize('base_url', [None, '', '/'])
def test_production_requires_app_base_url(base_url):
    with pytest.raises(RuntimeError, match='APP_BASE_URL'):
        load_config(prod_env(APP_BASE_URL=base_url))


@pytest.mark.parametrize('missing', [{'MP_CLIENT_ID': None}, {'MP_CLIENT_SECRET': None},
                                     {'MP_CLIENT_ID': '', 'MP_CLIENT_SECRET': ''}])
def test_production_requires_mp_credentials(missing):
    with pytest.raises(RuntimeError, match='MP_CLIENT_ID'):
        load_config(prod_env(**missing))


# ---------------------------------------------------------------------------
# Previews and local dev
# ---------------------------------------------------------------------------

def test_preview_without_mp_credentials_does_not_fail():
    cfg = load_config({'VERCEL_ENV': 'preview'})
    assert cfg['PRODUCTION'] is False and cfg['DEPLOYED'] is True
    assert cfg['LOGIN_AVAILABLE'] is False
    assert cfg['APP_BASE_URL'] == ''  # no localhost default once deployed
    assert len(cfg['SECRET_KEY']) >= 32


def test_preview_login_needs_base_url_and_both_credentials():
    full = {'VERCEL_ENV': 'preview', 'APP_BASE_URL': 'https://tnt-git-x.vercel.app', 'SECRET_KEY': PROD_SECRET,
            'MP_CLIENT_ID': 'cid', 'MP_CLIENT_SECRET': 'csecret'}
    assert load_config(full)['LOGIN_AVAILABLE'] is True
    for key in ('APP_BASE_URL', 'MP_CLIENT_ID', 'MP_CLIENT_SECRET'):
        env = {k: v for k, v in full.items() if k != key}
        assert load_config(env)['LOGIN_AVAILABLE'] is False, key


def test_local_dev_defaults():
    cfg = load_config({})
    assert cfg['VERCEL_ENV'] is None
    assert cfg['PRODUCTION'] is False and cfg['DEPLOYED'] is False
    assert cfg['APP_BASE_URL'] == 'http://localhost:5001'
    assert len(cfg['SECRET_KEY']) >= 32
    assert cfg['LOGIN_AVAILABLE'] is False


def test_generated_secret_differs_per_load():
    assert load_config({})['SECRET_KEY'] != load_config({})['SECRET_KEY']


def test_explicit_secret_key_used_outside_production():
    assert load_config({'SECRET_KEY': 'short'})['SECRET_KEY'] == 'short'


def test_local_dev_with_mp_credentials_can_log_in():
    cfg = load_config({'MP_CLIENT_ID': 'cid', 'MP_CLIENT_SECRET': 'csecret'})
    assert cfg['LOGIN_AVAILABLE'] is True


@pytest.mark.parametrize('vercel_env', ['', 'development'])
def test_blank_or_development_vercel_env_is_local(vercel_env):
    cfg = load_config({'VERCEL_ENV': vercel_env})
    assert cfg['DEPLOYED'] is False and cfg['PRODUCTION'] is False
    assert cfg['APP_BASE_URL'] == 'http://localhost:5001'
    assert cfg['SESSION_COOKIE_NAME'] == 'tnt_session'


def test_app_base_url_trailing_slash_stripped():
    assert load_config({'APP_BASE_URL': 'http://localhost:5001/'})['APP_BASE_URL'] == 'http://localhost:5001'
    assert load_config(prod_env(APP_BASE_URL='https://tnt.example.org/'))['APP_BASE_URL'] == 'https://tnt.example.org'


@pytest.mark.parametrize('value, expected', [
    ('https://TNT.Example.ORG', 'https://tnt.example.org'),
    ('https://tnt.example.org:443/', 'https://tnt.example.org'),
    ('http://localhost:80', 'http://localhost'),
    ('http://localhost:5001', 'http://localhost:5001'),
    ('https://tnt.example.org:8443', 'https://tnt.example.org:8443'),
    ('  https://tnt.example.org/  ', 'https://tnt.example.org'),
])
def test_app_base_url_is_normalized_to_an_origin(value, expected):
    """It is compared with the request host (login bounce) and stored as the session's aud."""
    assert load_config({'APP_BASE_URL': value})['APP_BASE_URL'] == expected


@pytest.mark.parametrize('value', ['https://tnt.example.org/app', 'https://tnt.example.org/?x=1',
                                   'https://tnt.example.org/#top', 'tnt.example.org', 'ftp://tnt.example.org',
                                   'https://'])
def test_app_base_url_with_a_path_or_no_origin_is_rejected(value):
    with pytest.raises(ValueError, match='APP_BASE_URL'):
        load_config({'APP_BASE_URL': value})


def test_load_config_reads_os_environ_by_default(monkeypatch):
    monkeypatch.setenv('ADMIN_USER_GROUP_IDS', '7')
    monkeypatch.delenv('VERCEL_ENV', raising=False)
    assert load_config()['ADMIN_USER_GROUP_IDS'] == (7,)


def test_load_config_does_not_mutate_env():
    env = {'VERCEL_ENV': 'preview'}
    load_config(env)
    assert env == {'VERCEL_ENV': 'preview'}


# ---------------------------------------------------------------------------
# Sessions and cookies
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('env, name, secure', [
    ({}, 'tnt_session', False),
    ({'APP_BASE_URL': 'http://localhost:5001'}, 'tnt_session', False),
    ({'APP_BASE_URL': 'https://tnt.example.org'}, '__Host-tnt', True),
    ({'VERCEL_ENV': 'preview'}, '__Host-tnt', True),
    ({'VERCEL_ENV': 'preview', 'APP_BASE_URL': 'http://tnt.example.org'}, '__Host-tnt', True),
    (prod_env(), '__Host-tnt', True),
])
def test_session_cookie_name_and_secure(env, name, secure):
    cfg = load_config(env)
    assert cfg['SESSION_COOKIE_NAME'] == name
    assert cfg['SESSION_COOKIE_SECURE'] is secure


@pytest.mark.parametrize('env', [{}, prod_env()])
def test_session_cookie_policy(env):
    cfg = load_config(env)
    assert cfg['SESSION_COOKIE_SAMESITE'] == 'Lax'
    assert cfg['SESSION_COOKIE_HTTPONLY'] is True
    assert cfg['PERMANENT_SESSION_LIFETIME'] == timedelta(days=35)
    assert cfg['SESSION_REFRESH_EACH_REQUEST'] is False    # the hourly re-check rewrites the cookie (rolling)


def test_session_lifetime_override():
    assert load_config({'SESSION_LIFETIME_DAYS': '7'})['PERMANENT_SESSION_LIFETIME'] == timedelta(days=7)
    assert load_config({'SESSION_LIFETIME_DAYS': ''})['PERMANENT_SESSION_LIFETIME'] == timedelta(days=35)


def test_auth_timing_defaults_and_overrides():
    cfg = load_config({})
    assert cfg['AUTH_RECHECK_SECONDS'] == 3600
    assert cfg['AUTH_OUTAGE_GRACE_SECONDS'] == 35 * 86400        # as long as the session itself lives
    assert cfg['AUTH_NOT_BEFORE'] == 0
    cfg = load_config({'AUTH_RECHECK_MINUTES': '15', 'AUTH_OUTAGE_GRACE_DAYS': '2', 'AUTH_NOT_BEFORE': '1790000000'})
    assert cfg['AUTH_RECHECK_SECONDS'] == 900
    assert cfg['AUTH_OUTAGE_GRACE_SECONDS'] == 2 * 86400
    assert cfg['AUTH_NOT_BEFORE'] == 1790000000


def test_secret_key_fallbacks():
    assert load_config({})['SECRET_KEY_FALLBACKS'] == []
    assert load_config({'SECRET_KEY_FALLBACKS': 'old1,,old2'})['SECRET_KEY_FALLBACKS'] == ['old1', 'old2']


def test_csrf_settings():
    cfg = load_config({})
    assert cfg['WTF_CSRF_ENABLED'] is True
    assert cfg['WTF_CSRF_CHECK_DEFAULT'] is False  # the auth gate calls csrf.protect() after the login check
    assert cfg['WTF_CSRF_TIME_LIMIT'] is None
    assert cfg['WTF_CSRF_SSL_STRICT'] is True


def test_max_content_length():
    assert load_config({})['MAX_CONTENT_LENGTH'] == 256 * 1024


# ---------------------------------------------------------------------------
# MinistryPlatform settings
# ---------------------------------------------------------------------------

WRITES_MATRIX = [
    ({}, False),
    ({'VERCEL_ENV': 'preview'}, False),
    ({'VERCEL_ENV': 'development'}, False),
    (prod_env(), True),
    (prod_env(MP_WRITES_ENABLED=''), True),
    (prod_env(MP_WRITES_ENABLED='0'), False),
    (prod_env(MP_WRITES_ENABLED='false'), False),
    ({'VERCEL_ENV': 'preview', 'MP_WRITES_ENABLED': 'true'}, True),
    ({'MP_WRITES_ENABLED': '1'}, True),
    ({'MP_WRITES_ENABLED': 'On'}, True),
    ({'MP_WRITES_ENABLED': 'nope'}, False),
]


@pytest.mark.parametrize('env, expected', WRITES_MATRIX)
def test_mp_writes_enabled_default_and_override(env, expected):
    assert load_config(env)['MP_WRITES_ENABLED'] is expected


@pytest.mark.parametrize('env, expected', WRITES_MATRIX)
def test_mp_writes_enabled_agrees_with_mp_client(env, expected):
    """config (drives the Assign UI) and MPClient.from_env (gates the PUT) read the flag the same way."""
    assert MPClient.from_env(env).writes_enabled is load_config(env)['MP_WRITES_ENABLED']


@pytest.mark.parametrize('value, expected', [
    ('29, 31', (29, 31)), ('29', (29,)), (' 29 ,, 31 , ', (29, 31)), ('', ()), ('   ', ()), (None, ()),
])
def test_admin_user_group_ids_parsing(value, expected):
    env = {} if value is None else {'ADMIN_USER_GROUP_IDS': value}
    assert load_config(env)['ADMIN_USER_GROUP_IDS'] == expected


@pytest.mark.parametrize('value', ['29,x', '0', '-5', '29;31', '29 31', '1.5'])
def test_admin_user_group_ids_rejects_non_positive_ints(value):
    with pytest.raises(ValueError):
        load_config({'ADMIN_USER_GROUP_IDS': value})


def test_mp_base_url_default_and_origin():
    cfg = load_config({})
    assert cfg['MP_BASE_URL'] == DEFAULT_MP_BASE_URL == 'https://moody.ministryplatform.com/ministryplatformapi'
    assert cfg['MP_ORIGIN'] == 'https://moody.ministryplatform.com'


def test_mp_base_url_custom_and_origin_keeps_port():
    cfg = load_config({'MINISTRY_PLATFORM_BASE_URL': 'https://mp.example.test:8443/ministryplatformapi/'})
    assert cfg['MP_BASE_URL'] == 'https://mp.example.test:8443/ministryplatformapi'
    assert cfg['MP_ORIGIN'] == 'https://mp.example.test:8443'


def test_mp_defaults():
    cfg = load_config({})
    assert cfg['MP_LEADER_GROUP_ID'] == 1000
    assert cfg['MP_ROSTER_GROUP_ID'] == 697
    assert cfg['MP_OIDC_SCOPES'] == 'openid http://www.thinkministry.com/dataplatform/scopes/all'
    assert cfg['MP_OIDC_PKCE'] is False
    assert cfg['MP_OIDC_REQUIRE_NONCE'] is False
    assert cfg['HELP_CONTACT'] == 'the Awana director'
    assert cfg['ASSET_VERSION'] == 'dev'


def test_mp_overrides():
    cfg = load_config({'MP_LEADER_GROUP_ID': '12', 'MP_ROSTER_GROUP_ID': ' 34 ', 'MP_OIDC_SCOPES': 'openid',
                       'MP_OIDC_PKCE': '1', 'MP_OIDC_REQUIRE_NONCE': 'true', 'HELP_CONTACT': 'Pat',
                       'VERCEL_GIT_COMMIT_SHA': '0123456789abcdef'})
    assert (cfg['MP_LEADER_GROUP_ID'], cfg['MP_ROSTER_GROUP_ID']) == (12, 34)
    assert cfg['MP_OIDC_SCOPES'] == 'openid'
    assert cfg['MP_OIDC_PKCE'] is True and cfg['MP_OIDC_REQUIRE_NONCE'] is True
    assert cfg['HELP_CONTACT'] == 'Pat'
    assert cfg['ASSET_VERSION'] == '0123456'


def test_bad_group_id_is_an_error():
    with pytest.raises(ValueError):
        load_config({'MP_LEADER_GROUP_ID': 'abc'})


# ---------------------------------------------------------------------------
# Date override
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('env', [{}, {'VERCEL_ENV': 'preview'}])
def test_date_override_allowed_outside_production(env):
    cfg = load_config({**env, 'TNT_DATE_OVERRIDE': '2026-09-23'})
    assert cfg['TNT_DATE_OVERRIDE'] == '2026-09-23'
    assert cfg['ALLOW_DATE_OVERRIDE'] is True


def test_blank_date_override_is_none():
    assert load_config({'TNT_DATE_OVERRIDE': ''})['TNT_DATE_OVERRIDE'] is None
    assert load_config({})['TNT_DATE_OVERRIDE'] is None


def test_date_override_ignored_in_production():
    cfg = load_config(prod_env(TNT_DATE_OVERRIDE='2026-09-23'))
    assert cfg['TNT_DATE_OVERRIDE'] is None
    assert cfg['ALLOW_DATE_OVERRIDE'] is False
