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
    assert get(path).status_code in (302, 404)
