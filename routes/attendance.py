from flask import abort, redirect, render_template, request, url_for

from models import data
from models.fields import ATTENDANCE_FLAG_COLUMNS, ATTENDANCE_FLAGS, bools_from_form, team_label
from models.utils import find_closest_date
from routes.auth import current_user
from routes.responses import saved
from routes._common import form_date, form_int, live_sync, require_writable, resolve_day, resolve_team, show_all_requested


def register_attendance_routes(app):
    """Register all attendance-related routes"""

    @app.route('/attendance')
    def attendance():
        schedule_data = data.get_schedule()
        return render_template('attendance.html', schedule_data=schedule_data,
                               closest_date=find_closest_date(schedule_data))

    @app.route('/attendance/<date_str>')
    def attendance_details(date_str):
        d, day = resolve_day(date_str, 'attendance')
        sync = live_sync(d)
        counts = data.get_board_counts(d)
        return render_template('attendance_details.html', day_data=day, date_str=date_str, sync=sync,
                               weekly_totals=data.get_attendance_totals(d),
                               unassigned=counts.get(None, {'here': 0, 'total': 0}))

    @app.route('/attendance/<date_str>/team/<team_name>')
    def team_attendance_details(date_str, team_name):
        d, day = resolve_day(date_str, 'attendance')
        team = resolve_team(team_name, allow_unassigned=True,
                            on_fail=url_for('attendance_details', date_str=date_str))
        sync = live_sync(d, force=request.args.get('refresh') == '1')
        board = data.get_board(d, team)
        counts = data.get_board_counts(d)
        return render_template('team_attendance_details.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), is_unassigned=team is None,
                               here=[k for k in board if k['present']],
                               absent=[k for k in board if not k['present']],
                               unassigned_here=counts.get(None, {}).get('here', 0) if team else 0,
                               show_all=show_all_requested(), sync=sync, flags=ATTENDANCE_FLAGS,
                               writable=data.is_writable_date(d))

    @app.route('/attendance/<date_str>/team/<team_name>/kid/<int:contact_id>')
    def kid_attendance_details(date_str, team_name, contact_id):
        d, day = resolve_day(date_str, 'attendance')
        team = resolve_team(team_name, allow_unassigned=True,
                            on_fail=url_for('attendance_details', date_str=date_str))
        kid = data.get_attendance(contact_id=contact_id, meeting_date=d) or abort(404)
        return render_template('kid_attendance_details.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), kid=kid, flags=ATTENDANCE_FLAGS,
                               writable=data.is_writable_date(d))

    @app.route('/attendance/<date_str>/team/<team_name>/checkin/<int:contact_id>')
    def checkin_form_for_kid(date_str, team_name, contact_id):
        d, day = resolve_day(date_str, 'attendance')
        team = resolve_team(team_name, allow_unassigned=True,
                            on_fail=url_for('attendance_details', date_str=date_str))
        kid = data.get_attendance(contact_id=contact_id, meeting_date=d) or abort(404)
        return render_template('checkin_form.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), kid=kid, flags=ATTENDANCE_FLAGS,
                               writable=data.is_writable_date(d))

    @app.route('/submit_checkin', methods=['POST'])
    def submit_checkin():
        """Check-in form for a kid who wasn't checked in at the kiosk: mark present + items."""
        d = form_date()
        require_writable(d)
        fields = bools_from_form(request.form, ATTENDANCE_FLAG_COLUMNS)
        if 'manual_present' in request.form:
            fields = {'manual_present': True, **fields}
        data.set_attendance(contact_id=form_int('contact_id'), meeting_date=d, fields=fields, by=current_user())
        return saved(url_for('team_attendance_details', date_str=d.isoformat(),
                             team_name=request.form.get('team') or 'Unassigned'))

    @app.route('/edit_attendance', methods=['POST'])
    def edit_kid_attendance():
        d = form_date()
        require_writable(d)
        contact_id = form_int('contact_id')
        fields = bools_from_form(request.form, ATTENDANCE_FLAG_COLUMNS)
        if request.form.get('can_mark_present') == '1':
            fields['manual_present'] = 'manual_present' in request.form
        data.set_attendance(contact_id=contact_id, meeting_date=d, fields=fields, by=current_user())
        return saved(url_for('kid_attendance_details', date_str=d.isoformat(),
                             team_name=request.form.get('team_name') or 'Unassigned',
                             contact_id=contact_id))
