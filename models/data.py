"""
Data layer: all Postgres reads and writes. Routes use this module, never SQL directly.

Conventions:
- Rows are snake_case dicts with real date/datetime values.
- Kid/date arguments are keyword-only (contact_id=, meeting_date=).
- team=ALL means "no team filter"; team=None means Unassigned.
- Every function takes conn=None (defaults to the request connection).
- Nothing is swallowed: errors raise models.errors.* or psycopg errors.
- No MP calls here; MP sync lives in models/roster.py and models/checkins.py.
"""
from psycopg import errors as pg_errors
from psycopg import sql

from models.db import get_conn
from models.errors import InvalidInput, NotFound, NotPresent
from models.fields import ATTENDANCE_FLAG_COLUMNS, ATTENDANCE_WRITABLE, SECTION_CREDIT_COLUMNS
from models.utils import get_today, num


class _All:
    def __repr__(self):
        return 'ALL'


ALL = _All()


def _conn(conn):
    return conn if conn is not None else get_conn()


def _by_contact_id(by):
    if by is None:
        return None
    return by.get('contact_id') if isinstance(by, dict) else getattr(by, 'contact_id', None)


def _by_name(by):
    if by is None:
        return None
    return by.get('name') if isinstance(by, dict) else getattr(by, 'name', None)


def _team_clause(column, team, params, key='team'):
    """SQL fragment for a team filter honoring the ALL / None (Unassigned) sentinels."""
    if team is ALL:
        return sql.SQL('TRUE')
    params[key] = team
    return sql.SQL('{} IS NOT DISTINCT FROM %({})s').format(sql.SQL(column), sql.SQL(key))


# =============================================================================
# Reference data
# =============================================================================

def get_schedule(conn=None):
    return _conn(conn).execute(
        """SELECT meeting_date, theme, sections, no_club, sequence, unit, scope, key_verses, details
             FROM schedule ORDER BY meeting_date""").fetchall()


def get_schedule_day(d, conn=None):
    return _conn(conn).execute(
        """SELECT meeting_date, theme, sections, no_club, sequence, unit, scope, key_verses, details
             FROM schedule WHERE meeting_date = %s""", (d,)).fetchone()


def get_teams(conn=None):
    return _conn(conn).execute('SELECT name, emoji, sort_order FROM teams ORDER BY sort_order').fetchall()


def get_book_sections(conn=None):
    return [r['section'] for r in _conn(conn).execute(
        'SELECT section FROM book_sections ORDER BY sort_order')]


# =============================================================================
# Kids
# =============================================================================

_KID_COLS = """k.contact_id, k.participant_id, k.group_participant_id, k.name, k.nickname,
               k.first_name, k.last_name, k.team, k.active"""


def get_kid(*, contact_id, conn=None):
    return _conn(conn).execute(f'SELECT {_KID_COLS} FROM kids k WHERE k.contact_id = %s',
                               (contact_id,)).fetchone()


def get_roster(team=ALL, *, include_inactive=False, conn=None):
    params = {}
    where = [_team_clause('k.team', team, params)]
    if not include_inactive:
        where.append(sql.SQL('k.active'))
    query = sql.SQL('SELECT {cols} FROM kids k WHERE {where} ORDER BY k.last_name, k.name').format(
        cols=sql.SQL(_KID_COLS), where=sql.SQL(' AND ').join(where))
    return _conn(conn).execute(query, params).fetchall()


# =============================================================================
# Attendance board
# =============================================================================

# A kid belongs to a team's list for a date by the team recorded on that date's
# attendance row (even NULL = Unassigned); kids with no row use their current team.
_BOARD_SQL = """
    SELECT k.contact_id, k.name, k.nickname, k.first_name, k.last_name, k.active,
           k.team AS kid_team,
           CASE WHEN a.id IS NOT NULL THEN a.team ELSE k.team END AS team,
           a.id AS attendance_id,
           COALESCE(a.present, false)        AS present,
           COALESCE(a.mp_checked_in, false)  AS mp_checked_in,
           a.mp_time_in, a.mp_time_confirmed,
           COALESCE(a.manual_present, false) AS manual_present,
           a.manual_present_by, a.manual_present_at,
           COALESCE(a.has_bible, false)      AS has_bible,
           COALESCE(a.wearing_shirt, false)  AS wearing_shirt,
           COALESCE(a.has_book, false)       AS has_book,
           COALESCE(a.did_homework, false)   AS did_homework,
           COALESCE(a.has_dues, false)       AS has_dues,
           a.updated_at
      FROM kids k
      LEFT JOIN attendance a ON a.contact_id = k.contact_id AND a.meeting_date = %(d)s
     WHERE (a.id IS NOT NULL OR k.active)
"""


