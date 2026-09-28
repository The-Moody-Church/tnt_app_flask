"""JSON endpoints (all POST, CSRF via the X-CSRFToken header, JSON contract in routes/responses.py)."""
from models.roster import sync_roster
from models.sync_state import Unlimited
from routes.responses import json_error, json_ok


def register_api_routes(app):

    @app.post('/api/sync/roster')
    def api_sync_roster():
        """Account page: pull the latest T&T roster from MP now."""
        status = sync_roster(force=True, budget=Unlimited(timeout=(3.05, 10)))
        if status.state == 'synced':
            return json_ok(message='Kids refreshed from MinistryPlatform')
        if status.state == 'fresh':
            return json_ok(message='Kids were refreshed moments ago')
        return json_error('mp_unavailable', 'Couldn’t reach MinistryPlatform. Try again in a minute.', 503)
