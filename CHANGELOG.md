# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-27

Nothing has to be reconfigured by hand. The database moves to schema v7 and the
migration writes one verified copy first (`timefence.db.pre-v7.bak`), so a 1.0.x
install upgrades on start and keeps every schedule it already had.

### Added

- **Any days you pick per allowed time.** A window used to be limited to every
  day, weekdays or weekends; it now names any set of days (`mon,fri`,
  `sat,sun`, all seven). The editor's Days select becomes seven day chips plus
  "Every day", and the Today card adds the picked days to the schedule line,
  e.g. "Allowed 16:00–18:00 (Mon, Fri)". The chips read "Mo Tu We Th Fr Sa Su",
  so Tuesday and Thursday (and Saturday and Sunday) are no longer the same
  letter; the full day name stays in the tooltip and the screen-reader label. Windows that wrap midnight are now
  expressible per schedule, for example `18:00 – 01:00` on weekdays, and the day
  list still names the day the window starts on.
- **Extra time and Clear on the Today card.** A free group's panel now ends with
  a Clear button (the existing `/free/clear` route) and its apply button is the
  one-tap **Extra time**, defaulting to an hour ahead in the server's timezone.
  The kid screen keeps showing "Free time until HH:MM" with no controls.
- **An "All groups" row on the Today screen.** One `Block | Auto | Free`
  control for the whole house, the same segmented control the cards use, so the
  choice is stored on every group and the 30-second reconcile keeps it. `Block`
  blocks every group that is not paused until local midnight; `Free` opens a
  time panel and, once applied, frees every group until then and clears any
  stored block so it cannot come back; `Auto` clears both and the allowed times
  take over again. A paused group stays paused and never receives Block or
  Free, and any Block or Free it carried is cancelled, so resuming it lands on
  its schedule. `Block` and `Auto` ask first and say what will happen, `Free` only
  asks for the time. A segment is highlighted only when every group agrees, and
  the row disappears when every group is paused. The answer says whether
  AdGuard Home has applied it yet; if it has not, the change is saved and the
  next reconcile pass applies it. One Apply at a time: a second tap while the
  first is in flight is ignored, and a failed one keeps the chosen time.
- **Two starter presets** for a new group: School day (09:00–17:00 on weekdays,
  09:00–18:00 at weekends) and No late nights (18:00–01:00 on weekdays,
  17:00–01:00 at weekends). They only fill the editor's fields — nothing is
  written until the group is saved — and they ship **windows only**: no AdGuard
  service catalogue was available to verify a domain list against, so the
  groups' own domains field stays in charge of targets.

### Changed

- The database is **schema v7**. The migration recreates `group_windows` with a
  free-form day list and maps the old values (`all` → `all`, `weekday` →
  `mon,tue,wed,thu,fri`, `weekend` → `sat,sun`). Because that step rewrites
  rows, `timefence passwd` asks for one server start first, exactly like a data
  migration from an older schema.
- The day-picker strings and the "days must be all, weekdays or weekends" error
  texts were replaced with the day-list wording.

### Fixed

- **Times are always 24-hour.** Some views printed the browser's 12-hour form
  ("09:00 PM") next to a 24-hour one ("21:00"), so one screen could show both.
  Every time the app prints is now 24-hour `HH:MM` in the server's timezone,
  on the kid screen as well. The time picker itself still follows the browser,
  as it has to.
- **A Free time on a clock-change day ends at the time you typed.** A time
  inside a spring-forward gap (the hour the clock skips) now lands on the gap
  edge instead of an hour later, and a time inside an autumn repeated hour
  takes the pass that is still ahead, so a free window is never stretched past
  the wall time. The same rule as the allowed times.
- **The AdGuard Home address is printed without the password in it.** If
  `ADGUARD_URL` carried credentials (`http://user:password@host`), the startup
  log line and `timefence check` printed them as typed. The address is now shown
  with `***` in place of everything before the host, and the client still gets
  exactly the URL you configured.
- **Focus stays put when a refresh lands while a name holds an odd character.**
  The editor rebuilds a control's identity to put focus back after a poll, and
  it did not escape a backslash or a line break in the value; a name with one of
  those could lose focus and the field's place in the page.
- **A future time now says which day it belongs to.** A group allowed only on
  Saturdays said "Opens at 16:00" every day of the week, and on a Sunday that
  is six days early. The kid screen and the Today card now say "Opens tomorrow
  at 16:00" or "Opens Sat at 16:00", the alert message names the day the same
  way, and a free time too: at 09:00, "Free until tomorrow at 08:00" no longer
  reads as a time already past. An ordinary overnight end ("Open until 07:00")
  keeps its bare time; only a group held open for a whole day or more by chained
  allowed times shows "Open until tomorrow at 20:00". The day comes from the
  server's timezone, so a kid's phone clock and zone never decide it.
