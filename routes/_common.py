"""Helpers shared by the route modules."""
import logging

from flask import abort, current_app, redirect, request, url_for

from models import data
from models.checkins import live_sync as _live_sync
from models.errors import FutureDate, InvalidInput
from models.fields import TEAMS, UNASSIGNED, canonical_team, team_label
from models.sync_state import SyncStatus
from models.utils import get_today, parse_url_date

log = logging.getLogger(__name__)


def resolve_day(date_str, list_endpoint):
    """URL date -> (date, schedule row). Unknown or malformed dates go back to the date list."""
    d = parse_url_date(date_str)
    day = data.get_schedule_day(d) if d else None
    if day is None:
        abort(redirect(url_for(list_endpoint)))
    return d, day


def resolve_team(team_name, *, allow_unassigned, on_fail):
    """URL team -> 'Red' | ... | None (Unassigned). Anything else redirects to `on_fail`."""
    try:
        team = canonical_team(team_name)
    except ValueError:
        abort(redirect(on_fail))
    if team is None and not allow_unassigned:
        abort(redirect(on_fail))
    return team


TEAM_COOKIE = 'tnt_team'
TEAM_COOKIE_MAX_AGE = 180 * 86400


def last_team():
    """The team this device last looked at (validated; never Unassigned)."""
    value = request.cookies.get(TEAM_COOKIE)
    return value if value in TEAMS else None


def remember_team(resp, team):
    if team in TEAMS:
        resp.set_cookie(TEAM_COOKIE, team, max_age=TEAM_COOKIE_MAX_AGE, httponly=True, samesite='Lax',
                        secure=current_app.config['SESSION_COOKIE_SECURE'])
    return resp


def tonight_redirect(tab):
    """On a club night, the Home/Attendance tabs open straight to tonight's team (or tonight's teams).

    tab: 'home' | 'attendance'. ?all=1 shows the date list instead. Returns a response or None.
    """
    if request.args.get('all'):
        return None
    d = get_today()
    day = data.get_schedule_day(d)
    if day is None or day['no_club']:
        return None
    team = last_team()
    if team:
        endpoint = 'home_team_details' if tab == 'home' else 'team_attendance_details'
        target = url_for(endpoint, date_str=d.isoformat(), team_name=team)
    else:
        target = url_for('home_details' if tab == 'home' else 'attendance_details', date_str=d.isoformat())
    resp = redirect(target)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def team_url_name(team):
    return team_label(team)


def live_sync(d, *, force=False):
    """MP roster + check-in sync for date d on the request path. Never breaks the page."""
    try:
        return _live_sync(d, force=force)
    except Exception:  # sync problems must not take the page down; the banner reports staleness
        log.exception('live sync failed for %s', d)
        return SyncStatus(f'checkins:{d.isoformat()}', 'error', None, 'sync_failed')


def show_all_requested():
    return request.args.get('show') == 'all'


def credit_states(history, section, d):
    """Per-credit state for one section on date d: 'today' | 'earlier' (another date) | 'none'."""
    h = history.get(section) or {}
    states = {}
    for field, key in (('section_complete', 'main_date'), ('silver_credit', 'silver_date'),
                       ('gold_credit', 'gold_date')):
        when = h.get(key)
        if when is None:
            states[field] = {'state': 'none', 'date': None}
        elif when == d:
            states[field] = {'state': 'today', 'date': when}
        else:
            states[field] = {'state': 'earlier', 'date': when}
    return states


def require_writable(d):
    if not data.is_writable_date(d):
        raise FutureDate('Can’t record for a future date')


def form_int(name):
    value = request.form.get(name, '')
    if not value.isdigit():
        raise InvalidInput(f'missing {name}')
    return int(value)


def form_date(name='date_str'):
    d = parse_url_date(request.form.get(name, ''))
    if d is None:
        raise InvalidInput('missing date')
    return d


__all__ = ['resolve_day', 'resolve_team', 'team_url_name', 'live_sync', 'show_all_requested',
           'credit_states', 'require_writable', 'form_int', 'form_date', 'UNASSIGNED', 'get_today',
           'last_team', 'remember_team', 'tonight_redirect']
