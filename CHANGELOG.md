# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-19

The first public release since 0.3.0. The 0.4 and 0.5 work was developed
together and is released as one version, so this entry covers everything a
0.3.0 deployment has to know before upgrading. See
[docs/upgrading.md](docs/upgrading.md) for the steps.

### Breaking

- **Session mode is gone.** Every group is "allowed until the day's minutes are
  used up". The `15/30/60` buttons, start/stop, expiry billing and
  `/api/session/*` were removed, and `/api/groups` rejects `"mode": "session"`
  with `400 mode_removed`.
- **The shared user PIN is gone; accounts replace it.** A child signs in with
  their own `user` account on a dedicated kid screen and can only view time
  left; every mutating API call is refused with `403 read_only`. Admins change
  their own password with `POST /api/account/password`;
  `/api/admin/credentials` is gone.
- **Everyone signs in again.** Cookies carry the account id and a session epoch,
  so every 0.3.0 cookie is invalid.
- **The database moves to schema v5** (`group_windows` and `alerts_sent`), applied
  on start. Before the first change the migration writes one verified copy next
  to the database as `timefence.db.pre-v5.bak` for any database older than v5; a
  fresh install writes none. A database from a newer schema is refused instead of
  being touched. The 0.3.0 `sessions` table is kept read-only as history; nothing
  reads it for budgets.
- **Stopping TimeFence blocks every managed group** until it starts again; run
  `timefence release` when you stop it on purpose (`uninstall.sh` does).
- **Docker defaults changed.** The image listens on `LISTEN_HOST=127.0.0.1` and
  sets `SECURE_COOKIES=true`. Bridge networking needs `LISTEN_HOST=0.0.0.0` and a
  published port (the example compose file binds `127.0.0.1:8099`); `docker run`
  without `LISTEN_HOST` only listens inside the container. Pin a release tag (or
  a digest) for controlled upgrades; `:latest` moves with every release.
- **`TZ` is no longer forced to UTC.** Without it the app follows the host
  (`/etc/timezone`, then `/etc/localtime`) and falls back to UTC only when
  neither is available, so budgets reset at local midnight. Set `TZ=UTC` to keep
  the old reset time.
- **Usage is metered in 15-second activity blocks.** Light or background use is
  charged less than in 0.3.0, so the same daily minutes can last longer.
  Blocked answers and free-allow windows are never charged.
- **Logout signs the account out everywhere.** It bumps the session epoch before
  clearing the cookie, so a copied cookie stops working too.
- **A rejected AdGuard credential ends the pass and backs off.** AdGuard Home
  locks a source address out for 15 minutes after five failed authentications,
  so TimeFence now stops at the first `401`/`403` and retries after 30 s, then
  doubling to 15 minutes (reset by the first success or by the admin connection
  test) instead of retrying into the lock. The banner names the rejection and
  the next attempt, and `/healthz` stays degraded until a pass succeeds.

### Added

- Dedicated read-only kid screen (phone-first, auto-refreshing) and a role-aware
  landing page.
- Server-side group `status` and `blocked_until`, and an admin state payload with
  warnings (AdGuard unreachable, query log disabled/short/ignored domains, `TZ`
  unresolved).
- `timefence release` (and an uninstall step) that lifts the managed blocks.
- Change-password card in the admin UI, backed by `POST /api/account/password`.
- `scripts/migrate_legacy.py` writes accounts into `users`, requires `--tz`, and
  cannot leave the old password working.
- Atomic, verified migrations; one store lock serialising all database access
  (requests, reconciler, shutdown); AdGuard HTTP calls made outside that lock.
- Query-log paging, 15-second activity buckets, midnight-split accounting,
  `MAX_CATCHUP` after downtime, and a `querylog_*` health warning set.
- Remote VM guide (`docs/remote-vm.md`), a DDNS allow-list helper with a systemd
  timer, and `shellcheck` in CI.
- A manual update script (`deploy/update.sh`): pull, build from the checkout,
  restart, health-check, and `--rollback <tag>` to the image you had before.
- `requirements.lock` with hashes, a digest-pinned base image, SHA-pinned
  actions, `pip-audit`, `gitleaks` and CodeQL CI jobs, and Dependabot
  configuration.
- **Bedtime windows.** Up to four daily no-go windows per group (start/end, every
  day, weekdays or weekends), including windows that cross midnight. A group in
  a window is blocked even if minutes are left and no minutes are used up; the
  kid screen shows "Bedtime until 07:00". An admin's "Allow freely until" still
  overrides a window. AdGuard's blocked-services list is global, so a window on a
  group that owns a service takes that service away from every device for the
  window.
