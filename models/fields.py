"""
Field names, display labels and write whitelists.
Single source of truth for the columns routes/templates may read or write.
"""

TEAMS = ('Red', 'Blue', 'Green', 'Yellow')
UNASSIGNED = 'Unassigned'

# (column, chip label, detail-page label) — detail labels keep the old sheet wording
ATTENDANCE_FLAGS = [
    ('has_bible', 'Bible', 'Has Bible'),
    ('wearing_shirt', 'Shirt', 'Wearing Shirt?'),
    ('has_book', 'Book', 'Has Book?'),
    ('did_homework', 'HW', 'Did Homework?'),
    ('has_dues', 'Dues', 'Has Dues?'),
]
ATTENDANCE_FLAG_COLUMNS = tuple(col for col, _, _ in ATTENDANCE_FLAGS)
ATTENDANCE_WRITABLE = frozenset(ATTENDANCE_FLAG_COLUMNS + ('manual_present',))

SECTION_CREDITS = [
    ('section_complete', 'Main', 'Section Complete'),
    ('silver_credit', 'Silver', 'Silver Credit'),
    ('gold_credit', 'Gold', 'Gold Credit'),
]
SECTION_CREDIT_COLUMNS = tuple(col for col, _, _ in SECTION_CREDITS)

# Weekly Totals stats shown on the team page, in sheet column order (label, key)
TEAM_STAT_FIELDS = [
    ('Team Color', 'team_color'),
    ('Number of Kids', 'number_of_kids'),
    ('Sections Completed', 'sections_completed'),
    ('Silvers Completed', 'silvers_completed'),
    ('Golds Completed', 'golds_completed'),
    ('Section points', 'section_points'),
    ('Attendance Points', 'attendance_points'),
    ('Total Points', 'total_points'),
    ('Points per Kid', 'points_per_kid'),
]


def parse_team(value):
    """MP Group_Participants.Notes -> 'Red' | ... | None. Anything else ('Created by CheckIn', '') is unassigned."""
    if not value:
        return None
    value = str(value).strip().casefold()
    for team in TEAMS:
        if team.casefold() == value:
            return team
    return None


def canonical_team(value):
    """URL segment -> canonical team name, None for 'Unassigned'. Raises ValueError for anything else."""
    if value is not None and str(value).strip().casefold() == UNASSIGNED.casefold():
        return None
    team = parse_team(value)
    if team is None:
        raise ValueError(f'unknown team {value!r}')
    return team


def team_label(team):
    return team or UNASSIGNED


def team_title(team):
    """'Red' -> 'Red Team'; None/'Unassigned' -> 'Unassigned'."""
    return UNASSIGNED if not team or team == UNASSIGNED else f'{team} Team'


def bools_from_form(form, columns):
    """Checkbox form -> {column: bool} for the declared columns only (absent checkbox = False)."""
    return {col: col in form for col in columns}
