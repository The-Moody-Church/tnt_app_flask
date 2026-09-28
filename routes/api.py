"""JSON endpoints (all POST, CSRF via the X-CSRFToken header, JSON contract in routes/responses.py)."""
from flask import current_app, request

from models import data, mp
from models.errors import InvalidInput, NotFound
from models.fields import ATTENDANCE_FLAG_COLUMNS, ATTENDANCE_WRITABLE, SECTION_CREDIT_COLUMNS
from models.roster import assign_team, sync_roster
from models.sync_state import Unlimited
from models.utils import date_to_mmdd, parse_url_date, time_short
from routes._common import credit_states, require_writable
from routes.auth import current_user
from routes.responses import json_error, json_ok


def _json_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise InvalidInput('expected a JSON object')
    return body


def _writable_day(date_str):
    d = parse_url_date(date_str)
    if d is None or data.get_schedule_day(d) is None:
        raise NotFound('no meeting on that date')
    require_writable(d)
    return d


def present_label(row):
    if row['source'] == 'mp':
        when = time_short(row['mp_time'])
        return 'Checked in' + (f' · {when}' if when else '')
    if row['source'] == 'manual':
        return 'Marked present' + (f" by {row['manual_present_by']}" if row.get('manual_present_by') else '')
    return 'Not checked in'


def _attendance_payload(row):
    payload = {'contact_id': row['contact_id'], 'present': row['present'], 'source': row['source'],
               'present_label': present_label(row)}
    payload.update({col: row[col] for col in ATTENDANCE_FLAG_COLUMNS})
    return payload


def _credit_payload(states):
    return {field: {'state': st['state'], 'label': f"✓ {date_to_mmdd(st['date'])}" if st['state'] == 'earlier' else ''}
            for field, st in states.items()}


def register_api_routes(app):

    @app.post('/api/attendance/<date_str>/<int:contact_id>')
    def api_attendance_set(date_str, contact_id):
        """Chip taps and mark-present: {"set": {"has_bible": true}}. Absolute values, so retries are safe."""
        d = _writable_day(date_str)
        fields = _json_body().get('set')
        if not isinstance(fields, dict) or not fields or not set(fields) <= ATTENDANCE_WRITABLE:
            raise InvalidInput('nothing valid to set')
        if data.get_kid(contact_id=contact_id) is None:
            raise NotFound('unknown kid')
        current = data.get_attendance(contact_id=contact_id, meeting_date=d)
        if fields.get('manual_present') is False and current['mp_checked_in']:
            return json_error('mp_checked_in', 'They were checked in at the kiosk, so they stay present.', 409)
        row = data.set_attendance(contact_id=contact_id, meeting_date=d, fields=fields, by=current_user())
        return json_ok(row=_attendance_payload(row))

    @app.post('/api/sections/<date_str>/<int:contact_id>')
    def api_section_credit(date_str, contact_id):
        """Tonight's-section chips: {"section": "1.5", "field": "silver_credit", "value": true}."""
        d = _writable_day(date_str)
        body = _json_body()
        section, field, value = body.get('section'), body.get('field'), body.get('value')
        if field not in SECTION_CREDIT_COLUMNS or not isinstance(value, bool) or not isinstance(section, str):
            raise InvalidInput('bad credit')
        if section not in data.get_book_sections():
            raise InvalidInput('unknown section')
        if data.get_kid(contact_id=contact_id) is None:
            raise NotFound('unknown kid')
        states = credit_states(data.get_section_history(contact_id=contact_id), section, d)
        if states[field]['state'] == 'earlier':
            return json_error('already_earned', f"Already earned {date_to_mmdd(states[field]['date'])}", 409,
                              states=_credit_payload(states))
        data.set_section_credit(contact_id=contact_id, meeting_date=d, section=section, field=field, value=value,
                                by=current_user())
        states = credit_states(data.get_section_history(contact_id=contact_id), section, d)
        return json_ok(section=section, states=_credit_payload(states))

    @app.post('/api/kids/<int:contact_id>/team')
    def api_assign_team(contact_id):
        """Assign an Unassigned kid to a team. Writes Group_Participants.Notes in MinistryPlatform."""
        if not current_app.config['MP_WRITES_ENABLED']:
            return json_error('disabled', 'Team assignment is turned off here.', 503)
        team = _json_body().get('team')
        try:
            kid = assign_team(contact_id=contact_id, team=team, by=current_user())
        except mp.MPWritesDisabled:
            return json_error('disabled', 'Team assignment is turned off here.', 503)
        except mp.MPError:
            return json_error('mp_unavailable', 'Couldn’t save to MinistryPlatform. Nothing changed — try again.', 502)
        return json_ok(kid={'contact_id': kid['contact_id'], 'name': kid['name'], 'team': kid['team']})

    @app.post('/api/sync/roster')
    def api_sync_roster():
        """Account page: pull the latest T&T roster from MP now."""
        status = sync_roster(force=True, budget=Unlimited(timeout=(3.05, 10)))
        if status.state == 'synced':
            return json_ok(message='Kids refreshed from MinistryPlatform')
        if status.state == 'fresh':
            return json_ok(message='Kids were refreshed moments ago')
        return json_error('mp_unavailable', 'Couldn’t reach MinistryPlatform. Try again in a minute.', 503)
