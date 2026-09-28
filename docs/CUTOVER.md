# Cutover from the Google Sheet

**Target: Wednesday 2026-10-14.** Fallback: 10/21. Club nights before cutover (9/30, 10/7) run on the old app
and sheet.

## Before cutover

- [ ] **MP admin:** create the API client and role, register redirect and post-logout URIs, and create the
      "TNT Tracker Staff" user group (see DEPLOYMENT.md §1).
- [ ] **Leader logins:** run `scripts/mp_probe.py --leaders`. Staff create MP logins for the leaders who have
      none, on their **existing** contacts.
- [ ] **Neon:** create `tnt` and `tnt_staging` with roles, run migrations and grants, and add the TMC1 backup.
- [ ] **Vercel:** create the project, domains and env vars. Staging deploys and smoke tests pass.
- [ ] **Coordinate with Ovi** (upstream author, active leader, runs `tnt-app-five.vercel.app` and the sheet):
  - [ ] Agree the cutover date.
  - [ ] Agree who freezes the sheet and exports the final copy.
  - [ ] **Go/no-go gate:** before the freeze, the old deployment must redirect to the new URL, or block POSTs.
        The old app swallows write errors, so a leader on the old icon would lose data silently.
- [ ] **Dry-run import on staging** with a fresh export. Review the report with the director:
  - [ ] Re-dated rows, merged duplicates and name matches.
  - [ ] The before/after team totals. Standings change because Present now comes from MP check-in (for
        example, Green on 9/16 goes from 10 to 17 kids present).
  - [ ] Confirm Imara Butler's team (Blue in the sheet, Red in MP).
- [ ] **Shadow night 10/7:** compare staging's read-only tonight view against the kiosk at 18:00, 18:45 and
      19:30.
- [ ] **Device checks on real phones:** iPhone Safari, **iPhone home-screen app** (sign in, background the app
      for 2 minutes, sign out; also kill the app mid-sign-in), and Android.
  - [ ] If home-screen sign-in fails on iPhone, set the manifest `display` to `browser` and remove
        `apple-mobile-web-app-capable` so the icon opens in Safari.

## Cutover day

1. **12:00:** the old deployment redirects to the new URL, and the sheet is frozen. Export the xlsx.
2. Import:
   ```bash
   DATABASE_URL_UNPOOLED=<owner direct url for tnt> MP_CLIENT_ID=... MP_CLIENT_SECRET=... \
     python scripts/import_sheet.py --xlsx <export>.xlsx --through <last club date> --report import_reports/cutover.md
   ```
   Review the report, then re-run with `--apply`. Parity A and parity B must both show 0 mismatches.
3. **Verify:**
   - The team cards on `/home/<date>` for each past date match the approved "after" table.
   - Spot-check 3 kids' Progress pages against the sheet.
4. **Roster:** the account page's "Refresh kids from MinistryPlatform" shows about 76 kids.
5. Production smoke tests pass, and a real sign-in works (yours, or Ovi's).
6. **15:00:** announce the new link to leaders (Group 1000). Tell them:
   - delete the old home-screen icon and add the new one;
   - sign in with MP;
   - here's the GUIDE.
7. **18:00–20:30:** on call, watching the Vercel logs.
   - If the app is down for more than 10 minutes: unfreeze the sheet, point leaders back to the old app, and
     reconcile the night by hand.
8. **Next morning:** check the totals, confirm the Neon backup ran, and collect feedback.

**Rollback before club:** unfreeze the sheet and point leaders back to the old app. The sheet was frozen at the
export, so nothing is lost.
