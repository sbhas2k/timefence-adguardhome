# Reverse proxy

TimeFence binds to `127.0.0.1:8099` by default and expects to sit behind TLS.
`BASE_PATH` must match the public path **exactly**.

## nginx — at the root of a hostname

`BASE_PATH=/` (default), ready-to-copy block in
[`deploy/nginx-root.conf`](../deploy/nginx-root.conf):

```nginx
location / {
    proxy_pass http://127.0.0.1:8099;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## nginx — under a sub-path

`BASE_PATH=/timer`, see [`deploy/nginx-subpath.conf`](../deploy/nginx-subpath.conf).
The important detail is the **missing trailing slash** on `proxy_pass`: the app
is prefix-aware and generates its own URLs with `/timer/...`.

```nginx
location = /timer { return 301 https://$host/timer/; }

location /timer/ {
    proxy_pass http://127.0.0.1:8099;      # no trailing slash!
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /timer;
}
```

If the proxy terminates TLS on the same host, add `127.0.0.1/32` to
`TRUSTED_PROXIES` so the login lockout sees the real client IP:

```
TRUSTED_PROXIES=127.0.0.1/32
```

`TRUSTED_PROXIES` only affects rate limiting; it is never used for
authentication.

## Caddy

See [`deploy/Caddyfile`](../deploy/Caddyfile). Either terminate at the root,
or preserve the prefix with `handle /timer/*` (recommended, matches
`BASE_PATH=/timer`). `handle_path /timer/*` strips the prefix, which only works
with `BASE_PATH=/`.

## Cookies and HTTPS

Session cookies are `Secure` by default. For plain-HTTP development set
`SECURE_COOKIES=false` (never in production). The cookie path is `BASE_PATH`, so
TimeFence and another app on the same hostname cannot see each other's session.
