"""
MinistryPlatform REST API client (server-to-server, OAuth2 client credentials).

Pattern follows facilities-charts/backend/getdata.py, with a cached token,
timeouts on every call, typed errors and safe $filter literal helpers.
Error messages never include response bodies, filters or tokens (MP echoes
filters, which can contain GUIDs, back in its error bodies).
"""
import logging
import os
import re
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

SCOPE_ALL = 'http://www.thinkministry.com/dataplatform/scopes/all'
DEFAULT_BASE_URL = 'https://moody.ministryplatform.com/ministryplatformapi'
CHICAGO = ZoneInfo('America/Chicago')

HOT_TIMEOUT = (2, 4)        # connect, read: request path (a leader is waiting)
DEFAULT_TIMEOUT = (3.05, 10)
SCRIPT_TIMEOUT = (5, 30)

TOKEN_SAFETY_MARGIN = 300   # refresh 5 min early (mp-charts client.ts margins)
TOKEN_MIN_LIFETIME = 30
PAGE_SIZE = 1000
MAX_PAGES = 20

_TABLE_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_GUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


class MPError(Exception):
    def __init__(self, kind, *, op='', table=None, status=None):
        self.kind = kind
        self.op = op
        self.table = table
        self.status = status
        super().__init__(f'MP {op} {table or ""} failed: {status or kind}'.replace('  ', ' '))


class MPUnavailable(MPError):
    """Network error, timeout, 5xx, 429, or a non-JSON body."""


class MPAuthError(MPError):
    """Token endpoint rejected the client credentials, or 401 after a token refresh."""


class MPForbidden(MPError):
    """403: the API client's user role lacks a table permission."""


class MPBadRequest(MPError):
    """400: bad $filter/$select (a code bug)."""


class MPNotFound(MPError):
    pass


class MPWritesDisabled(MPError):
    pass


# ---------------------------------------------------------------------------
# $filter literal helpers: the only way values may enter a filter string
# ---------------------------------------------------------------------------

def q_int(value):
    if isinstance(value, bool):
        raise ValueError('bool is not an id')
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.isdigit():
        n = int(value)
    else:
        raise ValueError(f'not an integer id: {value!r}')
    if n <= 0:
        raise ValueError('ids must be positive')
    return str(n)


def q_ids(values):
    values = list(values)
    if not values:
        raise ValueError('empty id list')
    return ','.join(q_int(v) for v in values)


def q_date(d):
    if isinstance(d, datetime) or not isinstance(d, date):
        raise ValueError('q_date needs a date')
    return f"'{d.isoformat()}'"


