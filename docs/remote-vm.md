# Running TimeFence on a remote VM

This guide is for the shape "AdGuard Home (and TimeFence) on a VM with a public
address, used by a home network whose public address changes". The VM resolves
for the household, so it must accept DNS from the internet — but only from the
household, and only DNS.

> ⚠️ **Never allow port 53 from anywhere.** A DNS resolver open to the internet
> is found and abused within hours: it is used for DNS amplification attacks and
> it leaks your household's lookups to strangers. Every example below opens port
> 53 for **one address only** — the current public address of the house, which
> the firewall has to be taught again whenever the ISP changes it.

## What is reachable from the internet

| port | service | rule |
|---|---|---|
| 53 tcp + udp | AdGuard Home (DNS) | allow, **only** from the home address |
| 22 | SSH | your own rule; limit it to your addresses (or a VPN) |
| 3000, 80, 443, 8099 | AdGuard UI, TimeFence, proxies | **closed** — keep them on loopback |

TimeFence and the AdGuard Home UI both bind to `127.0.0.1` by default
(`LISTEN_HOST=127.0.0.1`). Reach them through an SSH tunnel or a VPN
(WireGuard/Tailscale):

```bash
ssh -N -L 8099:127.0.0.1:8099 -L 3000:127.0.0.1:3000 you@vm.example.net
# then open http://127.0.0.1:8099/ and http://127.0.0.1:3000/ locally
```

Do not put TimeFence behind a public reverse proxy on this VM unless you know
why: the kid screen is for the household, not the internet, and every extra
public port is another thing to keep patched.

## Firewall: allow only port 53

### ufw

```bash
# CHANGE ME: the current public address of the house (documentation range here).
HOME_IP=203.0.113.10
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from "$HOME_IP" to any port 53 proto udp comment timefence-ddns
sudo ufw allow from "$HOME_IP" to any port 53 proto tcp comment timefence-ddns
# SSH: your own rule, from your operator address only.
sudo ufw allow from 203.0.113.20 to any port 22 proto tcp
sudo ufw enable
sudo ufw status numbered
```

`ufw allow 53` (or `ufw allow from any … port 53`) is exactly the mistake this
guide is about. The two rules above carry the helper's comment and are recorded
in its state, so the helper replaces them instead of adding a second pair.

### nftables

```nft
table inet timefence {
    set home_v4 {
        type ipv4_addr
        flags interval
        elements = { 203.0.113.10 }      # CHANGE ME: the home address
    }
    chain input {
        type filter hook input priority filter; policy drop;
        ct state established,related accept
        iif lo accept
        ip protocol icmp accept
        ip saddr @home_v4 udp dport 53 accept
        ip saddr @home_v4 tcp dport 53 accept
        ip saddr 203.0.113.20 tcp dport 22 accept   # CHANGE ME: operator address
    }
}
```

Update `home_v4` with `nft update element inet timefence home_v4 '{ 203.0.113.10 }'`
(or `delete element` for the old one). The bundled helper drives **ufw**; if you
run nftables, use its `--dry-run` output as a template for your own automation.

### Docker caveat

Published container ports are forwarded before ufw's rule set, so a published
`53:53/udp` is **not** protected by the ufw rules above. Two safe options:

- run AdGuard Home with `network_mode: host` (the recipes in
  [`deploy/`](../deploy/) do), so port 53 belongs to the host and ufw applies;
- or keep the bridge network and add the allow/deny rules in the `DOCKER-USER`
  chain yourself.

Bind every other published port to `127.0.0.1` (`127.0.0.1:8099:8099`), so the
UI and TimeFence never appear on a public interface.

## Dynamic home address (DDNS)

Most consumer ISPs change the public address every few days. The household side
enables DDNS (at the router, or through the ISP or a DDNS provider) and keeps a
hostname like `ddns.example.org` up to date; the VM re-resolves that name every
five minutes and rewrites only the port-53 rules.

Keep the DDNS TTL short (**60–300 s**). A long TTL means a stale address is
handed out after a change, and the helper keeps a rule for an address the
household no longer uses.

### Install the helper

