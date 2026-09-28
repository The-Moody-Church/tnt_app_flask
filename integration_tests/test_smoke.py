"""
Smoke tests against a live deployment. Read-only: never writes data.

    SMOKE_BASE_URL=https://<deployment> pytest integration_tests/ -v

Vercel Deployment Protection is bypassed with VERCEL_AUTOMATION_BYPASS_SECRET when set.
"""
import os

import pytest
import requests

BASE_URL = os.environ.get('SMOKE_BASE_URL', '').rstrip('/')
BYPASS = os.environ.get('VERCEL_AUTOMATION_BYPASS_SECRET')

pytestmark = pytest.mark.skipif(not BASE_URL, reason='SMOKE_BASE_URL not set')


def get(path, **kw):
    headers = {'x-vercel-protection-bypass': BYPASS} if BYPASS else {}
    return requests.get(BASE_URL + path, headers=headers, allow_redirects=False, timeout=30, **kw)


def test_healthz():
    resp = get('/healthz')
    assert resp.status_code == 200
    body = resp.json()
    assert body['ok'] is True and body['db'] is True


@pytest.mark.parametrize('path', ['/static/style.css', '/static/manifest.json', '/static/icon-192.png'])
def test_static_files(path):
    assert get(path).status_code == 200


@pytest.mark.parametrize('path', ['/metrics', '/test/reset', '/test/rate-limit/status'])
def test_removed_debug_endpoints(path):
    assert get(path).status_code in (302, 404)   # anonymous: login redirect; signed in: 404


@pytest.mark.parametrize('path', ['/', '/attendance', '/progress', '/account'])
def test_pages_require_login(path):
    resp = get(path)
    assert resp.status_code == 302
    assert resp.headers['Location'].split('?')[0].endswith('/login')


def test_login_page_renders():
    resp = get('/login')
    assert resp.status_code == 200
    assert 'MinistryPlatform' in resp.text


def test_post_without_session_is_401_json():
    headers = {'x-vercel-protection-bypass': BYPASS} if BYPASS else {}
    resp = requests.post(BASE_URL + '/submit_section', data={}, headers=headers, allow_redirects=False, timeout=30)
    assert resp.status_code == 401
    assert resp.json()['error'] == 'auth_required'


@pytest.mark.skipif(os.environ.get('SMOKE_EXPECT_LOGIN') != '1', reason='only where sign-in is configured')
def test_login_start_redirects_to_mp():
    resp = get('/login/start')
    assert resp.status_code == 302
    location = resp.headers['Location']
    assert '/ministryplatformapi/oauth/connect/authorize' in location
    assert 'code_challenge' not in location or os.environ.get('SMOKE_EXPECT_PKCE') == '1'


def test_security_headers():
    resp = get('/login')
    assert resp.headers.get('X-Frame-Options') == 'DENY'
    assert resp.headers.get('X-Content-Type-Options') == 'nosniff'
    assert 'frame-ancestors' in resp.headers.get('Content-Security-Policy', '')
