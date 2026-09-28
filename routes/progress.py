from flask import abort, redirect, render_template, request, url_for

from models import data
from models.fields import SECTION_CREDIT_COLUMNS, SECTION_CREDITS, bools_from_form
from routes._common import form_int, require_writable
from routes.auth import current_user
from routes.responses import saved


def register_progress_routes(app):
    """Register all progress-related routes"""

    @app.route('/progress')
    def progress():
        return render_template('progress.html', students=data.get_progress_summary())

    @app.route('/progress/student/<int:contact_id>')
    def student_progress(contact_id):
        kid = data.get_kid(contact_id=contact_id) or abort(404)
        history = data.get_section_history(contact_id=contact_id)
        order = data.get_book_sections()
        rank = {s: i for i, s in enumerate(order)}
        sections = [{'section': s, **h} for s, h in history.items()]
        sections.sort(key=lambda item: (rank.get(item['section'], len(rank)), item['section']))
        return render_template('student_progress.html', kid=kid, coalesced_sections=sections,
                               total_sections=len(sections),
                               silver_earned=sum(1 for s in sections if s['silver_date']),
                               gold_earned=sum(1 for s in sections if s['gold_date']))

    @app.route('/progress/student/<int:contact_id>/section/<section>')
    def student_section_log(contact_id, section):
        kid = data.get_kid(contact_id=contact_id) or abort(404)
        records = data.get_completed_sections(contact_id=contact_id, section=section)
        return render_template('student_section_log.html', kid=kid, section_name=section,
                               section_records=records)

    @app.route('/progress/student/<int:contact_id>/record/<int:entry_id>')
    def student_section_details(contact_id, entry_id):
        kid = data.get_kid(contact_id=contact_id) or abort(404)
        entry = data.get_section_entry(entry_id)
        if entry is None or entry['contact_id'] != contact_id:
            return redirect(url_for('student_progress', contact_id=contact_id))
        return render_template('student_section_details.html', kid=kid, section_entry=entry,
                               credits=SECTION_CREDITS, writable=data.is_writable_date(entry['meeting_date']))

    @app.route('/edit_progress_section', methods=['POST'])
    def edit_progress_section():
        entry = data.get_section_entry(form_int('entry_id')) or abort(404)
        require_writable(entry['meeting_date'])
        data.update_completed_section(entry_id=entry['id'],
                                      credits=bools_from_form(request.form, SECTION_CREDIT_COLUMNS),
                                      by=current_user())
        return saved(url_for('student_section_details', contact_id=entry['contact_id'],
                             entry_id=entry['id']))
