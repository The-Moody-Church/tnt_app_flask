"""
Pure-function tests for scripts/import_sheet.py and scripts/sheet_formulas.py.

Synthetic names only. The real-export parity test is skipped unless ~/Downloads/TNT_App_Data.xlsx
exists, and it reports counts only (never kids' names).
"""
from datetime import date, datetime, timezone
from pathlib import Path

import openpyxl
import pytest

from scripts import import_sheet as imp
from scripts import sheet_formulas as sf

TEAMS = ['Red', 'Blue', 'Green', 'Yellow']
D1, D2, D3 = date(2026, 9, 16), date(2026, 9, 23), date(2026, 9, 30)
REAL_XLSX = Path.home() / 'Downloads' / 'TNT_App_Data.xlsx'


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def att(name, team='Red', d=D1, present=True, **flags):
    row = {'name': name, 'team': team, 'date': d, 'present': present}
    for f in ('has_bible', 'wearing_shirt', 'has_book', 'did_homework', 'has_dues'):
        row[f] = flags.get(f, False)
    return row


def sec(name, team='Red', d=D1, complete=False, silver=False, gold=False):
    return {'name': name, 'team': team, 'date': d, 'section_complete': complete, 'silver_credit': silver,
            'gold_credit': gold}


def totals(att_rows, sec_rows, dates=(D1, D2)):
    a = sf.attendance_totals(att_rows, dates, TEAMS)
    return a, sf.weekly_totals(a, sec_rows, dates, TEAMS)


# =============================================================================
# sheet_formulas
# =============================================================================

def test_attendance_counts_distinct_kids_and_ors_flags_across_duplicates():
    a, _ = totals([att('Kid Alpha', has_bible=True), att('Kid Alpha', wearing_shirt=True),
                   att('Kid Alpha', has_bible=True)], [])
    red = a[(D1, 'Red')]
    assert red['kids_present'] == 1
    assert (red['total_bibles'], red['total_shirts'], red['total_workbooks']) == (1, 1, 0)
    assert red['attendance_points'] == 3
    assert red['points_per_kid'] == 3.0


def test_flags_count_without_present():
    a, w = totals([att('Kid Bravo', present=False, has_dues=True, has_book=True)], [])
    red = a[(D1, 'Red')]
    assert red['kids_present'] == 0
    assert (red['total_dues'], red['total_workbooks']) == (1, 1)
    assert red['attendance_points'] == 2
    assert red['points_per_kid'] == 0                        # IFERROR(J/0, 0)
    assert w[(D1, 'Red')]['team_total_points'] == 'Total Points : 0'


def test_grouped_by_row_team_and_grid_only():
    a, w = totals([att('Kid Alpha', team='Red'), att('Kid Alpha', team='Blue'),
                   att('Kid Charlie', team=None), att('Kid Delta', d=date(2026, 1, 1))],
                  [sec('Kid Charlie', team=None, complete=True)])
    assert a[(D1, 'Red')]['kids_present'] == 1
    assert a[(D1, 'Blue')]['kids_present'] == 1
    assert sum(r['kids_present'] for r in a.values()) == 2  # NULL team and off-grid dates are not counted
    assert sum(r['sections_completed'] for r in w.values()) == 0


def test_sections_are_row_counts_not_distinct():
    _, w = totals([att('Kid Alpha')], [sec('Kid Alpha', complete=True), sec('Kid Alpha', complete=True),
                                       sec('Kid Alpha', silver=True, gold=True)])
    red = w[(D1, 'Red')]
    assert (red['sections_completed'], red['silvers_completed'], red['golds_completed']) == (2, 1, 1)
    assert red['section_points'] == 8
    assert red['attendance_points'] == 1
    assert red['total_points'] == 9
    assert red['number_of_kids'] == 1
    assert red['team_total_points'] == 'Total Points : 9000'


def test_silver_on_a_later_date_counts_on_that_date():
    _, w = totals([], [sec('Kid Alpha', d=D1, complete=True), sec('Kid Alpha', d=D2, silver=True)])
    assert (w[(D1, 'Red')]['sections_completed'], w[(D1, 'Red')]['silvers_completed']) == (1, 0)
    assert (w[(D2, 'Red')]['sections_completed'], w[(D2, 'Red')]['silvers_completed']) == (0, 1)


