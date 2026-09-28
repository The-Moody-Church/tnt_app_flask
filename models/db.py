"""
PostgreSQL connections (psycopg 3).

One connection per request, opened lazily on first use and closed at app
context teardown. On Vercel DATABASE_URL points at Neon's pooled (-pooler)
host, which runs PgBouncer in transaction mode, so server-side prepared
statements are disabled (prepare_threshold=None) and no session state is used.
"""
import os
import time

import psycopg
from psycopg.rows import dict_row
from flask import g, has_app_context


def connect(url=None, *, autocommit=True):
    """Open a connection. Retries the connect once (Neon may be waking up); never retries statements."""
    url = url or os.environ['DATABASE_URL']
    timeout = int(os.environ.get('DB_CONNECT_TIMEOUT', '10'))
    for attempt in (0, 1):
        try:
            return psycopg.connect(url, row_factory=dict_row, autocommit=autocommit,
                                   prepare_threshold=None, connect_timeout=timeout,
                                   application_name='tnt-app')
        except psycopg.OperationalError:
            if attempt:
                raise
            time.sleep(0.4)


def get_conn():
    """The request's connection. Outside an app context (scripts), callers pass conn= explicitly."""
    if not has_app_context():
        raise RuntimeError('get_conn() needs an app context; pass conn= from scripts')
    if 'db' not in g:
        g.db = connect()
    return g.db


def close_conn(_exc=None):
    conn = g.pop('db', None)
    if conn is not None:
        conn.close()


def init_app(app):
    app.teardown_appcontext(close_conn)
