---
name: Bug report
about: Something behaves wrongly (not a security problem)
title: ""
labels: bug
---

<!--
Please do not report security problems here: use the private advisory
described in SECURITY.md instead.
-->

## What happened

<!-- What you expected, and what happened instead. -->

## Steps to reproduce

1.
2.
3.

## Your setup

- TimeFence version (the footer of the page shows it, or `timefence --version`):
- Install method (systemd script, Docker, host networking, other):
- AdGuard Home version:
- `TZ` (or "unset, follows the host"):
- Reverse proxy in front, or direct loopback (`BASE_PATH` if set):

## Logs

<!--
`journalctl -u timefence -n 100 --no-pager`, or `docker compose logs timefence`.
REDACT FIRST: the logs and the configuration contain the AdGuard Home password,
the ntfy topic/token and the webhook URL. Never paste
/etc/timefence/timefence.env or a full `timefence.db`.
-->