def test_zero_kids_gives_zero_and_total_points_zero():
    _, w = totals([], [])
    row = w[(D2, 'Yellow')]
    assert row['total_points'] == 0
    assert row['points_per_kid'] == 0
    assert row['team_total_points'] == 'Total Points : 0'


def test_team_total_points_is_per_kid_times_1000_with_float_math():
    assert sf.team_total_points(166, 16) == 'Total Points : 10375'
    # 323/20 = 16.15, but 16.15*1000 is 16149.999999999998 in IEEE doubles, as in Sheets (integer math says 16150)
    assert sf.team_total_points(323, 20) == 'Total Points : 16149'
    assert sf.team_total_points(105, 18) == 'Total Points : 5833'
    assert sf.team_total_points(7, 0) == 'Total Points : 0'


def test_team_colors_and_output_keys_match_the_views():
    a = sf.attendance_totals([], [D1], TEAMS, team_colors={'Red': 'R'})
    w = sf.weekly_totals(a, [], [D1], TEAMS, team_colors={'Red': 'R'})
    assert a[(D1, 'Red')]['team_color'] == 'R'
    assert set(imp.WAT_COLUMNS) <= set(a[(D1, 'Red')])
    assert set(imp.WT_COLUMNS) <= set(w[(D1, 'Red')])


def test_contact_id_key():
    a = sf.attendance_totals([dict(att('x'), contact_id=1), dict(att('y'), contact_id=1)], [D1], TEAMS,
                             key='contact_id')
    assert a[(D1, 'Red')]['kids_present'] == 1


# =============================================================================
# Cell parsing
# =============================================================================

@pytest.mark.parametrize('value, expected', [
    (1.1, '1.1'), (1.2000000000000002, '1.2'), (2.0, '2'), (3, '3'), (' Start Zone 1 ', 'Start Zone 1'),
    ('4.8', '4.8'),
])
def test_normalize_section(value, expected):
    assert imp.normalize_section(value) == expected


@pytest.mark.parametrize('value', [None, '', '   ', True])
def test_normalize_section_rejects_blank(value):
    with pytest.raises(ValueError):
        imp.normalize_section(value)


@pytest.mark.parametrize('value, expected', [
    ('Start Zone 1\nStart Zone 2', ['Start Zone 1', 'Start Zone 2']),
    ('1.5\n1.6', ['1.5', '1.6']),
    ('1.5\r\n\n 1.6 ', ['1.5', '1.6']),
    (' -- ', []),
    ('--\n2.1', ['2.1']),
    (1.1, ['1.1']),
    (None, []),
    ('', []),
])
def test_parse_schedule_sections(value, expected):
    assert imp.parse_schedule_sections(value) == expected


def test_to_bool_and_timestamps():
    assert imp.to_bool(True) is True and imp.to_bool(0) is False and imp.to_bool(None) is False
    assert imp.to_bool('TRUE') is True and imp.to_bool('false') is False
    with pytest.raises(imp.ImportAbort):
        imp.to_bool('yes')
    assert imp.sheet_ts(datetime(2026, 9, 17, 0, 30)) == utc(2026, 9, 17, 0, 30)   # naive = UTC
    assert imp.sheet_ts(None) is None
    assert imp.to_date(datetime(2026, 9, 16)) == D1
    with pytest.raises(imp.ImportAbort):
        imp.to_date(datetime(2026, 9, 16, 12, 0))


# =============================================================================
# Re-dating, merging
# =============================================================================

SCHEDULE = {date(2026, 9, 9), D1, D2, D3, date(2026, 11, 18), date(2026, 11, 25)}


def raw(row, d, ts, name='Kid Alpha', contact_id=1, team='Red', **fields):
    return {'row': row, 'name': name, 'contact_id': contact_id, 'team': team, 'date': d, 'ts': ts, **fields}


def test_redate_moves_rows_dated_after_their_chicago_timestamp():
    rows, moves = imp.redate_rows([raw(7, D2, utc(2026, 9, 17, 0, 30))], SCHEDULE)   # 7:30pm CDT on 9/16
    assert rows[0]['date'] == D1 and rows[0]['redated_from'] == D2
    assert [(m['row'], m['from'], m['to']) for m in moves] == [(7, D2, D1)]


