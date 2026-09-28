"""
MinistryPlatform sign-in (OIDC via Authlib), the login gate, CSRF, and security headers.

Who may sign in: see models/access.py. Identity in the session is written ONLY by the
OIDC callback below; no endpoint accepts identity fields from a client (the lesson of
better-auth GHSA-pqxp-c5mr-5398 in the sibling apps).

MP quirks handled here:
- The id_token may omit `nonce` (MPCodeIDToken tolerates absence, rejects a mismatch).
- PKCE is only sent when MP_OIDC_PKCE=1 (MP's shared TM.Widgets client rejects it).
- Logout must send id_token_hint, or MP ignores post_logout_redirect_uri.
"""
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import requests
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from authlib.integrations.flask_client import OAuth
from authlib.oidc.core import CodeIDToken
from flask import Blueprint, current_app, g, redirect, render_template, request, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from joserfc.errors import InvalidClaimError, JoseError, MissingClaimError

from models import mp
from models.access import check_access, lookup_mp_user
from routes.responses import json_error, wants_json

log = logging.getLogger(__name__)

bp = Blueprint('auth', __name__)
csrf = CSRFProtect()

PUBLIC_ENDPOINTS = frozenset({'static', 'healthz', 'favicon', 'auth.login', 'auth.login_start',
                              'auth.callback', 'auth.logged_out'})
ID_TOKEN_MAX_CHARS = 2400          # keeps the session cookie under the 4 KB browser limit
ACCESS_CACHE_SECONDS = 600
RECHECK_RETRY_SECONDS = 300
POST_TRUST_SECONDS = 6 * 3600      # a page load within this window covers a whole club night of saves
_ERROR_CODE_RE = re.compile(r'^[a-z_]{1,40}$')


class MPCodeIDToken(CodeIDToken):
    """MP id_tokens may omit `nonce`. Absence is accepted (unless required); a mismatch is rejected."""
    REQUIRE_NONCE = False

    def validate_nonce(self):
        sent = self.params.get('nonce')
        if 'nonce' in self:
            if not sent or self['nonce'] != sent:
                raise InvalidClaimError('nonce')
        elif self.REQUIRE_NONCE:
            raise MissingClaimError('nonce')


class MPCodeIDTokenNonceRequired(MPCodeIDToken):
    REQUIRE_NONCE = True


class LoginFailure(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class CurrentUser:
    guid: str
    user_id: int
    contact_id: int
    name: str
    via: str

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p]
        return ''.join(p[0] for p in parts[:2]).upper() or '?'

    @property
    def role_label(self):
        return 'T&T Leader' if self.via == 'leader' else 'Staff access'


def current_user():
    user = g.get('user')
    if user is None:
        raise RuntimeError('current_user() outside a signed-in request')
    return user


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def safe_next(value):
    """Only same-site relative paths; never back into the auth flow."""
    if not isinstance(value, str) or not value or len(value) > 512:
        return '/'
    if any(ord(ch) < 32 for ch in value) or '\\' in value:
        return '/'
    if not value.startswith('/') or value.startswith('//') or value.startswith('/.'):
        return '/'
    if '/./' in value or '/../' in value:
        return '/'
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return '/'
    if parts.path.startswith(('/login', '/auth/', '/logout', '/logged-out')):
        return '/'
    return value


def _cfg(key):
    return current_app.config[key]


def _positive_int(value):
    return type(value) is int and value > 0


def _valid_user_shape(user):
    return (isinstance(user, dict)
            and _positive_int(user.get('user_id'))
            and _positive_int(user.get('contact_id'))
            and mp.normalize_guid(user.get('guid')) is not None
            and user.get('via') in ('leader', 'admin')
            and isinstance(user.get('name'), str))


def _referer_path():
    referer = urlsplit(request.referrer or '')
    if referer.netloc != request.host:
        return '/'
    return referer.path + (f'?{referer.query}' if referer.query else '')


def _unauthenticated():
    if wants_json():
        return json_error('auth_required', 'Your sign-in expired. Sign in again, then redo your last change.',
                          401, login_url=url_for('auth.login', next=safe_next(_referer_path())))
    if request.method not in ('GET', 'HEAD'):
        # A native form post: come back to the page it was on, not to the POST-only URL.
        return redirect(url_for('auth.login', next=safe_next(_referer_path())), 303)
    target = request.full_path if request.query_string else request.path
    return redirect(url_for('auth.login', next=safe_next(target)))


