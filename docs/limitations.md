# Limitations

TimeFence blocks at the DNS level. That is cheap, works for every device on the
network without installing anything, and cannot be bypassed by "just using
another browser" — but it has honest limits.

## Budgets are household-wide

One set of budgets applies to the whole network. There is no per-device or
per-child accounting in 0.1.0. Workaround: give each child their own group
(e.g. "Games – Anna"), which is per-site, not per-person.

## Metering is approximate

In *auto* mode TimeFence charges time based on DNS activity in AdGuard's query
log. It cannot see the screen: a device that keeps streaming on cached IPs, or
that uses Private DNS/DoH, a VPN or mobile data, will use less (or no) budget.
If you need close accounting, prefer *session* mode (explicit start/stop) — or
fix the device/router so all DNS goes through AdGuard Home.

## DNS caching adds a delay

A block/unblock can take up to ~1 minute to reach a device (TimeFence caps
AdGuard's `cache_ttl_max` at 60 s). Browsers and OS resolvers may cache longer;
a reload or reconnect usually helps.

## Running games/apps may continue for a while

DNS only affects *new* lookups. An already-running game with an open connection
to a game server keeps playing until it reconnects or resolves something new.
Same for video that is already buffered.

## Bypasses to be aware of

- **Private DNS / DNS-over-TLS or DoH on the device** (Android Private DNS,
  iOS/macOS "encrypted DNS" profiles, browser "secure DNS", Firefox DoH).
  Disable those, or point them at your AdGuard Home.
- **VPNs** — traffic leaves through the tunnel; DNS may be resolved elsewhere.
- **Mobile data** — leaving Wi-Fi leaves the AdGuard Home entirely.
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

## AdGuard Home must be reachable

If AdGuard goes down, blocking stops (DNS falls back to whatever the device
uses). TimeFence keeps retrying and reports the error in the UI, but it cannot
enforce anything by itself.
