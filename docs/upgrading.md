# Upgrading

## A pre-release install

TimeFence had not been published before this release, so there is no supported
upgrade path into it from a pre-release build (0.4/0.5) that carried the daily
minutes budget. **Install this release fresh.**

- There is nothing to export. The usage history, the CSV export and the bonus
  minutes are gone from the product, so no file leaves the old build.
- Nothing has to be deleted by hand either. The database is migrated to schema
  v6 on start, and that step drops the metered usage and bonus tables and the
  old minute columns, and empties the stored windows — they were *no-go*
  windows, and an allowed window means the opposite, so they cannot be carried
  over. The migration writes its verified copy first
  (`timefence.db.pre-v6.bak`). Groups, targets and accounts survive; a group
  with no windows is simply always open.
- Run `timefence run` (or the container) exactly as before. Sign in, and give
  each group the allowed times you want.

If you prefer a clean slate, point `DATA_DIR` at an empty directory instead of
migrating the old database.

## Upgrading from 0.3.x to 1.0.x

1.0.0 is a breaking release for a 0.3.0 install: session mode and the shared PIN
are gone, the database moves to schema v6, and the Docker defaults changed.
Nothing has to be reconfigured by hand, but everyone signs in again and the
steps below are the safe order.

### What changes for you

- **Session mode is gone.** A group is never started or stopped by hand: it is
  open inside its allowed times and blocked outside them. The start/stop buttons
  and the `15/30/60` shortcuts were removed.
- **The daily minutes budget is gone.** Give each group 0–4 allowed times (start,
  end, every day / weekdays / weekends, may cross midnight); no windows means
  always open. The migration clears the old windows, so every existing group
  starts out open and you add the times you want.
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
  neither is available, so the allowed times are evaluated in local time. Set
  `TZ=UTC` explicitly to keep the old clock.
- **Nothing is counted or exported.** The History tab, the CSV download and the
  budget alerts are gone; there is no usage data to keep or to migrate.
- **New features, nothing to configure.** Allowed times per group, alerts (off
  by default; one Telegram bot, ntfy topic or webhook), and a phone-first kid
  screen.
- **The default-password check runs once per schema version.** If an admin still
  uses the default password `admin`, first-run setup is required again; change
  it before upgrading if you would rather not see that.

### Backup first

The upgrade writes its own verified copy before the first schema change. The
copy is named after the schema it reaches, so any database older than v6 gets
`timefence.db.pre-v6.bak` next to the database; a fresh install writes none. The
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

### Manual update from a git checkout (no registry, no CI)

If TimeFence runs from a git checkout on the same host, `deploy/update.sh` does the
whole update. It pulls (fast-forward only), builds `timefence:<version>` from the
checkout, points the compose file at that tag, restarts, and waits for `/healthz`.
Nothing is fetched from a registry.

```bash
cd timefence-adguardhome
sudo -E ./deploy/update.sh                  # pull, build, restart, health-check
sudo -E ./deploy/update.sh --rollback 1.0.0 # switch back to a tag you built earlier
```

It reads these environment variables (all optional):

| variable | default | meaning |
|---|---|---|
| `COMPOSE_FILE` | `docker-compose.yml` in the checkout | the compose file that runs TimeFence; its `image:` line must read `timefence:<tag>` |
| `BASE_PATH` | `/` | your `BASE_PATH` (for example `/timer`), used to build the health URL |
| `LISTEN_PORT` | `8099` | the port TimeFence listens on |
| `HEALTH_URL` | derived from the two above | override the full URL if a proxy is in the way |

Take a database copy first (the recipe above); the script does not. It keeps the
previous image tag, so a rollback restarts the old image, but the database may
already have been migrated: restore the copy as described under *Rollback*.
If `git pull --ff-only` refuses because the local history has diverged from the
remote's, nothing is changed and the script stops.

### Docker (image from a registry)

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
   `timefence.db.pre-v6.bak` — and drop stale write-ahead files:

   ```bash
   sudo install -o timefence -g timefence -m 600 \
     /root/timefence-pre-upgrade.db /var/lib/timefence/timefence.db
   sudo rm -f /var/lib/timefence/timefence.db-wal /var/lib/timefence/timefence.db-shm
   ```

3. Run the release you had before (for example the 0.3.x tag or image you were
   on) and start it. A 0.3.x release reads the restored v3/v4 database.

A database from a newer schema is refused on start instead of being silently
corrupted — that is why the copy matters. Anything that happened after the
upgrade (edits, accounts) is lost by the rollback.

## Upgrading within 1.x

There is no schema change unless a release's entry says so. When a release does
change the schema, the migration writes one verified copy named
`timefence.db.pre-v<schema>.bak` next to the database before its first change.

### 1.0.x to 1.1.0

- The database moves to **schema v7** and the copy is `timefence.db.pre-v7.bak`.
  The step rewrites the stored day filter of every allowed time: `all` stays
  `all`, `weekday` becomes `mon,tue,wed,thu,fri` and `weekend` becomes `sat,sun`,
  so every existing schedule keeps its meaning.
- **A window can now name any days**, not only weekdays or weekends. The editor
  shows seven day chips plus "Every day", and the Today card names the picked
  days when they are not all of them.
- The schema step rewrites rows, so `timefence passwd` reports that the database
  must be upgraded first: start TimeFence once after the update, then the
  command works again.

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
| 1.1.x | 0.107.x (control API v1) | 3.12 or newer |
| 1.0.x | 0.107.x (control API v1) | 3.12 or newer |
| 0.3.x | 0.107.x (control API v1) | 3.12 |

Newer AdGuard Home releases speak the same control API, so 0.107.x is a floor,
not a ceiling; check the changelog of a major AdGuard Home release before
upgrading it blindly.
