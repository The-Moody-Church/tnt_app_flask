"""
Apply migrations/NNNN_*.sql in order.

    DATABASE_URL_UNPOOLED=postgresql://owner@<direct-host>/tnt python scripts/migrate.py [--status]

Runs as the database owner over the DIRECT (unpooled) URL: DDL and the advisory
lock need a real session, which Neon's transaction pooler doesn't provide.
Never run at request time or in the Vercel build.
"""
import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS = Path(__file__).resolve().parent.parent / 'migrations'
LOCK_KEY = 'tnt_migrations'


def pending(conn):
    applied = {r[0]: r[1] for r in conn.execute('SELECT version, checksum FROM schema_migrations')}
    todo = []
    for path in sorted(MIGRATIONS.glob('[0-9][0-9][0-9][0-9]_*.sql')):
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.stem in applied:
            if applied[path.stem] != checksum:
                raise SystemExit(f'edited applied migration: {path.name} (write a new migration instead)')
            continue
        todo.append((path, checksum))
    return applied, todo


def migrate(url, *, status_only=False, out=print):
    if '-pooler' in url:
        raise SystemExit('migrate.py needs the direct (unpooled) URL, not the -pooler host')
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                            version text PRIMARY KEY, checksum text NOT NULL,
                            applied_at timestamptz NOT NULL DEFAULT now())""")
        conn.execute('SELECT pg_advisory_lock(hashtext(%s))', (LOCK_KEY,))
        try:
            applied, todo = pending(conn)
            if status_only:
                for version in sorted(applied):
                    out(f'applied  {version}')
                for path, _ in todo:
                    out(f'pending  {path.stem}')
                return len(todo)
            for path, checksum in todo:
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute('INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)',
                                 (path.stem, checksum))
                out(f'applied  {path.name}')
            if not todo:
                out('up to date')
            return len(todo)
        finally:
            conn.execute('SELECT pg_advisory_unlock(hashtext(%s))', (LOCK_KEY,))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--status', action='store_true', help='list applied/pending without applying')
    args = parser.parse_args(argv)
    url = os.environ.get('DATABASE_URL_UNPOOLED')
    if not url:
        sys.exit('DATABASE_URL_UNPOOLED is not set')
    migrate(url, status_only=args.status)


if __name__ == '__main__':
    main()
