# Limitations

TimeFence blocks at the DNS level. That is cheap, works for every device on the
network without installing anything, and cannot be bypassed by "just using
another browser" — but it has honest limits.

## Budgets are per group, not per device

One set of budgets applies per group, and a group is network-wide. There is no
per-device accounting. Workaround: give each child their own group owned by
their account (e.g. "Games – Anna"); that is per-group, not per-person-per-device.

## Metering is approximate

TimeFence charges 15-second buckets of real DNS activity from AdGuard's query
log. It cannot see the screen: a device that keeps streaming on cached IPs, or
that uses Private DNS/DoH, a VPN or mobile data, will use less (or no) budget;
background API calls may add a few buckets that are not "watching". Blocked
answers and free-allow windows are not charged. For close accounting, make sure
all DNS on the device and router goes through AdGuard Home, and keep AdGuard's
query log enabled (a retention of at least an hour is recommended; TimeFence
warns when it is shorter).

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

## AdGuard Home must be reachable

If AdGuard goes down, blocking stops (DNS falls back to whatever the device
uses). TimeFence keeps retrying and reports the problem in the admin banner and
`/healthz`, but it cannot enforce anything by itself.

## Shutdown blocks everything on purpose

Stopping TimeFence cleanly blocks every managed group, so children cannot
freebrowse while the controller is down. Use `timefence release` if you stop it
on purpose and want the blocks lifted; a crash cannot block, and AdGuard keeps
the last written state until TimeFence starts again.
