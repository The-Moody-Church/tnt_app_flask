"""Every page renders against a seeded database (catches template/route contract breaks)."""
import pytest

from tests.conftest import add_attendance, add_section, D_0916, D_0923


@pytest.fixture
def data(seeded):
    add_attendance(seeded, 1001, D_0923, mp_checked_in=True, has_bible=True)
    add_attendance(seeded, 1002, D_0923, manual_present=True)
    add_attendance(seeded, 1007, D_0923, team=None, mp_checked_in=True)
    add_section(seeded, 1001, D_0916, '1.1', complete=True)
    add_section(seeded, 1001, D_0923, '1.2', complete=True, silver=True)
    return seeded


PAGES = [
    '/?all=1', '/attendance?all=1', '/progress', '/account',
    '/home/2026-09-23', '/home/2026-09-23/team/Red', '/home/2026-09-23/team/Red/record_section',
    '/home/2026-09-23/team/Red/record_section?show=all',
    '/home/2026-09-23/team/Red/record_section/kid/1001',
    '/home/2026-09-23/team/Red/record_section/kid/1001/section/1.2',
    '/home/2026-09-23/team/Red/record_section/kid/1001/section/Start%20Zone%201',
    '/home/2026-09-23/team/Red/kid/1001/section/1.2',
    '/attendance/2026-09-23', '/attendance/2026-09-23/team/Red', '/attendance/2026-09-23/team/Red?show=all',
    '/attendance/2026-09-23/team/Unassigned', '/attendance/2026-09-23/team/Red/kid/1001',
    '/attendance/2026-09-23/team/Red/kid/1002', '/attendance/2026-09-23/team/Blue/kid/1003',
    '/progress/student/1001', '/progress/student/1001/section/1.2', '/progress/student/1001/record/2',
    '/progress/student/1008', '/home/2026-09-30/team/Red/record_section/kid/1001/section/1.3',
]


@pytest.mark.parametrize('path', PAGES)
def test_page_renders(client, data, path):
    resp = client.get(path)
    assert resp.status_code == 200, (path, resp.status_code, resp.get_data(as_text=True)[:500])
    body = resp.get_data(as_text=True)
    assert 'onclick="window.location' not in body


def test_healthz(client, conn):
    resp = client.get('/healthz')
    assert resp.status_code == 200
    assert resp.get_json()['db'] is True