def test_redate_handles_standard_time():
    rows, _ = imp.redate_rows([raw(1, date(2026, 11, 25), utc(2026, 11, 19, 1, 30))], SCHEDULE)  # 7:30pm CST
    assert rows[0]['date'] == date(2026, 11, 18)


@pytest.mark.parametrize('d, ts', [
    (D1, utc(2026, 9, 17, 0, 30)),        # already on its Chicago date
    (D1, utc(2026, 9, 24, 1, 0)),         # back-dated edit a week later: never moved
    (D2, utc(2026, 9, 20, 15, 0)),        # entered on a Sunday (not a meeting): not moved
    (D2, None),                           # no timestamp
])
def test_redate_leaves_other_rows(d, ts):
    rows, moves = imp.redate_rows([raw(1, d, ts)], SCHEDULE)
    assert rows[0]['date'] == d and not moves


def test_redate_does_not_mutate_input():
    original = raw(1, D2, utc(2026, 9, 17, 0, 30))
    imp.redate_rows([original], SCHEDULE)
    assert original['date'] == D2


def test_check_dates_aborts_on_non_schedule_and_after_through():
    with pytest.raises(imp.ImportAbort, match='not on a Schedule date'):
        imp.check_dates([raw(5, date(2026, 9, 17), None)], SCHEDULE, D3, 'X')
    with pytest.raises(imp.ImportAbort, match='after --through'):
        imp.check_dates([raw(5, D3, None)], SCHEDULE, D2, 'X')


def flags(**on):
    base = {f: False for f in ('present', 'has_bible', 'wearing_shirt', 'has_book', 'did_homework', 'has_dues')}
    return {**base, **on}


def test_merge_attendance_ors_flags_and_keeps_earliest_team():
    rows = [raw(11, D1, utc(2026, 9, 17, 0, 5), **flags(present=True, has_bible=True)),
            raw(12, D1, None, contact_id=2, name='Kid Bravo', **flags()),
            raw(29, D1, utc(2026, 9, 17, 0, 40), team='Blue', **flags(has_dues=True)),
            raw(30, D2, utc(2026, 9, 24, 0, 1), **flags(present=True))]
    merged, groups = imp.merge_attendance(rows)
    assert [m['row'] for m in merged] == [11, 12, 30]                   # sheet order of the first row
    first = merged[0]
    assert first['rows'] == [11, 29]
    assert first['present'] and first['has_bible'] and first['has_dues'] and not first['has_book']
    assert first['team'] == 'Red' and first['team_conflict'] == ['Blue', 'Red']
    assert first['created_at'] == utc(2026, 9, 17, 0, 5) and first['updated_at'] == utc(2026, 9, 17, 0, 40)
    assert merged[1]['created_at'] is None                              # no timestamp at all
    assert [g['rows'] for g in groups] == [[11, 29]]


def test_merge_sections_keys_on_section():
    credits = {'section_complete': True, 'silver_credit': False, 'gold_credit': False}
    rows = [raw(55, D1, utc(2026, 9, 17, 0, 1), section='Start Zone 2', **credits),
            raw(57, D1, utc(2026, 9, 17, 0, 2), section='Start Zone 2', **dict(credits, gold_credit=True)),
            raw(58, D1, utc(2026, 9, 17, 0, 3), section='1.1', **credits)]
    merged, groups = imp.merge_sections(rows)
    assert len(merged) == 2 and len(groups) == 1
    assert merged[0]['rows'] == [55, 57] and merged[0]['gold_credit'] and merged[0]['team_conflict'] is None


# =============================================================================
# Kid matching
# =============================================================================

def kid(cid, first, last, nickname=None, team='Red'):
    return {'contact_id': cid, 'participant_id': cid + 1000, 'first_name': first, 'last_name': last,
            'nickname': nickname, 'team': team}


