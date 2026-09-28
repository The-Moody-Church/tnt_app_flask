"""
Import the legacy Google Sheet export (xlsx) into Postgres, back-filling Present from MP check-ins.

    DATABASE_URL_UNPOOLED=postgresql://owner@<direct-host>/tnt MP_CLIENT_ID=... MP_CLIENT_SECRET=... \\
        python scripts/import_sheet.py --xlsx TNT_App_Data.xlsx --through 2026-09-23 \\
            [--season-start 2026-08-01] [--apply] [--report PATH] [--keep-app-present] [--force]
    python scripts/import_sheet.py --xlsx TNT_App_Data.xlsx --schedule-only [--apply]

Dry run by default: everything runs in one transaction that is rolled back; --apply commits.
Exits non-zero on any abort. The report contains kids' names: keep it out of the repo.

Steps: load and validate the workbook; parity A (the pure-Python formulas reproduce the
sheet's cached totals); match sheet kids to MP Group 697 by Nickname/First + Last name;
forward-only re-dating of rows hit by the old UTC date bug; OR-merge duplicates; back-fill
MP check-ins for every past date; replace every attendance/section row with
meeting_date <= --through; parity B (the SQL views equal the Python reference).
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402
import psycopg  # noqa: E402

from models import checkins as mp_checkins  # noqa: E402
from models import db, mp, roster  # noqa: E402
from models.errors import DataError  # noqa: E402
from models.fields import ATTENDANCE_FLAG_COLUMNS, SECTION_CREDIT_COLUMNS, TEAMS  # noqa: E402
from scripts import sheet_formulas  # noqa: E402

SEASON_START = date(2026, 8, 1)
CHICAGO = mp.CHICAGO
MANUAL_PRESENT_BY = 'Imported (legacy app Present)'

HEADERS = {
    'Master Roster': ['Name', 'Last Name', 'First Name', 'Group'],
    'Attendance Entries RAW': ['Name', 'Team', 'Date', 'Present', 'Has Bible', 'Wearing Shirt?', 'Has Book?',
                               'Did Homework?', 'Has Dues?', 'timestamp'],
    'Completed Sections RAW': ['Name', 'Team', 'Date', 'Section', 'Section Complete', 'Silver Credit',
                               'Gold Credit', 'timestamp'],
    'Schedule': ['Date', 'Theme', 'sequence', 'section', 'unit', 'scope', 'key verse(s)', 'Details'],
    'Book Sections': ['Section'],
    'Teams': ['Team', 'Color'],
    'Weekly Attendance Totals': ['Date', 'Team', 'Team Color', 'Kids Present', 'Total Bibles', 'Total Shirts',
                                 'Total Workbooks', 'Total hw done', 'Total Dues', 'Attendance Points',
                                 'Points Per Kid'],
    'Weekly Totals': ['Date', 'Team', 'Team Color', 'Number of Kids', 'Sections Completed', 'Silvers Completed',
                      'Golds Completed', 'Section points', 'Attendance Points', 'Total Points', 'Points per Kid',
                      'Team Total Points'],
}
LOAD_TEST_SHEET = 'Load Test Entries'

# Totals sheet columns C.. in sheet order, named like the SQL view columns
WAT_COLUMNS = ['team_color', 'kids_present', 'total_bibles', 'total_shirts', 'total_workbooks', 'total_hw_done',
               'total_dues', 'attendance_points', 'points_per_kid']
WT_COLUMNS = ['team_color', 'number_of_kids', 'sections_completed', 'silvers_completed', 'golds_completed',
              'section_points', 'attendance_points', 'total_points', 'points_per_kid', 'team_total_points']

# Before/after table: (label, source, column)
BEFORE_AFTER = [
    ('Kids Present', 'wat', 'kids_present'),
    ('Attendance Points', 'wat', 'attendance_points'),
    ('Sections', 'wt', 'sections_completed'),
    ('Silvers', 'wt', 'silvers_completed'),
    ('Golds', 'wt', 'golds_completed'),
    ('Total Points', 'wt', 'total_points'),
    ('Team Total Points', 'wt', 'team_total_points'),
]


class ImportAbort(Exception):
    """A condition that stops the import. Nothing is committed."""


# =============================================================================
# Cell helpers
# =============================================================================

def is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def cell_text(value):
    """Cell -> stripped text, or None when blank."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def to_bool(value, where=''):
    """Sheet boolean (TRUE/FALSE, 1/0, blank) -> bool."""
    if value is None or value == '':
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().upper() in ('TRUE', 'FALSE'):
        return value.strip().upper() == 'TRUE'
    raise ImportAbort(f'{where}: expected TRUE/FALSE, got a {type(value).__name__}')


def to_date(value, where=''):
    if isinstance(value, datetime):
        if value.time() != time(0):
            raise ImportAbort(f'{where}: date has a time of day')
        return value.date()
    if isinstance(value, date):
        return value
    raise ImportAbort(f'{where}: not a date')


def sheet_ts(value, where=''):
    """Naive sheet timestamp -> aware UTC (the old app wrote datetime.now() on Vercel, whose clock is UTC)."""
    if is_blank(value):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            raise ImportAbort(f'{where}: bad timestamp') from None
    if not isinstance(value, datetime):
        raise ImportAbort(f'{where}: bad timestamp')
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def normalize_section(value):
    """Section cell -> text. Numbers use the short form (1.1 -> '1.1'); text is stripped. ValueError if blank."""
    if value is None or isinstance(value, bool):
        raise ValueError('blank section')
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = f'{value:g}'
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f'unexpected section type {type(value).__name__}')
    if not text:
        raise ValueError('blank section')
    return text


def parse_schedule_sections(value):
    """Schedule 'section' cell -> list: '1.5\\n1.6' -> ['1.5', '1.6']; ' -- ' and blanks -> []."""
    if value is None:
        return []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [normalize_section(value)]
    parts = [p.strip() for p in str(value).split('\n')]
    return [p for p in parts if p and p != '--']


def name_key(value):
    """Case- and whitespace-insensitive name key."""
    return ' '.join(str(value or '').split()).casefold()


def mp_name(kid):
    return f"{kid.get('nickname') or kid['first_name']} {kid['last_name']}".strip()


# =============================================================================
# Workbook
# =============================================================================

