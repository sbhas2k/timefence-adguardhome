# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

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
