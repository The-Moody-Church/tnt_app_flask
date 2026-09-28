"""models/utils.py date helpers and filters, and models/fields.py team/form helpers."""
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from flask import g
from werkzeug.datastructures import MultiDict

from models import utils
from models.fields import (ATTENDANCE_FLAG_COLUMNS, ATTENDANCE_WRITABLE, SECTION_CREDIT_COLUMNS, TEAMS,
                           bools_from_form, canonical_team, parse_team, team_label)
from models.utils import (date_display, date_to_long, date_to_mmdd, date_to_url, find_closest_date, get_today,
                          num, parse_url_date, time_short)

UTC = timezone.utc
# 03:00 UTC on 9/24 is 22:00 on 9/23 in Chicago: the UTC date has already rolled over
FROZEN_INSTANT = datetime(2026, 9, 24, 3, 0, tzinfo=UTC)
CHICAGO_TODAY = date(2026, 9, 23)


@pytest.fixture
def frozen_now(monkeypatch):
    """Freeze models.utils' clock at FROZEN_INSTANT."""
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_INSTANT.astimezone(tz) if tz else FROZEN_INSTANT.replace(tzinfo=None)

    monkeypatch.setattr(utils, 'datetime', FrozenDatetime)


@pytest.fixture
def make_app():
    from tnt import create_app

    def make(**overrides):
        return create_app({'TESTING': True, 'TNT_DATE_OVERRIDE': None, 'ALLOW_DATE_OVERRIDE': True, **overrides})
    return make


# ---------------------------------------------------------------------------
# get_today
# ---------------------------------------------------------------------------

def test_get_today_outside_app_context_is_chicago_date(frozen_now):
    assert get_today() == CHICAGO_TODAY


def test_get_today_real_clock_matches_chicago():
    before = datetime.now(utils.CHICAGO).date()
    assert get_today() in (before, datetime.now(utils.CHICAGO).date())


def test_get_today_without_overrides_is_chicago_date(make_app, frozen_now):
    with make_app().app_context():
        assert get_today() == CHICAGO_TODAY


def test_get_today_uses_config_override(make_app, frozen_now):
    with make_app(TNT_DATE_OVERRIDE='2026-09-16').app_context():
        assert get_today() == date(2026, 9, 16)


def test_get_today_qa_cookie_beats_config_override(make_app, frozen_now):
    app = make_app(TNT_DATE_OVERRIDE='2026-09-16')
    with app.test_request_context('/'):
        g.today_override = date(2026, 9, 30)
        assert get_today() == date(2026, 9, 30)


def test_get_today_falls_back_to_config_when_no_qa_date(make_app, frozen_now):
    app = make_app(TNT_DATE_OVERRIDE='2026-09-16')
    with app.test_request_context('/'):
        g.today_override = None
        assert get_today() == date(2026, 9, 16)


def test_get_today_ignores_overrides_when_not_allowed(make_app, frozen_now):
    app = make_app(TNT_DATE_OVERRIDE='2026-09-16', ALLOW_DATE_OVERRIDE=False)
    with app.test_request_context('/'):
        g.today_override = date(2026, 9, 30)
        assert get_today() == CHICAGO_TODAY


def test_today_query_param_sets_qa_override(make_app, frozen_now):
    """?today=YYYY-MM-DD runs through the before_request hook into get_today()."""
    app = make_app(TNT_DATE_OVERRIDE='2026-09-16')
    with app.test_request_context('/?today=2026-10-07'):
        app.preprocess_request()
        assert get_today() == date(2026, 10, 7)
    with app.test_request_context('/?today=not-a-date'):
        app.preprocess_request()
        assert get_today() == date(2026, 9, 16)
    with app.test_request_context('/?today=clear', headers={'Cookie': 'tnt_today=2026-10-07'}):
        app.preprocess_request()
        assert get_today() == date(2026, 9, 16)


def test_today_cookie_sets_qa_override(make_app, frozen_now):
    app = make_app()
    with app.test_request_context('/', headers={'Cookie': 'tnt_today=2026-09-09'}):
        app.preprocess_request()
        assert get_today() == date(2026, 9, 9)


def test_today_query_param_ignored_when_not_allowed(make_app, frozen_now):
    app = make_app(ALLOW_DATE_OVERRIDE=False)
    with app.test_request_context('/?today=2026-10-07', headers={'Cookie': 'tnt_today=2026-09-09'}):
        app.preprocess_request()
        assert get_today() == CHICAGO_TODAY


