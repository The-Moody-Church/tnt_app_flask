# TNT Reading Teams Tracker

Mobile-first web app that The Moody Church's Awana T&T leaders use on their phones on Wednesday nights. It
records each kid's attendance items (Bible, Shirt, Book, Homework, Dues) and completed book sections
(Main / Silver / Gold) per team, and shows the team points.

- **Sign-in:** leaders sign in with their MinistryPlatform (MP) account. Only T&T Leaders (MP Group 1000) and
  configured staff get in.
- **Roster:** comes from MP Group 697 (T&T). Each kid's team (Red / Blue / Green / Yellow) is stored in MP.
- **Check-in:** who is "here" comes from MP check-in at the kiosk. Leaders can mark a kid present when they
  weren't checked in.
- **Data:** attendance, sections and the schedule live in PostgreSQL (Neon). The totals match the original
  Google Sheet formulas.

Forked from [ovitisler/tnt_app](https://github.com/ovitisler/tnt_app) (MIT). This fork has diverged and does not
track upstream.

## Local development

```bash
docker run -d --name tnt-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:17
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env        # set DATABASE_URL, MP_CLIENT_ID/SECRET, SECRET_KEY, TNT_DATE_OVERRIDE, ...
DATABASE_URL_UNPOOLED="$DATABASE_URL" .venv/bin/python scripts/migrate.py
.venv/bin/python tnt.py     # http://localhost:5001 (sign in needs the MP client's localhost redirect URI)
```

Tests run against a real Postgres (the container above) with MinistryPlatform faked:

```bash
.venv/bin/python -m pytest tests/ -q
```

## Environment variables

`.env.example` documents every variable. The important ones:

| Variable | Where | Purpose |
|---|---|---|
| `SECRET_KEY` | all | Session signing (32+ random chars; different per environment) |
| `APP_BASE_URL` | all | Exact origin registered on the MP API client |
| `DATABASE_URL` | Vercel | Neon **pooled** URL, least-privilege role `tnt_app` |
| `DATABASE_URL_UNPOOLED` | laptop/CI only | Owner role, direct host: migrations and the sheet import |
| `MINISTRY_PLATFORM_BASE_URL` | all | `https://moody.ministryplatform.com/ministryplatformapi` |
| `MP_CLIENT_ID`, `MP_CLIENT_SECRET` | all | The dedicated TNT API client (sign-in + server calls) |
| `ADMIN_USER_GROUP_IDS` | all | MP security User Groups whose members may also sign in |
| `MP_WRITES_ENABLED` | all | Team assignment writes to MP (on by default only in production) |

## Docs

- [DEPLOYMENT.md](DEPLOYMENT.md): Vercel, Neon and MP API client setup.
- [GUIDE.md](GUIDE.md): leader-facing how-to.
- [docs/CUTOVER.md](docs/CUTOVER.md): moving off the Google Sheet.
- [CLAUDE.md](CLAUDE.md): architecture and data rules.

## License

MIT, see [LICENSE](LICENSE).
