"""
Pure-Python reference of the legacy Google Sheets totals formulas.

Used by scripts/import_sheet.py for its two parity checks (A: this module vs the
sheet's cached values; B: this module vs the SQL views) and by the tests.
Output keys match the columns of the weekly_attendance_totals and weekly_totals views.

Weekly Attendance Totals, per (date, team), over 'Attendance Entries RAW':
    D  = COUNTUNIQUEIFS(Name, Team, B, Date, A, Present, TRUE)   kids_present
    E-I  same with Has Bible / Wearing Shirt? / Has Book? / Did Homework? / Has Dues?
         (flags count whether or not the kid was Present)
    J  = SUM(D:I)                                                attendance_points
    K  = IFERROR(J/D, 0)                                         points_per_kid

Weekly Totals, per (date, team), over 'Completed Sections RAW':
    D  = 'Weekly Attendance Totals'!D                            number_of_kids
    E-G = COUNTIFS(Team, B, Date, A, <credit>, TRUE)  (row counts, not distinct)
    H  = (E+F+G)*2                                               section_points
    I  = 'Weekly Attendance Totals'!J                            attendance_points
    J  = H+I                                                     total_points
    K  = IFERROR(J/D, 0)                                         points_per_kid
    L  = CONCATENATE("Total Points : ", TRUNC(K*1000))           team_total_points

Grouping uses the team written on each row, not the kid's roster team. Rows with
no team, or a date/team outside the grid, are not counted.
"""
import math

# (output column, row field)
ATTENDANCE_COUNTS = [
    ('kids_present', 'present'),
    ('total_bibles', 'has_bible'),
    ('total_shirts', 'wearing_shirt'),
    ('total_workbooks', 'has_book'),
    ('total_hw_done', 'did_homework'),
    ('total_dues', 'has_dues'),
]
SECTION_COUNTS = [
    ('sections_completed', 'section_complete'),
    ('silvers_completed', 'silver_credit'),
    ('golds_completed', 'gold_credit'),
]
TOTAL_POINTS_PREFIX = 'Total Points : '


def points_per_kid(points, kids):
    """=IFERROR(points/kids, 0)."""
    return points / kids if kids else 0.0


def team_total_points(points, kids):
    """=CONCATENATE("Total Points : ", TRUNC(K*1000)). Float math on purpose, as Sheets does (323/20 -> 16149)."""
    return TOTAL_POINTS_PREFIX + str(math.trunc(points_per_kid(points, kids) * 1000))


def attendance_totals(rows, dates, teams, *, key='name', team_colors=None):
    """COUNTUNIQUEIFS by `key` (name or contact_id) -> {(date, team): {view columns}}.

    rows: dicts with 'date', 'team', key, 'present' and the five flag fields.
    """
    dates, teams = list(dates), list(teams)
    seen = {(d, t): {col: set() for col, _ in ATTENDANCE_COUNTS} for d in dates for t in teams}
    for r in rows:
        cell = seen.get((r['date'], r['team']))
        if cell is None:
            continue
        for col, field in ATTENDANCE_COUNTS:
            if r.get(field):
                cell[col].add(r[key])
    out = {}
    for (d, t), cell in seen.items():
        counts = {col: len(cell[col]) for col, _ in ATTENDANCE_COUNTS}
        points = sum(counts.values())
        row = {'meeting_date': d, 'team': t}
        if team_colors is not None:
            row['team_color'] = team_colors.get(t)
        row.update(counts)
        row['attendance_points'] = points
        row['points_per_kid'] = points_per_kid(points, counts['kids_present'])
        out[(d, t)] = row
    return out


def weekly_totals(att_totals, section_rows, dates, teams, *, team_colors=None):
    """COUNTIFS row counts of each credit plus the points columns -> {(date, team): {view columns}}.

    att_totals: the result of attendance_totals() for the same dates and teams.
    section_rows: dicts with 'date', 'team' and the three credit fields.
    """
    dates, teams = list(dates), list(teams)
    counts = {(d, t): {col: 0 for col, _ in SECTION_COUNTS} for d in dates for t in teams}
    for r in section_rows:
        cell = counts.get((r['date'], r['team']))
        if cell is None:
            continue
        for col, field in SECTION_COUNTS:
            if r.get(field):
                cell[col] += 1
    out = {}
    for (d, t), cell in counts.items():
        att = att_totals[(d, t)]
        kids = att['kids_present']
        section_points = 2 * sum(cell.values())
        total = section_points + att['attendance_points']
        row = {'meeting_date': d, 'team': t}
        if team_colors is not None:
            row['team_color'] = team_colors.get(t)
        row['number_of_kids'] = kids
        row.update(cell)
        row['section_points'] = section_points
        row['attendance_points'] = att['attendance_points']
        row['total_points'] = total
        row['points_per_kid'] = points_per_kid(total, kids)
        row['team_total_points'] = team_total_points(total, kids)
        out[(d, t)] = row
    return out