# ---------------------------------------------------------------------------
# parse_url_date / find_closest_date
# ---------------------------------------------------------------------------

def test_parse_url_date_accepts_iso():
    assert parse_url_date('2026-09-23') == date(2026, 9, 23)
    assert parse_url_date('2028-02-29') == date(2028, 2, 29)


@pytest.mark.parametrize('value', ['2026-9-23', '2026-09-3', '20260923', '2026/09/23', '09-23-2026', '2026-13-01',
                                   '2027-02-29', '2026-09-31', ' 2026-09-23', '2026-09-23 ', '2026-09-23T00:00',
                                   '', 'clear', None, 20260923, date(2026, 9, 23)])
def test_parse_url_date_rejects_anything_else(value):
    assert parse_url_date(value) is None


def test_parse_url_date_rejects_iso_week_dates():
    assert parse_url_date('2026-W39-3') is None


SCHEDULE = [{'meeting_date': d} for d in (date(2026, 9, 9), date(2026, 9, 16), date(2026, 9, 23), date(2026, 9, 30))]


@pytest.mark.parametrize('today, expected', [
    (date(2026, 8, 1), date(2026, 9, 9)),       # before the season: first date
    (date(2026, 9, 16), date(2026, 9, 16)),     # a meeting night: that night
    (date(2026, 9, 17), date(2026, 9, 23)),     # between meetings: the next one
    (date(2026, 9, 30), date(2026, 9, 30)),
    (date(2026, 12, 1), date(2026, 9, 30)),     # after the season: the last date
])
def test_find_closest_date(today, expected):
    assert find_closest_date(SCHEDULE, today) == expected


def test_find_closest_date_empty_schedule():
    assert find_closest_date([], date(2026, 9, 23)) is None


def test_find_closest_date_defaults_to_get_today(make_app, frozen_now):
    with make_app(TNT_DATE_OVERRIDE='2026-09-17').app_context():
        assert find_closest_date(SCHEDULE) == date(2026, 9, 23)
    assert find_closest_date(SCHEDULE) == date(2026, 9, 23)  # frozen Chicago today


# ---------------------------------------------------------------------------
# Display filters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', [
    (date(2026, 9, 9), '2026-09-09'), (datetime(2026, 9, 9, 18, 30), '2026-09-09'),
    ('2026-09-09', '2026-09-09'), ('2026-09-09T18:30:00', '2026-09-09'), ('garbage', 'garbage'),
])
def test_date_to_url(value, expected):
    assert date_to_url(value) == expected


@pytest.mark.parametrize('value, expected', [
    (date(2026, 9, 9), 'September 9, 2026'), (date(2026, 10, 17), 'October 17, 2026'),
    (date(2027, 1, 1), 'January 1, 2027'), ('2026-05-06', 'May 6, 2026'),
    (datetime(2026, 12, 2, 19, 0), 'December 2, 2026'), ('TBD', 'TBD'),
])
def test_date_display_has_no_zero_padding(value, expected):
    assert date_display(value) == expected


@pytest.mark.parametrize('value, expected', [
    (date(2026, 9, 16), '9/16'), (date(2026, 10, 7), '10/7'), (date(2026, 1, 1), '1/1'), ('2026-09-09', '9/9'),
    ('nope', 'nope'),
])
def test_date_to_mmdd(value, expected):
    assert date_to_mmdd(value) == expected


@pytest.mark.parametrize('day, suffix', [
    (1, 'st'), (2, 'nd'), (3, 'rd'), (4, 'th'), (9, 'th'), (10, 'th'), (11, 'th'), (12, 'th'), (13, 'th'),
    (14, 'th'), (20, 'th'), (21, 'st'), (22, 'nd'), (23, 'rd'), (24, 'th'), (30, 'th'), (31, 'st'),
])
def test_date_to_long_ordinal_suffix(day, suffix):
    assert date_to_long(date(2026, 10, day)) == f'October {day}{suffix}'


def test_date_to_long_accepts_strings_and_passes_junk_through():
    assert date_to_long('2026-11-05') == 'November 5th'
    assert date_to_long('soon') == 'soon'


