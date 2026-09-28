import logging
import os
import psycopg
from flask import Flask, g, jsonify, render_template, request

from config import load_config
from models import db
from models.errors import DataError
from models.fields import team_title
from models.utils import (date_display, date_to_long, date_to_mmdd, date_to_url, get_today, num,
                          parse_url_date, time_short)
from routes.home import register_home_routes
from routes.attendance import register_attendance_routes
from routes.progress import register_progress_routes

log = logging.getLogger(__name__)


def _maybe_load_dotenv():
    if os.environ.get('VERCEL_ENV') or os.environ.get('TNT_TESTING'):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def wants_json():
    if request.method not in ('GET', 'HEAD') or request.path.startswith('/api/'):
        return True
    if request.headers.get('X-Requested-With') == 'fetch':
        return True
    best = request.accept_mimetypes.best_match(['text/html', 'application/json'])
    return best == 'application/json'


def json_error(code, message, status, **extra):
    resp = jsonify(ok=False, error=code, message=message, **extra)
    resp.status_code = status
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def register_error_handlers(app):
    @app.errorhandler(DataError)
    def handle_data_error(e):
        if wants_json():
            return json_error(e.code, e.message or 'Not saved', e.status, **e.extra)
        return render_template('error.html', error_title='Something went wrong',
                               error_message=e.message or 'That didn’t work.'), e.status

    @app.errorhandler(psycopg.OperationalError)
    def handle_db_down(e):
        log.exception('database unavailable')
        if wants_json():
            return json_error('db_unavailable', 'Can’t reach the database. Nothing was saved.', 503)
        return render_template('error.html', error_title='Database unavailable',
                               error_message='Can’t reach the database right now. Try again in a moment.'), 503

    @app.errorhandler(404)
    def handle_404(e):
        if wants_json():
            return json_error('not_found', 'Not found', 404)
        return render_template('error.html', error_title='Not found',
                               error_message='That page doesn’t exist.'), 404

    @app.errorhandler(500)
    def handle_500(e):
        if wants_json():
            return json_error('server_error', 'Something went wrong. Nothing was saved.', 500)
        return render_template('error.html', error_title='Something went wrong',
                               error_message='Please try again.'), 500


def register_date_override(app):
    """Non-production QA: ?today=YYYY-MM-DD sets a cookie that overrides get_today(); ?today=clear removes it."""
    @app.before_request
    def _today_override():
        if not app.config['ALLOW_DATE_OVERRIDE']:
            return
        raw = request.args.get('today') or request.cookies.get('tnt_today')
        if raw and raw != 'clear':
            g.today_override = parse_url_date(raw)

    @app.after_request
    def _today_cookie(resp):
        if not app.config['ALLOW_DATE_OVERRIDE'] or 'today' not in request.args:
            return resp
        raw = request.args['today']
        if raw == 'clear':
            resp.delete_cookie('tnt_today')
        elif parse_url_date(raw):
            resp.set_cookie('tnt_today', raw, max_age=86400, httponly=True, samesite='Lax',
                            secure=app.config['SESSION_COOKIE_SECURE'])
        return resp

    @app.context_processor
    def _today():
        return {'today': get_today()}

    @app.context_processor
    def _qa_date():
        if not app.config['ALLOW_DATE_OVERRIDE']:
            return {'qa_today': None}
        override = g.get('today_override') or parse_url_date(app.config.get('TNT_DATE_OVERRIDE'))
        return {'qa_today': override}


def create_app(overrides=None):
    _maybe_load_dotenv()
    app = Flask(__name__)
    app.config.from_mapping(load_config(os.environ))
    if overrides:
        app.config.update(overrides)

    for name, fn in (('date_to_url', date_to_url), ('date_to_mmdd', date_to_mmdd),
                     ('date_to_long', date_to_long), ('date_display', date_display),
                     ('time_short', time_short), ('num', num), ('team_title', team_title)):
        app.jinja_env.filters[name] = fn

    db.init_app(app)
    register_date_override(app)
    register_error_handlers(app)
    register_home_routes(app)
    register_attendance_routes(app)
    register_progress_routes(app)

    @app.get('/healthz')
    def healthz():
        ok = True
        try:
            db_ok = db.get_conn().execute('SELECT 1').fetchone() is not None
        except psycopg.Error:
            db_ok = ok = False
        resp = jsonify(ok=ok, db=db_ok, commit=app.config['ASSET_VERSION'])
        resp.headers['Cache-Control'] = 'no-store'
        return resp, (200 if ok else 503)

    return app


app = create_app()

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', port=5001)