def _forbidden():
    if wants_json():
        return json_error('not_authorized',
                          'Your MinistryPlatform login isn’t on the T&T Leaders list. Nothing was saved.', 403)
    return render_template('forbidden.html'), 403


def _unavailable():
    if wants_json():
        return json_error('mp_unavailable',
                          'Can’t confirm your leader status right now, so nothing was saved. Try again shortly.', 503)
    return render_template('error.html', error_title='MinistryPlatform unavailable',
                           error_message='Can’t confirm your leader status right now. Try again shortly.'), 503


def _access(user_id, max_age, timeout=mp.DEFAULT_TIMEOUT):
    return check_access(user_id, leader_group_id=_cfg('MP_LEADER_GROUP_ID'),
                        admin_group_ids=_cfg('ADMIN_USER_GROUP_IDS'), max_age=max_age, timeout=timeout)


def _recheck_if_due(user, now):
    """Hourly membership re-check. -> 'ok' | 'denied' | 'unavailable'.

    Saves (POSTs) made within POST_TRUST_SECONDS of the last successful check never wait on MP.
    MP outages and API-client misconfiguration keep existing sessions working for the grace
    period (sign-in itself always fails closed); a definite "not a member" ends the session.
    """
    checked = int(session.get('checked_at', 0))
    age = now - checked
    if age < _cfg('AUTH_RECHECK_SECONDS'):
        return 'ok'
    if request.method not in ('GET', 'HEAD') and age < POST_TRUST_SECONDS:
        return 'ok'
    in_grace = age <= _cfg('AUTH_OUTAGE_GRACE_SECONDS')
    failed = int(session.get('recheck_failed_at', 0))
    if failed and in_grace and now - failed < RECHECK_RETRY_SECONDS:
        return 'ok'
    try:
        decision = _access(user['user_id'], ACCESS_CACHE_SECONDS, timeout=mp.HOT_TIMEOUT)
    except (mp.MPUnavailable, mp.MPAuthError, mp.MPForbidden) as e:
        level = logging.WARNING if isinstance(e, mp.MPUnavailable) else logging.ERROR
        log.log(level, 'auth.recheck_unavailable user_id=%s kind=%s in_grace=%s',
                user['user_id'], type(e).__name__, in_grace)
        if in_grace:
            session['recheck_failed_at'] = now
            return 'ok'
        return 'unavailable'
    except mp.MPError as e:
        log.error('auth.recheck_failed user_id=%s kind=%s', user['user_id'], type(e).__name__)
        return 'unavailable'
    if not decision.allowed:
        log.info('auth.recheck_denied user_id=%s', user['user_id'])
        return 'denied'
    session['checked_at'] = now
    session.pop('recheck_failed_at', None)
    if decision.via != user['via']:
        session['user'] = {**user, 'via': decision.via}
    return 'ok'


def auth_gate():
    endpoint = request.endpoint
    if endpoint in PUBLIC_ENDPOINTS or request.path.startswith('/static/'):
        return None
    if endpoint == 'auth.logout':
        if session.get('user') or session.get('logout_hint'):
            _csrf_protect()
        return None
    user = session.get('user')
    if not _valid_user_shape(user):
        session.pop('user', None)
        return _unauthenticated()
    now = int(time.time())
    if int(session.get('auth_at', 0)) < _cfg('AUTH_NOT_BEFORE') or session.get('aud') != _cfg('APP_BASE_URL'):
        session.clear()
        return _unauthenticated()
    outcome = _recheck_if_due(user, now)
    if outcome == 'denied':
        hint = session.get('id_token')
        session.clear()
        if hint:
            session['logout_hint'] = hint
        return _forbidden()
    if outcome == 'unavailable':
        return _unavailable()
    user = session['user']
    g.user = CurrentUser(guid=user['guid'], user_id=user['user_id'], contact_id=user['contact_id'],
                         name=user['name'], via=user['via'])
    _csrf_protect()
    return None


def _csrf_protect():
    if current_app.config.get('WTF_CSRF_ENABLED'):
        csrf.protect()   # only acts on unsafe methods; raises CSRFError


def security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')  # Flask-WTF needs same-origin Referer
    resp.headers.setdefault('Content-Security-Policy',
                            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; "
                            f"form-action 'self' {_cfg('MP_ORIGIN')}")
    if not request.path.startswith('/static/'):
        if request.path.startswith(('/login', '/auth/', '/logout', '/logged-out')):
            resp.headers['Cache-Control'] = 'no-store'
        else:
            resp.headers.setdefault('Cache-Control', 'private, no-cache')
        resp.vary.add('Cookie')
    return resp


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def _client():
    return current_app.extensions['tnt_oauth'].mp


