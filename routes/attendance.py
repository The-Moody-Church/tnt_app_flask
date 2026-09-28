from flask import abort, current_app, make_response, render_template, request, url_for

from models import data
from models.fields import ATTENDANCE_FLAGS, TEAMS, team_label
from models.utils import find_closest_date, get_today
from routes._common import live_sync, remember_team, resolve_day, resolve_team, show_all_requested, tonight_redirect


def register_attendance_routes(app):
    """Register all attendance-related routes.

    Attendance is recorded with tap-to-toggle chips that post to /api/attendance (routes/api.py).
    """

    @app.route('/attendance')
    def attendance():
        redirect_tonight = tonight_redirect('attendance')
        if redirect_tonight:
            return redirect_tonight
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
        resp = make_response(render_template(
            'team_attendance_details.html', day_data=day, date_str=date_str,
            team_name=team_label(team), is_unassigned=team is None,
            here=[k for k in board if k['present']],
            absent=[k for k in board if not k['present']],
            unassigned_here=counts.get(None, {}).get('here', 0) if team else 0,
            show_all=show_all_requested(), sync=sync, flags=ATTENDANCE_FLAGS,
            writable=data.is_writable_date(d), teams=data.get_teams(), counts=counts,
            is_tonight=d == get_today(), can_assign=current_app.config['MP_WRITES_ENABLED'],
            assign_teams=TEAMS))
        return remember_team(resp, team)

    @app.route('/attendance/<date_str>/team/<team_name>/kid/<int:contact_id>')
    def kid_attendance_details(date_str, team_name, contact_id):
        d, day = resolve_day(date_str, 'attendance')
        team = resolve_team(team_name, allow_unassigned=True,
                            on_fail=url_for('attendance_details', date_str=date_str))
        kid = data.get_attendance(contact_id=contact_id, meeting_date=d) or abort(404)
        return render_template('kid_attendance_details.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), kid=kid, flags=ATTENDANCE_FLAGS,
                               writable=data.is_writable_date(d))
