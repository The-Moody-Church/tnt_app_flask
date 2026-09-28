"""
MP check-in sync: which T&T kids are checked in for a meeting date.

"Checked in" = an Event_Participants row on that date's check-in event(s) for Group 697
with Participation_Status_ID 3 (Attended) or 4 (Confirmed) AND Time_In set. Status 5
(Cancelled) is excluded. There is one shared Awana event per Wednesday; walk-ins can
have Group_ID NULL, so we also match on the roster's Participant_IDs.

The sync upserts attendance rows (mp_checked_in=true); it never touches the leader's
flags or manual_present. Kids who drop out of the MP result are cleared, which handles
cancellations; an empty result never clears anything.
"""
import logging
from datetime import datetime, timedelta, timezone

from models import mp, sync_state
from models.db import get_conn
from models.roster import excluded_participant_ids, roster_group_id, sync_roster
from models.sync_state import Budget, BudgetExceeded, SyncStatus
from models.utils import get_today

log = logging.getLogger(__name__)

TTL_LIVE = 45            # tonight
TTL_RECENT = 3600        # the last week
RECENT_DAYS = 7
RETRY_AFTER = 20
EVENT_CACHE_SECONDS = 6 * 3600
EMPTY_EVENT_CACHE_SECONDS = 600   # 'no event' may just mean check-in isn't switched on yet
CHECKED_IN_STATUSES = (3, 4)
EVENT_SELECT = 'Event_Groups.Event_ID,Event_ID_TABLE.Event_Start_Date'
CHECKIN_SELECT = ('Event_Participant_ID,Event_ID,Participant_ID,Participation_Status_ID,'
                  'Time_In,Time_Confirmed,Group_ID')


def key_for(d):
    return f'checkins:{d.isoformat()}'


def checkin_ttl(d, today=None):
    """None = don't sync (future dates; older dates are frozen unless forced)."""
    today = today or get_today()
    if d > today:
        return None
    if d == today:
        return TTL_LIVE
    if (today - d).days <= RECENT_DAYS:
        return TTL_RECENT
    return None


def event_filter(group_id, start, end):
    return (f'Event_Groups.Group_ID = {mp.q_int(group_id)} AND Event_ID_TABLE.[Allow_Check-in] = 1 '
            f'AND Event_ID_TABLE.Cancelled = 0 AND Event_ID_TABLE.Event_Start_Date >= {mp.q_date(start)} '
            f'AND Event_ID_TABLE.Event_Start_Date < {mp.q_date(end)}')


def fetch_event_ids_range(start, end_inclusive, *, timeout=mp.DEFAULT_TIMEOUT):
    """{date: [event_id, ...]} for check-in events linked to the T&T group between two dates."""
    rows = mp.mp_get('Event_Groups', select=EVENT_SELECT,
                     filter=event_filter(roster_group_id(), start, end_inclusive + timedelta(days=1)),
                     orderby='Event_Groups.Event_ID', timeout=timeout)
    by_date = {}
    for r in rows:
        d = mp.parse_mp_datetime(r['Event_Start_Date']).date()
        by_date.setdefault(d, [])
        if r['Event_ID'] not in by_date[d]:
            by_date[d].append(r['Event_ID'])
    return by_date


def fetch_event_ids(d, *, timeout=mp.DEFAULT_TIMEOUT):
    return fetch_event_ids_range(d, d, timeout=timeout).get(d, [])


def checkins_filter(event_ids, participant_ids):
    pid_clause = f' OR Participant_ID IN ({mp.q_ids(participant_ids)})' if participant_ids else ''
    statuses = ','.join(str(s) for s in CHECKED_IN_STATUSES)
    return (f'Event_ID IN ({mp.q_ids(event_ids)}) AND Participation_Status_ID IN ({statuses}) '
            f'AND Time_In IS NOT NULL AND (Group_ID = {mp.q_int(roster_group_id())} OR Group_ID IS NULL'
            f'{pid_clause})')


