"""
Who may sign in: active participants of MP Group 1000 (T&T Leaders), plus members of the
MP security User Groups listed in ADMIN_USER_GROUP_IDS (staff). Queries run with the
app's client-credentials token. MP errors propagate; callers decide the policy (fail closed
at sign-in).
"""
import logging
import threading
import time
from dataclasses import dataclass

from models import mp

USER_SELECT = ('dp_Users.User_ID,dp_Users.Contact_ID,Contact_ID_TABLE.Nickname AS Nickname,'
               'Contact_ID_TABLE.First_Name AS First_Name,Contact_ID_TABLE.Last_Name AS Last_Name')
LEADER_SELECT = 'Participant_ID_TABLE_Contact_ID_TABLE.User_Account AS User_ID'
LEADER_ORDERBY = 'Participant_ID_TABLE_Contact_ID_TABLE.User_Account'
ADMIN_SELECT = 'dp_User_User_Groups.User_ID'


def user_filter(guid):
    return f'dp_Users.User_GUID = {mp.q_guid(guid)}'


def leaders_filter(group_id):
    return (f'Group_Participants.Group_ID = {mp.q_int(group_id)} '
            'AND Group_Participants.Start_Date <= GETDATE() '
            'AND (Group_Participants.End_Date IS NULL OR Group_Participants.End_Date >= GETDATE()) '
            'AND Participant_ID_TABLE_Contact_ID_TABLE.User_Account IS NOT NULL')


def admins_filter(group_ids):
    return f'dp_User_User_Groups.User_Group_ID IN ({mp.q_ids(group_ids)})'


log = logging.getLogger(__name__)

_cache = {}
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class MPUser:
    user_id: int
    contact_id: int
    guid: str
    nickname: str | None
    first_name: str | None
    last_name: str | None

    @property
    def display_name(self):
        return f"{self.nickname or self.first_name or ''} {self.last_name or ''}".strip() or 'Leader'


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    via: str | None      # 'leader' | 'admin' | None


def lookup_mp_user(guid, *, timeout=mp.DEFAULT_TIMEOUT):
    """dp_Users by User_GUID (the OIDC `sub`). None if there is no such user."""
    row = mp.mp_get_one('dp_Users', select=USER_SELECT, filter=user_filter(guid), timeout=timeout)
    if not row or not row.get('User_ID') or not row.get('Contact_ID'):
        return None
    return MPUser(user_id=row['User_ID'], contact_id=row['Contact_ID'], guid=mp.normalize_guid(guid),
                  nickname=row.get('Nickname'), first_name=row.get('First_Name'), last_name=row.get('Last_Name'))


FAILURE_CACHE_SECONDS = 60


def _cached(key, max_age, fetch):
    """Wall-clock cache (serverless instances can be paused, which freezes monotonic clocks).
    An MP outage is remembered briefly so later requests fail fast instead of each waiting on MP."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        failed = _cache.get(('failed',) + key)
    if hit and max_age > 0 and now - hit[0] < max_age:
        return hit[1]
    if failed and max_age > 0 and now - failed[0] < FAILURE_CACHE_SECONDS:
        raise failed[1]
    try:
        value = fetch()
    except mp.MPUnavailable as e:
        with _cache_lock:
            _cache[('failed',) + key] = (time.time(), e)
        raise
    with _cache_lock:
        _cache[key] = (time.time(), value)
        _cache.pop(('failed',) + key, None)
    return value


def leader_user_ids(group_id, *, max_age, timeout=mp.DEFAULT_TIMEOUT):
    """User_IDs (Contacts.User_Account) of active participants of the leaders group."""
    def fetch():
        rows = mp.mp_get('Group_Participants', distinct=True, select=LEADER_SELECT,
                         filter=leaders_filter(group_id), orderby=LEADER_ORDERBY, timeout=timeout)
        ids = frozenset(r['User_ID'] for r in rows if r.get('User_ID'))
        if not ids:
            # Almost certainly a permissions/config change, not "nobody leads T&T": don't sign everyone out.
            log.error('auth.leader_set_empty group_id=%s', group_id)
            raise mp.MPUnavailable('empty_leader_set', op='get', table='Group_Participants')
        return ids
    return _cached(('leaders', group_id), max_age, fetch)


def admin_user_ids(group_ids, *, max_age, timeout=mp.DEFAULT_TIMEOUT):
    group_ids = tuple(group_ids)
    if not group_ids:
        return frozenset()

    def fetch():
        rows = mp.mp_get('dp_User_User_Groups', distinct=True, select=ADMIN_SELECT,
                         filter=admins_filter(group_ids), orderby=ADMIN_SELECT, timeout=timeout)
        return frozenset(r['User_ID'] for r in rows if r.get('User_ID'))
    return _cached(('admins', group_ids), max_age, fetch)


def check_access(user_id, *, leader_group_id, admin_group_ids, max_age, timeout=mp.DEFAULT_TIMEOUT):
    """Leader first; the admin groups are only queried for non-leaders."""
    if user_id in leader_user_ids(leader_group_id, max_age=max_age, timeout=timeout):
        return AccessDecision(True, 'leader')
    if user_id in admin_user_ids(admin_group_ids, max_age=max_age, timeout=timeout):
        return AccessDecision(True, 'admin')
    return AccessDecision(False, None)


def clear_cache():
    with _cache_lock:
        _cache.clear()