def test_match_by_nickname_first_name_and_case_whitespace():
    kids = [kid(1, 'Zedekiah', 'Testkid', nickname='Zed'), kid(2, 'Robin', 'Sample'),
            kid(3, 'Novella', 'Example', nickname='Vel'), kid(4, 'Newt', 'Walkin', team=None)]
    matches, info = imp.match_kids(['Zed Testkid', ' robin  SAMPLE', 'Novella Example'], kids)
    assert matches == {'Zed Testkid': 1, ' robin  SAMPLE': 2, 'Novella Example': 3}
    assert info['nickname_only'] == ['Zed Testkid']
    assert info['first_name_only'] == ['Novella Example']                 # the fallback index
    assert [k['contact_id'] for k in info['mp_not_in_sheet']] == [4]


def test_match_aborts_on_ambiguous_names():
    kids = [kid(1, 'Sam', 'Twin'), kid(2, 'Samuel', 'Twin', nickname='Sam')]
    with pytest.raises(imp.ImportAbort, match='1 ambiguous'):
        imp.match_kids(['Sam Twin'], kids)


def test_match_aborts_when_fallback_names_a_different_kid():
    kids = [kid(1, 'Samantha', 'Lee', nickname='Sam'), kid(2, 'Sam', 'Lee', nickname='Sammy')]
    with pytest.raises(imp.ImportAbort, match='ambiguous'):
        imp.match_kids(['Sam Lee'], kids)


def test_match_aborts_on_unmatched_and_on_shared_kid():
    kids = [kid(1, 'Quinella', 'Sample', nickname='Quin')]
    with pytest.raises(imp.ImportAbort, match='1 not in MP'):
        imp.match_kids(['Nobody Here'], kids)
    with pytest.raises(imp.ImportAbort, match='same MP kid'):
        imp.match_kids(['Quin Sample', 'Quinella Sample'], kids)


def test_attach_contacts_validates_roster_and_team():
    matches = {'Kid Alpha': 1}
    rows = imp.attach_contacts([{'row': 2, 'name': 'kid alpha ', 'team': ' Red'}], matches, 'X')
    assert rows[0]['contact_id'] == 1 and rows[0]['name'] == 'Kid Alpha' and rows[0]['team'] == 'Red'
    with pytest.raises(imp.ImportAbort, match=r'rows \[3\] name a kid'):
        imp.attach_contacts([{'row': 3, 'name': 'Kid Omega', 'team': 'Red'}], matches, 'X')
    with pytest.raises(imp.ImportAbort, match=r'rows \[4\] have a Team'):
        imp.attach_contacts([{'row': 4, 'name': 'Kid Alpha', 'team': 'Purple'}], matches, 'X')


# =============================================================================
# Back-fill
# =============================================================================

def test_infer_team_prefers_same_date_section_row():
    sections = [{'contact_id': 1, 'date': D1, 'team': 'Green'}]
    attendance = [{'contact_id': 1, 'date': D2, 'team': 'Blue'}]
    assert imp.infer_team(1, D1, sections, attendance, 'Red') == ('Green', 'section row same date')


def test_infer_team_nearest_app_row_ties_go_earlier():
    attendance = [{'contact_id': 1, 'date': date(2026, 9, 9), 'team': 'Yellow'},
                  {'contact_id': 1, 'date': D2, 'team': 'Blue'},
                  {'contact_id': 2, 'date': D1, 'team': 'Red'}]
    team, reason = imp.infer_team(1, D1, [], attendance, 'Red')        # 7 days either side
    assert team == 'Yellow' and '2026-09-09' in reason
    assert imp.infer_team(1, D3, [], attendance, 'Red')[0] == 'Blue'  # nearest


def test_infer_team_falls_back_to_current_mp_team():
    assert imp.infer_team(1, D1, [], [], 'Red') == ('Red', 'current MP team')
    assert imp.infer_team(1, D1, [], [], None) == (None, 'current MP team (Unassigned)')


def sheet_att(contact_id, d, present, row, name):
    return {**flags(present=present), 'row': row, 'rows': [row], 'name': name, 'contact_id': contact_id,
            'date': d, 'team': 'Red', 'created_at': None, 'updated_at': utc(2026, 9, 17, 0, 1)}


def checkin(minute):
    return {'ep_id': 900 + minute, 'time_in': datetime(2026, 9, 16, 18, minute, tzinfo=imp.CHICAGO),
            'time_confirmed': None}