def fetch_checkins(event_ids, participant_ids, *, timeout=mp.DEFAULT_TIMEOUT):
    """-> {participant_id: {ep_id, time_in, time_confirmed, group_id}} (earliest Time_In wins)."""
    rows = mp.mp_get('Event_Participants', select=CHECKIN_SELECT,
                     filter=checkins_filter(event_ids, participant_ids),
                     orderby='Event_Participant_ID', timeout=timeout)
    result = {}
    for r in rows:
        time_in = mp.parse_mp_datetime(r.get('Time_In'))
        if time_in is None:
            continue
        pid = r['Participant_ID']
        prev = result.get(pid)
        if prev is None or time_in < prev['time_in']:
            result[pid] = {'ep_id': r['Event_Participant_ID'], 'time_in': time_in,
                           'time_confirmed': mp.parse_mp_datetime(r.get('Time_Confirmed')),
                           'group_id': r.get('Group_ID')}
    return result


def apply_checkins(conn, d, checkins, queried_contact_ids):
    """checkins: {contact_id: {ep_id, time_in, time_confirmed}}. One transaction."""
    cids = list(checkins)
    with conn.transaction():
        inserted = 0
        if cids:
            inserted = conn.execute(
                """INSERT INTO attendance (contact_id, meeting_date, team, mp_checked_in, mp_time_in,
                                           mp_time_confirmed, mp_event_participant_id, mp_synced_at, source)
                   SELECT k.contact_id, %(d)s, k.team, true, v.time_in, v.time_confirmed, v.ep_id, now(), 'mp_sync'
                     FROM unnest(%(cids)s::int[], %(tin)s::timestamptz[], %(tconf)s::timestamptz[], %(eps)s::int[])
                          AS v(contact_id, time_in, time_confirmed, ep_id)
                     JOIN kids k USING (contact_id)
                   ON CONFLICT (contact_id, meeting_date) DO UPDATE SET
                       mp_checked_in = true, mp_time_in = EXCLUDED.mp_time_in,
                       mp_time_confirmed = EXCLUDED.mp_time_confirmed,
                       mp_event_participant_id = EXCLUDED.mp_event_participant_id, mp_synced_at = now()""",
                {'d': d, 'cids': cids,
                 'tin': [checkins[c]['time_in'] for c in cids],
                 'tconf': [checkins[c]['time_confirmed'] for c in cids],
                 'eps': [checkins[c]['ep_id'] for c in cids]}).rowcount
        cleared = conn.execute(
            """UPDATE attendance SET mp_checked_in = false, mp_time_in = NULL, mp_time_confirmed = NULL,
                                     mp_event_participant_id = NULL, mp_synced_at = now()
                WHERE meeting_date = %(d)s AND mp_checked_in
                  AND contact_id = ANY(%(queried)s::int[]) AND contact_id <> ALL(%(cids)s::int[])""",
            {'d': d, 'queried': list(queried_contact_ids), 'cids': cids}).rowcount
    return {'checked_in': len(cids), 'upserted': inserted, 'cleared': cleared}


def _pid_maps(conn, d):
    """Participant->contact for every kid, and the pids to query (active kids + tonight's checked-in rows)."""
    rows = conn.execute(
        """SELECT k.contact_id, k.participant_id, k.active,
                  EXISTS (SELECT 1 FROM attendance a WHERE a.contact_id = k.contact_id
                                                   AND a.meeting_date = %s AND a.mp_checked_in) AS checked
             FROM kids k""", (d,)).fetchall()
    pid_to_cid = {r['participant_id']: r['contact_id'] for r in rows if r['participant_id']}
    queried = {r['participant_id'] for r in rows if r['participant_id'] and (r['active'] or r['checked'])}
    return pid_to_cid, queried