- **The day chips fit on one line on a phone.** In the group editor the seven
  weekday chips were large two-column buttons; they now share one line at every
  phone width with "Every day" on its own line below, and the page never
  scrolls sideways, down to a 320 px screen.

## [1.0.0] - 2026-11-24

The first public release since 0.3.0. The 0.4 and 0.5 work was developed
together and is released as one version, so this entry covers everything a
0.3.0 deployment has to know before upgrading. See
[docs/upgrading.md](docs/upgrading.md) for the steps.

### Breaking

- **Session mode is gone.** A group is no longer started or stopped by hand:
  it is open inside its allowed times and blocked outside them. The `15/30/60`
  buttons, start/stop, expiry billing and `/api/session/*` were removed, and
  `/api/groups` rejects `"mode": "session"` with `400 mode_removed`.
- **The daily minutes budget is gone.** A group has 0–4 **allowed times** (a
  start, an end and every day / weekdays / weekends; may cross midnight, and no
  windows means always open) and is blocked outside them. The metered usage
  tables, bonus minutes, the `±15`/Custom/Reset controls, the History tab with
  its CSV export and the budget alerts went with it, so `limit_reached` and
  `bedtime` no longer exist as statuses (`outside_hours` replaces them).
  `Block now`, `Allow freely until` and pause are unchanged, and Free overrides
  the windows.
- **The shared user PIN is gone; accounts replace it.** A child signs in with
  their own `user` account on a dedicated kid screen and can only view its
  groups' schedule and status; every mutating API call is refused with
  `403 read_only`. Admins change
  their own password with `POST /api/account/password`;
  `/api/admin/credentials` is gone.
- **Everyone signs in again.** Cookies carry the account id and a session epoch,
  so every 0.3.0 cookie is invalid.
- **The database moves to schema v6** (`group_windows` and `alerts_sent` are
  kept; the metered usage and bonus tables and the minute columns are dropped),
  applied on start. Before the first change the migration writes one verified
  copy next to the database as `timefence.db.pre-v6.bak` for any database older
  than v6; a fresh install writes none. A database from a newer schema is
  refused instead of being touched. The 0.3.0 `sessions` table is kept
  read-only; nothing reads it.
