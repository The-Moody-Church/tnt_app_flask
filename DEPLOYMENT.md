# Deployment

Production runs on **Vercel** (Python runtime, zero-config Flask via `app.py`, region `cle1`) with the database in
**Neon** (project `restless-pond-21520685`, aws-us-east-2, database `tnt`). There is no TMC1 container.

## 1. MinistryPlatform API client (MP admin, once)

1. **API user and role.**
   - Create the contact "TNT Tracker API" and the dp_User `tnt-tracker-api`. Give it a random password nobody
     knows, and no user groups.
   - Create the security role "TNT Tracker API" and assign it to that user, with these rights:
     - **Read:** Group_Participants, Participants, Contacts, dp_Users, dp_User_User_Groups, Events,
       Event_Groups, Event_Participants.
     - **Update:** Group_Participants (team assignment writes `Notes`).
     - Nothing else.
2. **Administration → API Clients → New.**
   - Client ID `TNT.Tracker`, Client User `tnt-tracker-api`, a generated secret.
   - Access token 60 min, identity token 5 min.
   - Redirect URIs (semicolon-separated, trailing semicolon):
     `https://<prod-host>/auth/callback;https://<staging-host>/auth/callback;http://localhost:5001/auth/callback;`
   - Post-logout redirect URIs: the same hosts with `/logged-out`.
3. **Check each redirect URI.** A 302 to `/oauth/login` means it's registered; a page saying
   "not registered for the client" means it isn't:
   ```bash
   curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
     "https://moody.ministryplatform.com/ministryplatformapi/oauth/connect/authorize?response_type=code&client_id=TNT.Tracker&scope=openid&state=probe&redirect_uri=<urlencoded>"
   ```
4. **Staff user group.** Create a dp_User_Group "TNT Tracker Staff" for staff who aren't in Group 1000, and put
   its ID in `ADMIN_USER_GROUP_IDS`.
5. **Check the permissions:**
   `MP_CLIENT_ID=... MP_CLIENT_SECRET=... python scripts/mp_probe.py`. It is read-only and prints PASS/FAIL for
   each query the app runs.
   - `--leaders` lists Group 1000 leaders who have no MP login. Staff should create logins on those leaders'
     **existing** contacts; self-registered accounts create duplicates that get a 403.
   - `MP_WRITES_ENABLED=1 python scripts/mp_probe.py --write-probe <GP_ID> --user-id <your User_ID>` re-saves
     one kid's unchanged Notes, which proves the Update right.
6. **Sign in on localhost first.** Watch the logs for `auth.claims nonce_present=… id_token_len=…`.
   - Try `MP_OIDC_PKCE=1`, and keep it only if sign-in still works.

## 2. Neon (once)

- **Databases:** create `tnt` (and `tnt_staging` for staging) with SQL as `neondb_owner`, not in the console.
  Then run, against the DIRECT host, for each database:
  ```bash
  APP_PW=$(openssl rand -hex 24); RO_PW=$(openssl rand -hex 24)     # store both in the password manager
  psql "$OWNER_DIRECT_URL" -v app_role=tnt_app -v app_pw="$APP_PW" -v ro_pw="$RO_PW" -f scripts/neon/roles.sql
  DATABASE_URL_UNPOOLED="$OWNER_DIRECT_URL" python scripts/migrate.py
  psql "$OWNER_DIRECT_URL" -f scripts/neon/grants.sql                 # re-run after every migration
  ```
  For staging, use `app_role=tnt_staging_app`.
- **Pooled URL:** Vercel gets the pooled (`-pooler`) URL for `tnt_app`.
- **Don't install the Neon↔Vercel integration.** It injects owner credentials.
- **Don't use a Neon branch for staging.** A branch copies music-db's data.
- **Backups:** add a TNT block to TMC1 `/srv/music-tools/neon-backup.sh` using the `tnt_readonly` role over
  the direct URL. Keep the dump files at mode 600, because they contain kids' names.

## 3. Vercel (once)

1. Import `The-Moody-Church/tnt_app_flask` into The Moody Church team as project `tnt-tracker`. Vercel detects
   the framework as Flask; `vercel.json` pins the region to `cle1`.
2. **Environment variables:**
   - Production: `SECRET_KEY`, `APP_BASE_URL`, `DATABASE_URL`, `MINISTRY_PLATFORM_BASE_URL`, `MP_CLIENT_ID`,
     `MP_CLIENT_SECRET` (mark it Sensitive), `ADMIN_USER_GROUP_IDS`, `HELP_CONTACT`.
   - Preview (the `staging` branch): its own `SECRET_KEY`, `APP_BASE_URL`, the staging `DATABASE_URL`, and
     `MP_WRITES_ENABLED=0`.
3. **Domains:** production (e.g. `tnt.moodychurch.org`), plus a staging domain assigned to the `staging`
   branch. In Cloudflare, set the CNAME to `cname.vercel-dns.com` as DNS-only.
4. **Deployment Protection:** turn it on for previews. Create a "Protection Bypass for Automation" secret and
   save it as the GitHub secret `VERCEL_AUTOMATION_BYPASS_SECRET`; the smoke tests use it.
5. **First preview checks:**
   - `x-vercel-id` shows `cle1`.
   - `/static/style.css` is served by the CDN.
   - `/healthz` returns `{"ok": true, "db": true}`.
   - An anonymous `/` redirects to `/login`.
   - If the zero-config preset misbehaves, fall back to a legacy `builds` entry for `app.py`.

## Everyday operations

- **Schema changes:** add a new `migrations/NNNN_*.sql`. Never edit an applied migration; `migrate.py` refuses.
  Then run `scripts/neon/grants.sql`.
- **Schedule for a new season:**
  `python scripts/import_sheet.py --xlsx <file> --schedule-only` (dry run), then add `--apply`.
- **Force sign-out for everyone:** set `AUTH_NOT_BEFORE` to the current epoch seconds, or rotate `SECRET_KEY`.
- **Rollback:** promote the previous deployment in Vercel. The database schema is forward-compatible.