@bp.get('/login')
def login():
    if _valid_user_shape(session.get('user')):
        return redirect(safe_next(request.args.get('next')))
    error = request.args.get('error')
    return render_template('login.html', error=error, next=safe_next(request.args.get('next'))), \
        (503 if error == 'mp_unavailable' else 200)


@bp.get('/login/start')
def login_start():
    if not _cfg('LOGIN_AVAILABLE'):
        return render_template('login.html', error='unavailable', next='/'), 404
    base = _cfg('APP_BASE_URL')
    if urlsplit(base).netloc != request.host.lower() and not request.args.get('canon'):
        # The OAuth state lives in a host-scoped cookie; start on the canonical host.
        return redirect(f"{base}{url_for('auth.login_start', next=safe_next(request.args.get('next')), canon=1)}")
    for key in [k for k in session if k.startswith('_state_mp_')]:
        session.pop(key, None)
    session.pop('logout_hint', None)
    session['login_next'] = safe_next(request.args.get('next'))
    try:
        _checked_metadata(_client())
        return _client().authorize_redirect(f'{base}/auth/callback')
    except requests.RequestException:
        log.warning('auth.discovery_unavailable')
        return redirect(url_for('auth.login', error='mp_unavailable'))


def _checked_metadata(client):
    """Discovery document, validated: a maintenance page cached as metadata would break every sign-in."""
    metadata = client.load_server_metadata()
    expected = f"{_cfg('MP_BASE_URL')}/oauth"
    if (metadata.get('issuer') != expected or not metadata.get('authorization_endpoint')
            or not metadata.get('token_endpoint') or not metadata.get('jwks_uri')):
        log.error('auth.bad_discovery issuer=%s', str(metadata.get('issuer'))[:80])
        client.server_metadata = {}
        raise requests.ConnectionError('bad discovery document')
    return metadata


def _fetch_verified_claims(client):
    """Exchange the code; verify the id_token (RS256 via JWKS; iss, aud, exp, nonce). Patched in tests."""
    metadata = _checked_metadata(client)
    options = {'iss': {'essential': True, 'value': metadata['issuer']},
               'aud': {'essential': True, 'values': [client.client_id]},
               'sub': {'essential': True}}
    claims_cls = MPCodeIDTokenNonceRequired if _cfg('MP_OIDC_REQUIRE_NONCE') else MPCodeIDToken
    token = client.authorize_access_token(claims_cls=claims_cls, claims_options=options, leeway=120)
    claims = token.get('userinfo')
    if not claims:
        raise LoginFailure('no_id_token')
    return dict(claims), token.get('id_token')


@bp.get('/auth/callback')
def callback():
    try:
        claims, id_token = _fetch_verified_claims(_client())
    except MismatchingStateError:
        if _valid_user_shape(session.get('user')):
            return redirect(safe_next(session.get('login_next')))   # replayed callback; already signed in
        log.warning('auth.state_mismatch ua=%s', request.user_agent.string[:80])
        return render_template('login.html', error='state', next='/'), 400
    except OAuthError as e:
        log.warning('auth.oauth_error error=%s', e.error if _ERROR_CODE_RE.match(str(e.error or '')) else 'other')
        code = 'cancelled' if e.error == 'access_denied' else 'expired' if e.error == 'invalid_grant' else 'failed'
        return render_template('login.html', error=code, next='/'), 400
    except JoseError as e:
        log.warning('auth.id_token_invalid kind=%s', type(e).__name__)
        return render_template('login.html', error='verify', next='/'), 400
    except LoginFailure as e:
        return render_template('login.html', error=e.code, next='/'), e.status
    except requests.RequestException:
        return render_template('login.html', error='mp_unavailable', next='/'), 503

    guid = mp.normalize_guid(claims.get('sub'))
    if guid is None:
        return render_template('login.html', error='failed', next='/'), 400
    log.info('auth.claims nonce_present=%s id_token_len=%s', 'nonce' in claims, len(id_token or ''))
    try:
        mp_user = lookup_mp_user(guid)
        decision = _access(mp_user.user_id, 0) if mp_user else None
    except mp.MPError as e:
        log.warning('auth.callback_mp_error kind=%s', type(e).__name__)
        session.clear()
        return render_template('login.html', error='mp_unavailable', next='/'), 503

    if mp_user is None or not decision.allowed:
        log.info('auth.denied reason=%s user_id=%s contact_id=%s', 'no_dp_user' if mp_user is None else 'not_member',
                 getattr(mp_user, 'user_id', None), getattr(mp_user, 'contact_id', None))
        session.clear()
        if id_token and len(id_token) <= ID_TOKEN_MAX_CHARS:
            session['logout_hint'] = id_token
        return render_template('forbidden.html', denied_user=mp_user), 403

    nxt = safe_next(session.get('login_next'))
    session.clear()                                   # fixation defense; drops Authlib state + old CSRF token
    session.permanent = True
    session['user'] = {'guid': guid, 'user_id': mp_user.user_id, 'contact_id': mp_user.contact_id,
                       'name': mp_user.display_name, 'via': decision.via}
    session['auth_at'] = session['checked_at'] = int(time.time())
    session['aud'] = _cfg('APP_BASE_URL')        # a cookie from another environment is never accepted here
    if id_token and len(id_token) <= ID_TOKEN_MAX_CHARS:
        session['id_token'] = id_token
    else:
        log.warning('auth.id_token_not_stored length=%s', len(id_token or ''))
    log.info('auth.login_ok user_id=%s via=%s', mp_user.user_id, decision.via)
    return redirect(nxt)