def normalize_guid(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if _GUID_RE.match(value) else None


def q_guid(value):
    guid = normalize_guid(value)
    if not guid:
        raise ValueError('invalid GUID')
    return f"'{guid}'"


def parse_mp_datetime(value):
    """MP returns naive Central wall-clock datetimes ('2026-09-23T18:31:02'); attach the zone."""
    if not value:
        return None
    text = str(value)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'  # a real UTC value: convert rather than relabel
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=CHICAGO) if dt.tzinfo is None else dt.astimezone(CHICAGO)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MPClient:
    def __init__(self, base_url, client_id, client_secret, *, writes_enabled=False,
                 session_factory=requests.Session):
        self.base_url = base_url.rstrip('/')
        self.client_id = client_id
        self.client_secret = client_secret
        self.writes_enabled = writes_enabled
        self._session_factory = session_factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._token = None
        self._token_expires = 0.0
        self._token_failed_until = 0.0

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        writes = env.get('MP_WRITES_ENABLED')
        if writes is None or writes == '':
            writes_enabled = env.get('VERCEL_ENV') == 'production'
        else:
            writes_enabled = writes.strip().lower() in ('1', 'true', 'yes', 'on')
        return cls(env.get('MINISTRY_PLATFORM_BASE_URL') or DEFAULT_BASE_URL,
                   env.get('MP_CLIENT_ID', ''), env.get('MP_CLIENT_SECRET', ''),
                   writes_enabled=writes_enabled)

    # -- plumbing ----------------------------------------------------------

    def _session(self):
        s = getattr(self._local, 'session', None)
        if s is None:
            s = self._session_factory()
            s.headers.update({'Accept': 'application/json',
                              'User-Agent': f"tnt-tracker/{(os.environ.get('VERCEL_GIT_COMMIT_SHA') or 'dev')[:7]}"})
            self._local.session = s
        return s

    def token(self, *, force=False, timeout=DEFAULT_TIMEOUT):
        if not force and self._token and time.time() < self._token_expires:
            return self._token
        with self._lock:
            if not force and self._token and time.time() < self._token_expires:
                return self._token
            if time.time() < self._token_failed_until:
                raise MPUnavailable('token_recently_failed', op='token')
            if not (self.client_id and self.client_secret):
                raise MPAuthError('not_configured', op='token')
            try:
                resp = self._session().post(
                    f'{self.base_url}/oauth/connect/token',
                    data={'grant_type': 'client_credentials', 'scope': SCOPE_ALL,
                          'client_id': self.client_id, 'client_secret': self.client_secret},
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    timeout=timeout)
            except requests.RequestException as e:
                self._token_failed_until = time.time() + 15
                raise MPUnavailable(type(e).__name__, op='token') from None
            if resp.status_code in (400, 401):
                raise MPAuthError('token_rejected', op='token', status=resp.status_code)
            if resp.status_code >= 300:
                raise MPUnavailable('token_http', op='token', status=resp.status_code)
            try:
                body = resp.json()
                token = body['access_token']
                lifetime = int(body.get('expires_in') or 3600)
            except (ValueError, KeyError, TypeError, AttributeError):
                raise MPUnavailable('bad_body', op='token') from None
            if not isinstance(token, str) or not token:
                raise MPUnavailable('bad_body', op='token')
            self._token = token
            self._token_expires = time.time() + max(lifetime - TOKEN_SAFETY_MARGIN, TOKEN_MIN_LIFETIME)
            return token

    def invalidate_token(self):
        with self._lock:
            self._token = None
            self._token_expires = 0.0

    def _request(self, method, path, *, params, json_body=None, timeout, table=None):
        op = method.lower()
        for attempt in (0, 1):
            headers = {'Authorization': f'Bearer {self.token(force=attempt == 1, timeout=timeout)}'}
            started = time.monotonic()
            try:
                resp = self._session().request(method, f'{self.base_url}/{path}', params=params,
                                               json=json_body, headers=headers, timeout=timeout)
            except requests.RequestException as e:
                log.warning('mp.call op=%s table=%s kind=%s', op, table, type(e).__name__)
                raise MPUnavailable(type(e).__name__, op=op, table=table) from None
            ms = int((time.monotonic() - started) * 1000)
            status = resp.status_code
            if status == 401 and attempt == 0:
                self.invalidate_token()
                continue
            if status == 401:
                raise MPAuthError('unauthorized', op=op, table=table, status=status)
            if status == 400:
                raise MPBadRequest('bad_request', op=op, table=table, status=status)
            if status == 403:
                raise MPForbidden('forbidden', op=op, table=table, status=status)
            if status == 404:
                raise MPNotFound('not_found', op=op, table=table, status=status)
            if status == 429 or status >= 500:
                log.warning('mp.call op=%s table=%s status=%s ms=%s', op, table, status, ms)
                raise MPUnavailable('http', op=op, table=table, status=status)
            if status >= 300:
                raise MPUnavailable('unexpected_status', op=op, table=table, status=status)
            try:
                data = resp.json()
            except ValueError:
                raise MPUnavailable('bad_body', op=op, table=table, status=status) from None
            log.debug('mp.call op=%s table=%s status=%s ms=%s', op, table, status, ms)
            return data
        raise MPAuthError('unauthorized', op=op, table=table)

    # -- API ---------------------------------------------------------------

    def get(self, table, *, select, filter=None, orderby=None, top=None, distinct=False,
            paginate=None, timeout=DEFAULT_TIMEOUT):
        """GET tables/{table}. Paginates with $top/$skip when paginate (default: when top is None)."""
        if not _TABLE_RE.fullmatch(table):
            raise ValueError(f'bad table name {table!r}')
        if not select:
            raise ValueError('select is required (never fetch every column)')
        params = {'$select': select}
        if filter:
            params['$filter'] = filter
        if orderby:
            params['$orderby'] = orderby
        if distinct:
            params['$distinct'] = 'true'
        if paginate is None:
            paginate = top is None
        if not paginate:
            if top is not None:
                params['$top'] = top
            return self._rows(self._request('GET', f'tables/{table}', params=params, timeout=timeout,
                                            table=table), table)
        if not orderby:
            raise ValueError('orderby is required when paginating (unstable paging otherwise)')
        rows = []
        for page in range(MAX_PAGES):
            page_params = {**params, '$top': PAGE_SIZE, '$skip': page * PAGE_SIZE}
            batch = self._rows(self._request('GET', f'tables/{table}', params=page_params, timeout=timeout,
                                             table=table), table)
            rows.extend(batch)
            if len(batch) < PAGE_SIZE:
                return rows
        raise MPError('too_many_rows', op='get', table=table)

    @staticmethod
    def _rows(body, table):
        if not isinstance(body, list):
            raise MPUnavailable('bad_body', op='get', table=table)
        return body

    def get_one(self, table, *, select, filter, orderby=None, timeout=DEFAULT_TIMEOUT):
        rows = self.get(table, select=select, filter=filter, orderby=orderby, top=1, timeout=timeout)
        return rows[0] if rows else None

    def put(self, table, records, *, user_id, select=None, timeout=DEFAULT_TIMEOUT):
        """PUT tables/{table}?$userId=... . $userId is what MP's audit log records."""
        if not _TABLE_RE.fullmatch(table):
            raise ValueError(f'bad table name {table!r}')
        if not self.writes_enabled:
            log.info('mp.put blocked (writes disabled) table=%s', table)
            raise MPWritesDisabled('writes_disabled', op='put', table=table)
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError('user_id must be a positive int')
        if not records or not all(isinstance(r, dict) and r for r in records):
            raise ValueError('records must be non-empty dicts')
        params = {'$userId': user_id}
        if select:
            params['$select'] = select
        return self._request('PUT', f'tables/{table}', params=params, json_body=records,
                             timeout=timeout, table=table)


_client = None
_client_lock = threading.Lock()


def get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = MPClient.from_env()
    return _client


def set_client(client):
    """Tests: install a fake (or None to rebuild from env on next use)."""
    global _client
    _client = client


def mp_get(table, **kw):
    return get_client().get(table, **kw)


def mp_get_one(table, **kw):
    return get_client().get_one(table, **kw)


def mp_put(table, records, **kw):
    return get_client().put(table, records, **kw)


def writes_enabled():
    return get_client().writes_enabled
