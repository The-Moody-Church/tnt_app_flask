"""
MP Group 697 (T&T) roster sync into `kids`, and team assignment written back to MP.

Team color lives in Group_Participants.Notes (Red/Blue/Green/Yellow). Anything else,
e.g. 'Created by CheckIn' for walk-ins, means Unassigned (team NULL).
"""
import logging
import os
from datetime import datetime, timezone

import psycopg

from flask import current_app, has_app_context

from models import mp, sync_state
from models.db import get_conn
from models.errors import Conflict, InvalidInput, NotFound
from models.fields import parse_team
from models.sync_state import Budget, BudgetExceeded, SyncStatus
from models.utils import get_today

log = logging.getLogger(__name__)

ROSTER_KEY = 'roster'
ROSTER_TTL = 600
RETRY_AFTER = 20

GP_SELECT = 'Group_Participant_ID,Group_ID,Participant_ID,Notes,End_Date'   # assign_team pre-read
ROSTER_SELECT = ','.join([
    'Group_Participants.Group_Participant_ID',
    'Group_Participants.Participant_ID',
    'Group_Participants.Group_Role_ID',
    'Group_Participants.Notes',
    'Group_Participants.Start_Date',
    'Group_Participants.End_Date',
    'Participant_ID_TABLE.Contact_ID',
    'Participant_ID_TABLE_Contact_ID_TABLE.Nickname',
    'Participant_ID_TABLE_Contact_ID_TABLE.First_Name',
    'Participant_ID_TABLE_Contact_ID_TABLE.Last_Name',
])


def roster_group_id():
    if has_app_context():
        return current_app.config['MP_ROSTER_GROUP_ID']
    return int(os.environ.get('MP_ROSTER_GROUP_ID') or 697)


def kid_role_ids():
    return tuple(int(x) for x in (os.environ.get('MP_KID_ROLE_IDS') or '2').split(',') if x.strip())


def roster_filter(group_id, *, since=None):
    """Active members; with `since`, every row that started on/after that date (import: includes ended rows)."""
    base = f'Group_Participants.Group_ID = {mp.q_int(group_id)}'
    if since is not None:
        return f'{base} AND Group_Participants.Start_Date >= {mp.q_date(since)}'
    return (f'{base} AND Group_Participants.Start_Date <= GETDATE() AND '
            '(Group_Participants.End_Date IS NULL OR Group_Participants.End_Date >= GETDATE())')


def fetch_roster(*, since=None, timeout=mp.DEFAULT_TIMEOUT, group_id=None):
    """-> (kids: list[dict], excluded_participant_ids: list[int]). Kids = kid roles only, one row per contact."""
    rows = mp.mp_get('Group_Participants', select=ROSTER_SELECT,
                     filter=roster_filter(group_id or roster_group_id(), since=since),
                     orderby='Group_Participants.Group_Participant_ID', timeout=timeout)
    roles = kid_role_ids()
    by_contact, excluded = {}, set()
    for r in rows:
        if r.get('Group_Role_ID') not in roles:
            excluded.add(r['Participant_ID'])
            continue
        kid = {
            'contact_id': r['Contact_ID'],
            'participant_id': r['Participant_ID'],
            'group_participant_id': r['Group_Participant_ID'],
            'group_role_id': r['Group_Role_ID'],
            'nickname': (r.get('Nickname') or '').strip() or None,
            'first_name': (r.get('First_Name') or '').strip(),
            'last_name': (r.get('Last_Name') or '').strip(),
            'notes': r.get('Notes'),
            'team': parse_team(r.get('Notes')),
            'start_date': r.get('Start_Date') or '',
            'end_date': r.get('End_Date'),
        }
        prev = by_contact.get(kid['contact_id'])
        # Prefer an open row, then the latest start.
        if prev is None or (kid['end_date'] is None, kid['start_date']) > (prev['end_date'] is None, prev['start_date']):
            by_contact[kid['contact_id']] = kid
    return list(by_contact.values()), sorted(excluded - {k['participant_id'] for k in by_contact.values()})