@bp.post('/logout')
def logout():
    hint = session.get('id_token') or session.get('logout_hint')
    had_user = 'user' in session
    switching = request.form.get('switch') == '1'
    if not had_user and not hint and not switching:
        # Nothing to end (also: a cross-site POST without our cookie must not clear anything).
        return redirect(url_for('auth.logged_out'), 303)
    session.clear()
    log.info('auth.logout had_user=%s had_hint=%s switching=%s', had_user, bool(hint), switching)
    if not hint and not switching:
        # MP ignores post_logout_redirect_uri without id_token_hint and strands the user on its page.
        return redirect(url_for('auth.logged_out'), 303)
    params = {'post_logout_redirect_uri': f"{_cfg('APP_BASE_URL')}/logged-out"}
    if hint:
        params['id_token_hint'] = hint
    return redirect(f"{_cfg('MP_BASE_URL')}/oauth/connect/endsession?{urlencode(params)}", 303)


@bp.get('/logged-out')
def logged_out():
    return render_template('logged_out.html')


@bp.get('/account')
def account():
    return render_template('account.html', user=current_user())


def handle_csrf_error(e):
    referrer_problem = 'referrer' in (e.description or '').lower()
    log.warning('csrf.fail reason=%s endpoint=%s', e.description, request.endpoint)
    if wants_json():
        if referrer_problem:
            return json_error('csrf_referrer', 'Your browser didn’t send the page address, so nothing was saved. '
                                               'Try again from the app, or ask for help if this keeps happening.', 400)
        return json_error('csrf_failed', 'This page is out of date, so nothing was saved. It will reload; '
                                         'then try again.', 400, reload=True)
    return render_template('error.html', error_title='Page expired',
                           error_message='This page is out of date, so nothing was saved. Go back, reload it, '
                                         'and try again.'), 400


def init_auth(app):
    oauth = OAuth(app)
    kwargs = {'scope': app.config['MP_OIDC_SCOPES'], 'token_endpoint_auth_method': 'client_secret_post',
              'default_timeout': 10}
    if app.config['MP_OIDC_PKCE']:
        kwargs['code_challenge_method'] = 'S256'
    oauth.register('mp', client_id=app.config['MP_CLIENT_ID'], client_secret=app.config['MP_CLIENT_SECRET'],
                   server_metadata_url=f"{app.config['MP_BASE_URL']}/oauth/.well-known/openid-configuration",
                   client_kwargs=kwargs)
    app.extensions['tnt_oauth'] = oauth
    csrf.init_app(app)
    app.before_request(auth_gate)
    app.after_request(security_headers)
    app.register_error_handler(CSRFError, handle_csrf_error)
    app.register_blueprint(bp)

    @app.context_processor
    def _auth_context():
        return {'current_user': g.get('user'), 'help_contact': app.config['HELP_CONTACT'],
                'login_available': app.config['LOGIN_AVAILABLE']}

    @app.get('/favicon.ico', endpoint='favicon')
    def favicon():
        return redirect('/static/icon-192.png', 308)