- **Alerts.** One Telegram bot, one ntfy topic or one webhook under Settings →
  Alerts: budget low (10 minutes left), budget used up, bedtime started (default
  off), AdGuard Home unreachable and reachable again, and a weekly summary
  (default off, Monday morning). Messages carry a group name and numbers only;
  de-duplication and a 20-per-hour cap keep them quiet, and delivery failures are
  reduced to a category before they leave the sender. The bot token is never
  echoed back (two-character tails only) and a delivery cannot print it. Until a
  channel is configured, the Today tab offers a **Set up alerts** button.
- **Troubleshooting guide** (`docs/troubleshooting.md`) for the case "the
  minutes are gone but the device still works": IPv6 bypassing the DNS, apps
  with their own resolver, caches, a remote VM that sees one home address, and
  AdGuard's per-client settings.
- **VM deployment guide** (`docs/vm-deploy.md`): AdGuard Home and TimeFence on a
  VM for a home network in order (firewall, containers, DDNS, the allow-list
  helper, closed-to-the-world check, router DNS), including why a second public
  DNS server defeats the blocking.
- **Usage summary and CSV export.** The History tab shows 7, 14 or 30 days per
  group from a dedicated endpoint (two queries regardless of groups and days)
  with metered minutes, bonus minutes and the minutes allowed that day, plus a
  "Download CSV" link (`group,date,used_minutes,bonus_minutes`, formula-safe).
  `/api/state.history` keeps its 7-day shape. The usage summary and CSV
  endpoints are admin-only.
- `.dockerignore`, GitHub issue templates for bugs and feature requests, and a
  release workflow that runs lint and the test suite before it publishes an
  image.

### Changed

- **The timezone is detected, then reported.** `TZ` when set (validated;
  `:Area/City` and zoneinfo paths accepted), otherwise the host's
  `/etc/timezone`, the resolved `/etc/localtime`, or a content match against the
  zone database for a bind-mounted copy, and only then UTC. The admin warning and
  one startup log line naming the source appear only for the UTC fallback.
- **One verified backup per upgrade.** A `migrate()` call writes exactly one
  verified copy before its first schema change, named after the schema it reaches
  (`timefence.db.pre-v5.bak`). `TIMEFENCE_SKIP_MIGRATION_BACKUP=1` skips it.
- **The default-password check runs once per schema version.** If an admin still
  uses `admin`, first-run setup is required again after the upgrade, and
  completing it disables any other admin on the default password.
- **Login is JSON-only** and counts failures per account+address (5) and per
  address (20), runs a dummy hash for unknown accounts, and bounds concurrent key
  derivation. The client IP is the right-most untrusted hop of
  `X-Forwarded-For`; uvicorn's own proxy-header parsing is disabled.
- **Disabling, deleting, demoting or password-changing an account revokes its
  existing cookies**, and deleting an account leaves its groups in place as
  shared groups.
- The admin UI guards every quick action against double submission, confirms with
  toasts, keeps typed input and focus through polling, and takes group status
  from the server.
- `/healthz` reports `{ok,status,version}` and returns `503` after three intervals
  without a successful sync; the container health check reads `LISTEN_PORT` and
  `BASE_PATH` and treats `503` as unhealthy (do not auto-restart on it).
- History dates are computed from the local date, so DST days and `blocked_until`
  are correct.
- **Mobile polish**: the header stays on one line at 390 px, checkbox rows and
  icon-only buttons are 44 px targets, and the Settings save buttons have
  distinct accessible names.
- **Readable alert status**: the Settings card shows "Delivered at HH:MM" or a
  plain reason instead of the internal category, and a stored secret as
  "Saved (ends with ab)".
- **Stricter admin input**: `services` and `domains` must be lists, an updated
  group name is capped at 60 characters, a service icon must be plain base64,
  and a sign-in body over 16 KiB is refused with `413` before parsing (other JSON
  bodies are bounded at 1 MiB).
- **`timefence passwd` checks the stored role**: `--role` must match the account,
  and the password rules are applied to that role.
- **The installer treats its environment file as data**: it parses `KEY=VALUE`
  lines instead of sourcing them, quotes every value it writes, installs from
  `requirements.lock` with `--require-hashes` before the package with
  `--no-deps`, and probes `${BASE_PATH}/healthz`.
- **The DDNS helper records every address it adds** and removes all but the
  current one, so an intermediate or half-added address cannot keep port 53
  open.
