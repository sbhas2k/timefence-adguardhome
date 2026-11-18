# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [0.5.0] - 2026-11-18

### Added

- **Bedtime windows.** A group can carry up to four daily no-go windows
  (start/end, every day, weekdays or weekends), including windows that cross
  midnight. A group in a window is blocked even if minutes are left and no
  minutes are used up; the kid screen shows "Bedtime until 07:00". An admin's
  "Allow freely until" still overrides a window.
- **Alerts.** One ntfy topic or one webhook, configured under Settings →
  Alerts: budget low (10 minutes left), budget used up, bedtime started
  (default off), AdGuard Home unreachable and reachable again, and a weekly
  summary (default off, Monday morning). Messages carry a group name and
  numbers only; per-day de-duplication and a 20-per-hour cap keep them quiet,
  and delivery failures are reduced to a category before they leave the sender.
- **Usage summary and CSV export.** The History tab shows 7, 14 or 30 days per
  group from a dedicated endpoint (two queries regardless of groups and days),
  with metered minutes, bonus minutes and the minutes allowed that day, plus a
  "Download CSV" link (`group,date,used_minutes,bonus_minutes`, formula-safe).
  `/api/state.history` keeps its existing 7-day shape.

### Changed

- **Schema v5.** The database adds `group_windows` and `alerts_sent`; the v5
  step is plain DDL, so `timefence passwd` can upgrade a v4 database without
  starting the server.
- **One verified backup per upgrade.** A `migrate()` call now writes exactly one
  verified copy before its first schema change, named
  `timefence.db.pre-v<schema>.bak` for the highest schema it reaches
  (`pre-v5.bak` for 0.4 → 0.5, `pre-v4.bak` for a 0.3 → 0.4 run). A fresh
  install writes none; `TIMEFENCE_SKIP_MIGRATION_BACKUP=1` still skips it.
- **The default-password check runs once per schema version.** If an admin
  account still uses `admin`, first-run setup is required again after the
  upgrade.
- **Read-only users** still see only their own and shared groups; the usage and
  alert endpoints are admin-only (`403` for `user` accounts).

### Notes

- **Alert secrets live in the database.** The ntfy topic and token and the
  webhook URL are stored in plain text in SQLite and therefore in every backup;
  `GET /api/alerts` only returns a boolean and the last two characters of a
  tail.
- **Outbound requests.** Only `http`/`https`, never following redirects, no
  environment proxies, one attempt, a 3 s connect and 2 s read timeout per
  socket operation, and at most 1 KiB of the response body is read and
  discarded. Private/LAN targets are allowed on purpose.
- **Bedtime windows block house-wide.** AdGuard's blocked-services list is
  global, so a window on a group that owns a service takes that service away
  from every device for the window.

## [0.4.0] - 2026-11-18

### Breaking

- **Session mode is gone.** Every group is now "allowed until the day's minutes
  are used up". `15/30/60` buttons, start/stop, expiry billing and
  `/api/session/*` were removed; `/api/groups` rejects `"mode": "session"` with
  `400 mode_removed`.
- **Schema v4.** The database is migrated on start; when the upgrade starts from
  v3 or older, a verified copy is written next to it as
  `timefence.db.pre-v4.bak`. The old `sessions` rows are kept read-only as
  history.
- **Everyone signs in again.** Cookies now carry the account id and a session
  epoch, so all 0.3.0 cookies are invalid.
- **`user` accounts are read-only.** They see their own and shared groups on a
  dedicated kid screen; every mutating API call is refused with `403 read_only`.
  `/api/admin/credentials` is gone; admins change their own password with
  `POST /api/account/password`.
- **Stopping TimeFence now blocks every managed group until it starts again.**
  Use `timefence release` when you stop it on purpose.
