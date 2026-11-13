# Discover: finding a game's domains

Most mobile games use several domains: a CDN for assets, an API endpoint, an
analytics host and a login provider. Blocking just the obvious one usually
leaves the game playable. **Discover** helps you find the real set.

## Worked example: a mobile game

1. Make sure the phone uses your AdGuard Home as its only DNS server.
2. Force-stop the game, then open it and play for **2–3 minutes** — enough for
   it to load assets, log in and start a level.
3. In TimeFence: **Admin → Groups → Discover**, window **15 minutes**, client
   = the phone's IP (or *All clients*).
4. Read the list. Typical result:

   | domain | queries | meaning |
   |---|---|---|
   | `examplegame.com` | 42 | the game itself |
   | `examplegame-cdn.net` | 118 | assets (needed) |
   | `gameanalytics.io` | 9 | telemetry (safe to ignore) |
   | `unity3d.com` | 4 | engine licensing (usually safe) |

5. Tick the domains that belong to the game (start with the ones with many
   queries), choose the group, click **Add selected**.
6. Group → **Test** to confirm AdGuard blocks each host.
7. Relaunch the game. If it still starts, repeat with a longer window (30 min)
   and look for domains you missed — often the login or CDN host.

## Tips

- **Do one thing at a time.** Open only the game, not YouTube in the background.
- **The noise filter hides** captive-portal checks, OS telemetry, NTP, OCSP and
  analytics endpoints. Use *Show all* if you suspect something is missing.
- **Group by registrable domain** — Discover collapses `a.cdn.example.com` and
  `b.cdn.example.com` into `example.com`, because `||example.com^` covers all
  subdomains.
- **Already-managed domains are hidden**, so the list only shows what you have
  not classified yet.
- **Shared domains** (App Store, iCloud, Google, Cloudflare, Akamai, AWS) trigger
  a confirmation with an explanation — blocking them breaks other things.
- If a game uses a domain shared with other apps, prefer a narrower hostname
  (e.g. `api.examplegame.com` instead of `amazonaws.com`).