def read_sheet(wb, name):
    """-> [(sheet_row_number, values)] for non-blank data rows, after checking the exact header."""
    if name not in wb.sheetnames:
        raise ImportAbort(f'sheet {name!r} is missing')
    header = HEADERS[name]
    width = len(header)
    rows = wb[name].iter_rows(values_only=True)
    first = tuple(next(rows, None) or ())
    got = [cell_text(v) for v in first[:width]]
    extra = [cell_text(v) for v in first[width:] if not is_blank(v)]
    if got != header or extra:
        raise ImportAbort(f'{name}: header is {got + extra}, expected {header}')
    out = []
    for number, values in enumerate(rows, start=2):
        values = tuple(values[:width]) + (None,) * max(0, width - len(values))
        if all(is_blank(v) for v in values):
            continue  # formatting-only rows
        out.append((number, values))
    return out


def parse_book_sections(rows):
    sections = []
    for number, (value,) in rows:
        try:
            section = normalize_section(value)
        except ValueError as e:
            raise ImportAbort(f'Book Sections row {number}: {e}') from None
        if section in sections:
            raise ImportAbort(f'Book Sections row {number}: duplicate section {section!r}')
        sections.append(section)
    if not sections:
        raise ImportAbort('Book Sections is empty')
    return sections


def parse_schedule(rows, book_sections):
    known, seen, out = set(book_sections), set(), []
    for number, v in rows:
        where = f'Schedule row {number}'
        d = to_date(v[0], where)
        if d in seen:
            raise ImportAbort(f'{where}: duplicate date {d}')
        seen.add(d)
        try:
            sections = parse_schedule_sections(v[3])
        except ValueError as e:
            raise ImportAbort(f'{where}: {e}') from None
        unknown = [s for s in sections if s not in known]
        if unknown:
            raise ImportAbort(f'{where}: section(s) {unknown} are not in Book Sections')
        out.append({'meeting_date': d, 'theme': cell_text(v[1]) or '', 'sections': sections,
                    'sequence': cell_text(v[2]), 'unit': cell_text(v[4]), 'scope': cell_text(v[5]),
                    'key_verses': cell_text(v[6]), 'details': cell_text(v[7])})
    if not out:
        raise ImportAbort('Schedule is empty')
    return out


def parse_teams(rows):
    colors = {}
    for number, (team, color) in rows:
        team, color = cell_text(team), cell_text(color)
        if team in colors or not color:
            raise ImportAbort(f'Teams row {number}: duplicate team or missing color')
        colors[team] = color
    if sorted(colors, key=str) != sorted(TEAMS):
        raise ImportAbort(f'Teams sheet has {list(colors)}, expected {list(TEAMS)}')
    return colors


def parse_roster(rows):
    out, seen = [], set()
    for number, (name, last, first, group) in rows:
        name = cell_text(name)
        if not name:
            raise ImportAbort(f'Master Roster row {number}: empty Name')
        if name_key(name) in seen:
            raise ImportAbort(f'Master Roster row {number}: duplicate Name')
        seen.add(name_key(name))
        out.append({'row': number, 'name': name, 'last_name': cell_text(last), 'first_name': cell_text(first),
                    'group': cell_text(group)})
    return out


def _raw_name(value, where):
    if not isinstance(value, str) or not value.strip():
        raise ImportAbort(f'{where}: empty Name')
    return value


def parse_attendance(rows):
    """Attendance Entries RAW rows. name/team stay raw (parity A needs exact sheet values)."""
    out = []
    for number, v in rows:
        where = f'Attendance Entries RAW row {number}'
        r = {'row': number, 'name': _raw_name(v[0], where), 'team': v[1], 'date': to_date(v[2], where)}
        for field, value in zip(('present',) + ATTENDANCE_FLAG_COLUMNS, v[3:9]):
            r[field] = to_bool(value, where)
        r['ts'] = sheet_ts(v[9], where)
        out.append(r)
    return out


def parse_sections(rows, book_sections):
    known, out = set(book_sections), []
    for number, v in rows:
        where = f'Completed Sections RAW row {number}'
        try:
            section = normalize_section(v[3])
        except ValueError as e:
            raise ImportAbort(f'{where}: {e}') from None
        if section not in known:
            raise ImportAbort(f'{where}: section {section!r} is not in Book Sections')
        r = {'row': number, 'name': _raw_name(v[0], where), 'team': v[1], 'date': to_date(v[2], where),
             'section': section}
        for field, value in zip(SECTION_CREDIT_COLUMNS, v[4:7]):
            r[field] = to_bool(value, where)
        r['ts'] = sheet_ts(v[7], where)
        out.append(r)
    return out


def parse_totals(rows, name, columns):
    """Cached totals -> {(date, team): {column: value, 'row': n}}."""
    out = {}
    for number, v in rows:
        where = f'{name} row {number}'
        key = (to_date(v[0], where), v[1])
        if key in out:
            raise ImportAbort(f'{where}: duplicate date/team')
        out[key] = {**dict(zip(columns, v[2:])), 'row': number}
    return out


def count_rows(wb, name):
    if name not in wb.sheetnames:
        return 0
    return sum(1 for row in wb[name].iter_rows(min_row=2, values_only=True)
               if any(not is_blank(v) for v in row))


def load_workbook(path, *, schedule_only=False):
    """Read and validate the export -> dict of parsed sheets."""
    try:
        wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except (OSError, ValueError, KeyError) as e:
        raise ImportAbort(f'cannot open {Path(path).name}: {type(e).__name__}') from None
    try:
        sheet = {'book_sections': parse_book_sections(read_sheet(wb, 'Book Sections'))}
        sheet['schedule'] = parse_schedule(read_sheet(wb, 'Schedule'), sheet['book_sections'])
        if schedule_only:
            return sheet
        sheet['teams'] = parse_teams(read_sheet(wb, 'Teams'))
        sheet['roster'] = parse_roster(read_sheet(wb, 'Master Roster'))
        sheet['attendance'] = parse_attendance(read_sheet(wb, 'Attendance Entries RAW'))
        sheet['sections'] = parse_sections(read_sheet(wb, 'Completed Sections RAW'), sheet['book_sections'])
        sheet['wat'] = parse_totals(read_sheet(wb, 'Weekly Attendance Totals'), 'Weekly Attendance Totals',
                                    WAT_COLUMNS)
        sheet['wt'] = parse_totals(read_sheet(wb, 'Weekly Totals'), 'Weekly Totals', WT_COLUMNS)
        sheet['load_test_rows'] = count_rows(wb, LOAD_TEST_SHEET)
        return sheet
    finally:
        wb.close()


# =============================================================================
# Parity
# =============================================================================