- **This release is a fresh install, not a data migration.** TimeFence had not
  been published before this version, so a pre-release database is not carried
  over: schema v6 drops the metered usage and bonus data and empties the stored
  windows, because the old windows were *no-go* windows and their meaning
  inverts. Nothing has to be exported first and nothing can be; re-create your
  groups (see [docs/upgrading.md](docs/upgrading.md#a-pre-release-install)).
- **Stopping TimeFence blocks every managed group** until it starts again; run
  `timefence release` when you stop it on purpose (`uninstall.sh` does).
- **Docker defaults changed.** The image listens on `LISTEN_HOST=127.0.0.1` and
  sets `SECURE_COOKIES=true`. Bridge networking needs `LISTEN_HOST=0.0.0.0` and a
  published port (the example compose file binds `127.0.0.1:8099`); `docker run`
  without `LISTEN_HOST` only listens inside the container. Pin a release tag (or
  a digest) for controlled upgrades; `:latest` moves with every release.
- **`TZ` is no longer forced to UTC.** Without it the app follows the host
  (`/etc/timezone`, then `/etc/localtime`) and falls back to UTC only when
  neither is available, so the allowed times are evaluated in local time. Set
  `TZ=UTC` to keep the old clock.
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
  warnings (AdGuard unreachable, `TZ` unresolved).
- `timefence release` (and an uninstall step) that lifts the managed blocks.
- Change-password card in the admin UI, backed by `POST /api/account/password`.
- `scripts/migrate_legacy.py` writes accounts into `users`, requires `--tz`, and
  cannot leave the old password working.
- Atomic, verified migrations; one store lock serialising all database access
  (requests, reconciler, shutdown); AdGuard HTTP calls made outside that lock.
- Remote VM guide (`docs/remote-vm.md`), a DDNS allow-list helper with a systemd
  timer, and `shellcheck` in CI.
- A manual update script (`deploy/update.sh`): pull, build from the checkout,
  restart, health-check, and `--rollback <tag>` to the image you had before.
- `requirements.lock` with hashes, a digest-pinned base image, SHA-pinned
  actions, `pip-audit`, `gitleaks` and CodeQL CI jobs, and Dependabot
  configuration.
- **Allowed times.** Up to four windows per group (a start, an end, every day,
  weekdays or weekends), including windows that cross midnight: inside them the
  group is open, outside them it is blocked. A group with no windows is always
  open, and an admin's "Allow freely until" overrides a window. AdGuard's
  blocked-services list is global, so a window on a group that owns a service
  takes that service away from every device outside the window.
- **Alerts.** One Telegram bot, one ntfy topic or one webhook under Settings →
  Alerts: a group's allowed time is over (default off), AdGuard Home unreachable
  and reachable again. Messages carry a group name and numbers only;
  de-duplication and a 20-per-hour cap keep them quiet, and delivery failures are
  reduced to a category before they leave the sender. The bot token is never
  echoed back (two-character tails only) and a delivery cannot print it. Until a
  channel is configured, the Today tab offers a **Set up alerts** button.
- **Troubleshooting guide** (`docs/troubleshooting.md`) for the case "the allowed
  time is over but the device still works": IPv6 bypassing the DNS, apps with
  their own resolver, caches, a remote VM that sees one home address, and
  AdGuard's per-client settings.
- **VM deployment guide** (`docs/vm-deploy.md`): AdGuard Home and TimeFence on a
  VM for a home network in order (firewall, containers, DDNS, the allow-list
  helper, closed-to-the-world check, router DNS), including why a second public
  DNS server defeats the blocking.
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
  (`timefence.db.pre-v6.bak`). `TIMEFENCE_SKIP_MIGRATION_BACKUP=1` skips it.
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
- **Mobile polish**: the header stays on one line at 390 px, checkbox rows and
  icon-only buttons are 44 px targets, and the Settings save buttons have
  distinct accessible names.
- **A simpler Today card.** Each group has one Block | Auto | Free control
  instead of a row of buttons (Free asks until when before anything changes),
  one live line naming the schedule ("Open until 19:00", "Opens at 16:00",
  "Open all day"), a one-word state pill, and a list of apps and domains that
  collapses to "4 apps · 2 domains" until it is opened. A paused group keeps
  only Resume. The three admin tabs stay on one row on a phone, and the alerts
  reminder is one dismissible line that comes back after two weeks while alerts
  stay off.
- **The group editor edits allowed times** instead of minutes: up to four rows
  with a start, an end and a day filter, and the helper text "Outside these
  times the group is blocked. No times = always allowed."
- **A group must block something.** Creating or saving a group with no AdGuard
  service and no domain is refused with a clear message, and sending an empty
  domain list through the API now really clears the domains (it used to keep the
  old ones). An update that leaves out `services` or `domains` keeps what the
  group already has.
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
  longer includes services, domains, owner names or the query log.
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
- A shared `# timefence:begin` marker without its `end` could delete foreign
  rules.
- A closing alert fired once per closing, so a window that crosses midnight does
  not repeat it after 00:00.
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
  "blocked": it now uses the same list as a block, where only a refused answer
  is a block and a rewritten one is a real answer.
- The group **Test** screen now probes a representative host of each service: the
  real catalogue is sorted alphabetically, so a YouTube group used to test
  `ggpht.cn` and never `youtube.com`.
- The query-log health warnings (`querylog_disabled`, `querylog_retention_short`,
  `querylog_truncated`, `querylog_ignores_group`) went with the metering code
  that raised them. Discover still reads the log and says "Nothing new in this
  window." when it finds nothing to show.
- Large service icons (the real catalogue has one at ~9.9k characters) were
  dropped from the picker; the cap now covers them, and the base64-only check
  still rejects anything else.
- Service rules written with a single leading pipe (`|host^`, 21 in the real
  catalogue) were not recognised, and a `$modifier` suffix turned
  `||qq.com^$denyallow=…` into a junk hostname. One parser now handles both
  forms, drops the modifier, and returns nothing for wildcards and regexes.
- Query-log paging lost entries that shared the page's oldest timestamp (busy
  clients can produce several with one stamp), so Discover could miss a domain.
  Pages are now large and de-duplicated by entry key, and the cursor steps past
  the boundary stamp.
- `discover.parse_time` mishandled negative UTC offsets.
- An unexpected exception from AdGuard aborted the whole reconcile pass instead
  of one phase.
- The SQLite file (which holds password hashes) and the data directory have
  private modes (`umask 077`, `0600`, `0700`).
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
