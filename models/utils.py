import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import current_app, g, has_app_context, has_request_context

CHICAGO = ZoneInfo('America/Chicago')
_ISO_DATE_RE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def get_today():
    """Today's date in Chicago.

    Non-production overrides, in order: the ?today= QA cookie (g.today_override),
    then TNT_DATE_OVERRIDE. Both are ignored in production.
    """
    if has_app_context() and current_app.config.get('ALLOW_DATE_OVERRIDE'):
        if has_request_context() and g.get('today_override'):
            return g.today_override
        override = parse_url_date(current_app.config.get('TNT_DATE_OVERRIDE'))
        if override:
            return override
    return datetime.now(CHICAGO).date()


def parse_url_date(value):
    """Strict YYYY-MM-DD -> date, else None."""
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def find_closest_date(schedule, today=None):
    """First schedule date on/after today, else the last date (None for an empty schedule)."""
    today = today or get_today()
    fallback = None
    for day in schedule:
        d = day['meeting_date']
        fallback = d
        if d >= today:
            return d
    return fallback


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def date_to_url(value):
    """date -> 'YYYY-MM-DD'."""
    try:
        return _as_date(value).isoformat()
    except (TypeError, ValueError):
        return str(value)


def date_display(value):
    """date -> 'September 9, 2026' (the sheet's display format)."""
    try:
        d = _as_date(value)
    except (TypeError, ValueError):
        return str(value)
    return f'{d:%B} {d.day}, {d.year}'


def date_to_mmdd(value):
    """date -> '9/16'."""
    try:
        d = _as_date(value)
    except (TypeError, ValueError):
        return str(value)
    return f'{d.month}/{d.day}'


def date_to_long(value):
    """date -> 'November 5th'."""
    try:
        d = _as_date(value)
    except (TypeError, ValueError):
        return str(value)
    suffix = 'th' if d.day in (11, 12, 13) else {1: 'st', 2: 'nd', 3: 'rd'}.get(d.day % 10, 'th')
    return f'{d:%B} {d.day}{suffix}'


def time_short(value):
    """aware datetime -> '6:12pm' in Chicago time."""
    if not value:
        return ''
    if isinstance(value, datetime) and value.tzinfo:
        value = value.astimezone(CHICAGO)
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d}{'am' if value.hour < 12 else 'pm'}"


def num(value):
    """Totals display: ints as ints, floats like the sheet's automatic format (up to 10 significant digits)."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    try:
        f = float(value)
    except (TypeError, ValueError):
        return value
    if f == int(f):
        return int(f)
    return float(f'{f:.10g}')