def values_equal(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def compare_totals(label, expected, actual, columns, *, both_ways=False):
    """{(date, team): {col: value}} x2 -> list of mismatch dicts (dates, teams, numbers; never names)."""
    out = []
    for (d, t), exp in expected.items():
        act = actual.get((d, t))
        if act is None:
            out.append({'sheet': label, 'date': d, 'team': t, 'column': '(row)', 'expected': 'present',
                        'actual': 'missing'})
            continue
        for col in columns:
            if not values_equal(exp.get(col), act.get(col)):
                out.append({'sheet': label, 'date': d, 'team': t, 'column': col, 'expected': exp.get(col),
                            'actual': act.get(col)})
    if both_ways:
        for d, t in actual:
            if (d, t) not in expected:
                out.append({'sheet': label, 'date': d, 'team': t, 'column': '(row)', 'expected': 'missing',
                            'actual': 'present'})
    return out


def parity_a(sheet):
    """sheet_formulas over the raw RAW rows (keyed by name) vs the sheet's cached totals -> (mismatches, rows)."""
    keys = list(dict.fromkeys(list(sheet['wat']) + list(sheet['wt'])))
    dates = sorted({d for d, _ in keys})
    teams = list(dict.fromkeys(t for _, t in keys))
    colors = sheet['teams']
    att = sheet_formulas.attendance_totals(sheet['attendance'], dates, teams, key='name', team_colors=colors)
    wt = sheet_formulas.weekly_totals(att, sheet['sections'], dates, teams, team_colors=colors)
    mismatches = compare_totals('Weekly Attendance Totals', sheet['wat'], att, WAT_COLUMNS)
    mismatches += compare_totals('Weekly Totals', sheet['wt'], wt, WT_COLUMNS)
    return mismatches, len(sheet['wat']) + len(sheet['wt'])


# =============================================================================
# Matching and cleaning (pure)
# =============================================================================

def match_kids(sheet_names, mp_kids):
    """Sheet names -> MP contact_ids by 'Nickname-or-First Last', with 'First Last' as the fallback.

    A name is ambiguous when the two indexes together give more than one kid.
    -> (matches {sheet_name: contact_id}, info). Raises ImportAbort on unmatched/ambiguous names.
    """
    by_nick, by_first = defaultdict(set), defaultdict(set)
    for kid in mp_kids:
        by_nick[name_key(mp_name(kid))].add(kid['contact_id'])
        by_first[name_key(f"{kid['first_name']} {kid['last_name']}")].add(kid['contact_id'])
    matches, unmatched, ambiguous, nickname_only, first_name_only = {}, [], [], [], []
    for name in sheet_names:
        nick, first = by_nick.get(name_key(name), set()), by_first.get(name_key(name), set())
        candidates = nick | first
        if not candidates:
            unmatched.append(name)
        elif len(candidates) > 1:
            ambiguous.append(name)
        else:
            matches[name] = next(iter(candidates))
            if not first:
                nickname_only.append(name)
            elif not nick:
                first_name_only.append(name)
    claimed = defaultdict(list)
    for name, cid in matches.items():
        claimed[cid].append(name)
    shared = sorted(n for names in claimed.values() if len(names) > 1 for n in names)
    problems = []
    if unmatched:
        problems.append(f'{len(unmatched)} not in MP: {", ".join(sorted(unmatched))}')
    if ambiguous:
        problems.append(f'{len(ambiguous)} ambiguous: {", ".join(sorted(ambiguous))}')
    if shared:
        problems.append(f'{len(shared)} sheet names match the same MP kid: {", ".join(shared)}')
    if problems:
        raise ImportAbort('kid matching failed: ' + '; '.join(problems))
    matched = set(matches.values())
    return matches, {
        'nickname_only': sorted(nickname_only),
        'first_name_only': sorted(first_name_only),
        'mp_not_in_sheet': sorted((k for k in mp_kids if k['contact_id'] not in matched), key=mp_name),
    }


def row_list(rows, limit=20):
    text = ', '.join(map(str, rows[:limit]))
    return f'[{text}, ... +{len(rows) - limit} more]' if len(rows) > limit else f'[{text}]'


def attach_contacts(rows, matches, label):
    """Add contact_id and the canonical roster name; validate the row team. Aborts listing row numbers."""
    by_key = {name_key(name): name for name in matches}
    out, not_on_roster, bad_team = [], [], []
    for r in rows:
        name = by_key.get(name_key(r['name']))
        team = r['team'].strip() if isinstance(r['team'], str) else r['team']
        if name is None:
            not_on_roster.append(r['row'])
            continue
        if team not in TEAMS:
            bad_team.append(r['row'])
            continue
        out.append({**r, 'name': name, 'team': team, 'contact_id': matches[name]})
    if not_on_roster:
        raise ImportAbort(f'{label}: rows {row_list(not_on_roster)} name a kid who is not on the Master Roster')
    if bad_team:
        raise ImportAbort(f'{label}: rows {row_list(bad_team)} have a Team that is not one of {list(TEAMS)}')
    return out


def redate_rows(rows, schedule_dates):
    """Forward-only fix for the old UTC get_today() bug -> (rows, moves).

    A row is moved to the Chicago date of its (UTC) timestamp only when the row is dated LATER
    than that and that date is a meeting. Rows dated earlier than their timestamp are later
    edits or back-fills (the timestamp is last-modified) and are never moved.
    """
    out, moves = [], []
    for r in rows:
        r = dict(r)
        ts = r.get('ts')
        if ts is not None:
            local = ts.astimezone(CHICAGO)
            if r['date'] > local.date() and local.date() in schedule_dates:
                moves.append({'row': r['row'], 'name': r.get('name'), 'from': r['date'], 'to': local.date(),
                              'local_ts': local, 'section': r.get('section')})
                r['redated_from'] = r['date']
                r['date'] = local.date()
        out.append(r)
    return out, moves


def check_dates(rows, schedule_dates, through, label):
    off = [r['row'] for r in rows if r['date'] not in schedule_dates]
    if off:
        raise ImportAbort(f'{label}: rows {row_list(off)} are not on a Schedule date')
    late = [r['row'] for r in rows if r['date'] > through]
    if late:
        raise ImportAbort(f'{label}: rows {row_list(late)} are dated after --through {through}')


def _merge(rows, key_fields, bool_fields):
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r[f] for f in key_fields), []).append(r)
    merged, dups = [], []
    for grp in groups.values():
        grp = sorted(grp, key=lambda r: r['row'])
        m = dict(grp[0])  # team (and everything else) from the earliest sheet row
        for f in bool_fields:
            m[f] = any(r[f] for r in grp)
        stamps = [r['ts'] for r in grp if r.get('ts')]
        m['created_at'] = min(stamps) if stamps else None
        m['updated_at'] = max(stamps) if stamps else None
        m['rows'] = [r['row'] for r in grp]
        teams = sorted({r['team'] for r in grp}, key=str)
        m['team_conflict'] = teams if len(teams) > 1 else None
        merged.append(m)
        if len(grp) > 1:
            dups.append(m)
    merged.sort(key=lambda m: m['row'])
    return merged, dups


