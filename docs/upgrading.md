# Upgrading

## Script install

```bash
cd timefence-adguardhome
git fetch --tags && git checkout v0.2.0        # or main
sudo ./install.sh                              # idempotent, keeps config/data
sudo journalctl -u timefence -n 50 --no-pager
```

`install.sh` reinstalls the package into the existing venv, keeps
`/etc/timefence/timefence.env` and never touches the database. Schema changes
are applied by TimeFence's built-in migrations on start (`PRAGMA user_version`).

Back up first if you are jumping several versions:

```bash
sudo cp /var/lib/timefence/timefence.db /root/timefence-pre-upgrade.db
```

## Docker

```bash
docker compose pull timefence
docker compose up -d timefence
docker compose logs -f timefence
```

Pin a version (`ghcr.io/sbhas2k/timefence-adguardhome:0.1.0`) instead of
`:latest` if you want controlled upgrades.

## Rolling back

- Stop the service, restore the previous `timefence.db` backup and the previous
  image/tag.
- A database from a **newer** schema is refused on start (the app tells you so)
  instead of being silently corrupted — that is why the backup matters.

## Version compatibility

| TimeFence | AdGuard Home | Python |
|---|---|---|
| 0.1.x | 0.107.x (control API v1) | 3.12 |
