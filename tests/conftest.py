"""
Shared test fixtures.

Tests run against a real PostgreSQL database (no fake data layer, so the SQL can't drift
from what the routes expect). Locally:

    docker run -d --name tnt-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17
    pytest tests/ -q

TEST_DATABASE_URL overrides the default database. It is created if missing and the schema is
rebuilt once per session. MinistryPlatform is always faked (FakeMP).
"""
import os
from datetime import date, datetime, timezone

import psycopg
import pytest

TEST_DATABASE_URL = os.environ.get('TEST_DATABASE_URL',
                                   'postgresql://postgres:postgres@localhost:55432/tnt_test')

os.environ['TNT_TESTING'] = '1'
os.environ['DATABASE_URL'] = TEST_DATABASE_URL
os.environ.setdefault('SECRET_KEY', 'test-' + 'x' * 40)
os.environ.setdefault('APP_BASE_URL', 'http://localhost:5001')
os.environ.setdefault('MINISTRY_PLATFORM_BASE_URL', 'https://mp.example.test/ministryplatformapi')
os.environ.setdefault('MP_CLIENT_ID', 'test-client')
os.environ.setdefault('MP_CLIENT_SECRET', 'test-secret')
os.environ.pop('VERCEL_ENV', None)

from models import mp, sync_state  # noqa: E402

TODAY = date(2026, 9, 23)          # the app's "today" in tests (TNT_DATE_OVERRIDE)
D_0909, D_0916, D_0923, D_0930, D_1007 = (date(2026, 9, 9), date(2026, 9, 16), date(2026, 9, 23),
                                          date(2026, 9, 30), date(2026, 10, 7))
BOOK_SECTIONS = ['Start Zone 1', 'Start Zone 2', '1.1', '1.2', '1.3', '1.4', '1.5', '1.6']

# contact_id, participant_id, group_participant_id, nickname, first, last, team, active
KIDS = [
    (1001, 5001, 9001, None, 'Alice', 'Anders', 'Red', True),
    (1002, 5002, 9002, 'Bo', 'Robert', 'Brandt', 'Red', True),
    (1003, 5003, 9003, None, 'Cara', 'Cole', 'Blue', True),
    (1004, 5004, 9004, None, 'Dev', 'Dunn', 'Blue', True),
    (1005, 5005, 9005, None, 'Eli', 'Eames', 'Green', True),
    (1006, 5006, 9006, None, 'Fay', 'Ford', 'Yellow', True),
    (1007, 5007, 9007, None, 'Gus', 'Gray', None, True),      # walk-in, Unassigned
    (1008, 5008, None, None, 'Hal', 'Hart', 'Red', False),   # last season's kid (inactive)
]


def _admin_url(url):
    base, _, _ = url.rpartition('/')
    return base + '/postgres'


@pytest.fixture(scope='session')
def database():
    """Create the test database if needed and rebuild the schema once per session."""
    dbname = TEST_DATABASE_URL.rsplit('/', 1)[1].split('?')[0]
    with psycopg.connect(_admin_url(TEST_DATABASE_URL), autocommit=True) as admin:
        exists = admin.execute('SELECT 1 FROM pg_database WHERE datname = %s', (dbname,)).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{dbname}"')
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute('DROP SCHEMA public CASCADE')
        conn.execute('CREATE SCHEMA public')
    from scripts.migrate import migrate
    migrate(TEST_DATABASE_URL, out=lambda *_: None)
    return TEST_DATABASE_URL


