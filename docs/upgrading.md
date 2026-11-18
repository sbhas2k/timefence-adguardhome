# Upgrading

## 0.4 → 0.5

0.5.0 adds bedtime windows and moves the database to schema v5. Nothing has to
be reconfigured and existing sign-ins keep working.

### What changes for you

- **Bedtime windows.** A group can carry up to four daily no-go windows (start,
  end, and every day / weekdays / weekends), including ones that cross midnight.
  A group in a window is blocked even if minutes are left, and no minutes are
  used up. The admin's **Allow freely until** still overrides a window. See
  [how it works](how-it-works.md#bedtime-windows).
- **A service in a bedtime group is blocked house-wide.** AdGuard's blocked
  services are global; a window on a group that owns YouTube takes YouTube away
  from every device for the window. See [limitations](limitations.md).
- **The default-password check runs once per schema version.** If an admin
  account still uses the default password `admin`, first-run setup is required
  again after the upgrade. Change that password before upgrading if you would
  rather not see it.
- **Sessions stay signed in**; no cookie changes in this release.

### Backup first

The upgrade writes its own verified backup once, before the first schema change
it makes: `timefence.db.pre-v5.bak` next to the database (a database older than
0.4 is upgraded straight to v5 and gets the same single, verified copy named
after the schema it reaches). A fresh install writes no copy. The migration
verifies the copy before touching anything and stops if it cannot write it. Take
your own copy as well:

```bash
sudo systemctl stop timefence
sudo -u timefence python3 - <<'PY'
import sqlite3
src = sqlite3.connect("/var/lib/timefence/timefence.db")
dst = sqlite3.connect("/root/timefence-pre-0.5.db")
src.backup(dst)
dst.close()
PY
sudo systemctl start timefence     # or continue with the upgrade below
```

`TIMEFENCE_SKIP_MIGRATION_BACKUP=1` skips the automatic copy (not recommended).
The copies contain the database, so they contain password hashes: keep them at
mode 0600 like the database.

### Script install

```bash
cd timefence-adguardhome
git fetch --tags && git checkout v0.5.0
sudo ./install.sh                              # idempotent, keeps config and data
sudo journalctl -u timefence -n 50 --no-pager
```

Schema changes are applied by the built-in migration on start
(`PRAGMA user_version`); a failure leaves the previous schema intact. The v5
step is plain DDL, so `timefence passwd` can also open and upgrade a v4
database without starting the server.

### Docker

```bash
docker compose pull timefence
docker compose up -d timefence
docker compose logs -f timefence
```

The image entry point is unchanged in this release; `:latest` moves with the
release, so pin `:0.5.0` (or a digest) if you want controlled upgrades.

### Rollback

1. Stop the new version (`sudo systemctl stop timefence` or
   `docker compose stop timefence`).
2. Restore the database you saved before the upgrade, or the automatic
   `timefence.db.pre-v5.bak`, over `/var/lib/timefence/timefence.db` and keep
   it at mode 0600 and owner `timefence`.
3. Go back to the previous version and start it.

A database from a newer schema is refused on start instead of being silently
corrupted — that is why the copy matters. Anything that happened after the
upgrade is lost by the rollback.

## 0.3 → 0.4

0.4.0 is a breaking release: session mode is removed, the database moves to
schema v4 and the Docker defaults changed. Read this before you upgrade.

### What changes for you

- **Session mode is gone.** Groups that were "blocked unless a session is
  running" become "allowed until the day's minutes are used up", like every
  other group. Their minutes for today are kept, so a group that had already
  used its budget stays blocked until local midnight; anything not yet used is
  available right away.
- **The old sessions stay in the database as history.** They are not billed any
  more and nothing reads them for budgets.
- **Everybody signs in again.** Cookies now carry the account id and an epoch,
  so all existing sign-ins are invalid. `user` accounts become read-only and
  land on the kid screen; `admin` accounts keep the full UI.
- **`user` accounts cannot change anything** (server-side `403 read_only`). If a
  `user` account was doing admin work, promote it or use a separate admin.
- **Stopping TimeFence blocks every managed group.** That is deliberate; run
  `timefence release` if you stop it on purpose (`uninstall.sh` does).
- **The UI changed** (new kid screen, reworked admin quick actions). Nothing to
  configure.

### Backup first

The upgrade writes its own backup (`timefence.db.pre-v4.bak` next to the
database, and the migration verifies it before touching anything), but take your
own copy as well:

```bash
sudo systemctl stop timefence
sudo -u timefence python3 - <<'PY'
import sqlite3
src = sqlite3.connect("/var/lib/timefence/timefence.db")
dst = sqlite3.connect("/root/timefence-pre-0.4.db")
src.backup(dst)
dst.close()
PY
sudo systemctl start timefence     # or continue with the upgrade below
```

### Script install

```bash
cd timefence-adguardhome
git fetch --tags && git checkout v0.4.0
sudo ./install.sh                              # idempotent, keeps config and data
sudo journalctl -u timefence -n 50 --no-pager
```

`install.sh` reinstalls the package into the existing venv from
`requirements.lock`, keeps `/etc/timefence/timefence.env` (mode 600) and never
touches the database. Schema changes are applied by the built-in migration on
start (`PRAGMA user_version`); a failure leaves the previous schema intact.

### Docker

```bash
docker compose pull timefence
docker compose up -d timefence
docker compose logs -f timefence
```

Two defaults changed in the image: `LISTEN_HOST=127.0.0.1` and
`SECURE_COOKIES=true`.

- A **bridge-network** deployment must set `LISTEN_HOST: 0.0.0.0` (the bundled
  compose file does) and still publish to the host loopback
  (`127.0.0.1:8099:8099`). `docker run` without `LISTEN_HOST` only listens
  inside the container, so the port is unreachable from the host.
- With bridge networking every client looks like the Docker gateway address;
  set `TRUSTED_PROXIES` (and a proxy that sends `X-Forwarded-For`) or use host
  networking, otherwise a burst of failed sign-ins locks the household out for
  15 minutes.
- The health check now reports `503` when TimeFence cannot sync with AdGuard.
  Do not auto-restart on it — restarting TimeFence does not fix AdGuard.
- `:latest` moves with every release; pin `:0.4.0` (or a digest) if you want
  controlled upgrades.

### Rollback

1. Stop the new version:

   ```bash
   sudo systemctl stop timefence            # or: docker compose stop timefence
   ```

2. Restore the database you saved before the upgrade (or the automatic
   `timefence.db.pre-v4.bak`) over `/var/lib/timefence/timefence.db`:

   ```bash
   sudo cp /root/timefence-pre-0.4.db /var/lib/timefence/timefence.db
   sudo chown timefence:timefence /var/lib/timefence/timefence.db
   sudo chmod 600 /var/lib/timefence/timefence.db
   ```

3. Go back to the previous version (`git checkout v0.3.0 && sudo ./install.sh`,
   or `docker compose up -d` with the previous tag) and start it.

A database from a newer schema is refused on start instead of being silently
corrupted — that is why the backup matters. Anything that happened *after* the
upgrade (usage, edits, accounts) is lost by the rollback.

## Version compatibility

| TimeFence | AdGuard Home | Python |
|---|---|---|
| 0.4.x | 0.107.x (control API v1) | 3.12 |
| 0.3.x | 0.107.x (control API v1) | 3.12 |
| 0.1.x – 0.2.x | 0.107.x (control API v1) | 3.12 |

Newer AdGuard Home releases speak the same control API, so 0.107.x is a floor,
not a ceiling; check the changelog of a major AdGuard Home release before
upgrading it blindly.
