# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