def _board_row(row):
    if row is None:
        return None
    row = dict(row)
    row['source'] = 'mp' if row['mp_checked_in'] else ('manual' if row['manual_present'] else None)
    row['mp_time'] = row['mp_time_confirmed'] or row['mp_time_in']
    return row


def get_board(d, team, *, conn=None):
    """Kids on a team's list for date d (team=None: Unassigned), sorted by name."""
    params = {'d': d}
    query = sql.SQL(_BOARD_SQL + ' AND {team} ORDER BY k.last_name, k.name').format(
        team=_team_clause('CASE WHEN a.id IS NOT NULL THEN a.team ELSE k.team END', team, params))
    return [_board_row(r) for r in _conn(conn).execute(query, params)]


def get_board_counts(d, *, conn=None):
    """{team_or_None: {'here': n, 'total': m}} using the board membership rule."""
    rows = _conn(conn).execute(
        f"""SELECT team, COUNT(*) FILTER (WHERE present) AS here, COUNT(*) AS total
              FROM ({_BOARD_SQL}) b GROUP BY team""", {'d': d}).fetchall()
    return {r['team']: {'here': r['here'], 'total': r['total']} for r in rows}


def get_attendance(*, contact_id, meeting_date, conn=None):
    """The kid's board row for a date (defaults when there is no attendance row); None for an unknown kid."""
    row = _conn(conn).execute(
        _BOARD_SQL.replace('WHERE (a.id IS NOT NULL OR k.active)', 'WHERE k.contact_id = %(cid)s'),
        {'d': meeting_date, 'cid': contact_id}).fetchone()
    return _board_row(row)


# =============================================================================
# Totals (SQL views reproducing the sheet formulas)
# =============================================================================

def _totals(view, d, team, conn):
    params = {}
    where = [_team_clause('team', team, params)]
    if d is not None:
        params['d'] = d
        where.append(sql.SQL('meeting_date = %(d)s'))
    query = sql.SQL('SELECT * FROM {view} WHERE {where} ORDER BY meeting_date, team_sort').format(
        view=sql.Identifier(view), where=sql.SQL(' AND ').join(where))
    rows = []
    for r in _conn(conn).execute(query, params):
        r = dict(r)
        r['points_per_kid'] = num(r['points_per_kid'])
        rows.append(r)
    return rows


def get_weekly_totals(d=None, team=ALL, *, conn=None):
    return _totals('weekly_totals', d, team, conn)


def get_attendance_totals(d=None, team=ALL, *, conn=None):
    return _totals('weekly_attendance_totals', d, team, conn)


# =============================================================================
# Completed sections
# =============================================================================

_SECTION_COLS = """c.id, c.contact_id, k.name, c.meeting_date, c.section, c.team,
                   c.section_complete, c.silver_credit, c.gold_credit, c.updated_at"""


def get_completed_sections(*, contact_id=None, meeting_date=None, team=ALL, section=None,
                           include_empty=False, conn=None):
    params = {}
    where = [_team_clause('c.team', team, params)]
    if contact_id is not None:
        params['cid'] = contact_id
        where.append(sql.SQL('c.contact_id = %(cid)s'))
    if meeting_date is not None:
        params['d'] = meeting_date
        where.append(sql.SQL('c.meeting_date = %(d)s'))
    if section is not None:
        params['section'] = section
        where.append(sql.SQL('c.section = %(section)s'))
    if not include_empty:
        where.append(sql.SQL('(c.section_complete OR c.silver_credit OR c.gold_credit)'))
    query = sql.SQL("""SELECT {cols} FROM completed_sections c JOIN kids k USING (contact_id)
                        WHERE {where} ORDER BY c.meeting_date, c.id""").format(
        cols=sql.SQL(_SECTION_COLS), where=sql.SQL(' AND ').join(where))
    return _conn(conn).execute(query, params).fetchall()


def get_section_entry(entry_id, *, conn=None):
    return _conn(conn).execute(
        f'SELECT {_SECTION_COLS} FROM completed_sections c JOIN kids k USING (contact_id) WHERE c.id = %s',
        (entry_id,)).fetchone()


def get_section_history(*, contact_id, conn=None):
    """{section: {'main_date', 'silver_date', 'gold_date'}}: the first date each credit was earned."""
    rows = _conn(conn).execute(
        """SELECT section,
                  MIN(meeting_date) FILTER (WHERE section_complete) AS main_date,
                  MIN(meeting_date) FILTER (WHERE silver_credit)    AS silver_date,
                  MIN(meeting_date) FILTER (WHERE gold_credit)      AS gold_date
             FROM completed_sections WHERE contact_id = %s GROUP BY section""", (contact_id,))
    return {r['section']: {'main_date': r['main_date'], 'silver_date': r['silver_date'],
                           'gold_date': r['gold_date']}
            for r in rows if r['main_date'] or r['silver_date'] or r['gold_date']}


