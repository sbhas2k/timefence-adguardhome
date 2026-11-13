# Security policy

## Reporting a vulnerability

Please open a private security advisory on GitHub
(<https://github.com/sbhas2k/timefence-adguardhome/security/advisories/new>) or
email the maintainer. Do not open a public issue. You will get an answer within
about a week.

## Design notes

- Passwords and PINs are hashed with Argon2id (`argon2-cffi`), scrypt as a
  fallback. Hashes live in SQLite; plaintext exists only in the environment
  file you create.
- Sessions are signed cookies (itsdangerous), `HttpOnly`, `SameSite=Lax`,
  `Secure` by default, scoped to `BASE_PATH`.
- Every state-changing API call needs the `X-CSRF-Token` header that is issued
  with the session.
- Login is rate limited: 5 failures lock the client IP for 15 minutes.
  `X-Forwarded-For` is only trusted from `TRUSTED_PROXIES`.
- The app is meant to listen on loopback behind a TLS reverse proxy. It does not
  manage TLS itself.
- TimeFence talks to AdGuard Home's control API with the credentials you give
  it; protect `/etc/timefence/timefence.env` (600) and any password file.
