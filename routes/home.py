from collections import OrderedDict

from flask import abort, redirect, render_template, request, url_for

from models import data
from models.fields import SECTION_CREDIT_COLUMNS, TEAM_STAT_FIELDS, bools_from_form, team_label
from models.utils import find_closest_date
from routes.auth import current_user
from models.errors import InvalidInput
from routes.responses import saved
from routes._common import (credit_states, form_date, form_int, live_sync, require_writable, resolve_day,
                            resolve_team, show_all_requested)


def register_home_routes(app):
    """Register all home-related routes"""

    @app.route('/')
    def home():
        schedule_data = data.get_schedule()
        return render_template('home.html', schedule_data=schedule_data,
                               closest_date=find_closest_date(schedule_data))

    @app.route('/home/<date_str>')
    def home_details(date_str):
        d, day = resolve_day(date_str, 'home')
        sync = live_sync(d)
        return render_template('home_details.html', day_data=day, date_str=date_str, sync=sync,
                               weekly_totals=data.get_weekly_totals(d))

    @app.route('/home/<date_str>/team/<team_name>')
    def home_team_details(date_str, team_name):
        d, day = resolve_day(date_str, 'home')
        team = resolve_team(team_name, allow_unassigned=False, on_fail=url_for('home_details', date_str=date_str))
        sync = live_sync(d)
        totals = data.get_weekly_totals(d, team)
        kids_sections = OrderedDict()
        for row in data.get_completed_sections(meeting_date=d, team=team):
            kid = kids_sections.setdefault(row['contact_id'], {'contact_id': row['contact_id'],
                                                               'name': row['name'], 'sections': []})
            kid['sections'].append(row['section'])
        return render_template('home_team_details.html', day_data=day, date_str=date_str, team_name=team,
                               team_data=totals[0] if totals else None, stat_fields=TEAM_STAT_FIELDS,
                               kids_sections=list(kids_sections.values()), sync=sync)

    @app.route('/home/<date_str>/team/<team_name>/record_section')
    def record_section_kid_picker(date_str, team_name):
        d, day = resolve_day(date_str, 'home')
        team = resolve_team(team_name, allow_unassigned=True, on_fail=url_for('home_details', date_str=date_str))
        sync = live_sync(d, force=request.args.get('refresh') == '1')
        board = data.get_board(d, team)
        here = [k for k in board if k['present']]
        absent = [k for k in board if not k['present']]
        return render_template('record_section_kid_picker.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), here=here, absent=absent,
                               show_all=show_all_requested(), sync=sync)

    @app.route('/home/<date_str>/team/<team_name>/record_section/kid/<int:contact_id>')
    def record_section_list(date_str, team_name, contact_id):
        d, day = resolve_day(date_str, 'home')
        team = resolve_team(team_name, allow_unassigned=True, on_fail=url_for('home_details', date_str=date_str))
        kid = data.get_kid(contact_id=contact_id) or abort(404)
        history = data.get_section_history(contact_id=contact_id)
        sections = []
        for sec in data.get_book_sections():
            h = history.get(sec, {})
            sections.append({'section': sec, 'main_date': h.get('main_date'),
                             'silver': bool(h.get('silver_date')), 'gold': bool(h.get('gold_date'))})
        return render_template('record_section_list.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), kid=kid, sections=sections)

    @app.route('/home/<date_str>/team/<team_name>/record_section/kid/<int:contact_id>/section/<section>')
    def record_section_form(date_str, team_name, contact_id, section):
        d, day = resolve_day(date_str, 'home')
        team = resolve_team(team_name, allow_unassigned=True, on_fail=url_for('home_details', date_str=date_str))
        kid = data.get_kid(contact_id=contact_id) or abort(404)
        if section not in data.get_book_sections():
            return redirect(url_for('record_section_list', date_str=date_str, team_name=team_name,
                                    contact_id=contact_id))
        history = data.get_section_history(contact_id=contact_id).get(section, {})
        return render_template('record_section_form.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), kid=kid, section_name=section,
                               history={'main_date': history.get('main_date'),
                                        'silver_date': history.get('silver_date'),
                                        'gold_date': history.get('gold_date')},
                               writable=data.is_writable_date(d))

    @app.route('/home/<date_str>/team/<team_name>/kid/<int:contact_id>/section/<section>')
    def home_section_details(date_str, team_name, contact_id, section):
        d, day = resolve_day(date_str, 'home')
        team = resolve_team(team_name, allow_unassigned=True, on_fail=url_for('home_details', date_str=date_str))
        kid = data.get_kid(contact_id=contact_id) or abort(404)
        rows = data.get_completed_sections(contact_id=contact_id, meeting_date=d, section=section,
                                           include_empty=True)
        return render_template('home_section_details.html', day_data=day, date_str=date_str,
                               team_name=team_label(team), kid=kid, section_name=section,
                               section_entry=rows[0] if rows else None,
                               writable=data.is_writable_date(d))

    @app.route('/submit_section', methods=['POST'])
    def submit_section():
        d = form_date()
        require_writable(d)
        contact_id = form_int('contact_id')
        credits = bools_from_form(request.form, SECTION_CREDIT_COLUMNS)
        if not any(credits.values()):
            raise InvalidInput('Pick at least one credit')
        data.upsert_completed_section(contact_id=contact_id, meeting_date=d,
                                      section=request.form.get('section', ''), credits=credits, by=current_user())
        team = request.form.get('team') or 'Unassigned'
        if team == 'Unassigned':
            return saved(url_for('record_section_kid_picker', date_str=d.isoformat(), team_name=team))
        return saved(url_for('home_team_details', date_str=d.isoformat(), team_name=team))

    @app.route('/edit_section', methods=['POST'])
    def edit_section():
        entry = data.get_section_entry(form_int('entry_id')) or abort(404)
        require_writable(entry['meeting_date'])
        entry = data.update_completed_section(entry_id=entry['id'],
                                              credits=bools_from_form(request.form, SECTION_CREDIT_COLUMNS),
                                              by=current_user())
        return saved(url_for('home_section_details', date_str=entry['meeting_date'].isoformat(),
                             team_name=request.form.get('team_name') or 'Unassigned',
                             contact_id=entry['contact_id'], section=entry['section']))

    app.jinja_env.globals['credit_states'] = credit_states