- **The image drops its own `/etc/localtime` and `/etc/timezone`**, so a host
  mount arrives as a regular file and can be matched; without a mount the app
  falls back to UTC and says so.
- **Compose files wait 45 s for a clean stop** (`stop_grace_period`) and their
  headers point at the Docker guide with the loopback URL, and uvicorn is
  installed without its `[standard]` extras.

### Security

- Runtime dependencies are bumped past every known advisory (starlette
  CVE-2025-62727 and others); `pip-audit --require-hashes -r requirements.lock`
  reports nothing, and CI fails on a new advisory.
- `Secure` cookies are the default; the login page explains plain-HTTP behaviour
  and reports a cookie the browser refused to store.
- Read-only accounts are enforced on the server, and the kid state payload no
  longer includes services, domains, owner names or history.
- Login attempts are rate-limited per account+address and per address, unknown
  and disabled accounts get a dummy Argon2 verification, and the session cookie
  names the account and its revocation epoch.
- The image runs from a hash-locked requirements file on a digest-pinned base,
  actions are pinned by commit SHA, and `gitleaks` scans the history.
- **Alert targets are admin input and reach the network the app runs on**
  (loopback, LAN or a metadata address); the Test button reports only a
  category, which is still a blind port/status probe. Private targets are allowed
  on purpose, and each delivery is abandoned after 5 seconds in total.
- **Alert secrets live in the database** (plain text in SQLite, so in every
  backup): the API returns a boolean and the last two characters of a tail, and
  the event log never sees them.
- `.gitignore` covers `adguard_password.txt`, `*.bak` and `session-secret`.

### Fixed

- First-run redirect loop; the default-login hint disappears once setup is
  complete.
- Change-password wrote to the retired credentials table and returned 500 for
  `user` accounts.
- Another account's successful sign-in no longer clears a lockout, and the
  lockout limiter no longer grows without bound.
- A negative bonus could erase metered usage ("used" went down); a shared
  `# timefence:begin` marker without its `end` could delete foreign rules.
- A bedtime alert sent again after midnight for the same window, and a "budget
  low" message fired during a free window.
- Deleting an account left a dangling group owner.
- A trickling alert server could hold the delivery worker indefinitely.
- `timefence passwd` hung when stdin was not a terminal (Windows, containers),
  and it applied the wrong password rules when `--role` did not match the
  account.
- A quoted value in the environment file kept its backslash escapes.
- A migration failure could leave a half-migrated database.
- A connection failure or timeout in the admin banner is a short sentence
  ("cannot reach AdGuard Home", "AdGuard Home did not answer within 10 s")
  instead of raw transport text with the host, port and path.
- The group **Test** screen no longer calls a SafeSearch or `Rewrite*` answer
  "blocked": it now uses the same list as metering, where only a refused answer
  is a block and a rewritten one is a real answer.
- The group **Test** screen now probes a representative host of each service: the
  real catalogue is sorted alphabetically, so a YouTube group used to test
  `ggpht.cn` and never `youtube.com`.
- The query-log health check now runs once a minute instead of every ten, so a
  log that is switched off (or back on) is reported within a minute instead of
  up to ten minutes of usage going uncounted.
- Large service icons (the real catalogue has one at ~9.9k characters) were
  dropped from the picker; the cap now covers them, and the base64-only check
  still rejects anything else.
- Service rules written with a single leading pipe (`|host^`, 21 in the real
  catalogue) were ignored for metering, and a `$modifier` suffix turned
  `||qq.com^$denyallow=…` into a junk hostname. One parser now handles both
  forms, drops the modifier, and returns nothing for wildcards and regexes.
- Query-log paging lost entries that shared the page's oldest timestamp (busy
  clients can produce several with one stamp), so a busy minute could go
  uncharged. Pages are now large and de-duplicated by entry key, and the cursor
  steps past the boundary stamp.
- `discover.parse_time` mishandled negative UTC offsets.
- An unexpected exception from AdGuard aborted the whole reconcile pass instead
  of one phase.
- The SQLite file (which holds password hashes) and the data directory have
  private modes (`umask 077`, `0600`, `0700`).
- The warning that AdGuard's query-log ignore list hides a group's domain now
  appears in the admin banner, and it is raised only when ignoring is actually
  enabled and the entry really covers the domain (a plain entry hides that exact
  host; only the anchored form covers subdomains).
- Docs: the backup and restore recipes, the upgrade and compatibility tables, the
  reverse-proxy and bridge-network notes, the outage-alert minimum and the
  changelog's backup name now match the code.

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