def merge_attendance(rows):
    """OR-merge duplicate (contact_id, date) rows -> (rows, merged_groups)."""
    return _merge(rows, ('contact_id', 'date'), ('present',) + ATTENDANCE_FLAG_COLUMNS)


def merge_sections(rows):
    """OR-merge duplicate (contact_id, date, section) rows -> (rows, merged_groups)."""
    return _merge(rows, ('contact_id', 'date', 'section'), SECTION_CREDIT_COLUMNS)


def infer_team(contact_id, d, section_rows, attendance_rows, current_team):
    """Team for an MP-only attendance row -> (team, reason).

    1. the kid's section row on the same date; 2. the kid's nearest-dated app attendance
    row (ties go to the earlier date); 3. the current MP team (may be None = Unassigned).
    """
    for r in section_rows:
        if r['contact_id'] == contact_id and r['date'] == d and r.get('team'):
            return r['team'], 'section row same date'
    best = None
    for r in attendance_rows:
        if r['contact_id'] != contact_id or r['date'] == d or not r.get('team'):
            continue
        rank = (abs((r['date'] - d).days), r['date'])
        if best is None or rank < best[0]:
            best = (rank, r)
    if best is not None:
        return best[1]['team'], f"nearest app row {best[1]['date'].isoformat()}"
    return current_team, 'current MP team' if current_team else 'current MP team (Unassigned)'


def _mp_fields(info, synced_at):
    return {'mp_checked_in': info is not None,
            'mp_time_in': info['time_in'] if info else None,
            'mp_time_confirmed': info['time_confirmed'] if info else None,
            'mp_event_participant_id': info['ep_id'] if info else None,
            'mp_synced_at': synced_at}


def combine_with_mp(attendance_rows, section_rows, checkins, kids, names, *, keep_app_present, synced_at):
    """Merged sheet attendance + MP check-ins -> final attendance rows (sheet rows first, then MP-only).

    checkins: {date: {contact_id: {ep_id, time_in, time_confirmed}}} for the back-filled dates.
    Present = MP check-in. With keep_app_present, a legacy Present without an MP check-in
    becomes manual_present; otherwise (strict, the default) it is dropped.
    """
    out, have = [], set()
    for r in attendance_rows:
        day = checkins.get(r['date'])
        info = day.get(r['contact_id']) if day is not None else None
        manual = bool(keep_app_present and r['present'] and info is None)
        row = {**r, 'legacy_present': r['present'], **_mp_fields(info, synced_at if day is not None else None),
               'manual_present': manual,
               'manual_present_by': MANUAL_PRESENT_BY if manual else None,
               'manual_present_at': (r.get('updated_at') or synced_at) if manual else None,
               'source': 'import', 'team_reason': None,
               'created_at': r.get('created_at') or synced_at, 'updated_at': r.get('updated_at') or synced_at}
        row['present'] = row['mp_checked_in'] or manual
        out.append(row)
        have.add((r['contact_id'], r['date']))
    for d in sorted(checkins):
        for cid, info in sorted(checkins[d].items(), key=lambda kv: (kv[1]['time_in'], kv[0])):
            if (cid, d) in have:
                continue
            kid = kids[cid]
            team, reason = infer_team(cid, d, section_rows, attendance_rows, kid.get('team'))
            row = {'row': None, 'rows': [], 'name': names.get(cid) or mp_name(kid), 'contact_id': cid, 'date': d,
                   'team': team, 'team_reason': reason, 'legacy_present': False, **_mp_fields(info, synced_at),
                   'manual_present': False, 'manual_present_by': None, 'manual_present_at': None,
                   'source': 'backfill', 'created_at': info['time_in'], 'updated_at': info['time_in'],
                   'present': True}
            row.update({f: False for f in ATTENDANCE_FLAG_COLUMNS})
            out.append(row)
    return out


def gp_ended(kid, now=None):
    end = mp.parse_mp_datetime(kid.get('end_date'))
    return end is not None and end < (now or datetime.now(CHICAGO))


# =============================================================================
# MP
# =============================================================================

def fetch_mp_checkins(season_start, through, kids, schedule_dates):
    """-> ({date: {contact_id: info}} for back-filled dates, {date: stats})."""
    events = mp_checkins.fetch_event_ids_range(season_start, through, timeout=mp.SCRIPT_TIMEOUT)
    pid_to_cid = {k['participant_id']: k['contact_id'] for k in kids}
    by_date, stats = {}, {}
    for d in sorted(events):
        if d not in schedule_dates:
            stats[d] = {'event_ids': events[d], 'skipped': 'not a Schedule date'}
            continue
        rows = mp_checkins.fetch_checkins(events[d], sorted(pid_to_cid), timeout=mp.SCRIPT_TIMEOUT)
        by_date[d] = {pid_to_cid[pid]: info for pid, info in rows.items() if pid in pid_to_cid}
        stats[d] = {'event_ids': events[d], 'ignored': sum(1 for pid in rows if pid not in pid_to_cid)}
    return by_date, stats


# =============================================================================
# Database
# =============================================================================

ATTENDANCE_INSERT = """
    INSERT INTO attendance (contact_id, meeting_date, team, mp_checked_in, mp_time_in, mp_time_confirmed,
                            mp_event_participant_id, mp_synced_at, manual_present, manual_present_by,
                            manual_present_at, has_bible, wearing_shirt, has_book, did_homework, has_dues,
                            source, created_at, updated_at)
    VALUES (%(contact_id)s, %(date)s, %(team)s, %(mp_checked_in)s, %(mp_time_in)s, %(mp_time_confirmed)s,
            %(mp_event_participant_id)s, %(mp_synced_at)s, %(manual_present)s, %(manual_present_by)s,
            %(manual_present_at)s, %(has_bible)s, %(wearing_shirt)s, %(has_book)s, %(did_homework)s,
            %(has_dues)s, %(source)s, %(created_at)s, %(updated_at)s)"""

