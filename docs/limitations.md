# Limitations

TimeFence blocks at the DNS level. That is cheap, works for every device on the
network without installing anything, and cannot be bypassed by "just using
another browser" — but it has honest limits.

## Allowed times are per group, not per device

One schedule applies per group, and a group is network-wide. There is no
per-device rule. Workaround: give each child their own group owned by their
account (e.g. "Games – Anna"); that is per-group, not per-person-per-device.

## A window blocks network-wide

The block outside an allowed window uses the same AdGuard switches as "Block
now": the group's domains become rules and its AdGuard services go on the
blocked-services list. AdGuard's service list is global, so an outside-the-window
block on a group that owns a service such as YouTube takes that service away from
**every device in the house**, parents' included. Per-device identity and
per-device rules are deferred (see above); use separate custom domains per group
if you need finer control.

A window is applied like any other block: the reconciler re-evaluates on its
next pass (`RECONCILE_INTERVAL`, 30 s by default), so a window starts and ends
within one interval, and DNS caching can add a little more. Everything uses the
local wall clock in `TZ`; on the night a clock falls back, a window whose edge
lies inside the repeated hour can close and open again during it. That is the
wall clock, not a missed block.

## DNS caching adds a delay

A block/unblock can take a while to reach a device: AdGuard caches answers, and
browsers and OS resolvers may cache longer. TimeFence does not change AdGuard's
DNS settings; lowering AdGuard's maximum cache TTL under *Settings → DNS
settings* makes changes land sooner. A reload or reconnect usually helps.

## Running games/apps may continue for a while

DNS only affects *new* lookups. An already-running game with an open connection
to a game server keeps playing until it reconnects or resolves something new.
Same for video that is already buffered.

## Bypasses to be aware of

- **Private DNS / DNS-over-TLS or DoH on the device** (Android Private DNS,
  iOS/macOS "encrypted DNS" profiles, browser "secure DNS", Firefox DoH).
  Disable those, or point them at your AdGuard Home.
- **VPNs** — traffic leaves through the tunnel; DNS may be resolved elsewhere.
- **Mobile data** — leaving Wi-Fi leaves your own AdGuard Home (unless you run
  one reachable from the internet; see [remote-vm.md](remote-vm.md)).
- **Hard-coded IP addresses** — apps that talk to an IP without a DNS lookup are
  not affected by DNS blocking.
- **A second DNS server** configured manually on a device.

## Devices to configure

- Android: Private DNS → *Off* (or the AdGuard hostname).
- iOS/iPadOS: Settings → Wi-Fi → your network → Configure DNS → *Manual* →
  the AdGuard Home IP; turn off "Limit IP Address Tracking" if you rely on
  iCloud Private Relay being off; remove any installed DoH profile.
- macOS: System Settings → Network → DNS, and disable "Private Relay"/"Limit
  IP Address Tracking".
- Chrome/Edge/Firefox: disable "Use secure DNS".
- Windows: disable DoH per network adapter.

## It is not a parental-control system

There is no filtering of content, no logging of what was visited beyond the
AdGuard query log, and no protection outside your network. AdGuard Home itself
has more options (Safe Search, parental filters) — use those alongside.

## A wrong AdGuard password locks the address out for a while

AdGuard Home counts failed authentications per source address: after five it
refuses every request from that address for `block_auth_min` (15 minutes by
default), including the right password, the AdGuard web UI on the same host and
`timefence check`. TimeFence stops at the first rejection and backs off (30 s,
doubling to 15 minutes), so it does not re-arm the lock, but a wrong password in
`ADGUARD_PASSWORD` still costs up to that window: fix the password, then use the
admin connection test or wait for the back-off to expire.

## AdGuard Home must be reachable

If AdGuard goes down, blocking stops (DNS falls back to whatever the device
uses). TimeFence keeps retrying and reports the problem in the admin banner and
`/healthz`, but it cannot enforce anything by itself.

## Alerts are best effort

Alerts are a convenience, not a guarantee. There is **one** channel at a time
(Telegram, ntfy or one webhook), each message is tried **once** (no retry), and
at most 20 messages go out per rolling hour (with the outage message
protected). A message carries only a group name, numbers and a time — no
domains, client addresses or usernames — so treat it as a nudge, not a log.

Delivery runs in one background thread with a bounded queue (50): while a slow
server holds it, newer messages are dropped (the oldest of the same kind first)
and the drop counter in the alert settings goes up. Redirects are not followed,
environment proxies are ignored, and each delivery is abandoned after a hard 5 s
total deadline. Private/LAN targets are allowed on purpose, because a
self-hosted ntfy is the normal setup.

"AdGuard unreachable" needs three reconcile runs in a row without a successful
sync. A run counts as stale only once the last success is older than three
intervals, so with the default 30 s interval the earliest message is about
150 s (2.5 minutes) after the outage starts, not 90 s; any success in between
resets it, so a flapping connection may never alert. A clock that jumps
backwards can suppress a duplicate message and a jump forwards can skip a day;
both are accepted for a household tool. The topic, token and webhook path are
stored in plain text in the database, so they are in every backup.

## The closing alert names a time of day, not a date

The `group_closed` alert says when the group opens again as a local `HH:MM`.
With windows that only apply to some days (for example weekends), the next
opening can be several days away and the message still shows only the time.

## Shutdown blocks everything on purpose

Stopping TimeFence cleanly blocks every managed group, so children cannot
freebrowse while the controller is down. Use `timefence release` if you stop it
on purpose and want the blocks lifted; a crash cannot block, and AdGuard keeps
the last written state until TimeFence starts again.