@pytest.fixture
def conn(database):
    """Direct autocommit connection for seeding and assertions. Tables are emptied before each test."""
    with psycopg.connect(database, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        c.execute('TRUNCATE kids, attendance, completed_sections, schedule, book_sections, sync_state, '
                  'team_assignments RESTART IDENTITY CASCADE')
        yield c


class FakeMP:
    """Stands in for models.mp.MPClient. Configure `tables[name]` with rows or a callable(**kw) -> rows."""

    def __init__(self, writes_enabled=True):
        self.writes_enabled = writes_enabled
        self.tables = {}
        self.calls = []
        self.puts = []
        self.fail_with = None

    def _rows(self, table, kw):
        self.calls.append((table, kw))
        if self.fail_with:
            raise self.fail_with
        rows = self.tables.get(table, [])
        return rows(**kw) if callable(rows) else list(rows)

    def get(self, table, **kw):
        return self._rows(table, kw)

    def get_one(self, table, **kw):
        rows = self._rows(table, kw)
        return rows[0] if rows else None

    def put(self, table, records, **kw):
        if not self.writes_enabled:
            raise mp.MPWritesDisabled('writes_disabled', op='put', table=table)
        self.puts.append((table, records, kw))
        if self.fail_with:
            raise self.fail_with
        return [dict(r) for r in records]


@pytest.fixture
def fake_mp():
    fake = FakeMP()
    mp.set_client(fake)
    sync_state.memo_clear()
    yield fake
    mp.set_client(None)
    sync_state.memo_clear()


@pytest.fixture
def app(conn, fake_mp):
    from tnt import create_app
    app = create_app({'TESTING': True, 'WTF_CSRF_ENABLED': False, 'TNT_DATE_OVERRIDE': TODAY.isoformat()})
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def app_ctx(app):
    with app.app_context():
        yield


def mark_synced(conn, *keys, detail='{}'):
    """Mark MP syncs fresh so routes don't call MP."""
    for key in keys:
        conn.execute("""INSERT INTO sync_state (key, synced_at, attempted_at, detail) VALUES (%s, now(), now(), %s)
                        ON CONFLICT (key) DO UPDATE SET synced_at = now(), attempted_at = now(),
                                                        last_error = NULL, detail = EXCLUDED.detail""",
                     (key, detail))


def add_attendance(conn, contact_id, d, *, team='__kid__', mp_checked_in=False, manual_present=False,
                   source='app', **flags):
    team_sql = '(SELECT team FROM kids WHERE contact_id = %(cid)s)' if team == '__kid__' else '%(team)s'
    cols = ['has_bible', 'wearing_shirt', 'has_book', 'did_homework', 'has_dues']
    conn.execute(
        f"""INSERT INTO attendance (contact_id, meeting_date, team, mp_checked_in, mp_time_in, manual_present,
                                    source, {', '.join(cols)})
            VALUES (%(cid)s, %(d)s, {team_sql}, %(mp)s, CASE WHEN %(mp)s THEN %(tin)s END, %(man)s, %(src)s,
                    {', '.join('%(' + c + ')s' for c in cols)})""",
        {'cid': contact_id, 'd': d, 'team': team, 'mp': mp_checked_in, 'man': manual_present, 'src': source,
         'tin': datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).replace(hour=23, minute=12),
         **{c: flags.get(c, False) for c in cols}})


def add_section(conn, contact_id, d, section, *, team='__kid__', complete=False, silver=False, gold=False):
    team_sql = '(SELECT team FROM kids WHERE contact_id = %(cid)s)' if team == '__kid__' else '%(team)s'
    return conn.execute(
        f"""INSERT INTO completed_sections (contact_id, meeting_date, section, team, section_complete,
                                            silver_credit, gold_credit)
            VALUES (%(cid)s, %(d)s, %(s)s, {team_sql}, %(c)s, %(sv)s, %(g)s) RETURNING id""",
        {'cid': contact_id, 'd': d, 's': section, 'team': team, 'c': complete, 'sv': silver, 'g': gold}
    ).fetchone()['id']


@pytest.fixture
def seeded(conn):
    """A small season: 5 schedule dates, 8 book sections, 8 kids (1 unassigned, 1 inactive), syncs fresh."""
    conn.execute("""INSERT INTO schedule (meeting_date, theme, sections) VALUES
        ('2026-09-09', 'Welcome!', '{"Start Zone 1","Start Zone 2"}'),
        ('2026-09-16', 'Regular Meeting', '{1.1}'),
        ('2026-09-23', 'Sports Team Night!', '{1.2}'),
        ('2026-09-30', 'Regular Meeting', '{1.3}'),
        ('2026-10-07', 'NO AWANA - Fall Break', '{}')""")
    for i, s in enumerate(BOOK_SECTIONS):
        conn.execute('INSERT INTO book_sections (section, sort_order) VALUES (%s, %s)', (s, i))
    for cid, pid, gpid, nick, first, last, team, active in KIDS:
        conn.execute("""INSERT INTO kids (contact_id, participant_id, group_participant_id, nickname, first_name,
                                          last_name, team, mp_notes, group_role_id, active)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 2, %s)""",
                     (cid, pid, gpid, nick, first, last, team, team or 'Created by CheckIn', active))
    mark_synced(conn, 'roster', *(f'checkins:{d.isoformat()}' for d in (D_0909, D_0916, D_0923, D_0930)))
    return conn