SECTION_INSERT = """
    INSERT INTO completed_sections (contact_id, meeting_date, section, team, section_complete, silver_credit,
                                    gold_credit, source, created_at, updated_at)
    VALUES (%(contact_id)s, %(date)s, %(section)s, %(team)s, %(section_complete)s, %(silver_credit)s,
            %(gold_credit)s, 'import', %(created_at)s, %(updated_at)s)"""


def upsert_teams(conn, colors):
    """Teams sheet -> teams. Sort order is fixed by fields.TEAMS. -> [(team, old_emoji, new_emoji)]."""
    current = {r['name']: r['emoji'] for r in conn.execute('SELECT name, emoji FROM teams')}
    for team, emoji in colors.items():
        conn.execute('INSERT INTO teams (name, emoji, sort_order) VALUES (%s, %s, %s) '
                     'ON CONFLICT (name) DO UPDATE SET emoji = EXCLUDED.emoji',
                     (team, emoji, TEAMS.index(team) + 1))
    return [(t, current.get(t), e) for t, e in colors.items() if current.get(t) != e]


def upsert_book_sections(conn, sections):
    """sort_order = sheet order. Sections no longer in the sheet are kept (rows may reference them), at the end."""
    conn.execute('UPDATE book_sections SET sort_order = -sort_order - 1')  # out of the way of 1..n (UNIQUE)
    conn.cursor().executemany(
        'INSERT INTO book_sections (section, sort_order) VALUES (%s, %s) '
        'ON CONFLICT (section) DO UPDATE SET sort_order = EXCLUDED.sort_order',
        [(s, i) for i, s in enumerate(sections, start=1)])
    kept = conn.execute(
        """WITH extra AS (SELECT section, row_number() OVER (ORDER BY sort_order DESC) AS n
                            FROM book_sections WHERE sort_order < 0)
           UPDATE book_sections b SET sort_order = %s + extra.n FROM extra
            WHERE b.section = extra.section RETURNING b.section""", (len(sections),)).fetchall()
    return sorted(r['section'] for r in kept)


def upsert_schedule(conn, schedule):
    existing = {r['meeting_date'] for r in conn.execute('SELECT meeting_date FROM schedule')}
    conn.cursor().executemany(
        """INSERT INTO schedule (meeting_date, theme, sections, sequence, unit, scope, key_verses, details, updated_at)
           VALUES (%(meeting_date)s, %(theme)s, %(sections)s::text[], %(sequence)s, %(unit)s, %(scope)s,
                   %(key_verses)s, %(details)s, now())
           ON CONFLICT (meeting_date) DO UPDATE SET
               theme = EXCLUDED.theme, sections = EXCLUDED.sections, sequence = EXCLUDED.sequence,
               unit = EXCLUDED.unit, scope = EXCLUDED.scope, key_verses = EXCLUDED.key_verses,
               details = EXCLUDED.details, updated_at = now()""", schedule)
    return sum(1 for s in schedule if s['meeting_date'] not in existing)


def upsert_kids(conn, kids, fetched_at):
    """Season roster -> kids (never deactivates missing kids); kids whose GP row has ended become inactive."""
    result = roster.upsert_roster(conn, kids, fetched_at=fetched_at, deactivate_missing=False)
    ended = [k['contact_id'] for k in kids if gp_ended(k)]
    if ended:
        conn.execute('UPDATE kids SET active = false, group_participant_id = NULL, synced_at = now() '
                     'WHERE contact_id = ANY(%s)', (ended,))
    return result, ended


def window_guard(conn, through):
    """Rows in the window written through the new app (a re-run after cutover would destroy them)."""
    row = conn.execute(
        """SELECT (SELECT COUNT(*) FROM attendance WHERE meeting_date <= %(t)s
                     AND (source = 'app' OR updated_by_contact_id IS NOT NULL)) AS attendance,
                  (SELECT COUNT(*) FROM completed_sections WHERE meeting_date <= %(t)s
                     AND (source = 'app' OR updated_by_contact_id IS NOT NULL)) AS sections""",
        {'t': through}).fetchone()
    return row['attendance'], row['sections']


def replace_window(conn, through, attendance_rows, section_rows):
    """Delete every row with meeting_date <= through, then insert the cleaned rows in order."""
    deleted_sections = conn.execute('DELETE FROM completed_sections WHERE meeting_date <= %s', (through,)).rowcount
    deleted_attendance = conn.execute('DELETE FROM attendance WHERE meeting_date <= %s', (through,)).rowcount
    cur = conn.cursor()
    if attendance_rows:
        cur.executemany(ATTENDANCE_INSERT, attendance_rows)
    if section_rows:
        cur.executemany(SECTION_INSERT, section_rows)
    return deleted_attendance, deleted_sections


def set_sync_state(conn, key, synced_at, detail):
    conn.execute(
        """INSERT INTO sync_state (key, synced_at, attempted_at, last_error, detail)
           VALUES (%s, %s, now(), NULL, %s)
           ON CONFLICT (key) DO UPDATE SET synced_at = EXCLUDED.synced_at, attempted_at = now(),
                                           last_error = NULL, detail = EXCLUDED.detail""",
        (key, synced_at, json.dumps(detail, default=str)))


def read_views(conn, dates=None):
    """-> ({(date, team): weekly_attendance_totals row}, {(date, team): weekly_totals row})."""
    where, params = ('WHERE meeting_date = ANY(%s)', (list(dates),)) if dates is not None else ('', ())
    wat = {(r['meeting_date'], r['team']): r
           for r in conn.execute(f'SELECT * FROM weekly_attendance_totals {where}', params)}
    wt = {(r['meeting_date'], r['team']): r for r in conn.execute(f'SELECT * FROM weekly_totals {where}', params)}
    return wat, wt


def parity_b(conn, through, attendance_rows, section_rows):
    """SQL views vs sheet_formulas over the final rows (keyed by contact_id) -> (mismatches, rows)."""
    teams = conn.execute('SELECT name, emoji FROM teams ORDER BY sort_order').fetchall()
    names, colors = [t['name'] for t in teams], {t['name']: t['emoji'] for t in teams}
    dates = [r['meeting_date'] for r in
             conn.execute('SELECT meeting_date FROM schedule WHERE meeting_date <= %s ORDER BY 1', (through,))]
    att = sheet_formulas.attendance_totals(attendance_rows, dates, names, key='contact_id', team_colors=colors)
    wt = sheet_formulas.weekly_totals(att, section_rows, dates, names, team_colors=colors)
    view_att, view_wt = read_views(conn, dates)
    mismatches = compare_totals('weekly_attendance_totals', att, view_att, WAT_COLUMNS, both_ways=True)
    mismatches += compare_totals('weekly_totals', wt, view_wt, WT_COLUMNS, both_ways=True)
    return mismatches, len(att) + len(wt)


