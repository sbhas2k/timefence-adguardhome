# Backup and restore

Everything TimeFence knows is in one SQLite file plus two small files.

| file | contents |
|---|---|
| `/var/lib/timefence/timefence.db` | groups, budgets, usage, history, events, hashed credentials |
| `/var/lib/timefence/session-secret` | signing key for session cookies |
| `/etc/timefence/timefence.env` | configuration (mode 600; contains the AdGuard password if inline) |

The data directory is mode 0700, the database and secret 0600. Keep backups at
the same mode: they contain password hashes and, once alerts are configured, the
ntfy topic/token and the webhook URL in plain text.

Each schema upgrade first writes one verified copy next to the database, named
`timefence.db.pre-v<schema>.bak` (for example `timefence.db.pre-v5.bak`). Those
files are full copies of the database: treat them like the database itself, and
delete them once you are happy with the upgrade.

## Backup

SQLite's online backup API writes a consistent copy while TimeFence keeps
running, WAL included. The service user writes the copy into its own data
directory first (it cannot write to `/root`), and root takes it away:

```bash
# 1. A consistent copy, written by the service user into the data directory.
sudo -u timefence python3 - <<'PY'
import sqlite3

source = sqlite3.connect("/var/lib/timefence/timefence.db")
copy = sqlite3.connect("/var/lib/timefence/backup.db")
source.backup(copy)
copy.close()
source.close()
PY

# 2. Pack the copy with the secret and the configuration, then remove the copy.
sudo tar czf "/root/timefence-backup-$(date +%F).tar.gz" \
  -C /var/lib/timefence backup.db session-secret \
  -C /etc/timefence timefence.env
sudo rm -f /var/lib/timefence/backup.db
```

Docker:

```bash
docker compose exec timefence python -c "import sqlite3; s=sqlite3.connect('/data/timefence.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp timefence:/data/backup.db ./timefence-backup.db
docker compose exec timefence rm -f /data/backup.db
```

Restoring `session-secret` keeps everybody signed in; skip it to sign everyone
out (for example after losing the secret). Either way, a disabled, deleted,
demoted or password-changed account's cookies stop working as soon as the app
sees them, because the database records what each cookie was issued for. Signing
out in the UI also ends every session of that account, so a cookie copied from
the browser stops working too.

## Restore

```bash
# 1. Stop the service: a clean stop blocks every managed group in AdGuard.
sudo systemctl stop timefence

# 2. Unpack the files you saved into a scratch directory.
sudo mkdir -p /tmp/timefence-restore
sudo tar xzf /root/timefence-backup-YYYY-MM-DD.tar.gz -C /tmp/timefence-restore

# 3. Put the database back, drop stale write-ahead files, fix owner and mode.
sudo install -o timefence -g timefence -m 600 \
  /tmp/timefence-restore/backup.db /var/lib/timefence/timefence.db
sudo rm -f /var/lib/timefence/timefence.db-wal /var/lib/timefence/timefence.db-shm
sudo install -o timefence -g timefence -m 600 \
  /tmp/timefence-restore/session-secret /var/lib/timefence/session-secret   # optional
sudo rm -rf /tmp/timefence-restore

# 4. Start it again.
sudo systemctl start timefence
```

Docker: `docker compose stop timefence`, then `docker compose cp
./timefence-backup.db timefence:/data/timefence.db`, then `docker compose exec
timefence rm -f /data/timefence.db-wal /data/timefence.db-shm`, then `docker
compose start timefence`.

Old `-wal` and `-shm` files must go: SQLite would otherwise replay them over the
restored file.

On startup TimeFence applies the restored state and blocks every group whose
budget is gone, so a restore never leaves a group allowed by accident (but a
clean stop of the *current* process had already blocked everything, so expect
the groups to re-block right after the next reconcile).

## Locked out of the admin account

```bash
sudo -u timefence env DATA_DIR=/var/lib/timefence \
  /opt/timefence/venv/bin/python -m timefence passwd --role admin
```

It prompts twice (input hidden), validates the rules against the account's
stored role (an admin password needs at least 10 characters and may not be
`admin`; a `user` password needs at least 8), refuses a `--role` that does not
match the stored role, and writes a fresh Argon2id hash into the database. It
also signs that account out everywhere. Use `--username NAME --role user` for
another account. If sign-in is being refused because of the login lockout,
`sudo systemctl restart timefence` resets it (it is in-memory).

The command needs stdin: on a terminal it hides both prompts with `getpass`;
when stdin is piped (`printf '%s\n' "$NEW" "$NEW" | ... timefence passwd`), it
reads the two lines instead, which is what makes it usable from a script and on
Windows without a console.

## AdGuard Home side

TimeFence only owns its `# timefence:begin … # timefence:end` user-rule section
and the blocked services belonging to its groups. Backing up
`/opt/AdGuardHome/AdGuardHome.yaml` covers that.
