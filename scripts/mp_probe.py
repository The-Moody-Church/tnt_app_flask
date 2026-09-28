"""
Read-only MinistryPlatform permission probe for the TNT Tracker API client.

    MP_CLIENT_ID=... MP_CLIENT_SECRET=... python scripts/mp_probe.py            # every query shape, $top=1
    MP_CLIENT_ID=... MP_CLIENT_SECRET=... python scripts/mp_probe.py --leaders  # who can't sign in yet
    MP_WRITES_ENABLED=1 ... python scripts/mp_probe.py --write-probe GP_ID --user-id USER_ID

Runs each production query shape once and prints PASS/FAIL per query. A 403 names the
tables the client's security role needs Read on. --write-probe re-saves one Group 697
Group_Participants row's Notes with its current value to prove the Update permission.
Prints names and IDs only (never tokens, filters or Notes values).
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_config  # noqa: E402
from models import checkins, mp, roster  # noqa: E402

DUMMY_GUID = '00000000-0000-0000-0000-000000000000'
DEFAULT_ADMIN_GROUP_IDS = (29,)   # 'MP Next Admin' until a TNT Tracker Staff group exists

EVENT_SELECT = 'Event_Groups.Event_ID,Event_ID_TABLE.Event_Start_Date'
CHECKIN_SELECT = 'Event_Participant_ID,Event_ID,Participant_ID,Participation_Status_ID,Time_In,Time_Confirmed,Group_ID'
LEADER_SELECT = 'Participant_ID_TABLE_Contact_ID_TABLE.User_Account'
LEADER_LIST_SELECT = ('Group_Participants.Group_Participant_ID,Participant_ID_TABLE.Contact_ID,'
                      'Participant_ID_TABLE_Contact_ID_TABLE.Display_Name,'
                      'Participant_ID_TABLE_Contact_ID_TABLE.User_Account')
ADMIN_SELECT = 'dp_User_User_Groups.User_ID,dp_User_User_Groups.User_Group_ID'
USER_SELECT = ('dp_Users.User_ID,dp_Users.Contact_ID,Contact_ID_TABLE.Display_Name,Contact_ID_TABLE.Nickname,'
               'Contact_ID_TABLE.First_Name,Contact_ID_TABLE.Last_Name')
GP_SELECT = 'Group_Participant_ID,Group_ID,Participant_ID,Notes,End_Date'   # assign_team's pre-read


def active_filter(group_id):
    return (f'Group_Participants.Group_ID = {mp.q_int(group_id)} AND Group_Participants.Start_Date <= GETDATE() '
            'AND (Group_Participants.End_Date IS NULL OR Group_Participants.End_Date >= GETDATE())')


def leaders_filter(group_id):
    """The sign-in membership query: active leaders who have an MP login."""
    return f'{active_filter(group_id)} AND Participant_ID_TABLE_Contact_ID_TABLE.User_Account IS NOT NULL'


def probes(cfg):
    """-> [(name, tables needing Read, callable, expect_rows)] in order; `found` passes ids between probes."""
    today = datetime.now(mp.CHICAGO).date()
    admin_ids = cfg['ADMIN_USER_GROUP_IDS'] or DEFAULT_ADMIN_GROUP_IDS
    found = {}

    def roster_probe():
        rows = mp.mp_get('Group_Participants', select=roster.ROSTER_SELECT,
                         filter=roster.roster_filter(cfg['MP_ROSTER_GROUP_ID']), top=1, timeout=mp.SCRIPT_TIMEOUT)
        if rows:
            found['gp_id'] = rows[0].get('Group_Participant_ID')
        return rows

    def gp_probe():
        gp_id = found.get('gp_id') or 1
        return mp.mp_get('Group_Participants', select=GP_SELECT, filter=f'Group_Participant_ID = {mp.q_int(gp_id)}',
                         top=1, timeout=mp.SCRIPT_TIMEOUT)

    def event_probe():
        rows = mp.mp_get('Event_Groups', select=EVENT_SELECT,
                         filter=checkins.event_filter(cfg['MP_ROSTER_GROUP_ID'], today - timedelta(days=365),
                                                      today + timedelta(days=1)),
                         orderby='Event_Groups.Event_ID DESC', top=1, timeout=mp.SCRIPT_TIMEOUT)
        if rows:
            found['event_id'] = rows[0].get('Event_ID')
        return rows

    def checkin_probe():
        return mp.mp_get('Event_Participants', select=CHECKIN_SELECT,
                         filter=checkins.checkins_filter([found.get('event_id') or 1], []),
                         orderby='Event_Participant_ID', top=1, timeout=mp.SCRIPT_TIMEOUT)

    def leaders_probe():
        return mp.mp_get('Group_Participants', select=LEADER_SELECT, filter=leaders_filter(cfg['MP_LEADER_GROUP_ID']),
                         distinct=True, top=1, timeout=mp.SCRIPT_TIMEOUT)

    def admins_probe():
        return mp.mp_get('dp_User_User_Groups', select=ADMIN_SELECT,
                         filter=f'dp_User_User_Groups.User_Group_ID IN ({mp.q_ids(admin_ids)})', top=1,
                         timeout=mp.SCRIPT_TIMEOUT)

    def user_probe():
        return mp.mp_get('dp_Users', select=USER_SELECT, filter=f'dp_Users.User_GUID = {mp.q_guid(DUMMY_GUID)}',
                         top=1, timeout=mp.SCRIPT_TIMEOUT)

    return [
        ('roster', 'Group_Participants, Participants, Contacts', roster_probe, True),
        ('team pre-read', 'Group_Participants', gp_probe, False),
        ('check-in event', 'Event_Groups, Events', event_probe, True),
        ('check-ins', 'Event_Participants, Events', checkin_probe, False),
        ('leaders', 'Group_Participants, Participants, Contacts, dp_Users', leaders_probe, True),
        ('admin groups', 'dp_User_User_Groups', admins_probe, False),
        ('user by GUID', 'dp_Users, Contacts', user_probe, False),
    ]


def explain(error, tables):
    if isinstance(error, mp.MPForbidden):
        return f'403 Forbidden: the API client\'s security role needs Read on {tables}'
    if isinstance(error, mp.MPAuthError):
        return 'authentication failed: check MP_CLIENT_ID / MP_CLIENT_SECRET and the client\'s grant types'
    if isinstance(error, mp.MPBadRequest):
        return '400 Bad Request: the query shape was rejected (a column or table name changed?)'
    if isinstance(error, mp.MPNotFound):
        return '404 Not Found: table not exposed to the API'
    return f'{type(error).__name__}: {error}'


def run_probes(cfg, out=print):
    failed = 0
    for name, tables, probe, expect_rows in probes(cfg):
        try:
            rows = probe()
        except mp.MPError as e:
            failed += 1
            out(f'FAIL  {name:<15} {explain(e, tables)}')
            if isinstance(e, mp.MPAuthError):
                out('      (stopping: every probe needs a token)')
                return failed
            continue
        note = f'{len(rows)} row(s)'
        if expect_rows and not rows:
            note += '; expected data: an empty 200 can also mean a missing Read on a joined table'
        out(f'PASS  {name:<15} {tables} ({note})')
    out('')
    out('all probes passed' if not failed else f'{failed} probe(s) failed')
    return failed


def list_leaders(cfg, out=print):
    rows = mp.mp_get('Group_Participants', select=LEADER_LIST_SELECT, filter=active_filter(cfg['MP_LEADER_GROUP_ID']),
                     orderby='Group_Participants.Group_Participant_ID', timeout=mp.SCRIPT_TIMEOUT)
    leaders = {}
    for r in rows:
        prev = leaders.get(r['Contact_ID'])
        if prev is None or (r.get('User_Account') and not prev.get('User_Account')):
            leaders[r['Contact_ID']] = r
    no_account = [cid for cid, r in leaders.items() if not r.get('User_Account')]
    has_user = set()
    if no_account:
        users = mp.mp_get('dp_Users', select='dp_Users.User_ID,dp_Users.Contact_ID',
                          filter=f'dp_Users.Contact_ID IN ({mp.q_ids(no_account)})', orderby='dp_Users.User_ID',
                          timeout=mp.SCRIPT_TIMEOUT)
        has_user = {u['Contact_ID'] for u in users}
    out(f"Active Group {cfg['MP_LEADER_GROUP_ID']} leaders: {len(leaders)}")
    out(f'{"Contact_ID":>10}  {"Login":<5}  Name')
    for cid, r in sorted(leaders.items(), key=lambda kv: (bool(kv[1].get('User_Account')),
                                                         (kv[1].get('Display_Name') or '').casefold())):
        if r.get('User_Account'):
            login = 'yes'
        elif cid in has_user:
            login = 'no*'
        else:
            login = 'no'
        out(f'{cid:>10}  {login:<5}  {r.get("Display_Name") or ""}')
    out('')
    out(f'{len(leaders) - len(no_account)} can sign in; {len(no_account)} cannot.')
    if has_user:
        out('no* = a dp_Users login exists but Contacts.User_Account is empty: link it on the contact.')
    if len(no_account) > len(has_user):
        out('no  = no MP login: create one on the EXISTING contact (not a self-registered duplicate).')
    return 0


def write_probe(cfg, gp_id, user_id, out=print):
    """Re-save one Group 697 row's Notes unchanged: proves Update on Group_Participants."""
    if (os.environ.get('MP_WRITES_ENABLED') or '').strip().lower() not in ('1', 'true', 'yes', 'on'):
        out('FAIL  write probe: set MP_WRITES_ENABLED=1 to allow the (no-op) PUT')
        return 1
    if not user_id:
        out('FAIL  write probe: --user-id (your dp_Users.User_ID, recorded in the MP audit log) is required')
        return 1
    gp = mp.mp_get_one('Group_Participants', select=GP_SELECT, filter=f'Group_Participant_ID = {mp.q_int(gp_id)}',
                       timeout=mp.SCRIPT_TIMEOUT)
    if gp is None:
        out(f'FAIL  write probe: Group_Participant {gp_id} not found (or not readable)')
        return 1
    if gp.get('Group_ID') != cfg['MP_ROSTER_GROUP_ID']:
        out(f"FAIL  write probe: Group_Participant {gp_id} is not in Group {cfg['MP_ROSTER_GROUP_ID']}; refusing")
        return 1
    try:
        result = mp.mp_put('Group_Participants', [{'Group_Participant_ID': gp_id, 'Notes': gp.get('Notes')}],
                           user_id=user_id, select='Group_Participant_ID,Notes', timeout=mp.SCRIPT_TIMEOUT)
    except mp.MPError as e:
        out(f'FAIL  write probe: {explain(e, "Group_Participants (Update)")}')
        return 1
    if not result or result[0].get('Notes') != gp.get('Notes'):
        out(f'FAIL  write probe: PUT returned but Notes did not round-trip for Group_Participant {gp_id}')
        return 1
    out(f'PASS  write probe: Group_Participant {gp_id} Notes re-saved unchanged as User_ID {user_id}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only MP permission probe for the TNT API client.')
    parser.add_argument('--leaders', action='store_true', help='list active leaders and whether they can sign in')
    parser.add_argument('--write-probe', type=int, metavar='GP_ID',
                        help='PUT a Group 697 Group_Participant row\'s current Notes back (needs MP_WRITES_ENABLED=1)')
    parser.add_argument('--user-id', type=int, help='dp_Users.User_ID recorded as $userId for --write-probe')
    args = parser.parse_args(argv)
    cfg = load_config()
    if not (cfg['MP_CLIENT_ID'] and cfg['MP_CLIENT_SECRET']):
        print('MP_CLIENT_ID and MP_CLIENT_SECRET must be set (the dedicated TNT API client)')
        return 2
    print(f"MP: {cfg['MP_BASE_URL']}  client: {cfg['MP_CLIENT_ID']}")
    print('')
    try:
        if args.write_probe:
            return write_probe(cfg, args.write_probe, args.user_id)
        if args.leaders:
            return list_leaders(cfg)
        return 1 if run_probes(cfg) else 0
    except mp.MPError as e:
        print(f'FAIL  {explain(e, "the tables above")}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