def get_progress_summary(*, conn=None):
    """Every active kid (and inactive kids with history) with distinct section/silver/gold counts."""
    return _conn(conn).execute(
        """SELECT k.contact_id, k.name, k.team, k.active,
                  COUNT(DISTINCT c.section) FILTER (WHERE c.section_complete) AS total_sections,
                  COUNT(DISTINCT c.section) FILTER (WHERE c.silver_credit)    AS total_silver,
                  COUNT(DISTINCT c.section) FILTER (WHERE c.gold_credit)      AS total_gold
             FROM kids k LEFT JOIN completed_sections c ON c.contact_id = k.contact_id
            GROUP BY k.contact_id
           HAVING k.active
               OR COUNT(c.id) FILTER (WHERE c.section_complete OR c.silver_credit OR c.gold_credit) > 0
            ORDER BY k.last_name, k.name""").fetchall()


def ping(*, conn=None):
    return _conn(conn).execute('SELECT 1 AS ok').fetchone()['ok'] == 1


# =============================================================================
# Writes
# =============================================================================

def _check_bools(fields, allowed):
    if not fields:
        raise InvalidInput('nothing to update')
    for key, value in fields.items():
        if key not in allowed:
            raise InvalidInput(f'unknown field {key!r}')
        if not isinstance(value, bool):
            raise InvalidInput(f'{key} must be true or false')


def _translate_fk(e):
    raise InvalidInput('unknown date or section') from e


def set_attendance(*, contact_id, meeting_date, fields, by=None, conn=None):
    """Upsert only the given attendance columns (so concurrent chip taps don't clobber each other).

    Setting a Bible/Shirt/Book/HW/Dues flag to true requires the kid to be present
    (MP check-in or manual). Raises NotFound, InvalidInput, NotPresent.
    """
    _check_bools(fields, ATTENDANCE_WRITABLE)
    conn = _conn(conn)
    cols = list(fields)
    values = {f'v_{c}': fields[c] for c in cols}
    extra_insert, extra_update = [], []
    if 'manual_present' in fields:
        values['by_name'] = _by_name(by) if fields['manual_present'] else None
        extra_insert = ['manual_present_by', 'manual_present_at']
        extra_update = [sql.SQL('manual_present_by = EXCLUDED.manual_present_by'),
                        sql.SQL('manual_present_at = EXCLUDED.manual_present_at')]
    insert_cols = [sql.Identifier(c) for c in cols] + [sql.Identifier(c) for c in extra_insert]
    insert_vals = [sql.Placeholder(f'v_{c}') for c in cols]
    if extra_insert:
        insert_vals += [sql.Placeholder('by_name'),
                        sql.SQL('CASE WHEN %(v_manual_present)s THEN now() END')]
    updates = [sql.SQL('{c} = EXCLUDED.{c}').format(c=sql.Identifier(c)) for c in cols] + extra_update
    query = sql.SQL("""
        INSERT INTO attendance (contact_id, meeting_date, team, {cols}, updated_by_contact_id, source)
        SELECT k.contact_id, %(d)s, k.team, {vals}, %(by)s, 'app' FROM kids k WHERE k.contact_id = %(cid)s
        ON CONFLICT (contact_id, meeting_date) DO UPDATE SET {updates},
            updated_at = now(), updated_by_contact_id = EXCLUDED.updated_by_contact_id
        RETURNING present, has_bible, wearing_shirt, has_book, did_homework, has_dues""").format(
        cols=sql.SQL(', ').join(insert_cols), vals=sql.SQL(', ').join(insert_vals),
        updates=sql.SQL(', ').join(updates))
    params = {'d': meeting_date, 'cid': contact_id, 'by': _by_contact_id(by), **values}
    try:
        with conn.transaction():
            prev = conn.execute(
                """SELECT manual_present, has_bible, wearing_shirt, has_book, did_homework, has_dues
                     FROM attendance WHERE contact_id = %s AND meeting_date = %s FOR UPDATE""",
                (contact_id, meeting_date)).fetchone() or {}
            row = conn.execute(query, params).fetchone()
            if row is None:
                raise NotFound('unknown kid')
            # Only newly-set flags need presence (old rows may carry flags without Present).
            newly_set = [c for c in cols if c in ATTENDANCE_FLAG_COLUMNS and fields[c] and not prev.get(c)]
            if newly_set and not row['present']:
                raise NotPresent('Mark them present first')
            if fields.get('manual_present') is False and prev.get('manual_present') and not row['present']:
                # A leader un-marked present: the items they recorded go with it.
                conn.execute("""UPDATE attendance SET has_bible = false, wearing_shirt = false, has_book = false,
                                       did_homework = false, has_dues = false
                                 WHERE contact_id = %s AND meeting_date = %s""", (contact_id, meeting_date))
    except pg_errors.ForeignKeyViolation as e:
        _translate_fk(e)
    return get_attendance(contact_id=contact_id, meeting_date=meeting_date, conn=conn)


