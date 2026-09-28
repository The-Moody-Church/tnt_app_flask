"""
App configuration, read from environment variables.

load_config() is a pure function of the environment mapping so it can be
unit-tested. Deployed context comes from Vercel's VERCEL_ENV
(production | preview | development); anything else is local dev or tests.
"""
import os
import re
import secrets
from datetime import date, timedelta
from urllib.parse import urlsplit

DEFAULT_MP_BASE_URL = 'https://moody.ministryplatform.com/ministryplatformapi'
DEFAULT_MP_SCOPES = 'openid http://www.thinkministry.com/dataplatform/scopes/all'


def _bool(value, default=False):
    if value is None or value == '':
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _int(value, default):
    if value is None or str(value).strip() == '':
        return default
    return int(str(value).strip())


def _int_list(value):
    """'29, 31' -> (29, 31). Rejects anything that isn't a positive int."""
    if not value or not str(value).strip():
        return ()
    ids = []
    for part in str(value).split(','):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) <= 0:
            raise ValueError(f'Expected comma-separated positive integers, got {value!r}')
        ids.append(int(part))
    return tuple(ids)


def _normalize_origin(value):
    """'https://TNT.example.org:443/' -> 'https://tnt.example.org'. Rejects paths and queries."""
    value = value.strip().rstrip('/')
    if not value:
        return ''
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.path or parts.query or parts.fragment:
        raise ValueError(f'APP_BASE_URL must be an origin like https://tnt.example.org, got {value!r}')
    host = parts.hostname.lower()
    port = parts.port
    if port and not ((parts.scheme == 'https' and port == 443) or (parts.scheme == 'http' and port == 80)):
        host = f'{host}:{port}'
    return f'{parts.scheme}://{host}'


def load_config(env=None):
    env = os.environ if env is None else env
    vercel_env = env.get('VERCEL_ENV') or None
    production = vercel_env == 'production'
    deployed = vercel_env in ('production', 'preview')

    secret_key = env.get('SECRET_KEY', '')
    if production and len(secret_key) < 32:
        raise RuntimeError('SECRET_KEY (32+ chars) is required in production')
    if deployed and secret_key and len(secret_key) < 32:
        raise RuntimeError('SECRET_KEY must be 32+ chars on deployed environments')
    has_stable_secret = bool(secret_key)
    if not secret_key:
        # Local dev only: sessions reset on restart. On serverless previews each instance would get a
        # different key, which breaks sessions and OAuth state, so login is disabled there (below).
        secret_key = secrets.token_urlsafe(48)

    app_base_url = _normalize_origin(env.get('APP_BASE_URL') or '')
    if production and not app_base_url:
        raise RuntimeError('APP_BASE_URL is required in production')
    if not app_base_url and not deployed:
        app_base_url = 'http://localhost:5001'

    mp_base_url = (env.get('MINISTRY_PLATFORM_BASE_URL') or DEFAULT_MP_BASE_URL).rstrip('/')
    mp_client_id = env.get('MP_CLIENT_ID', '')
    mp_client_secret = env.get('MP_CLIENT_SECRET', '')
    if production and not (mp_client_id and mp_client_secret):
        raise RuntimeError('MP_CLIENT_ID and MP_CLIENT_SECRET are required in production')

    mp_parts = urlsplit(mp_base_url)
    secure = app_base_url.startswith('https://') or deployed

    date_override = (env.get('TNT_DATE_OVERRIDE') or '').strip() or None
    if production or (date_override and not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date_override)):
        date_override = None
    if date_override:
        try:
            date.fromisoformat(date_override)
        except ValueError:
            date_override = None

    return {
        'VERCEL_ENV': vercel_env,
        'PRODUCTION': production,
        'DEPLOYED': deployed,
        'SECRET_KEY': secret_key,
        'SECRET_KEY_FALLBACKS': [k.strip() for k in (env.get('SECRET_KEY_FALLBACKS') or '').split(',') if k.strip()],
        'APP_BASE_URL': app_base_url,
        'ASSET_VERSION': (env.get('VERCEL_GIT_COMMIT_SHA') or 'dev')[:7],

        # MinistryPlatform
        'MP_BASE_URL': mp_base_url,
        'MP_ORIGIN': f'{mp_parts.scheme}://{mp_parts.netloc}',
        'MP_CLIENT_ID': mp_client_id,
        'MP_CLIENT_SECRET': mp_client_secret,
        'MP_OIDC_SCOPES': env.get('MP_OIDC_SCOPES') or DEFAULT_MP_SCOPES,
        'MP_OIDC_PKCE': _bool(env.get('MP_OIDC_PKCE')),
        'MP_OIDC_REQUIRE_NONCE': _bool(env.get('MP_OIDC_REQUIRE_NONCE')),
        'MP_LEADER_GROUP_ID': _int(env.get('MP_LEADER_GROUP_ID'), 1000),
        'MP_ROSTER_GROUP_ID': _int(env.get('MP_ROSTER_GROUP_ID'), 697),
        'ADMIN_USER_GROUP_IDS': _int_list(env.get('ADMIN_USER_GROUP_IDS')),
        'MP_WRITES_ENABLED': _bool(env.get('MP_WRITES_ENABLED'), default=production),
        'LOGIN_AVAILABLE': bool(app_base_url and mp_client_id and mp_client_secret
                                and (has_stable_secret or not deployed)),
        'HELP_CONTACT': env.get('HELP_CONTACT') or 'the Awana director',

        # Sessions and auth
        'SESSION_COOKIE_NAME': '__Host-tnt' if secure else 'tnt_session',
        'SESSION_COOKIE_SECURE': secure,
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',  # Strict breaks the redirect back from MP
        'PERMANENT_SESSION_LIFETIME': timedelta(days=_int(env.get('SESSION_LIFETIME_DAYS'), 35)),
        'SESSION_REFRESH_EACH_REQUEST': False,  # the hourly re-check rewrites the cookie (rolling)
        'AUTH_RECHECK_SECONDS': _int(env.get('AUTH_RECHECK_MINUTES'), 60) * 60,
        'AUTH_OUTAGE_GRACE_SECONDS': _int(env.get('AUTH_OUTAGE_GRACE_DAYS'), 35) * 86400,
        'AUTH_NOT_BEFORE': _int(env.get('AUTH_NOT_BEFORE'), 0),
        'MAX_CONTENT_LENGTH': 256 * 1024,

        # CSRF: checked by the auth gate after the login check (see routes/auth.py)
        'WTF_CSRF_ENABLED': True,
        'WTF_CSRF_CHECK_DEFAULT': False,
        'WTF_CSRF_TIME_LIMIT': None,
        'WTF_CSRF_SSL_STRICT': True,

        # Dates
        'TNT_DATE_OVERRIDE': date_override,
        'ALLOW_DATE_OVERRIDE': not production,
    }
