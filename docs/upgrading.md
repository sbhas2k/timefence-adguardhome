# Upgrading

## Upgrading from 0.3.x to 1.0.x

1.0.0 is a breaking release for a 0.3.0 install: session mode and the shared PIN
are gone, the database moves to schema v5, and the Docker defaults changed.
Nothing has to be reconfigured by hand, but everyone signs in again and the
steps below are the safe order.

### What changes for you

- **Session mode is gone.** Every group is now "allowed until the day's minutes
  are used up". The start/stop buttons and the `15/30/60` shortcuts were
  removed. A group that had already used its minutes stays blocked until local
  midnight; anything not yet used is available right away.
- **The shared PIN is gone.** Each person signs in with their own account: an
  `admin` (the full UI) or a `user` (a read-only kid screen). A `user` account
  cannot change anything; every mutating API call is refused with
  `403 read_only`.
- **Everybody signs in again.** Cookies now carry the account id and a session
  epoch, so every 0.3.0 cookie is invalid.
- **Stopping TimeFence blocks every managed group** until it starts again. That
  is deliberate; run `timefence release` if you stop it on purpose
  (`uninstall.sh` does).
- **Docker defaults changed.** The image listens on `LISTEN_HOST=127.0.0.1` and
  sets `SECURE_COOKIES=true`. A bridge-network deployment must set
  `LISTEN_HOST: 0.0.0.0` and publish a port (the bundled compose file binds
  `127.0.0.1:8099`); `docker run` without `LISTEN_HOST` only listens inside the
  container. With bridge networking every client looks like the Docker gateway,
  so set `TRUSTED_PROXIES` behind a proxy or use host networking. See
  [docker.md](docker.md).
- **`TZ` is no longer forced to UTC.** Without it the app follows the host
  (`/etc/timezone`, then `/etc/localtime`) and falls back to UTC only when
  neither is available. Budgets therefore reset at **local** midnight; set
  `TZ=UTC` explicitly to keep the old reset time. Existing usage rows keep the
  dates they were recorded under.
- **Usage is metered in 15-second activity blocks.** Light or background use is
  charged less than in 0.3.0, so the same daily minutes can last longer.
  Blocked answers and free-allow windows are never charged.
- **New features, nothing to configure.** Bedtime windows per group, alerts
  (off by default; one Telegram bot, ntfy topic or webhook), a History tab with a CSV export,
  and a phone-first kid screen.
- **The default-password check runs once per schema version.** If an admin still
  uses the default password `admin`, first-run setup is required again; change
  it before upgrading if you would rather not see that.

### Backup first

The upgrade writes its own verified copy before the first schema change. The
copy is named after the schema it reaches, so any database older than v5 gets
`timefence.db.pre-v5.bak` next to the database; a fresh install writes none. The
migration verifies the copy before touching anything and stops if it cannot
write it. Take your own copy as well:

```bash
sudo -u timefence python3 - <<'PY'
import sqlite3

source = sqlite3.connect("/var/lib/timefence/timefence.db")
copy = sqlite3.connect("/var/lib/timefence/backup.db")
source.backup(copy)
copy.close()
source.close()
PY
sudo install -o root -g root -m 600 /var/lib/timefence/backup.db /root/timefence-pre-upgrade.db
sudo rm -f /var/lib/timefence/backup.db
```

`TIMEFENCE_SKIP_MIGRATION_BACKUP=1` skips the automatic copy (not recommended).
The copies contain the database, so they contain password hashes: keep them at
mode 0600 like the database.

### Script install

```bash
cd timefence-adguardhome
git fetch --tags
git checkout 1.0.0                            # the release you install
sudo ./install.sh                             # idempotent, keeps config and data
sudo journalctl -u timefence -n 50 --no-pager
```

`install.sh` reinstalls the package into the existing venv from
`requirements.lock` (hash-checked) and keeps `/etc/timefence/timefence.env`
(mode 600). It never touches the database; the schema change is applied by the
built-in migration on start (`PRAGMA user_version`), and a failure leaves the
previous schema intact.

### Docker

```bash
docker compose pull timefence
docker compose up -d timefence
docker compose logs -f timefence
```

Pin `1.0.0` (or a digest) if you want controlled upgrades; `:latest` moves with
every release. The image has no `/etc/localtime` of its own, so a read-only
mount of the host's file is what lets it follow the host timezone.

### Rollback

1. Stop the new version (`sudo systemctl stop timefence` or
   `docker compose stop timefence`).
2. Restore the pre-upgrade backup — your own copy or the automatic
   `timefence.db.pre-v5.bak` — and drop stale write-ahead files:

   ```bash
   sudo install -o timefence -g timefence -m 600 \
     /root/timefence-pre-upgrade.db /var/lib/timefence/timefence.db
   sudo rm -f /var/lib/timefence/timefence.db-wal /var/lib/timefence/timefence.db-shm
   ```

3. Run the release you had before (for example the 0.3.x tag or image you were
   on) and start it. A 0.3.x release reads the restored v3/v4 database.

A database from a newer schema is refused on start instead of being silently
corrupted — that is why the copy matters. Anything that happened after the
upgrade (usage, edits, accounts) is lost by the rollback.

## Upgrading within 1.x

There is no schema change unless a release's entry says so. When a release does
change the schema, the migration writes one verified copy named
`timefence.db.pre-v<schema>.bak` next to the database before its first change.

```bash
# script install
cd timefence-adguardhome
git fetch --tags
git checkout <the release tag>
sudo ./install.sh

# Docker
docker compose pull timefence
docker compose up -d timefence
```

Take a copy first if you are unsure (the recipe above works for any version),
and roll back by stopping the service, restoring that copy over
`/var/lib/timefence/timefence.db` (removing `-wal`/`-shm`), and running the
previous release.

## Version compatibility

| TimeFence | AdGuard Home | Python |
|---|---|---|
| 1.0.x | 0.107.x (control API v1) | 3.12 or newer |
| 0.3.x | 0.107.x (control API v1) | 3.12 |

Newer AdGuard Home releases speak the same control API, so 0.107.x is a floor,
not a ceiling; check the changelog of a major AdGuard Home release before
upgrading it blindly.