def _section_team_sql():
    # Section rows take the same-date attendance row's team (even NULL = Unassigned that night),
    # falling back to the kid's current team only when there is no attendance row.
    return """CASE WHEN EXISTS (SELECT 1 FROM attendance a
                                 WHERE a.contact_id = k.contact_id AND a.meeting_date = %(d)s)
                   THEN (SELECT a.team FROM attendance a
                          WHERE a.contact_id = k.contact_id AND a.meeting_date = %(d)s)
                   ELSE k.team END"""


def upsert_completed_section(*, contact_id, meeting_date, section, credits, by=None, conn=None):
    """Form path: OR-merge credits into the (kid, date, section) row; never downgrades a TRUE."""
    _check_bools(credits, set(SECTION_CREDIT_COLUMNS))
    c = {col: bool(credits.get(col, False)) for col in SECTION_CREDIT_COLUMNS}
    conn = _conn(conn)
    try:
        row = conn.execute(
            f"""INSERT INTO completed_sections (contact_id, meeting_date, section, team, section_complete,
                                                silver_credit, gold_credit, updated_by_contact_id, source)
                SELECT k.contact_id, %(d)s, %(section)s, {_section_team_sql()}, %(c)s, %(s)s, %(g)s, %(by)s, 'app'
                  FROM kids k WHERE k.contact_id = %(cid)s
                ON CONFLICT (contact_id, meeting_date, section) DO UPDATE SET
                    section_complete = completed_sections.section_complete OR EXCLUDED.section_complete,
                    silver_credit    = completed_sections.silver_credit    OR EXCLUDED.silver_credit,
                    gold_credit      = completed_sections.gold_credit      OR EXCLUDED.gold_credit,
                    updated_at = now(), updated_by_contact_id = EXCLUDED.updated_by_contact_id
                RETURNING id""",
            {'d': meeting_date, 'section': section, 'cid': contact_id, 'by': _by_contact_id(by),
             'c': c['section_complete'], 's': c['silver_credit'], 'g': c['gold_credit']}).fetchone()
    except pg_errors.ForeignKeyViolation as e:
        _translate_fk(e)
    if row is None:
        raise NotFound('unknown kid')
    return get_section_entry(row['id'], conn=conn)


def set_section_credit(*, contact_id, meeting_date, section, field, value, by=None, conn=None):
    """Chip path: set one credit to an exact value on the (kid, date, section) row (un-toggling allowed)."""
    _check_bools({field: value}, set(SECTION_CREDIT_COLUMNS))
    conn = _conn(conn)
    query = sql.SQL(f"""
        INSERT INTO completed_sections (contact_id, meeting_date, section, team, {{col}},
                                        updated_by_contact_id, source)
        SELECT k.contact_id, %(d)s, %(section)s, {_section_team_sql()}, %(v)s, %(by)s, 'app'
          FROM kids k WHERE k.contact_id = %(cid)s
        ON CONFLICT (contact_id, meeting_date, section) DO UPDATE SET {{col}} = EXCLUDED.{{col}},
            updated_at = now(), updated_by_contact_id = EXCLUDED.updated_by_contact_id
        RETURNING id""").format(col=sql.Identifier(field))
    try:
        row = conn.execute(query, {'d': meeting_date, 'section': section, 'cid': contact_id,
                                   'v': value, 'by': _by_contact_id(by)}).fetchone()
    except pg_errors.ForeignKeyViolation as e:
        _translate_fk(e)
    if row is None:
        raise NotFound('unknown kid')
    return get_section_entry(row['id'], conn=conn)


def update_completed_section(*, entry_id, credits, by=None, conn=None):
    """Edit pages: set exact credit values on an existing row. Returns the row, or None if it doesn't exist."""
    _check_bools(credits, set(SECTION_CREDIT_COLUMNS))
    conn = _conn(conn)
    sets = [sql.SQL('{} = {}').format(sql.Identifier(col), sql.Placeholder(col)) for col in credits]
    query = sql.SQL("""UPDATE completed_sections SET {sets}, updated_at = now(), updated_by_contact_id = %(by)s
                        WHERE id = %(id)s RETURNING id""").format(sets=sql.SQL(', ').join(sets))
    row = conn.execute(query, {**credits, 'by': _by_contact_id(by), 'id': entry_id}).fetchone()
    return get_section_entry(row['id'], conn=conn) if row else None


def is_writable_date(d):
    """No writes for future dates (the old UTC bug recorded kids a week ahead)."""
    return d <= get_today()
