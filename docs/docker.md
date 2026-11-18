# Running in Docker

TimeFence ships as a small image (`ghcr.io/sbhas2k/timefence-adguardhome`) and
works in two shapes.

The image keeps the app on loopback (`LISTEN_HOST=127.0.0.1`) and cookies
`Secure` (`SECURE_COOKIES=true`) by default; bridge networking needs
`LISTEN_HOST: 0.0.0.0` (the example compose file sets it, with the published
port still bound to `127.0.0.1`).

## A. Everything in one compose project (bridge network)

Use the root [`docker-compose.yml`](../docker-compose.yml). TimeFence talks to
`http://adguardhome:3000` over the compose network (AdGuard itself runs with
host networking for port 53) and publishes `127.0.0.1:8099` on the host.

> ⚠️ **Bridge networking and the login lockout.** With bridge networking
> TimeFence sees every client as the Docker gateway address. Without
> `TRUSTED_PROXIES` and a proxy that sends `X-Forwarded-For`, 20 failed sign-ins
> from any device lock sign-in for the whole household for 15 minutes. Put a
> reverse proxy in front with `TRUSTED_PROXIES` set, or use host networking.

## B. AdGuard Home already runs on the host (host networking)

If AdGuard Home is native (systemd) or in another compose file, its admin API is
usually bound to `127.0.0.1:3000`, which a bridge container **cannot reach**.
Run TimeFence with host networking:

```bash
cp deploy/docker-compose.host.yml docker-compose.yml
printf '%s' "$ADGUARD_PASSWORD" | sudo tee /etc/timefence/adguard.pass >/dev/null
sudo chmod 640 /etc/timefence/adguard.pass          # readable by the container user
docker compose up -d
```

`deploy/docker-compose.host.yml` sets:

- `network_mode: host` — reaches `127.0.0.1:3000` and binds the host's loopback;
- `LISTEN_HOST: 127.0.0.1` — the entrypoint honours it, so the app is **not**
  exposed on `0.0.0.0` even with host networking (put nginx/Caddy in front);
- `ADGUARD_PASSWORD_FILE` mounted read-only from the host;
- `/data` as a named volume (or bind-mount an existing data directory to keep
  history when migrating from a systemd install);
- `journald` logging, so `journalctl CONTAINER_NAME=timefence` works;
- `stop_grace_period: 20s`, because a clean stop blocks every managed group and
  that takes a few seconds.

### Migrating a systemd install to this container

```bash
sudo systemctl disable --now timefence
# keep the same data directory and run the container as the same uid/gid
# (see `user:` in the compose file) or chown it to the image user (10001)
docker compose up -d
```

Rollback: `docker compose down && sudo systemctl enable --now timefence`.

## C. AdGuard Home in Docker too

[`deploy/adguard-compose.host.yml`](../deploy/adguard-compose.host.yml) runs the
official image with host networking (required for port 53) and its config/work
directories on the host:

```
/opt/adguard-docker/conf/AdGuardHome.yaml
/opt/adguard-docker/work/data/{querylog.json,sessions.db,stats.db,filters}
```

To move a native AdGuard Home install to that container:

```bash
sudo systemctl disable --now AdGuardHome
sudo install -d -m 700 /opt/adguard-docker/{conf,work}
sudo cp /opt/AdGuardHome/AdGuardHome.yaml /opt/adguard-docker/conf/
sudo cp -a /opt/AdGuardHome/data /opt/adguard-docker/work/data
docker compose -f deploy/adguard-compose.host.yml up -d
```

Everything (filters, blocked services, user rules, hashes, query history,
upstreams, cache settings) lives in those two directories, so it comes along.

## Stopping, upgrading and removing

- **A clean stop blocks every managed group** (`docker compose stop timefence`,
  `docker compose down`, an upgrade). That is intentional. To lift the blocks
  while TimeFence is down:

  ```bash
  docker compose stop timefence
  docker compose run --rm --no-deps --entrypoint timefence timefence release
  ```

  `timefence release` removes the managed rules section and the services
  TimeFence took over, and keeps everything else. It prints what it removed.
- **Upgrade** with `docker compose pull && docker compose up -d`; the schema
  upgrade writes one verified copy named `timefence.db.pre-v<schema>.bak` next
  to the database before it changes anything (`pre-v5.bak` for 0.4 → 0.5, and a
  fresh install writes none). See [upgrading.md](upgrading.md).
- **Remove**: `docker compose rm -sf timefence`, then delete the volume.

## Gotchas

- **`TZ`** matters for the midnight reset, AdGuard's *"Local"* schedule
  timezone and container logs. Set it explicitly (the examples carry a CHANGE ME
  comment).
- **Loopback binding** is not the same inside a container: `127.0.0.1` in a
  bridge container is the container itself. Use `host.docker.internal` +
  `extra_hosts` only if AdGuard listens on a non-loopback address.
- **One reconciler**: never run two TimeFence instances against the same
  AdGuard and data volume.
- **Health check**: the image's `HEALTHCHECK` reads `LISTEN_PORT` and
  `BASE_PATH` and treats `503` as unhealthy. It means "TimeFence has not synced
  with AdGuard recently", so do not wire an auto-restart tool to it: when
  AdGuard is down, restarting TimeFence does not help.
- **Plain HTTP on a LAN**: cookies are `Secure` by default, so sign-in will fail
  over `http://` unless you set `SECURE_COOKIES=false` (development only). The
  login page says so when it is opened over plain HTTP.

Running TimeFence on a VM with a public address (firewall rules and a DDNS
helper) is covered in [remote-vm.md](remote-vm.md).