@pytest.mark.parametrize('value, expected', [
    (datetime(2026, 9, 23, 23, 12, tzinfo=UTC), '6:12pm'),            # CDT (UTC-5)
    (datetime(2026, 1, 15, 0, 12, tzinfo=UTC), '6:12pm'),             # CST (UTC-6), previous day in Chicago
    (datetime(2026, 9, 23, 5, 0, tzinfo=UTC), '12:00am'),             # Chicago midnight
    (datetime(2026, 9, 23, 17, 0, tzinfo=UTC), '12:00pm'),            # Chicago noon
    (datetime(2026, 9, 23, 17, 5, tzinfo=UTC), '12:05pm'),
    (datetime(2026, 9, 23, 5, 30, tzinfo=UTC), '12:30am'),
    (datetime(2026, 9, 23, 14, 5, tzinfo=UTC), '9:05am'),
    (datetime(2026, 9, 24, 4, 59, tzinfo=UTC), '11:59pm'),
    (datetime(2026, 9, 23, 18, 31, tzinfo=utils.CHICAGO), '6:31pm'),  # already Chicago (parse_mp_datetime)
    (datetime(2026, 9, 23, 18, 7), '6:07pm'),                         # naive: used as-is
    (time(0, 0), '12:00am'), (time(12, 0), '12:00pm'), (time(18, 7), '6:07pm'),
])
def test_time_short(value, expected):
    assert time_short(value) == expected


@pytest.mark.parametrize('value', [None, ''])
def test_time_short_empty(value):
    assert time_short(value) == ''


@pytest.mark.parametrize('value, expected', [
    (5, 5), (0, 0), (5.0, 5), (-2.0, -2), (Decimal('10375'), 10375), (Decimal('648.4375'), 648.4375),
    (166 / 16, 10.375), (2 / 3, 0.6666666667), ('12', 12), ('12.5', 12.5),
])
def test_num(value, expected):
    result = num(value)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize('value', [None, True, False, 'n/a', ''])
def test_num_passes_through_non_numbers(value):
    assert num(value) is value


# ---------------------------------------------------------------------------
# fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', [
    ('Red', 'Red'), ('red', 'Red'), (' BLUE ', 'Blue'), ('green\n', 'Green'), ('YeLLoW', 'Yellow'),
    ('Created by CheckIn', None), ('', None), (None, None), ('Purple', None), ('Red team', None),
    ('Unassigned', None), ('R', None),
])
def test_parse_team(value, expected):
    assert parse_team(value) == expected


@pytest.mark.parametrize('value, expected', [
    ('Red', 'Red'), ('red', 'Red'), ('GREEN', 'Green'), (' yellow ', 'Yellow'), ('Blue', 'Blue'),
    ('Unassigned', None), ('unassigned', None), ('UNASSIGNED', None), (' Unassigned ', None),
])
def test_canonical_team(value, expected):
    assert canonical_team(value) == expected


@pytest.mark.parametrize('value', ['bogus', '', None, 'Created by CheckIn', 'Red,Blue', 'unassign'])
def test_canonical_team_rejects_unknown(value):
    with pytest.raises(ValueError):
        canonical_team(value)


def test_canonical_team_round_trips_every_team_label():
    for team in (*TEAMS, None):
        assert canonical_team(team_label(team)) == team


def test_team_label():
    assert team_label(None) == 'Unassigned'
    assert team_label('Red') == 'Red'


def test_bools_from_form_presence_means_checked():
    """Any submitted value counts as checked; absent columns are False; undeclared keys are dropped."""
    form = MultiDict([('has_bible', 'on'), ('has_dues', ''), ('did_homework', 'off'), ('manual_present', 'on'),
                      ('team', 'Red')])
    assert bools_from_form(form, ATTENDANCE_FLAG_COLUMNS) == {
        'has_bible': True, 'wearing_shirt': False, 'has_book': False, 'did_homework': True, 'has_dues': True}


def test_bools_from_form_empty_form_is_all_false():
    assert bools_from_form(MultiDict(), SECTION_CREDIT_COLUMNS) == {
        'section_complete': False, 'silver_credit': False, 'gold_credit': False}
    assert bools_from_form({}, ()) == {}


def test_write_whitelists():
    assert ATTENDANCE_FLAG_COLUMNS == ('has_bible', 'wearing_shirt', 'has_book', 'did_homework', 'has_dues')
    assert ATTENDANCE_WRITABLE == frozenset(ATTENDANCE_FLAG_COLUMNS) | {'manual_present'}
    assert SECTION_CREDIT_COLUMNS == ('section_complete', 'silver_credit', 'gold_credit')
    assert TEAMS == ('Red', 'Blue', 'Green', 'Yellow')