# =============================================================================
# Report
# =============================================================================

class Report:
    def __init__(self):
        self.lines = []

    def section(self, title):
        self.lines += ['', f'## {title}', '']

    def add(self, *lines):
        self.lines.extend(lines)

    def bullets(self, items, empty='none'):
        items = list(items)
        self.lines.extend([f'- {i}' for i in items] if items else [f'- {empty}'])

    def table(self, headers, rows):
        self.lines.append('| ' + ' | '.join(headers) + ' |')
        self.lines.append('|' + '---|' * len(headers))
        for r in rows:
            self.lines.append('| ' + ' | '.join('' if c is None else str(c) for c in r) + ' |')

    def text(self):
        return '\n'.join(self.lines).strip() + '\n'


def _date_counts(rows):
    counts = defaultdict(int)
    for r in rows:
        counts[r['date']] += 1
    return ', '.join(f'{d.isoformat()}: {n}' for d, n in sorted(counts.items()))


def report_sheet(report, sheet):
    report.section('Sheet')
    sched = sheet['schedule']
    lines = [f"Book Sections: {len(sheet['book_sections'])}",
             f"Schedule: {len(sched)} dates ({sched[0]['meeting_date']} to {sched[-1]['meeting_date']})"]
    if 'roster' in sheet:
        lines += [f"Master Roster: {len(sheet['roster'])} kids",
                  f"Attendance Entries RAW: {len(sheet['attendance'])} rows ({_date_counts(sheet['attendance'])})",
                  f"Completed Sections RAW: {len(sheet['sections'])} rows ({_date_counts(sheet['sections'])})",
                  f"Weekly Attendance Totals / Weekly Totals: {len(sheet['wat'])} / {len(sheet['wt'])} rows",
                  f"{LOAD_TEST_SHEET}: {sheet['load_test_rows']} rows skipped"]
    report.bullets(lines)


def report_parity(report, title, mismatches, compared):
    report.section(title)
    if not mismatches:
        report.add(f'0 mismatches across {compared} totals rows (every column).')
        return
    report.add(f'**{len(mismatches)} mismatches** across {compared} totals rows:', '')
    report.table(['Sheet/view', 'Date', 'Team', 'Column', 'Expected', 'Got'],
                 [[m['sheet'], m['date'], m['team'], m['column'], m['expected'], m['actual']]
                  for m in mismatches[:100]])


def report_matching(report, args, kids, excluded, matches, info, sheet, kids_by_id, ended, att, sec):
    report.section(f'Matching (MP Group {roster.roster_group_id()}, Start_Date >= {args.season_start})')
    first_names = {name: kids_by_id[cid]['first_name'] for name, cid in matches.items()}
    nicknames = {name: kids_by_id[cid].get('nickname') for name, cid in matches.items()}
    diffs = []
    for r in sheet['roster']:
        mp_team = kids_by_id[matches[r['name']]].get('team')
        if (r['group'] or None) != mp_team:
            diffs.append(f"{r['name']}: sheet {r['group'] or '(blank)'}, MP {mp_team or 'Unassigned'}")
    group = {r['name']: r['group'] for r in sheet['roster']}
    raw_diffs = {label: [r['row'] for r in rows if r['team'] != group[r['name']]]
                 for label, rows in (('Attendance', att), ('Sections', sec))}
    report.bullets([
        f'MP season kids (kid roles): {len(kids)}; non-kid participants excluded: {len(excluded)}',
        f"Sheet kids matched uniquely: {len(matches)}/{len(sheet['roster'])}",
        f"Matched on Nickname only (MP First_Name differs): {len(info['nickname_only'])}"
        + ''.join(f'\n  - {n} (First_Name {first_names[n]})' for n in info['nickname_only']),
        f"Matched on First_Name only (MP Nickname differs): {len(info['first_name_only'])}"
        + ''.join(f'\n  - {n} (Nickname {nicknames[n]})' for n in info['first_name_only']),
        f"In MP but not on the sheet: {len(info['mp_not_in_sheet'])}"
        + ''.join(f"\n  - {mp_name(k)} (contact {k['contact_id']}, {k.get('team') or 'Unassigned'})"
                  for k in info['mp_not_in_sheet']),
        f'Sheet Group vs MP team differences: {len(diffs)}' + ''.join(f'\n  - {d}' for d in diffs),
        'RAW rows whose Team differs from the Master Roster Group: '
        + '; '.join(f'{label} {len(rows)}' + (f' (rows {rows})' if rows else '') for label, rows in raw_diffs.items()),
        f'Season GP rows that have ended (kid marked inactive): {len(ended)}',
    ])


def report_cleaning(report, moves, groups):
    report.section('Re-dated rows (forward-only UTC fix)')
    if moves:
        report.table(['Sheet', 'Row', 'Kid', 'Section', 'From', 'To', 'Timestamp (Chicago)'],
                     [[label, m['row'], m['name'], m.get('section') or '', m['from'], m['to'],
                       m['local_ts'].strftime('%Y-%m-%d %H:%M')] for label, m in moves])
        report.add('', f'{len(moves)} rows re-dated.')
    else:
        report.add('None.')
    report.section('Merged duplicates (OR of every flag)')
    if groups:
        report.table(['Sheet', 'Rows', 'Kid', 'Date', 'Section', 'Team conflict'],
                     [[label, ', '.join(map(str, g['rows'])), g['name'], g['date'], g.get('section') or '',
                       ' / '.join(g['team_conflict']) if g['team_conflict'] else '']
                      for label, g in groups])
        report.add('', f'{len(groups)} groups merged.')
    else:
        report.add('None.')


