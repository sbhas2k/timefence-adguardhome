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

TimeFence reads `X-Forwarded-For` itself: it joins every header line, walks the
hops from the right and uses the first address that is not in
`TRUSTED_PROXIES`. Uvicorn's own proxy-header handling is disabled (the CLI, the
systemd unit and the container entrypoint all pass `--no-proxy-headers`), so the
header is interpreted in exactly one place. A malformed hop falls back to the
direct peer address. `TRUSTED_PROXIES` only affects rate limiting and the login
event log; it is never used for authentication.

If your proxy **replaces** rather than appends `X-Forwarded-For`, list the
proxy's own address in `TRUSTED_PROXIES`; TimeFence then uses the right-most
untrusted hop, which is the client.

## Caddy

See [`deploy/Caddyfile`](../deploy/Caddyfile). Either terminate at the root, or
preserve the prefix with `handle /timer/*` (`BASE_PATH=/timer`). Do **not** use
`handle_path`: it strips the prefix, while the app emits absolute `/static`,
`/api` and `/login` URLs under `BASE_PATH`, so a stripped request reaches
TimeFence at a path it does not serve.

## Cookies and HTTPS

Session cookies are `Secure` by default. For plain-HTTP development set
`SECURE_COOKIES=false` (never in production). The login page shows a note when
it is opened over plain HTTP, and after a successful sign-in over HTTP without
HTTPS the browser silently drops the cookie — the page then says the cookie was
not stored instead of looping. The cookie path is `BASE_PATH`, so under a
sub-path (`/timer`) TimeFence and another app on the same hostname cannot see
each other's session. At the root the cookie path is `/`, so both apps share the
hostname's cookie namespace — session values are signed and name-bound, so a
sibling app still cannot forge or read one.