- **Docker defaults changed.** The image listens on `LISTEN_HOST=127.0.0.1` and
  sets `SECURE_COOKIES=true`. Bridge networking needs
  `-e LISTEN_HOST=0.0.0.0 -p 127.0.0.1:8099:8099` (the example compose file does
  it); `docker run` without `LISTEN_HOST` only listens inside the container.
  `:latest` moves with every release — pin a tag for controlled upgrades.
- **`/healthz` is honest.** It returns `503 {"ok":false,"status":"degraded"}`
  when the reconciler has not synced for three intervals, so container
  supervisors see an unhealthy app. Do not wire an auto-restart to it.
- uvicorn is installed without its `[standard]` extras (no `websockets`,
  `uvloop`, `httptools`, `watchfiles`, `python-dotenv`, `pyyaml`, `colorama`).

### Added

- Dedicated read-only kid screen (phone-first, auto-refreshing) and a role-aware
  landing page.
- Server-side group `status` and `blocked_until`, and an admin state payload
  with warnings (AdGuard unreachable, query log disabled/short/ignored domains,
  `TZ` still `UTC`).
- `timefence release` (and an uninstall step) that lifts the managed blocks.
- Change-password card in the admin UI, backed by `POST /api/account/password`.
- `scripts/migrate_legacy.py` writes accounts into `users`, requires `--tz`, and
  cannot leave the old password working.
- Atomic, verified migrations; one store lock serialising all database access
  (requests, reconciler, shutdown); AdGuard HTTP calls made outside that lock.
- Query-log paging, 15-second activity buckets, midnight-split accounting,
  `MAX_CATCHUP` after downtime, and a `querylog_*` health warning set.
- Remote VM guide (`docs/remote-vm.md`), a DDNS allow-list helper with systemd
  timer, and `shellcheck` in CI.
- `requirements.lock` with hashes, digest-pinned base image, SHA-pinned actions,
  `pip-audit` and `gitleaks` CI jobs, CodeQL and Dependabot configuration.
- `TZ` startup warning and a `CHANGE ME` placeholder in `.env.example`.

### Changed

- Usage is counted in 15-second blocks with real DNS activity. Light or
  background use is charged less than in 0.3.0, so the same daily minutes can
  last longer. Blocked answers and free-allow windows are never charged, and a
  negative bonus can no longer erase metered time.
- Login is JSON-only, counts failures per account+address (5) and per address
  (20), runs a dummy hash for unknown accounts, and limits concurrent key
  derivation. The client IP is the right-most untrusted hop of
  `X-Forwarded-For`; uvicorn's own proxy-header parsing is disabled.
- Disabling, deleting, demoting or renaming an account, and changing a password,
  now revoke that account's existing cookies.
- The admin UI guards every quick action against double submission, confirms
  with toasts, keeps typed input and focus through polling, and takes the group
  status from the server.
- `/healthz` reports `{ok,status,version}`; the container health check reads
  `LISTEN_PORT` and `BASE_PATH` and treats `503` as unhealthy.
- History dates are computed from the local date, so DST days and
  `blocked_until` are correct.
- Docker compose uses a 20-second stop grace period so the shutdown block can
  finish.

### Fixed

- First-run redirect loop, and the default-login hint now disappears once setup
  is complete.
- Change-password wrote to the retired credentials table and returned 500 for
  `user` accounts.
- Another account's successful sign-in no longer clears a lockout, and the
  lockout limiter no longer grows without bound.
- Negative bonus erased metered usage ("used" could go down); a shared
  `# timefence:begin` marker without its `end` could delete foreign rules.
- `timefence passwd` hung when stdin was not a terminal (Windows, containers).
- A migration failure could leave a half-migrated database.
- `discover.parse_time` mishandled negative UTC offsets.
- An unexpected exception from AdGuard aborted the whole reconcile pass instead
  of one phase.
- The SQLite file (which holds password hashes) and the data directory now have
  private modes (`umask 077`, `0600`, `0700`).

### Security

- Runtime dependencies bumped past every known advisory (starlette
  CVE-2025-62727 and others); `pip-audit --require-hashes -r requirements.lock`
  reports nothing. CI fails on a new advisory.
