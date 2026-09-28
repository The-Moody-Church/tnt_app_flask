"""The JSON contract every POST follows: {"ok": true, ...} or {"ok": false, "error": code, "message": text}."""
from flask import jsonify, redirect, request


def wants_json():
    """fetch() calls (tnt.post sends X-Requested-With: fetch) and /api/* get JSON; browsers get HTML."""
    if request.path.startswith('/api/') or request.headers.get('X-Requested-With') == 'fetch':
        return True
    return request.accept_mimetypes.best_match(['text/html', 'application/json']) == 'application/json'


def saved(redirect_url, **data):
    """Success response for a form POST: JSON for fetch, a plain redirect if the page posted natively."""
    if wants_json():
        return json_ok(redirect=redirect_url, **data)
    return redirect(redirect_url, 303)


def json_ok(**data):
    resp = jsonify(ok=True, **data)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def json_error(code, message, status, **extra):
    resp = jsonify(ok=False, error=code, message=message, **extra)
    resp.status_code = status
    resp.headers['Cache-Control'] = 'no-store'
    return resp