Prerequisites on the VM: `ufw` (or nftables), `util-linux` (the `flock` that
serialises runs), `systemd` and `getent` (glibc, usually present). Without
`flock` the helper still works but logs a notice and runs unserialised.

```bash
# Hand the two rules above to the helper: it deletes and re-adds only rules
# with this address inside its state, so seed the address it should own.
sudo install -d -m 700 /var/lib/timefence-ddns
echo "$HOME_IP" | sudo tee /var/lib/timefence-ddns/ip >/dev/null
sudo install -m 755 deploy/ddns-allowlist.sh /usr/local/bin/ddns-allowlist.sh
sudo ddns-allowlist.sh ddns.example.org --dry-run    # prints the commands
sudo ddns-allowlist.sh ddns.example.org              # applies them
sudo install -m 644 deploy/timefence-ddns.service /etc/systemd/system/
sudo install -m 644 deploy/timefence-ddns.timer /etc/systemd/system/
sudo editor /etc/systemd/system/timefence-ddns.service   # set the hostname
sudo systemctl daemon-reload
sudo systemctl enable --now timefence-ddns.timer
systemctl list-timers timefence-ddns.timer
journalctl -u timefence-ddns.service -n 20
```

The timer runs the helper one minute after boot and every five minutes after
that. The helper writes its state to `/var/lib/timefence-ddns/` (mode 0700;
override with `--state-dir`), so a run that finds the same address makes **no**
firewall call at all.

What it guarantees:

- it only ever adds or removes rules whose comment is `timefence-ddns`, for the
  address it just resolved and the addresses recorded in the state directory
  (`ip`, plus every address in `added` that still has a rule); it never touches
  other rules and never runs `ufw reset`, `ufw disable` or `ufw default`;
- it adds the new rules **before** removing the old ones, and records a new
  address in `added` as soon as a rule for it exists, so an address that was
  only briefly current, or a run that stopped half way, is cleaned up by the
  next run;
- a failed add exits non-zero and keeps the previous rules; when only one of the
  two protocols could be added, the address stays recorded until a later run
  removes it;
- it refuses to run when the name resolves to a loopback, private, link-local,
  CGNAT, multicast or `0.0.0.0` address (exit 2), and when the name looks like
  shell syntax (exit 64) — a hijacked or misconfigured DDNS name cannot open DNS
  to the wrong network;
- a resolution failure (exit 1) keeps the existing rules, because the previous
  address may still be correct;
- `--dry-run` prints every command and changes nothing.

When the address really changed, the two rules are replaced within five minutes.
Devices keep working throughout because the old rule is removed only after the
new one exists.

## Caveats

- **Plain DNS is unencrypted.** Anyone between the house and the VM can read the
  lookups. If the router supports DNS-over-TLS, point it at the VM with a
  certificate and restrict port 853 the same way — or prefer DNS-over-HTTPS to a
  provider you trust over plain DNS to your own box.
- **NAT collapses the household into one address** from the VM's point of view.
  Budgets stay per group, not per device; per-device budgets are a later
  feature.
- **Do not add a second public DNS server** on the router as a "backup". Devices
  spread queries over both, so half the blocking silently disappears.
- **The VM is now part of your household's DNS.** Patch it, and keep the
  TimeFence and AdGuard UIs on loopback.

## If DNS stops working

Have this written down before you need it:

1. On the router, set DNS back to the ISP's resolvers (or `1.1.1.1`) so the
   household is online again.
2. Check the VM: `systemctl status AdGuardHome timefence`, `journalctl -u
   timefence-ddns.service -n 50`, `ufw status numbered`.
3. Fix the rule: `sudo ddns-allowlist.sh <your-ddns-host> --dry-run` first, then
   without `--dry-run`.
4. Point the router back at the VM and confirm with
   `dig @<vm-address> example.com +short` from the house.

Keep the SSH port reachable (from your operator address) so you can always get
in; the console of the VM provider is the last resort.

## Tests

`tests/test_ddns_allowlist.py` runs the helper against stub `getent` and `ufw`
executables: it checks the refusal of shell-looking hostnames, the refusal of
non-public addresses, add-before-delete ordering, the no-op on an unchanged
address, the failure paths and `--dry-run`. CI also runs `shellcheck` on the
script.
