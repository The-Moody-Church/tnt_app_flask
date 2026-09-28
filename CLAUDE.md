# TNT App — Claude Context

Mobile-first Flask PWA that Awana T&T leaders use on their phones on Wednesday nights. It tracks each kid's
attendance items (Bible, Shirt, Book, HW, Dues) and completed book sections per team per date, and shows team points.

- **Roster and check-in come from MinistryPlatform (MP):** Group 697 "T&T" is the roster (team color in
  `Group_Participants.Notes`), and MP check-in decides who is "present".
- **App data lives in PostgreSQL** (Neon, project `restless-pond-21520685`, database `tnt`).
- **Deployed on Vercel** (region `cle1`, next to Neon us-east-2).

This repo is The Moody Church's fork of `ovitisler/tnt_app` (the `upstream` remote, reference only). The fork has
diverged: kids are keyed by MP `Contact_ID`, data moved off Google Sheets. Upstream changes won't merge cleanly.

## Running Locally

```bash
docker run -d --name tnt-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17   # once
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                       # fill in DATABASE_URL, MP_CLIENT_ID/SECRET, ...
DATABASE_URL_UNPOOLED=$DATABASE_URL .venv/bin/python scripts/migrate.py
.venv/bin/python tnt.py                    # http://localhost:5001
.venv/bin/python -m pytest tests/ -q       # uses postgres on localhost:55432 (TEST_DATABASE_URL overrides)
```

Pretend it's club night: `TNT_DATE_OVERRIDE=2026-09-23` in `.env`, or `?today=2026-09-23` in the browser
(non-production only; `?today=clear` removes it).

## Architecture

- `tnt.py` — `create_app()` factory (module-level `app` kept); error handlers, `/healthz`, QA date override.
  `app.py` is the Vercel zero-config entrypoint. `config.py` — pure `load_config(env)`.