@pytest.mark.parametrize('keep', [False, True])
def test_combine_with_mp(keep):
    synced = utc(2026, 9, 27, 12, 0)
    attendance = [sheet_att(1, D1, True, 2, 'Kid Alpha'), sheet_att(2, D1, True, 3, 'Kid Bravo'),
                  sheet_att(1, D2, True, 4, 'Kid Alpha')]
    kids = {1: kid(1, 'Kid', 'Alpha'), 2: kid(2, 'Kid', 'Bravo'), 3: kid(3, 'Newt', 'Walkin', team=None)}
    rows = imp.combine_with_mp(attendance, [], {D1: {1: checkin(5), 3: checkin(9)}}, kids, {1: 'Kid Alpha'},
                               keep_app_present=keep, synced_at=synced)
    alpha, bravo, alpha_d2, walkin = rows
    assert alpha['mp_checked_in'] and alpha['present'] and not alpha['manual_present']
    assert alpha['source'] == 'import' and alpha['mp_event_participant_id'] == 905 and alpha['mp_synced_at'] == synced
    assert alpha['created_at'] == synced                           # no sheet timestamp -> import time
    assert not bravo['mp_checked_in']
    assert bravo['manual_present'] is keep and bravo['present'] is keep
    assert (bravo['manual_present_by'] is not None) is keep
    assert alpha_d2['mp_synced_at'] is None                        # date not back-filled
    assert alpha_d2['present'] is keep
    assert walkin['source'] == 'backfill' and walkin['present'] and walkin['team'] is None
    assert walkin['name'] == 'Newt Walkin' and not any(walkin[f] for f in imp.ATTENDANCE_FLAG_COLUMNS)


def test_gp_ended():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=imp.CHICAGO)
    assert imp.gp_ended({'end_date': '2026-09-20T00:00:00'}, now)
    assert not imp.gp_ended({'end_date': None}, now)
    assert not imp.gp_ended({'end_date': '2026-12-31T00:00:00'}, now)


# =============================================================================
# Workbook loading and parity A
# =============================================================================

def _sheet(wb, name, header, rows):
    ws = wb.create_sheet(name)
    ws.append(header)
    for r in rows:
        ws.append(r)
    return ws


