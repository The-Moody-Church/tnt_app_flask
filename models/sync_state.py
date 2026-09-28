"""
Single-flight, sync-on-read bookkeeping for MP syncs on serverless.

There are no background threads (unreliable on Vercel). A request that finds
data stale tries to *claim* the sync with one atomic statement; the winner
fetches from MP while everyone else serves what's in Postgres. No transaction
is held during the HTTP call.

Also: a per-request MP time Budget, and an 'mp_health' circuit breaker shared
across instances so an MP outage costs one slow request, not every request.
"""
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from models import mp

MP_DOWN_SECONDS = 60


@dataclass(frozen=True)
class SyncStatus:
    key: str
    state: str                 # fresh | synced | stale | pending | error | no_event | frozen | skipped
    synced_at: datetime | None = None
    error: str | None = None

    @property
    def ok(self):
        return self.state in ('fresh', 'synced', 'no_event', 'frozen')


class BudgetExceeded(Exception):
    pass


class Budget:
    """Wall-clock allowance for MP calls within one request."""

    def __init__(self, seconds=3.0, *, base_timeout=mp.HOT_TIMEOUT):
        self.deadline = time.monotonic() + seconds
        self.base_timeout = base_timeout

    def remaining(self):
        return self.deadline - time.monotonic()

    def timeout(self):
        left = self.remaining()
        if left < 0.5:
            raise BudgetExceeded()
        connect, read = self.base_timeout
        return (min(connect, left), min(read, left))


class Unlimited(Budget):
    """For scripts and manual refreshes: fixed timeouts, no deadline."""

    def __init__(self, timeout=mp.SCRIPT_TIMEOUT):
        super().__init__(10 ** 9, base_timeout=timeout)


def claim(conn, key, *, ttl, retry_after, force=False):
    """Try to become the one request that syncs `key`. Returns the prior row dict if claimed, else None."""
    row = conn.execute(
        """INSERT INTO sync_state AS s (key, attempted_at) VALUES (%(key)s, now())
           ON CONFLICT (key) DO UPDATE SET attempted_at = now()
            WHERE (%(force)s OR s.synced_at IS NULL OR s.synced_at < now() - make_interval(secs => %(ttl)s))
              AND (s.attempted_at IS NULL OR s.attempted_at < now() - make_interval(secs => %(retry)s))
           RETURNING s.synced_at, s.detail""",
        {'key': key, 'force': force, 'ttl': ttl, 'retry': retry_after}).fetchone()
    return dict(row) if row else None


def read(conn, key):
    row = conn.execute('SELECT synced_at, attempted_at, last_error, detail FROM sync_state WHERE key = %s',
                       (key,)).fetchone()
    return dict(row) if row else None


def complete(conn, key, *, synced_at, detail):
    conn.execute('UPDATE sync_state SET synced_at = %s, last_error = NULL, detail = %s WHERE key = %s',
                 (synced_at, json.dumps(detail, default=str), key))


def fail(conn, key, error):
    conn.execute('UPDATE sync_state SET last_error = %s WHERE key = %s', (error[:300], key))


def db_now(conn):
    """Postgres clock (the one now() uses), so fetch timestamps compare cleanly with DB-written ones."""
    return conn.execute('SELECT clock_timestamp() AS t').fetchone()['t']


def status_when_mp_down(key, row, ttl):
    """Circuit breaker open: serve what we have, but say so when nothing fresh exists."""
    status = status_from_row(key, row, ttl)
    if status.state in ('pending', 'stale'):
        return SyncStatus(key, 'error', status.synced_at, 'mp_unavailable')
    return status


def status_from_row(key, row, ttl):
    """Status for a request that lost (or skipped) the claim."""
    if not row or not row.get('synced_at'):
        return SyncStatus(key, 'pending' if not (row and row.get('last_error')) else 'error',
                          None, row.get('last_error') if row else None)
    age = (datetime.now(timezone.utc) - row['synced_at']).total_seconds()
    if row.get('last_error'):
        return SyncStatus(key, 'error', row['synced_at'], row['last_error'])
    return SyncStatus(key, 'fresh' if age < ttl else 'stale', row['synced_at'])


# -- per-process memo so warm instances skip the DB check while data is fresh ----

_memo = {}
_memo_lock = threading.Lock()


def memo_fresh(key):
    """The memoized SyncStatus if this process knows `key` is still fresh, else None."""
    with _memo_lock:
        hit = _memo.get(key)
    if hit is None or time.time() >= hit[0]:
        return None
    return SyncStatus(key, 'fresh', hit[1])


def memo_set(key, fresh_for_seconds, synced_at=None):
    if fresh_for_seconds <= 0:
        return
    with _memo_lock:
        _memo[key] = (time.time() + fresh_for_seconds, synced_at)


def memo_clear():
    with _memo_lock:
        _memo.clear()


# -- MP circuit breaker ----------------------------------------------------------

def mp_is_down(conn):
    row = read(conn, 'mp_health')
    if not row:
        return False
    until = (row.get('detail') or {}).get('down_until')
    if not until:
        return False
    return datetime.fromisoformat(until) > datetime.now(timezone.utc)


def mark_mp_down(conn, error, seconds=MP_DOWN_SECONDS):
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    conn.execute(
        """INSERT INTO sync_state (key, attempted_at, last_error, detail)
           VALUES ('mp_health', now(), %(err)s, %(detail)s)
           ON CONFLICT (key) DO UPDATE SET attempted_at = now(), last_error = EXCLUDED.last_error,
                                           detail = EXCLUDED.detail""",
        {'err': error[:300], 'detail': json.dumps({'down_until': until.isoformat()})})


def mark_mp_up(conn):
    conn.execute("""UPDATE sync_state SET last_error = NULL, detail = '{}'::jsonb
                     WHERE key = 'mp_health' AND detail ? 'down_until'""")
