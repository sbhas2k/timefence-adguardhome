# Backup and restore

Everything TimeFence knows is in one SQLite file plus two small files.

| file | contents |
|---|---|
| `/var/lib/timefence/timefence.db` | groups, sessions, history, events, hashed credentials |
| `/var/lib/timefence/session-secret` | signing key for session cookies |
| `/etc/timefence/timefence.env` | configuration (mode 600; contains the AdGuard password if inline) |

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

## Restore

```bash
sudo systemctl stop timefence
sudo cp timefence-backup.db /var/lib/timefence/timefence.db
sudo cp session-secret     /var/lib/timefence/session-secret   # optional
sudo chown timefence:timefence /var/lib/timefence/*
sudo systemctl start timefence
```

On startup TimeFence closes any session that was open when the backup was taken
and re-blocks everything, so a restore never leaves something unblocked.

## AdGuard Home side

TimeFence only owns its `# timefence:begin … # timefence:end` user-rule section
and the blocked services belonging to its groups. Backing up
`/opt/AdGuardHome/AdGuardHome.yaml` covers that.