def sync_checkins(d, *, force=False, budget=None, conn=None):
    conn = conn if conn is not None else get_conn()
    budget = budget or Budget()
    key = key_for(d)
    ttl = checkin_ttl(d)
    if ttl is None and not force:
        return SyncStatus(key, 'frozen', (sync_state.read(conn, key) or {}).get('synced_at'))
    ttl = ttl or TTL_RECENT
    memo = None if force else sync_state.memo_fresh(key)
    if memo:
        return memo
    if not force and sync_state.mp_is_down(conn):
        return _no_event_or(sync_state.read(conn, key),
                            lambda row: sync_state.status_when_mp_down(key, row, ttl), key)
    claimed = sync_state.claim(conn, key, ttl=ttl, retry_after=5 if force else RETRY_AFTER, force=force)
    if claimed is None:
        row = sync_state.read(conn, key)
        status = _no_event_or(row, lambda r: sync_state.status_from_row(key, r, ttl), key)
        if status.state == 'fresh':
            age = (datetime.now(timezone.utc) - row['synced_at']).total_seconds()
            sync_state.memo_set(key, ttl - age, row['synced_at'])
        return status

    detail = dict(claimed.get('detail') or {})
    detail.pop('warning', None)
    fetched_at = sync_state.db_now(conn)
    try:
        events_checked = detail.get('events_checked_at')
        cache_for = EVENT_CACHE_SECONDS if detail.get('event_ids') else EMPTY_EVENT_CACHE_SECONDS
        if (force or not events_checked or 'event_ids' not in detail or
                (fetched_at - datetime.fromisoformat(events_checked)).total_seconds() > cache_for):
            detail['event_ids'] = fetch_event_ids(d, timeout=budget.timeout())
            detail['events_checked_at'] = fetched_at.isoformat()
        if not detail['event_ids']:
            sync_state.complete(conn, key, synced_at=fetched_at, detail=detail)
            return SyncStatus(key, 'no_event', fetched_at)

        pid_to_cid, queried = _pid_maps(conn, d)
        rows = fetch_checkins(detail['event_ids'], sorted(queried), timeout=budget.timeout())

        # Walk-ins the roster hasn't seen yet: one forced roster sync per new participant per date.
        excluded = excluded_participant_ids(conn)
        tried = set(detail.get('tried_pids') or [])
        unknown = {pid for pid in rows if pid not in pid_to_cid and pid not in excluded and pid not in tried}
        if unknown:
            detail['tried_pids'] = sorted(tried | unknown)
            status = sync_roster(force=True, budget=budget, conn=conn)
            if status.state == 'synced':
                pid_to_cid, queried = _pid_maps(conn, d)

        checkins = {pid_to_cid[pid]: info for pid, info in rows.items() if pid in pid_to_cid}
        queried_cids = {pid_to_cid[pid] for pid in queried if pid in pid_to_cid}
        existing = conn.execute('SELECT COUNT(*) AS n FROM attendance WHERE meeting_date = %s AND mp_checked_in',
                                (d,)).fetchone()['n']
        if not checkins and existing:
            detail['warning'] = 'empty MP result; kept existing check-ins'
            queried_cids = set()
        counts = apply_checkins(conn, d, checkins, queried_cids)
    except BudgetExceeded:
        sync_state.fail(conn, key, 'skipped: request MP budget spent')
        return SyncStatus(key, 'skipped', claimed.get('synced_at'), 'budget')
    except mp.MPUnavailable as e:
        sync_state.fail(conn, key, str(e))
        sync_state.mark_mp_down(conn, str(e))
        return SyncStatus(key, 'error', claimed.get('synced_at'), 'mp_unavailable')
    except mp.MPError as e:
        log.error('check-in sync failed for %s: %s', d, e)
        sync_state.fail(conn, key, str(e))
        return SyncStatus(key, 'error', claimed.get('synced_at'), str(e))
    detail['counts'] = counts
    sync_state.complete(conn, key, synced_at=fetched_at, detail=detail)
    sync_state.mark_mp_up(conn)
    sync_state.memo_set(key, ttl, fetched_at)
    return SyncStatus(key, 'synced', fetched_at)


def _no_event_or(row, fallback, key):
    """A date whose last successful sync found no check-in event reports 'no_event'."""
    if row and row.get('synced_at') and not row.get('last_error') \
            and (row.get('detail') or {}).get('event_ids') == []:
        return SyncStatus(key, 'no_event', row['synced_at'])
    return fallback(row)


def ensure_checkins_fresh(d, *, budget=None, conn=None):
    return sync_checkins(d, force=False, budget=budget, conn=conn)


def live_sync(d, *, force=False, seconds=3.0, conn=None):
    """Request-path sync: roster (10 min TTL) then check-ins for d, within one MP time budget."""
    conn = conn if conn is not None else get_conn()
    budget = Budget(seconds)
    roster_status = sync_roster(force=force, budget=budget, conn=conn)
    status = sync_checkins(d, force=force, budget=budget, conn=conn)
    if roster_status.state == 'error' and status.state in ('fresh', 'synced'):
        return SyncStatus(status.key, 'stale', status.synced_at, roster_status.error)
    if roster_status.state == 'error' and status.state == 'pending':
        return SyncStatus(status.key, 'error', None, roster_status.error)
    return status
