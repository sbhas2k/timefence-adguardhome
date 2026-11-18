# Security policy

## Supported versions

| version | supported |
|---|---|
| 0.4.x | yes |
| ≤ 0.3.x | no (upgrade; see [upgrading.md](docs/upgrading.md)) |

## Reporting a vulnerability

Open a private security advisory on GitHub
(<https://github.com/sbhas2k/timefence-adguardhome/security/advisories/new>).
Do not open a public issue. You will get an answer within about a week. Please
include the version, how TimeFence is deployed (systemd/Docker, proxy or not) and
what an attacker gains.

## Threat model in one paragraph

TimeFence is a household tool that runs on your own network (or on a VM you
control), listens on loopback behind your reverse proxy, and holds the AdGuard
Home admin credentials. Anyone who can read its data directory or
`/etc/timefence/timefence.env` can control AdGuard Home; anyone who can run
commands as its service user or as root can reset passwords. Protecting those
two things, and the network path to the app, is the security boundary. TimeFence
is **not** hardened against a hostile multi-tenant deployment.

## Design notes

- **Credentials.** Passwords are hashed with Argon2id (`argon2-cffi`); hashes
  live in SQLite and plaintext exists only in the environment file you create.
  Admin passwords need ≥ 10 characters and must not be `admin`; `user`
  passwords need ≥ 8 characters.
- **First run.** A fresh database gets `admin` / `admin`, and every API call
  except login/setup/logout is refused until an admin completes setup. The
  default-login hint disappears once setup is done, and completing setup
  disables any admin account still using the default password.
- **Sessions.** Signed cookies (itsdangerous), `HttpOnly`, `SameSite=Lax`,
  `Secure` by default, path `BASE_PATH`. The cookie names the account *and* the
  account's current `session_epoch`; disabling, deleting, demoting, renaming or
  password-changing an account bumps the epoch, so every existing cookie for it
  stops working on the next request. Changing your own password re-issues your
  cookie in the same response; `/api/setup` does the same for the caller. The
  check is skipped only for `/static/` and `/healthz`, which never read or write
  account state.
- **Logout limitation.** Logout clears the cookie in your browser; it does not
  revoke a copy of the token someone already holds. The token stays valid until
  the account's epoch changes (password change, role change, disable, delete,
  or `timefence passwd`). If you suspect a token leaked, change that account's
  password.
- **Lockout vs. attacks.** Sign-in is JSON-only (`415` otherwise) and counted
  twice: 5 failures per account+address and 20 per address in 15 minutes. The
  attempt that reaches a limit is the one that gets `429`. Lockout is in memory,
  so restarting the service clears it — a deliberate trade-off for a household
  tool; it also means an attacker cannot permanently lock you out.
- **Sign-in timing.** Unknown and disabled accounts run a dummy Argon2
  verification, so an attacker cannot distinguish "no such user" from "wrong
  password" by timing. Key derivation is bounded (4 concurrent sign-ins, 2
  concurrent hashes) so a burst of logins cannot pin every CPU.
- **CSRF.** Every state-changing API call needs the `X-CSRF-Token` header that is
  issued with the session, and the login/setup endpoints are JSON-only.
- **Read-only accounts.** A `user` session is refused (`403 read_only`) on every
  mutating `/api/*` call, and its state payload contains no services, domains,
  owner names or history. Role separation is enforced on the server, not by
  hiding buttons.
- **Client address.** `X-Forwarded-For` is parsed by TimeFence only: all header
  lines are joined, hops are walked from the right, and the first address not in
  `TRUSTED_PROXIES` wins; anything malformed falls back to the peer address.
  Uvicorn's own proxy-header parsing is disabled (`--no-proxy-headers`). The
  address is used for rate limiting and the login event log, never for
  authentication.
- **Files.** The process sets `umask 077`, creates `DATA_DIR` as 0700, and keeps
  `timefence.db` (password hashes) and `session-secret` at 0600. `install.sh`
  writes `/etc/timefence/timefence.env` at 600.
- **Web surface.** No CDN, no inline scripts, no `eval`; the CSP forbids them.
  Templates are autoescaped. The app does not manage TLS itself: it binds to
  loopback and expects nginx/Caddy in front, and it never trusts
  `SECURE_COOKIES=false` in production (the login page says so when opened over
  plain HTTP).
- **AdGuard credentials.** TimeFence talks to AdGuard Home's control API with the
  credentials you give it and never writes them except into
  `/etc/timefence/timefence.env` or the password file you mount. Give it a
  dedicated AdGuard Home user with admin rights if you do not want to share the
  main one.
- **Break-glass.** Anyone who can run commands as root or as the `timefence`
  service user can reset any password with `timefence passwd`; that is
  deliberate local recovery, not a remote feature.
- **DNS-level limits.** Blocking is DNS-level. It does not stop DoH/Private
  DNS, VPNs, mobile data, hard-coded IPs, or an already-open connection. Do not
  treat TimeFence as a security control.