def upsert_roster(conn, kids, *, fetched_at, deactivate_missing=True, allow_shrink=False):
    """Write the roster snapshot. Guards against an empty or suspiciously small fetch (e.g. a permission problem)."""
    active_now = conn.execute('SELECT COUNT(*) AS n FROM kids WHERE active').fetchone()['n']
    if not kids:
        raise InvalidInput('MP returned no kids; refusing to empty the roster')
    if deactivate_missing and not allow_shrink and active_now and len(kids) < active_now * 0.5:
        raise InvalidInput(f'MP returned {len(kids)} kids but {active_now} are active; refusing to shrink')
    cols = {k: [kid[k] for kid in kids] for k in (
        'contact_id', 'participant_id', 'group_participant_id', 'nickname', 'first_name', 'last_name',
        'team', 'notes', 'group_role_id')}
    with conn.transaction():
        # MP contact merges can move a Participant_ID to another contact; free it from the old one.
        conn.execute(
            """UPDATE kids k SET participant_id = NULL, active = false, synced_at = now()
                 FROM unnest(%(contact_id)s::int[], %(participant_id)s::int[]) AS v(cid, pid)
                WHERE k.participant_id = v.pid AND k.contact_id <> v.cid""", cols)
        conn.execute(
            """INSERT INTO kids (contact_id, participant_id, group_participant_id, nickname, first_name,
                                 last_name, team, mp_notes, group_role_id, active, synced_at)
               SELECT cid, pid, gpid, nick, first, last, team, notes, role, true, now()
                 FROM unnest(%(contact_id)s::int[], %(participant_id)s::int[], %(group_participant_id)s::int[],
                             %(nickname)s::text[], %(first_name)s::text[], %(last_name)s::text[],
                             %(team)s::text[], %(notes)s::text[], %(group_role_id)s::int[])
                      AS v(cid, pid, gpid, nick, first, last, team, notes, role)
               ON CONFLICT (contact_id) DO UPDATE SET
                   participant_id = EXCLUDED.participant_id,
                   group_participant_id = EXCLUDED.group_participant_id,
                   nickname = EXCLUDED.nickname, first_name = EXCLUDED.first_name,
                   last_name = EXCLUDED.last_name,
                   team = CASE WHEN kids.team_assigned_at IS NOT NULL AND kids.team_assigned_at > %(fetched_at)s
                               THEN kids.team ELSE EXCLUDED.team END,
                   mp_notes = CASE WHEN kids.team_assigned_at IS NOT NULL AND kids.team_assigned_at > %(fetched_at)s
                                   THEN kids.mp_notes ELSE EXCLUDED.mp_notes END,
                   group_role_id = EXCLUDED.group_role_id, active = true, synced_at = now()""",
            {**cols, 'fetched_at': fetched_at})
        deactivated = 0
        if deactivate_missing:
            deactivated = conn.execute(
                """UPDATE kids SET active = false, group_participant_id = NULL, synced_at = now()
                    WHERE active AND contact_id <> ALL(%s::int[])""", (cols['contact_id'],)).rowcount
        # A team set directly in MP also fills tonight's Unassigned rows (never past dates).
        _fill_todays_null_teams(conn, cols['contact_id'])
    return {'kids': len(kids), 'deactivated': deactivated}


def _fill_todays_null_teams(conn, contact_ids):
    params = {'cids': list(contact_ids), 'today': get_today()}
    for table in ('attendance', 'completed_sections'):
        conn.execute(
            f"""UPDATE {table} r SET team = k.team FROM kids k
                 WHERE r.contact_id = k.contact_id AND r.team IS NULL AND k.team IS NOT NULL
                   AND r.meeting_date = %(today)s AND k.contact_id = ANY(%(cids)s::int[])""", params)


def sync_roster(*, force=False, budget=None, conn=None):
    conn = conn if conn is not None else get_conn()
    budget = budget or Budget()
    memo = None if force else sync_state.memo_fresh(ROSTER_KEY)
    if memo:
        return memo
    if not force and sync_state.mp_is_down(conn):
        return sync_state.status_when_mp_down(ROSTER_KEY, sync_state.read(conn, ROSTER_KEY), ROSTER_TTL)
    claimed = sync_state.claim(conn, ROSTER_KEY, ttl=ROSTER_TTL, retry_after=60 if force else RETRY_AFTER,
                               force=force)
    if claimed is None:
        row = sync_state.read(conn, ROSTER_KEY)
        status = sync_state.status_from_row(ROSTER_KEY, row, ROSTER_TTL)
        if status.state == 'fresh':
            age = (datetime.now(timezone.utc) - row['synced_at']).total_seconds()
            sync_state.memo_set(ROSTER_KEY, ROSTER_TTL - age, row['synced_at'])
        return status
    fetched_at = sync_state.db_now(conn)
    try:
        kids, excluded = fetch_roster(timeout=budget.timeout())
        result = upsert_roster(conn, kids, fetched_at=fetched_at)
    except BudgetExceeded:
        sync_state.fail(conn, ROSTER_KEY, 'skipped: request MP budget spent')
        return SyncStatus(ROSTER_KEY, 'skipped', claimed.get('synced_at'), 'budget')
    except mp.MPUnavailable as e:
        sync_state.fail(conn, ROSTER_KEY, str(e))
        sync_state.mark_mp_down(conn, str(e))
        return SyncStatus(ROSTER_KEY, 'error', claimed.get('synced_at'), 'mp_unavailable')
    except (mp.MPError, InvalidInput, psycopg.Error) as e:
        log.error('roster sync failed: %s', e)
        sync_state.fail(conn, ROSTER_KEY, str(e))
        return SyncStatus(ROSTER_KEY, 'error', claimed.get('synced_at'), str(e)[:200])
    sync_state.complete(conn, ROSTER_KEY, synced_at=fetched_at,
                        detail={**result, 'excluded_pids': excluded})
    sync_state.mark_mp_up(conn)
    sync_state.memo_set(ROSTER_KEY, ROSTER_TTL, fetched_at)
    return SyncStatus(ROSTER_KEY, 'synced', fetched_at)