- `Secure` cookies are the default; the login page explains plain-HTTP
  behaviour and reports a cookie the browser refused to store.
- Read-only accounts are enforced on the server, and the user state payload no
  longer includes services, domains or history.
- The image runs from a hash-locked requirements file on a digest-pinned base,
  actions are pinned by commit SHA, and `gitleaks` scans the history.
- `.gitignore` covers `adguard_password.txt`, `*.bak` and `session-secret`.

## [0.3.0] - 2026-11-17

### Added

- Per-account budgets: a group can be owned by a `user` account, so each child
  gets their own budget.
- Decrease and custom bonus minutes (`−15`, any amount), plus `Reset today`.

### Changed

- Docker: host-networking recipes, and the entrypoint honours `LISTEN_HOST`.
- The SQLite file is forced to mode 0600 (it stores password hashes).

## [0.2.0] - 2026-11-17

### Changed

- **Groups are allowed by default.** A group now has a `mode`:
  - `auto` (new default) — nothing is blocked until the day's minutes are used
    up, then the group is blocked until midnight. Usage is metered from the
    AdGuard query log (see below).
  - `session` — the previous behaviour: blocked unless a session is running.
- **Multiple accounts.** Login is now username + password. Each account is
  either `admin` or `user`; admins manage groups/users, users can see status and
  start/stop sessions for session-mode groups. The shared "user PIN" is gone
  (the old PIN becomes the password of an account called `user`).
- `Block now` sets a force-block until local midnight (instead of pausing the
  group), and can be cleared with `Unblock`.
- The first-run setup only asks for a new admin password.

### Added

- Usage metering: every reconcile tick, query-log activity for a group's
  domains/services charges the elapsed seconds to that group (loopback queries
  ignored, never above the budget).
- Users admin UI (create, change password, promote/demote, disable, delete) with
  guards for the last admin and self-deletion.
- Mobile layout pass: 2-column tabs, stacked controls, 16px inputs (no iOS
  zoom), safe-area insets, larger tap targets.

### Removed

- The 4–8 digit shared PIN login and the PIN fields in the setup screen.

### Added

- `timefence passwd --role admin|user` recovery command (offline password/PIN
  reset, works while the service runs).
- `timefence.credentials` module shared by the web app and the CLI, with
  explicit tests for legacy Werkzeug `scrypt:` hashes.

### Changed

- Login hashes are upgraded to Argon2id automatically after a successful
  sign-in with a legacy hash.

## [0.1.0] - 2026-11-13

### Added

- First release of TimeFence as a standalone project (successor of the
  single-purpose `screentime` app).
- Unlimited groups with weekday/weekend budgets, AdGuard blocked services
  and/or custom domains.
- Default-blocked behaviour, timed sessions (15/30/60, capped at the remaining
  budget), stop with per-second billing, daily reset in the configured
  timezone.
- Reconciler every 30 s plus immediately after each action; only writes to
  AdGuard when the desired state differs; preserves foreign services, the
  schedule object and foreign user rules.
- Fail-safe startup: open sessions are closed and everything is re-blocked.
- Admin tools: bonus minutes, free-allow until HH:MM, block now, pause, edit,
  delete (history kept), per-group rule test.
- Discover: recent query-log domains grouped by registrable domain (offline
  Public Suffix List), noise filtering, add to group.
- Shared/critical-domain guard with explanations.
- Two roles (admin password, shared user PIN), Argon2 hashing, login lockout,
  CSRF protection, CSP/security headers, forced first-run setup.
- SQLite storage with versioned migrations, event log, 7-day history.
- Packaging: Dockerfile (non-root, healthcheck), docker-compose example,
  systemd unit, nginx/Caddy examples, idempotent install/uninstall scripts.
- Tests (pytest, mocked AdGuard), ruff, GitHub Actions (tests + multi-arch
  image to GHCR on `v*` tags).