- `models/`:
  - `db.py` — psycopg 3, one connection per request (Neon pooled URL; `prepare_threshold=None`).
  - `data.py` — all app reads/writes. Pure SQL, no MP calls. snake_case dict rows with real dates.
    Keyword-only `contact_id=`/`meeting_date=`; `team=ALL` means no filter, `team=None` means Unassigned.
    Errors raise `models/errors.py` types and are never swallowed.
  - `mp.py` — MP REST client (client-credentials token cache, typed errors, `q_*` filter-literal helpers).
    Values only enter a `$filter` through `q_int`/`q_ids`/`q_date`/`q_guid`.
  - `roster.py` — Group 697 → `kids` sync, and `assign_team` (writes `Notes` back to MP with `$userId`).
  - `checkins.py` — MP check-in → `attendance` sync; `live_sync(d)` is the request-path entry point.
  - `sync_state.py` — single-flight sync-on-read claims, per-request MP time budget, `mp_health` circuit breaker.
    There are no background threads (they're unreliable on serverless).
  - `fields.py` — column whitelists, labels, team parsing. `utils.py` — `get_today()`, date filters.
- `routes/` — `home.py` (sections), `attendance.py`, `progress.py`, `api.py` (JSON endpoints),
  `auth.py` (MP sign-in + login gate), `responses.py` (JSON contract), `_common.py` (shared helpers).
- `models/access.py` — who may sign in (Group 1000 leaders + `ADMIN_USER_GROUP_IDS` user groups).
- Static files live in `public/static/` (Vercel's CDN serves `public/`; Flask serves them locally at `/static`).
- `migrations/NNNN_*.sql` + `scripts/migrate.py` (owner role, DIRECT url). Grants: `scripts/neon/grants.sql`.
- `scripts/import_sheet.py` — one-time import of the legacy sheet export (dry run by default).
- `scripts/mp_probe.py` — read-only check that the MP API client has the table permissions it needs.

## Data rules (don't break these)

- **Present:** present = MP check-in (status 3/4 with `Time_In`) OR a leader's manual mark.
  - Lists hide kids who aren't checked in; "Show all" reveals them.
  - Bible/Shirt/… can only be newly set for present kids.
- **Team snapshot:** each attendance/section row keeps the team it was first written under, so past totals never
  shift when MP changes a kid's team. Sections take that night's attendance-row team.
  - A NULL (Unassigned) team is filled in only for *today's* rows, when the kid gets a team.
- **Never delete rows.** The app DB role has no DELETE. A section row with all credits false scores 0 and is hidden.
- **No writes for future dates** (`data.is_writable_date`); this closes the old UTC bug class.
- **Totals** are the SQL views `weekly_attendance_totals` / `weekly_totals`, which reproduce the old sheet
  formulas exactly (including the "Total Points : 10375" string = points per kid × 1000). Don't "fix" them.
- **Identity:** kids are keyed by MP `Contact_ID` everywhere (URLs, forms, rows). Never match on names.

## Auth (routes/auth.py)

- MP OIDC via Authlib. `sub` = `dp_Users.User_GUID`. MP's id_token may omit `nonce` (`MPCodeIDToken`);
  PKCE is off unless `MP_OIDC_PKCE=1`. Logout sends `id_token_hint` (MP ignores the redirect without it).
- **Only the OIDC callback writes `session["user"]`** (lesson of GHSA-pqxp-c5mr-5398). Never add an endpoint that
  accepts identity fields. Sessions are bound to `APP_BASE_URL` (`session["aud"]`).
- The gate (`auth_gate`) runs first: anonymous GET → `/login`, anonymous fetch/API POST → 401 JSON. CSRF is
  checked *after* the login check (`WTF_CSRF_CHECK_DEFAULT=False`), so an expired session gets 401, not 400.
- Membership is re-checked hourly on page loads (saves within 6 h of a check never wait on MP). MP outages keep
  existing sessions working for the 35-day grace; sign-in itself always fails closed.
- Every POST follows the JSON contract (`routes/responses.py`): `{"ok": true, "redirect"?}` or
  `{"ok": false, "error", "message"}`. `public/static/app.js` `tnt.post()` is the only client for it; a failed save
  must never look like success.

## MP facts

- Groups: 697 = T&T kids (role 2 = kid; other roles are volunteers); 1000 = T&T Leaders (who may sign in).
- One shared Awana check-in event per Wednesday; find it via `Event_Groups.Group_ID=697`, `[Allow_Check-in]=1`.
- Walk-ins can have `Event_Participants.Group_ID` NULL and Notes "Created by CheckIn" (team = Unassigned).
- MP datetimes are naive Central time (`mp.parse_mp_datetime` attaches America/Chicago).

## Club-night UX

- `/` and `/attendance` open straight to tonight's team (cookie `tnt_team`, set by team pages) on a scheduled,
  non-"NO AWANA" date; `?all=1` shows the date list (`routes/_common.tonight_redirect`).
- Attendance is tap-to-toggle chips → `POST /api/attendance/<d>/<cid>` `{"set": {field: bool}}` (absolute values,
  so retries are safe; JS reconciles only the tapped field and ignores stale Retries).
- Tonight's scheduled section(s) (`schedule.sections`) are pinned on the record-section list with one-tap credits
  → `POST /api/sections/<d>/<cid>`. Credits earned on another night are shown "✓ m/d" and can't be tapped.
- Unassigned kids get "Assign team" (bottom sheet) → `POST /api/kids/<cid>/team`, which writes MP `Notes`; only
  when `MP_WRITES_ENABLED`.
- Toasts appear at the top (never over the rows just tapped); a red toast means "not saved".

## Templates / CSS

- `base.html` (header + bottom nav) and `fullscreen_base.html` (form pages). Shared bits in `_macros.html`.
- Rows are real `<a href>` links built with `url_for` (never `onclick="window.location…"`).
- Tap targets are at least 44px. Hover styles are neutralized on touch screens (`@media (hover: none)`).

## Testing

- Tests run against real Postgres (fixtures in `tests/conftest.py`: `conn`, `seeded`, `client` (signed-in leader),
  `anon_client`, `fake_mp`, `app_ctx`; `login_session()` builds a session the way the callback would).
- `client.post()` sends `X-Requested-With: fetch` like `tnt.post()`; pass `headers={'X-Requested-With': ''}` to
  simulate a native HTML form post.
- MP is always faked (`FakeMP`); `seeded` marks syncs fresh so routes don't call MP.
- There is no fake data layer on purpose: the SQL must stay in step with what routes expect.

## Git

- Remote `origin` = `The-Moody-Church/tnt_app_flask`. Run `gh repo set-default The-Moody-Church/tnt_app_flask`
  once per clone: the `upstream` remote is marked `gh-resolved = base`, so `gh` would otherwise target Ovi's repo.
- Branch + PR for code (merge commits); docs-only changes may go straight to `main`. Push right after committing.
- Never embed tokens in remote URLs.