def report_backfill(report, args, final, checkins, stats, window_dates):
    mode = 'kept as manual_present (--keep-app-present)' if args.keep_app_present else 'dropped (strict MP Present)'
    report.section('MP check-in back-fill')
    report.add(f'Present = MP check-in. Sheet Present without an MP check-in is {mode}.', '')
    dates = sorted(set(window_dates) | set(stats))
    by_date = defaultdict(list)
    for r in final:
        by_date[r['date']].append(r)
    table, details = [], []
    for d in dates:
        rows, st = by_date.get(d, []), stats.get(d, {})
        sheet_rows = [r for r in rows if r['source'] == 'import']
        added = sorted((r for r in rows if r['source'] == 'backfill'), key=lambda r: r['name'])
        no_mp = sorted((r['name'] for r in sheet_rows if r['legacy_present'] and not r['mp_checked_in']))
        mp_not_legacy = sum(1 for r in sheet_rows if r['mp_checked_in'] and not r['legacy_present'])
        events = ', '.join(map(str, st.get('event_ids', []))) or 'none'
        if st.get('skipped'):
            events += f" ({st['skipped']})"
        table.append([d, events, len(checkins[d]) if d in checkins else '-', len(sheet_rows),
                      sum(1 for r in sheet_rows if r['legacy_present']), len(added), len(no_mp), mp_not_legacy,
                      st.get('ignored', '-')])
        notes = []
        if d not in checkins and any(r['legacy_present'] for r in sheet_rows):
            notes.append('**WARNING**: no MP check-in event; sheet Present kids are not present after import')
        elif d in checkins and not checkins[d] and sheet_rows:
            notes.append('**WARNING**: MP returned 0 check-ins for this date')
        if added:
            notes.append(f'MP-only additions ({len(added)}): ' + ', '.join(
                f"{r['name']} [{r['team'] or 'Unassigned'}: {r['team_reason']}]" for r in added))
        if no_mp:
            notes.append(f'Sheet Present without MP check-in ({len(no_mp)}, {mode}): ' + ', '.join(no_mp))
        if notes:
            details.append((d, notes))
    report.table(['Date', 'MP events', 'MP kids', 'Sheet rows', 'Sheet Present', 'MP-only added',
                  'Present w/o MP', 'MP w/o sheet Present', 'Non-kid check-ins ignored'], table)
    for d, notes in details:
        report.add('', f'**{d}**', '')
        report.bullets(notes)


def _ba_value(value):
    if isinstance(value, str) and value.startswith(sheet_formulas.TOTAL_POINTS_PREFIX):
        value = value[len(sheet_formulas.TOTAL_POINTS_PREFIX):]
        return int(value) if value.lstrip('-').isdigit() else value
    return value


def before_after(sheet, after_wat, after_wt, team_order):
    """-> (table rows, changed cell count). Before = sheet cached values, after = SQL views."""
    src = {'before': {'wat': sheet['wat'], 'wt': sheet['wt']}, 'after': {'wat': after_wat, 'wt': after_wt}}
    keys = set(sheet['wat']) | set(after_wat)
    rows, changed = [], 0
    for d, t in sorted(keys, key=lambda k: (k[0], team_order.get(k[1], 99), str(k[1]))):
        vals = {side: [_ba_value((src[side][s].get((d, t)) or {}).get(col)) for _, s, col in BEFORE_AFTER]
                for side in src}
        if not any(v not in (None, 0) for v in vals['before'] + vals['after']):
            continue
        cells = []
        for b, a in zip(vals['before'], vals['after']):
            if values_equal(b, a):
                cells.append(a)
            else:
                changed += 1
                cells.append(f"{'—' if b is None else b} → **{'—' if a is None else a}**")
        rows.append([d, t] + cells)
    return rows, changed


# =============================================================================
# Run
# =============================================================================

def database_url():
    url = os.environ.get('DATABASE_URL_UNPOOLED')
    if not url:
        raise ImportAbort('DATABASE_URL_UNPOOLED is not set (owner role, direct host)')
    if '-pooler' in url:
        raise ImportAbort('DATABASE_URL_UNPOOLED must be the direct host, not -pooler')
    return url


def finish(args):
    """Last step inside the transaction: roll back unless --apply."""
    if not args.apply:
        raise psycopg.Rollback()


def run_schedule_only(url, sheet, args, report):
    with db.connect(url) as conn:
        with conn.transaction():
            kept = upsert_book_sections(conn, sheet['book_sections'])
            new = upsert_schedule(conn, sheet['schedule'])
            report.section('Database')
            report.bullets([f"book_sections: {len(sheet['book_sections'])} upserted"
                            + (f'; kept (not in sheet): {kept}' if kept else ''),
                            f"schedule: {len(sheet['schedule'])} upserted ({new} new)"])
            finish(args)