def build_workbook(path, *, red_total_points=8):
    """A tiny export: one meeting with data, hand-computed cached totals."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    d1, d2 = datetime(2026, 9, 16), datetime(2026, 9, 23)
    ts = datetime(2026, 9, 17, 0, 30)
    colors = {'Red': 'R', 'Blue': 'B', 'Green': 'G', 'Yellow': 'Y'}
    _sheet(wb, 'Master Roster', imp.HEADERS['Master Roster'],
           [['Kid Alpha', 'Alpha', 'Kid', 'Red'], ['Kid Bravo', 'Bravo', 'Kid', 'Red'],
            ['Kid Charlie', 'Charlie', 'Kid', 'Blue']])
    _sheet(wb, 'Attendance Entries RAW', imp.HEADERS['Attendance Entries RAW'],
           [['Kid Alpha', 'Red', d1, True, True, False, False, False, False, ts],
            ['Kid Alpha', 'Red', d1, True, False, True, False, False, False, ts],
            ['Kid Bravo', 'Red', d1, False, False, False, False, False, True, ts],
            ['Kid Charlie', 'Blue', d1, True, False, False, False, False, False, ts]])
    _sheet(wb, 'Completed Sections RAW', imp.HEADERS['Completed Sections RAW'],
           [['Kid Alpha', 'Red', d1, 1.1, True, False, False, ts],
            ['Kid Alpha', 'Red', d1, 1.1, True, False, False, ts],
            ['Kid Charlie', 'Blue', d1, ' Start Zone 1', False, True, True, ts],
            [None] * 8])
    _sheet(wb, 'Schedule', imp.HEADERS['Schedule'] + [None],
           [[d1, 'Regular Meeting', 'a', 1.1, 'u', 's', 'k', 'x'],
            [d2, 'NO AWANA', 'b', ' -- ', None, None, None, None]])
    _sheet(wb, 'Book Sections', ['Section'], [['Start Zone 1'], [1.1], [1.2]])
    _sheet(wb, 'Teams', ['Team', 'Color'], [[t, c] for t, c in colors.items()])
    red = [1, 1, 1, 0, 0, 1, 4, 4.0]       # kids, bible, shirt, book, hw, dues, points, per kid
    blue = [1, 0, 0, 0, 0, 0, 1, 1]
    zero = [0, 0, 0, 0, 0, 0, 0, 0]
    wat, wt = [], []
    for team in colors:
        for d in (d1, d2):
            vals = {'Red': red, 'Blue': blue}.get(team, zero) if d == d1 else zero
            wat.append([d, team, colors[team]] + vals)
            if d == d1 and team == 'Red':
                w = [1, 2, 0, 0, 4, 4, red_total_points, 8.0, 'Total Points : 8000']
            elif d == d1 and team == 'Blue':
                w = [1, 0, 1, 1, 4, 1, 5, 5, 'Total Points : 5000']
            else:
                w = [0, 0, 0, 0, 0, 0, 0, 0, 'Total Points : 0']
            wt.append([d, team, colors[team]] + w)
    _sheet(wb, 'Weekly Attendance Totals', imp.HEADERS['Weekly Attendance Totals'], wat)
    _sheet(wb, 'Weekly Totals', imp.HEADERS['Weekly Totals'], wt)
    _sheet(wb, 'Load Test Entries', ['timestamp', 'Name', 'Team', 'Date', 'Section', 'Type'],
           [[ts, 'Test Kid A', 'Red', d1, 1.1, 'load_test'], [ts, 'Test Kid B', 'Red', d1, 1.1, 'load_test']])
    wb.save(path)
    return path


def test_load_workbook_and_parity_a_on_a_synthetic_export(tmp_path):
    sheet = imp.load_workbook(build_workbook(tmp_path / 'export.xlsx'))
    assert (len(sheet['roster']), len(sheet['attendance']), len(sheet['sections'])) == (3, 4, 3)
    assert sheet['load_test_rows'] == 2
    assert sheet['book_sections'] == ['Start Zone 1', '1.1', '1.2']
    assert [s['sections'] for s in sheet['schedule']] == [['1.1'], []]
    assert sheet['sections'][0]['section'] == '1.1' and sheet['sections'][2]['section'] == 'Start Zone 1'
    assert sheet['attendance'][0]['ts'] == utc(2026, 9, 17, 0, 30)
    mismatches, compared = imp.parity_a(sheet)
    assert compared == 16
    assert mismatches == []


def test_parity_a_reports_a_wrong_cached_value(tmp_path):
    sheet = imp.load_workbook(build_workbook(tmp_path / 'export.xlsx', red_total_points=9))
    mismatches, _ = imp.parity_a(sheet)
    assert [(m['date'], m['team'], m['column'], m['expected'], m['actual']) for m in mismatches] == [
        (D1, 'Red', 'total_points', 9, 8)]


def test_load_workbook_rejects_a_changed_header(tmp_path):
    path = build_workbook(tmp_path / 'export.xlsx')
    wb = openpyxl.load_workbook(path)
    wb['Attendance Entries RAW']['D1'] = 'Here'
    wb.save(path)
    with pytest.raises(imp.ImportAbort, match='Attendance Entries RAW: header'):
        imp.load_workbook(path)


def test_schedule_only_needs_just_schedule_and_book_sections(tmp_path):
    path = build_workbook(tmp_path / 'export.xlsx')
    wb = openpyxl.load_workbook(path)
    del wb['Weekly Totals']
    wb.save(path)
    assert set(imp.load_workbook(path, schedule_only=True)) == {'book_sections', 'schedule'}
    with pytest.raises(imp.ImportAbort, match='missing'):
        imp.load_workbook(path)


@pytest.mark.skipif(not REAL_XLSX.exists(), reason='real export not present (~/Downloads/TNT_App_Data.xlsx)')
def test_parity_a_against_the_real_export():
    sheet = imp.load_workbook(REAL_XLSX)
    mismatches, compared = imp.parity_a(sheet)
    count = len(mismatches)                  # counts only: never put kids' names in assertion output
    assert compared == len(sheet['wat']) + len(sheet['wt'])
    assert compared > 0
    assert count == 0, f'{count} parity A mismatches across {compared} totals rows'
