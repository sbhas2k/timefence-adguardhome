# Running in Docker

TimeFence is a small Python image. It works in two shapes. The examples build
it from the checkout (`build: .`); if you publish an image, swap that for
`image: ghcr.io/<you>/timefence-adguardhome:<tag>`.

## A. Everything in one compose project (bridge network)

Use the root [`docker-compose.yml`](../docker-compose.yml). TimeFence talks to
`http://adguardhome:3000` over the compose network and listens on
`127.0.0.1:8099`. This is the recommended setup for a new install.

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
- `journald` logging, so `journalctl CONTAINER_NAME=timefence` works.

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

## Gotchas

- **`TZ`** matters for AdGuard's *"Local"* schedule timezone and for container
  logs. Set it explicitly.
- **Loopback binding** is not the same inside a container: `127.0.0.1` in a
  bridge container is the container itself. Use `host.docker.internal` +
  `extra_hosts` only if AdGuard listens on a non-loopback address.
- **One reconciler**: never run two TimeFence instances against the same
  AdGuard and data volume.
- **Health check**: the image exposes `/healthz`; with a non-root `BASE_PATH`
  it is `${BASE_PATH}/healthz` (the host-network example overrides it).