def ensure_roster_fresh(*, budget=None, conn=None):
    return sync_roster(force=False, budget=budget, conn=conn)


def excluded_participant_ids(conn):
    row = sync_state.read(conn, ROSTER_KEY)
    return set((row or {}).get('detail', {}).get('excluded_pids') or [])


# =============================================================================
# Team assignment (writes Group_Participants.Notes back to MP)
# =============================================================================

def assign_team(*, contact_id, team, by, conn=None):
    """Assign an unassigned kid to a team in MP, then locally.

    No DB transaction is held across the MP calls: pre-read MP, PUT, then a
    compare-and-set UPDATE locally. Only today's NULL-team rows are backfilled,
    so past totals never shift. Raises InvalidInput, NotFound, Conflict and MP errors.
    """
    team = parse_team(team)
    if team is None:
        raise InvalidInput('pick Red, Blue, Green or Yellow')
    by_user_id = by['user_id'] if isinstance(by, dict) else getattr(by, 'user_id', None)
    by_contact_id = by['contact_id'] if isinstance(by, dict) else getattr(by, 'contact_id', None)
    if not by_user_id or not by_contact_id:
        raise InvalidInput('a signed-in leader is required')
    conn = conn if conn is not None else get_conn()
    kid = conn.execute('SELECT contact_id, participant_id, group_participant_id, team, mp_notes, active '
                       'FROM kids WHERE contact_id = %s', (contact_id,)).fetchone()
    if kid is None or not kid['active'] or not kid['group_participant_id']:
        raise NotFound('kid is not on the current roster')
    if kid['team']:
        raise Conflict('already_assigned', f"Already on {kid['team']}", team=kid['team'])

    gp = mp.mp_get_one('Group_Participants', select=GP_SELECT,
                       filter=f"Group_Participant_ID = {mp.q_int(kid['group_participant_id'])}")
    if (gp is None or gp.get('Group_ID') != roster_group_id()
            or gp.get('Participant_ID') != kid['participant_id']):
        raise NotFound('kid is no longer in the T&T group in MP')
    ended = mp.parse_mp_datetime(gp.get('End_Date'))
    if ended is not None and ended.date() < get_today():
        raise NotFound('kid is no longer in the T&T group in MP')
    current = parse_team(gp.get('Notes'))
    if current:
        # Someone assigned them in MP already; adopt it locally (tonight's rows included).
        with conn.transaction():
            conn.execute('UPDATE kids SET team = %s, mp_notes = %s WHERE contact_id = %s AND team IS NULL',
                         (current, gp.get('Notes'), contact_id))
            _fill_todays_null_teams(conn, [contact_id])
        raise Conflict('already_assigned', f'Already on {current}', team=current)

    result = mp.mp_put('Group_Participants',
                       [{'Group_Participant_ID': kid['group_participant_id'], 'Notes': team}],
                       user_id=by_user_id, select='Group_Participant_ID,Notes')
    if not result or parse_team(result[0].get('Notes')) != team:
        raise mp.MPUnavailable('write_not_confirmed', op='put', table='Group_Participants')

    today = get_today()
    with conn.transaction():
        updated = conn.execute(
            """UPDATE kids SET team = %(t)s, mp_notes = %(t)s, team_assigned_at = now()
                WHERE contact_id = %(c)s AND team IS NULL""", {'t': team, 'c': contact_id}).rowcount
        if not updated:
            current = conn.execute('SELECT team FROM kids WHERE contact_id = %s', (contact_id,)).fetchone()['team']
            raise Conflict('already_assigned', f'Already on {current}', team=current)
        conn.execute('UPDATE attendance SET team = %(t)s WHERE contact_id = %(c)s AND team IS NULL '
                     'AND meeting_date = %(d)s', {'t': team, 'c': contact_id, 'd': today})
        conn.execute('UPDATE completed_sections SET team = %(t)s WHERE contact_id = %(c)s AND team IS NULL '
                     'AND meeting_date = %(d)s', {'t': team, 'c': contact_id, 'd': today})
        conn.execute(
            """INSERT INTO team_assignments (contact_id, group_participant_id, old_notes, new_team,
                                             assigned_by_contact_id, assigned_by_user_id)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (contact_id, kid['group_participant_id'], gp.get('Notes'), team, by_contact_id, by_user_id))
    return conn.execute('SELECT contact_id, name, team FROM kids WHERE contact_id = %s', (contact_id,)).fetchone()