def run_import(args, report):
    url = database_url()
    sheet = load_workbook(args.xlsx, schedule_only=args.schedule_only)
    report_sheet(report, sheet)
    if args.schedule_only:
        return run_schedule_only(url, sheet, args, report)
    through = args.through

    mismatches, compared = parity_a(sheet)
    report_parity(report, 'Parity A: Python formulas vs the sheet\'s cached totals', mismatches, compared)
    if mismatches:
        raise ImportAbort(f'parity A: {len(mismatches)} mismatches; the formula port is wrong or the export is stale')

    fetched_at = datetime.now(timezone.utc)
    kids, excluded = roster.fetch_roster(since=args.season_start, timeout=mp.SCRIPT_TIMEOUT)
    if not kids:
        raise ImportAbort('MP returned no season kids (check the API client permissions and --season-start)')
    kids_by_id = {k['contact_id']: k for k in kids}
    matches, info = match_kids([r['name'] for r in sheet['roster']], kids)
    att = attach_contacts(sheet['attendance'], matches, 'Attendance Entries RAW')
    sec = attach_contacts(sheet['sections'], matches, 'Completed Sections RAW')
    ended = [k['contact_id'] for k in kids if gp_ended(k)]
    report_matching(report, args, kids, excluded, matches, info, sheet, kids_by_id, ended, att, sec)

    schedule_dates = {s['meeting_date'] for s in sheet['schedule']}
    att, att_moves = redate_rows(att, schedule_dates)
    sec, sec_moves = redate_rows(sec, schedule_dates)
    check_dates(att, schedule_dates, through, 'Attendance Entries RAW')
    check_dates(sec, schedule_dates, through, 'Completed Sections RAW')
    att, att_groups = merge_attendance(att)
    sec, sec_groups = merge_sections(sec)
    report_cleaning(report, [('Attendance', m) for m in att_moves] + [('Sections', m) for m in sec_moves],
                    [('Attendance', g) for g in att_groups] + [('Sections', g) for g in sec_groups])

    checkins, stats = fetch_mp_checkins(args.season_start, through, kids, schedule_dates)
    if not any(checkins.values()) and any(r['present'] for r in att) and not args.force:
        raise ImportAbort('MP returned no check-ins for any date in the window (permissions?); --force to proceed')
    synced_at = datetime.now(timezone.utc)
    for r in sec:
        r['created_at'] = r['created_at'] or synced_at
        r['updated_at'] = r['updated_at'] or synced_at
    names = {cid: name for name, cid in matches.items()}
    final = combine_with_mp(att, sec, checkins, kids_by_id, names, keep_app_present=args.keep_app_present,
                            synced_at=synced_at)
    window_dates = sorted(d for d in schedule_dates if args.season_start <= d <= through)
    report_backfill(report, args, final, checkins, stats, window_dates)

    with db.connect(url) as conn:
        with conn.transaction():
            emoji_changes = upsert_teams(conn, sheet['teams'])
            kept = upsert_book_sections(conn, sheet['book_sections'])
            new_dates = upsert_schedule(conn, sheet['schedule'])
            kid_result, ended = upsert_kids(conn, kids, fetched_at)
            app_att, app_sec = window_guard(conn, through)
            if (app_att or app_sec) and not args.force:
                raise ImportAbort(f'the window (meeting_date <= {through}) holds {app_att} attendance and {app_sec} '
                                  'section rows written through the app; --force deletes them')
            deleted_att, deleted_sec = replace_window(conn, through, final, sec)
            set_sync_state(conn, roster.ROSTER_KEY, fetched_at,
                           {**kid_result, 'deactivated': len(ended), 'excluded_pids': excluded, 'source': 'import'})
            for d, day in checkins.items():
                set_sync_state(conn, mp_checkins.key_for(d), synced_at,
                               {'event_ids': stats[d]['event_ids'], 'events_checked_at': synced_at.isoformat(),
                                'counts': {'checked_in': len(day)}, 'source': 'import'})
            report.section('Database')
            report.bullets([
                f"teams: {len(sheet['teams'])}; emoji changes: "
                + (', '.join(f'{t} {o} -> {n}' for t, o, n in emoji_changes) or 'none'),
                f"book_sections: {len(sheet['book_sections'])} upserted"
                + (f'; kept (not in sheet): {kept}' if kept else ''),
                f"schedule: {len(sheet['schedule'])} upserted ({new_dates} new)",
                f"kids: {kid_result['kids']} upserted; {len(ended)} marked inactive (GP row ended)",
                f'window guard (meeting_date <= {through}): {app_att} attendance / {app_sec} section rows written '
                'through the app' + (' (deleted: --force)' if app_att or app_sec else ''),
                f'deleted: {deleted_att} attendance, {deleted_sec} completed_sections',
                f"inserted: {len(final)} attendance ({sum(r['source'] == 'import' for r in final)} import, "
                f"{sum(r['source'] == 'backfill' for r in final)} backfill), {len(sec)} completed_sections",
                'sync_state: ' + ', '.join([roster.ROSTER_KEY] + [mp_checkins.key_for(d) for d in sorted(checkins)]),
            ])

            team_order = {t: i for i, t in enumerate(TEAMS)}
            after_wat, after_wt = read_views(conn)
            rows, changed = before_after(sheet, after_wat, after_wt, team_order)
            report.section('Before / after (before = sheet cached totals, after = SQL views)')
            report.add(f'{changed} changed cells (bold); rows where everything is 0 are omitted.', '')
            report.table(['Date', 'Team'] + [label for label, _, _ in BEFORE_AFTER], rows)

            mismatches, compared = parity_b(conn, through, final, sec)
            report_parity(report, 'Parity B: SQL views vs Python formulas over the imported rows', mismatches,
                          compared)
            if mismatches:
                raise ImportAbort(f'parity B: {len(mismatches)} mismatches between the SQL views and the reference')
            finish(args)


def check_report_path(path):
    """The report holds kids' names; refuse to write it inside the repo (import_reports/ is git-ignored)."""
    if path is None:
        return
    resolved = str(path.expanduser().resolve()).casefold()   # macOS paths are case-insensitive
    root = str(ROOT).casefold()
    inside = resolved == root or resolved.startswith(root + os.sep)
    if inside and not resolved.startswith(str(ROOT / 'import_reports').casefold() + os.sep):
        sys.exit("--report must be outside the repo or under import_reports/ (it contains kids' names)")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='Import the legacy TNT sheet export (dry run unless --apply).')
    parser.add_argument('--xlsx', required=True, type=Path, help='the exported workbook')
    parser.add_argument('--through', type=date.fromisoformat,
                        help='last meeting date to import (YYYY-MM-DD); every row on or before it is replaced')
    parser.add_argument('--season-start', type=date.fromisoformat, default=SEASON_START,
                        help=f'first date of the MP roster/check-in season (default {SEASON_START})')
    parser.add_argument('--apply', action='store_true', help='commit (default: dry run, rolled back)')
    parser.add_argument('--report', type=Path, help="also write the markdown report here (contains kids' names)")
    parser.add_argument('--keep-app-present', action='store_true',
                        help='legacy Present without an MP check-in becomes manual_present (default: dropped)')
    parser.add_argument('--force', action='store_true',
                        help='replace app-written rows in the window; proceed when MP returns no check-ins')
    parser.add_argument('--schedule-only', action='store_true', help='only upsert Schedule and Book Sections')
    args = parser.parse_args(argv)
    if not args.schedule_only and args.through is None:
        parser.error('--through is required (unless --schedule-only)')
    return args


def main(argv=None):
    args = parse_args(argv)
    check_report_path(args.report)
    report = Report()
    report.add(f"# TNT sheet import ({'APPLY' if args.apply else 'DRY RUN'})", '')
    report.bullets([f'xlsx: {args.xlsx.name}',
                    'mode: schedule only' if args.schedule_only else
                    f'through: {args.through}; season start: {args.season_start}; '
                    f"present: {'keep app Present' if args.keep_app_present else 'strict MP'}"
                    + ('; --force' if args.force else ''),
                    f"run at: {datetime.now(CHICAGO):%Y-%m-%d %H:%M %Z}"])
    code = 0
    try:
        run_import(args, report)
        report.section('Result')
        report.add('**COMMITTED**' if args.apply else '**ROLLED BACK** (dry run). Re-run with --apply to commit.')
    except ImportAbort as e:
        report.section('Result')
        report.add(f'**ABORTED**: {e}', '', 'Nothing was committed.')
        code = 1
    except (mp.MPError, DataError, psycopg.Error) as e:
        report.section('Result')
        report.add(f'**FAILED** ({type(e).__name__}): {e}', '', 'Nothing was committed.')
        code = 1
    text = report.text()
    sys.stdout.write(text)
    if args.report:
        path = args.report.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o600)
    return code


if __name__ == '__main__':
    sys.exit(main())
