# Troubleshooting: a device is still not blocked

TimeFence blocks at the DNS level: it asks AdGuard Home to answer "blocked" for
a group's domains, and the device is expected to stop working the moment it has
to look a name up again. A device that keeps working is almost always a device
that never asks AdGuard Home. Work through the checks below in order — the first
two explain most cases.

## 1. The device bypasses DNS over IPv6

This is the most common cause. The device has a public IPv6 address and uses the
ISP's IPv6 DNS server, so its lookups never reach AdGuard Home at all; the IPv4
answer it would have got from AdGuard Home never matters.

- Check the device's network screen: if it shows an IPv6 address (an address
  with colons, e.g. `2a02:…`), the device can bypass your DNS.
- Turn IPv6 off in the router — or disable prefix delegation (the setting that
  hands a public IPv6 prefix to the LAN).
- Reboot the device (a sleeping phone keeps its old addresses and its resolver
  cache). Check again that its network screen shows only IPv4.
- Setting a static **IPv4** DNS server on the device does *not* turn IPv6 off;
  the device keeps using IPv6 DNS for IPv6 names.

Some routers have no IPv6 off switch. In that case use the device's own
"private DNS"/"DNS-over-TLS" setting to point at AdGuard Home, block IPv6 DNS
traffic (port 53 to any IPv6 address) in the router firewall, or fall back to
the measures in step 2.

## 2. The app uses its own DNS (hardcoded servers or DNS-over-HTTPS)

Some apps and TV boxes ignore the DNS server they were given and talk to a
hardcoded resolver, or use DNS-over-HTTPS to a provider over port 443. DNS-level
blocking cannot see or stop that.

- Some smart TVs, streaming boxes and game launchers are reported to do this.
- Router options: block the device's traffic to the public DoH/DoT endpoints
  (port 853, and 443 to the resolver's hostnames), or block the whole MAC
  address at the times you want the device offline.
- App options: the console's or app store's own PIN/parental control still
  works, because it does not rely on DNS.
- There is no DNS configuration in TimeFence that fixes this: treat it as a
  network or device-level problem.

## 3. A stale connection or a DNS cache

A device that already holds an IP address, or an open connection (a video
stream, a game session), keeps using it without a new lookup. A block can
therefore take a few minutes, up to about five, to reach a device even though
TimeFence applied it within seconds.

- Fully power the device off and on again — not just standby or sleep.
- Then open the app fresh and wait up to a minute for the next lookup.

## 4. You run AdGuard Home on a remote VM

When the VM sits outside the home network, every device in the house reaches it
through the home router's NAT, so AdGuard Home sees **one** address for the whole
household: the home's public address. The device's own `192.168.x.x` address
never appears in the query log, and per-device filtering in AdGuard Home cannot
tell the devices apart.

- To confirm blocking works: open the app on the device and watch the query log
  for blocked lines that come from your home address.
- Allowed times are per group, not per device, by design. See
  [Running on a remote VM](remote-vm.md) for the firewall rules and the
  allow-list, which covers IPv4 only.

## 5. AdGuard Home client settings override the global ones

AdGuard Home applies the global settings to a client unless that client has its
own entry under *Settings → Client settings*. A per-client setting (or a stale
client entry for an address the device had before) can silently disable
filtering for it.

- Check *Settings → Client settings* and remove or reset the entry for that
  device, so it uses the global settings again.

## 6. Prove in one minute whether the device uses your DNS

Change the DNS server on the device (or in the router's DHCP lease for it) to
`192.0.2.1`. That address is reserved for documentation and answers nothing.

- If the app still works, the device is not using your DNS server at all:
  return to steps 1 and 2.
- If the app breaks within a minute, the device *does* use your DNS server, so
  the block is a TimeFence or AdGuard setting problem — check the group's
  allowed times and block state in TimeFence first, then the query log.

Remember to set the DNS server back afterwards.

## 7. Change the router's default admin password

While you are in the router: if its admin password is still the one printed on
the label (or `admin`), change it. A device that bypasses your DNS is a nuisance;
a router anyone on the network can administer is worse, and it is the one place
where all of the above is actually decided.

## Still stuck?

- [Limitations](limitations.md) explains what DNS-level time limits can and
  cannot do.
- [Discover workflow](discover.md) shows how to find the domains an app really
  uses, which is the other reason a block "does nothing": the group is blocking
  the wrong domains.
