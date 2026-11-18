# Backup and restore

Everything TimeFence knows is in one SQLite file plus two small files.

| file | contents |
|---|---|
| `/var/lib/timefence/timefence.db` | groups, budgets, usage, history, events, hashed credentials |
| `/var/lib/timefence/session-secret` | signing key for session cookies |
| `/etc/timefence/timefence.env` | configuration (mode 600; contains the AdGuard password if inline) |

The data directory is mode 0700, the database and secret 0600. Keep backups at
the same mode (they contain password hashes).

Each schema upgrade first writes one verified copy next to the database, named
`timefence.db.pre-v<schema>.bak` (for example `timefence.db.pre-v5.bak`). Those
files are full copies of the database: treat them like the database itself, and
delete them once you are happy with the upgrade.

## Backup

With SQLite's online backup API (safe while running):

```bash
sudo -u timefence python3 - <<'PY'
import sqlite3
src = sqlite3.connect("/var/lib/timefence/timefence.db")
dst = sqlite3.connect("/root/timefence-$(date +%F).db")
src.backup(dst)
dst.close()
PY
sudo tar czf /root/timefence-backup-$(date +%F).tar.gz \
  -C /var/lib/timefence timefence.db session-secret \
  -C /etc/timefence timefence.env
```

Docker:

```bash
docker compose exec timefence python -c "import sqlite3; s=sqlite3.connect('/data/timefence.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d)"
docker compose cp timefence:/data/backup.db ./timefence-backup.db
```

Restoring `session-secret` keeps everybody signed in; skip it to sign everyone
out (for example after losing the secret). Either way, a disabled, deleted,
demoted or password-changed account's cookies stop working as soon as the app
sees them, because the database records what each cookie was issued for.

## Restore

```bash
sudo systemctl stop timefence
sudo cp timefence-backup.db /var/lib/timefence/timefence.db
sudo cp session-secret     /var/lib/timefence/session-secret   # optional
sudo chown timefence:timefence /var/lib/timefence/*
sudo chmod 600 /var/lib/timefence/timefence.db /var/lib/timefence/session-secret
sudo systemctl start timefence
```

On startup TimeFence applies the restored state and blocks every group whose
budget is gone, so a restore never leaves a group allowed by accident (but a
clean stop of the *current* process had already blocked everything, so expect
the groups to re-block right after the next reconcile).

## Locked out of the admin account

```bash
sudo -u timefence env DATA_DIR=/var/lib/timefence \
  /opt/timefence/venv/bin/python -m timefence passwd --role admin
```

It prompts twice (input hidden), validates the rules (≥ 10 characters, not
`admin`) and writes a fresh Argon2id hash into the database. It also signs that
account out everywhere. Use `--username NAME --role user` for another account.
If sign-in is being refused because of the login lockout, `sudo systemctl
restart timefence` resets it (it is in-memory).

The command needs stdin (it asks twice); on Windows it falls back to
`getpass.getpass`, so it also works over a terminal without a console.

## AdGuard Home side

TimeFence only owns its `# timefence:begin … # timefence:end` user-rule section
and the blocked services belonging to its groups. Backing up
`/opt/AdGuardHome/AdGuardHome.yaml` covers that.
