# TimeFence for AdGuard Home

**Daily time budgets for websites and apps, for AdGuard Home.**

TimeFence is a small companion app for [AdGuard Home](https://adguard.com/adguard-home/overview.html).
You group the apps and sites you want to ration (YouTube, Roblox, "the whole
Play Store"...) and give each group a daily number of minutes. Everything is
**blocked by default**; a child taps **15 / 30 / 60 min**, the group is
unblocked at the DNS level for that long, and it re-blocks automatically when
the time is up.

> **Unofficial community project, not affiliated with or endorsed by AdGuard.**

**Screenshots:** _placeholders — add `today.png`, `groups.png` and
`discover.png` in [`docs/screenshots/`](docs/screenshots/) (see the note there)._

## Features

- **Unlimited groups.** Each group has a name, an emoji, weekday/weekend
  minutes, any number of AdGuard *blocked services* and any number of custom
  domains.
- **Blocked by default.** The default state of every group is blocked; a
  session is the only way to unblock it. On restart TimeFence closes any open
  session and re-blocks, so a crash can never leave something unblocked.
- **Live countdown**, progress bar, "40 of 60 min left", and a big Stop button.
  Stop bills only the seconds actually used.
- **Reconciler** every 30 seconds: AdGuard is only written to when the desired
  state differs, and it preserves services/rules that TimeFence does not own.
- **Admin tools**: bonus minutes for today, "allow freely until HH:MM" (no
  budget), "block now", pause a group, edit minutes, **Discover** recent
  domains from the AdGuard query log, and a per-group rule **Test**.
- **Shared/critical-domain guard** with a plain-language warning
  ("Blocking apps.apple.com blocks the whole App Store, not one app.").
- **History**: last 7 days per group plus today's session list.
- **Two logins**: a shared user PIN (start/stop sessions only) and an admin
  password (everything). Argon2 hashing, login lockout, CSRF, CSP.
- Mobile-first UI, light/dark, no CDN, no frontend build step.

## Quick start

### Docker (next to the official AdGuard Home image)

```bash
git clone https://github.com/sbhas2k/timefence-adguardhome.git
cd timefence-adguardhome
cp .env.example .env          # set ADGUARD_PASSWORD or ADGUARD_PASSWORD_FILE
printf 'your-adguard-admin-password' > adguard_password.txt   # or use a secret
docker compose up -d
```

The example compose file runs `adguard/adguardhome` (host network, needs port
53) and TimeFence on `127.0.0.1:8099`.

### Script (systemd + venv)

```bash
git clone https://github.com/sbhas2k/timefence-adguardhome.git
cd timefence-adguardhome
sudo ./install.sh
# or, behind a reverse-proxy sub-path:
sudo BASE_PATH=/timer TZ=Asia/Kolkata ADGUARD_PASSWORD_FILE=/etc/timefence/adguard.pass ./install.sh
```

`install.sh` is idempotent. It creates the `timefence` system user, a venv in
`/opt/timefence`, `/var/lib/timefence`, `/etc/timefence/timefence.env` (600),
and the `timefence.service` unit.

### First login

> ⚠️ **The fresh install uses `admin` / `admin` and the user PIN `0000`.**
> The first admin login forces you to choose a new password (at least 10
> characters, not `admin`) and a new PIN (4–8 digits, not `0000`). Until you
> do that, every API call except login/setup/logout is rejected and the user
> PIN is disabled.

Open `http://127.0.0.1:8099/` (or your reverse-proxied URL) and sign in as
**Admin**.

## Configuration

Everything comes from environment variables (or an `.env` file for Docker /
local runs). `install.sh` writes `/etc/timefence/timefence.env` (mode 600).

| Variable | Default | Meaning |
|---|---|---|
| `ADGUARD_URL` | `http://127.0.0.1:3000` | AdGuard Home base URL |
| `ADGUARD_USER` | `admin` | AdGuard Home admin user |
| `ADGUARD_PASSWORD` | – | AdGuard Home password (inline) |
| `ADGUARD_PASSWORD_FILE` | – | File containing the password (wins over the inline value) |
| `BASE_PATH` | `/` | URL prefix the app is served under, e.g. `/timer` |
| `TZ` | `UTC` | IANA timezone for budgets and the midnight reset |
| `LISTEN_HOST` | `127.0.0.1` | Bind address |
| `LISTEN_PORT` | `8099` | Bind port |
| `DATA_DIR` | `/var/lib/timefence` | SQLite database + session secret |
| `TRUSTED_PROXIES` | *(empty)* | Comma separated CIDRs whose `X-Forwarded-For` is trusted |
| `RECONCILE_INTERVAL` | `30` | Seconds between reconciler passes (min 5) |
| `SECURE_COOKIES` | `true` | `false` only for plain-HTTP development |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

`timefence check` validates the configuration and prints the AdGuard connection
result without starting the web server.

## Adding a group

Admin → **Groups** → **New group**:

1. Name + emoji (e.g. `Games` 🎮).
2. Weekday minutes, and weekend minutes if they differ.
3. Pick AdGuard **services** with the searchable picker (these are the official
   AdGuard "blocked services" ids), and/or paste **domains**, one per line.
   Pasted URLs are reduced to their hostname; each becomes `||domain^`, which
   also covers subdomains. A domain or service can belong to only one group.
4. If a domain is shared/critical (App Store, iCloud, Google, Akamai, ...),
   TimeFence asks for explicit confirmation and explains what else breaks.

Changes are applied immediately; no restart is needed.

### Finding a game's domains

Admin → **Groups** → **Discover**: open the game on the device for 2–3 minutes,
then click **Discover**. TimeFence groups the recent query-log hostnames by
registrable domain, hides OS/telemetry noise and already-managed domains, and
lets you tick domains and add them to a group. See
[docs/discover.md](docs/discover.md) for a worked example.

## Why `cache_ttl_max = 60`?

AdGuard Home caches DNS answers. With a long TTL, a device that has just been
unblocked (or re-blocked) keeps using the old answer until the cache entry
expires. TimeFence sets AdGuard's `cache_ttl_max` to 60 seconds so changes take
effect within about a minute — a deliberate trade-off between responsiveness
and cache efficiency. TimeFence does not touch any other AdGuard setting.

## Documentation

- [How it works](docs/how-it-works.md) — reconciler, ownership, fail-safety
- [Reverse proxy](docs/reverse-proxy.md) — nginx (root and sub-path), Caddy
- [Limitations](docs/limitations.md) — what DNS-level time limits cannot do
- [Discover workflow](docs/discover.md) — worked example
- [Backup & restore](docs/backup-restore.md)
- [Upgrading](docs/upgrading.md)

## FAQ

**Does this work without AdGuard Home?** No, it drives AdGuard's API.

**Does it need the AdGuard Home add-on/Pro?** No, the free AdGuard Home has the
control API it uses.

**Why are my changes to AdGuard's user rules not visible?** TimeFence owns only
the `# timefence:begin … # timefence:end` block and the services belonging to
its groups. Everything else is preserved verbatim.

**Can different devices have different budgets?** Not in 0.1.0 — budgets are
household-wide and enforced network-wide. See the roadmap.

**Do I need HTTPS?** For anything but localhost, yes: session cookies are
`Secure` by default. Put it behind nginx/Caddy.

**How do I add a second child?** You cannot yet; there is one shared user PIN.
Use groups per child as a workaround (e.g. "Child A – games").

## Roadmap (not in 0.1.0)

- Per-device budgets and multiple user profiles
- Auto mode: count usage from the query log instead of timed sessions
- YouTube restricted mode as an alternative to blocking
- Pi-hole support
- Notifications (e.g. "5 minutes left")
- Multiple AdGuard instances
- Translations (the UI strings already live in `src/timefence/i18n.py`)

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests && ruff format --check src tests
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). The bundled Public Suffix List
(`src/timefence/data/public_suffix_list.dat`) is MPL-2.0, see
`src/timefence/data/NOTICE`.

Unofficial community project, not affiliated with or endorsed by AdGuard.
