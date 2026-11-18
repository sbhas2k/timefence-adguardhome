# TimeFence for AdGuard Home

**Daily time budgets for websites and apps, for AdGuard Home.**

TimeFence is a small companion app for [AdGuard Home](https://adguard.com/adguard-home/overview.html).
You group the apps and sites you want to ration (YouTube, Roblox, "the whole
Play Store"...) and give each group a daily number of minutes. A group is
**allowed until its daily minutes are used up**; TimeFence meters real DNS
activity from AdGuard's query log and blocks the group at the DNS level until
local midnight once the budget is gone.

> **Unofficial community project, not affiliated with or endorsed by AdGuard.**

**Screenshots:** _placeholders — add `kid-390.png`, `kid-1280.png`,
`admin-today-390.png` and `admin-today-1280.png` in
[`docs/screenshots/`](docs/screenshots/) (see the note there)._

## Features

- **Unlimited groups.** Each group has a name, an emoji, weekday/weekend
  minutes, any number of AdGuard *blocked services* and any number of custom
  domains.
- **Allowed until the budget runs out.** Nothing is blocked up front. Usage is
  metered from the query log in 15-second activity buckets; when the daily
  minutes are gone the group is blocked until local midnight.
- **Read-only kid accounts with their own screen.** Admins manage everything; a
  `user` account sees only its own groups (plus shared ones) and gets one calm
  phone-first screen: minutes left, a progress bar, "time's up, back tomorrow".
  It cannot change anything, and the server enforces that on every route.
- **Per-account budgets.** A group can be owned by a user, so each child has
  their own budget. Both roles use username + password (no PIN).
- **Bedtime windows.** Give a group up to four daily no-go windows ("21:00 –
  07:00", weekday or weekend), including ones that cross midnight. A group in a
  window is blocked even if minutes are left, and no minutes are used up; the
  kid screen shows a calm "Bedtime until 07:00" instead of a countdown. An
  admin's **Allow freely until** still overrides a window.
- **Adjust in both directions.** `+15`, `−15` or any custom amount on the Today
  card, plus `Block now`, `Allow freely until HH:MM`, pause and `Reset today`.
  Every action is applied once (double taps are ignored while in flight) and
  confirmed with a toast.
- **Reconciler** every 30 seconds: AdGuard is only written to when the desired
  state differs, and it preserves services/rules that TimeFence does not own.
- **Admin tools**: edit minutes and targets, **Discover** recent domains from
  the AdGuard query log, a per-group rule **Test**, user management, and a
  password card that actually changes your own password.
- **Shared/critical-domain guard** with a plain-language warning
  ("Blocking apps.apple.com blocks the whole App Store, not one app.").
- **History**: the last 7 days per group, with dates that stay correct across
  daylight-saving changes.
- **Accounts that end sessions.** Disabling, deleting, demoting or changing the
  password of an account signs its existing sessions out on the next request.
  Login is JSON-only, rate limited per account+address and per address, and
  always runs a hash so unknown users cannot be timed.
- **Honest health check** for container supervisors, and a safe shutdown that
  blocks every managed group (see [Fail-safety](docs/how-it-works.md#fail-safety)).
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

> ⚠️ **A fresh install uses `admin` / `admin`.** That login lands on the
> first-run screen and forces a new admin password (at least 10 characters, not
> `admin`). Until setup is complete every other API call is rejected. The
> default login hint is only shown while setup is incomplete.

Accounts are username + password. Create one per child/family member in
**Settings → Users** and pick the role: `admin` (everything) or `user`
(read-only, own screen). A `user` password needs 8 characters, an `admin`
password 10.

Open `http://127.0.0.1:8099/` (or your reverse-proxied URL) and sign in.

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
| `TZ` | `UTC` | IANA timezone for budgets and the midnight reset (**set yours**) |
| `LISTEN_HOST` | `127.0.0.1` | Bind address (the image and `install.sh` keep it on loopback) |
| `LISTEN_PORT` | `8099` | Bind port |
| `DATA_DIR` | `/var/lib/timefence` | SQLite database + session secret (mode 0700 / 0600) |
| `TRUSTED_PROXIES` | *(empty)* | Comma separated CIDRs whose `X-Forwarded-For` is trusted |
| `RECONCILE_INTERVAL` | `30` | Seconds between reconciler passes (min 5) |
| `SECURE_COOKIES` | `true` | `false` only for plain-HTTP development |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

`TZ` defaults to `UTC`; TimeFence logs a warning and shows an admin banner until
you set your IANA timezone.

`timefence check` validates the configuration and prints the AdGuard connection
result without starting the web server.

## Adding a group

Admin → **Groups** → **New group**:

1. Name + emoji (e.g. `Games` 🎮).
2. Weekday minutes, and weekend minutes if they differ.
3. Optionally add **bedtime windows**: a start and end time, and whether they
   apply every day, on weekdays or at weekends. The day is the day the window
   starts on, and a window may cross midnight.
4. Pick AdGuard **services** with the searchable picker (these are the official
   AdGuard "blocked services" ids), and/or paste **domains**, one per line.
   Pasted URLs are reduced to their hostname; each becomes `||domain^`, which
   also covers subdomains. A domain or service can belong to only one group.
5. Optionally assign an **owner** (a user account) so the group shows up on that
   child's screen.
6. If a domain is shared/critical (App Store, iCloud, Google, Akamai, ...),
   TimeFence asks for explicit confirmation and explains what else breaks.

Changes are applied immediately; no restart is needed.

### Finding a game's domains

Admin → **Groups** → **Discover**: open the game on the device for 2–3 minutes,
then click **Discover**. TimeFence groups the recent query-log hostnames by
registrable domain, hides OS/telemetry noise and already-managed domains, and
lets you tick domains and add them to a group. See
[docs/discover.md](docs/discover.md) for a worked example.

## DNS caching and how fast a block lands

AdGuard Home caches DNS answers, so a device that has just lost access may keep
using a cached answer until that entry expires. TimeFence does **not** change
any of AdGuard's DNS settings (including its maximum cache TTL). If you want
changes to land faster, lower AdGuard's maximum cache TTL yourself under
*Settings → DNS settings*; how long devices keep answers also depends on the
TTLs the sites publish and on browser/OS resolver caches.

## Running on a remote VM

TimeFence can serve a home network from a VM with a public address. Only DNS
(port 53, TCP and UDP) should be reachable from the internet, only from the home
connection, and the home address changes with consumer ISPs. The full guide —
firewall rules, an SSH tunnel for the UI, and a helper that re-resolves a DDNS
name every five minutes — is in [docs/remote-vm.md](docs/remote-vm.md).

## Recovering access

If the admin password is lost or a sign-in is refused, set a new one from the
server. Nothing is printed, and the service can keep running:

```bash
sudo -u timefence env DATA_DIR=/var/lib/timefence \
  /opt/timefence/venv/bin/python -m timefence passwd --role admin
```

Add `--username NAME` to reset another account (`--role admin|user`). Rules:
admin password ≥ 10 characters and not `admin`; user password ≥ 8 characters.
Changing a password signs that account out everywhere.

Sign-in failures are counted per account+address (5 in 15 minutes) and per
address (20 in 15 minutes). The counters are held in memory, so
`sudo systemctl restart timefence` clears them immediately.

`timefence release` removes TimeFence's blocks from AdGuard Home (the managed
rules section and the services it took over) while keeping everything else. A
clean stop of TimeFence blocks every managed group, so use `release` whenever
you stop TimeFence on purpose — for example before `uninstall.sh`, which calls
it for you:

```bash
docker compose stop timefence && \
  docker compose run --rm --no-deps --entrypoint timefence timefence release
```

## Documentation

- [How it works](docs/how-it-works.md) — metering, reconciler, ownership, fail-safety
- [Reverse proxy](docs/reverse-proxy.md) — nginx (root and sub-path), Caddy
- [Limitations](docs/limitations.md) — what DNS-level time limits cannot do
- [Discover workflow](docs/discover.md) — worked example
- [Backup & restore](docs/backup-restore.md)
- [Upgrading](docs/upgrading.md)
- [Running in Docker](docs/docker.md)
- [Running on a remote VM](docs/remote-vm.md)

## FAQ

**Does this work without AdGuard Home?** No, it drives AdGuard's API.

**Does it need the AdGuard Home add-on/Pro?** No, the free AdGuard Home has the
control API it uses.

**How is "used" time measured?** Every 15-second bucket in which one of the
group's domains was actually resolved through AdGuard counts once. Blocked
answers (ad filters, parental filters, blocked services) and free-allow windows
do not count, and TimeFence ignores its own loopback lookups. Devices that keep
streaming on cached IPs or bypass DNS with DoH/VPN/mobile data therefore
under-report — see [limitations](docs/limitations.md).

**I forgot the admin password.** See [Recovering access](#recovering-access).

**Why are my changes to AdGuard's user rules not visible?** TimeFence owns only
the `# timefence:begin … # timefence:end` block and the services belonging to
its groups. Everything else is preserved verbatim.

**Can different devices have different budgets?** Not yet — budgets are per
group/account, not per device. Give each child their own group (owned by their
account) for per-child budgets.

**Do I need HTTPS?** For anything but localhost, yes: session cookies are
`Secure` by default. Put it behind nginx/Caddy. On a plain-HTTP LAN the login
page says so and sign-in will appear to fail unless you set
`SECURE_COOKIES=false` (development only).

**How do I add a second child?** Create a second `user` account and one group
per child, owned by that account. Each child signs in and sees only their
groups.

**Can I run it on a server with a changing home IP?** Yes — see
[Running on a remote VM](docs/remote-vm.md).

## Roadmap

- Per-device budgets and a 7-day usage export
- Notifications (e.g. "5 minutes left")
- YouTube restricted mode as an alternative to blocking
- Pi-hole support
- Multiple AdGuard instances
- Translations (the UI strings already live in `src/timefence/i18n.py`)

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests scripts && ruff format --check src tests scripts
```

The runtime dependencies are pinned in `requirements.lock` (with hashes); the
image installs from it. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE). The bundled Public Suffix List
(`src/timefence/data/public_suffix_list.dat`) is MPL-2.0, see
`src/timefence/data/NOTICE`.

Unofficial community project, not affiliated with or endorsed by AdGuard.
