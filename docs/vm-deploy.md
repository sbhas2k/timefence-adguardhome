# Deploy on a VM for your home network, step by step

This is the end-to-end walkthrough for the setup most families want: AdGuard Home
and TimeFence on a small VM with a public address, and every device at home
using that VM for DNS. The reference material (firewall rules in detail, the
allow-list helper's guarantees, recovery) is in [remote-vm.md](remote-vm.md);
this page is the order to do things in.

```
 phone / TV / laptop ──► home router ──► internet ──► VM: AdGuard Home (port 53)
                         (DDNS keeps its                    ▲
                          name up to date)                  │ TimeFence writes the
                                                            │ block state (loopback)
                         you, over an SSH tunnel ──► VM: TimeFence UI (8099)
```

What you need before you start:

- a Linux VM with a public IPv4 address, `sudo`, Docker with the compose plugin,
  `ufw`, and SSH access from an address you control;
- admin access to the home router;
- about 30 minutes. Do the steps in order: the firewall comes first, and DNS is
  only pointed at the VM at the end, once everything else works.

> ⚠️ Port 53 must never be open to the whole internet. Every step below keeps it
> reachable from your home address only.

## 1. Lock the VM down before anything listens

```bash
# Your own address, the one you SSH from (documentation range shown).
sudo ufw allow from 203.0.113.20 to any port 22 proto tcp
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw enable
```

Allow the SSH rule **before** `ufw enable`, or you lock yourself out. Nothing else
is opened yet; port 53 comes in step 4.

## 2. Run AdGuard Home and TimeFence

Use host networking for AdGuard Home, so that port 53 belongs to the host and
the firewall rules apply (a published Docker port is forwarded before ufw sees
it; see the [Docker caveat](remote-vm.md#docker-caveat)).

```bash
git clone https://github.com/sbhas2k/timefence-adguardhome.git && cd timefence-adguardhome

sudo install -d -m 700 /opt/adguard-docker/conf /opt/adguard-docker/work
docker compose -f deploy/adguard-compose.host.yml up -d
```

AdGuard Home's first-run wizard listens on port 3000 of the VM. The firewall
keeps it closed, so open it through a tunnel from your own computer:

```bash
ssh -N -L 3000:127.0.0.1:3000 you@vm.example.net
# then open http://127.0.0.1:3000/
```

In the wizard choose **Admin Web Interface: 127.0.0.1, port 3000** (so the admin
interface never listens on a public address) and **DNS server: all interfaces,
port 53**. Create the AdGuard admin account.

Now TimeFence, next to it with host networking:

```bash
sudo install -d -m 755 /etc/timefence
printf '%s' 'YOUR_ADGUARD_ADMIN_PASSWORD' | sudo tee /etc/timefence/adguard.pass >/dev/null
sudo chown root:10001 /etc/timefence/adguard.pass && sudo chmod 640 /etc/timefence/adguard.pass

cp deploy/docker-compose.host.yml docker-compose.yml
# optional: set TZ in that file, for example TZ: Europe/London
docker compose up -d
docker compose logs --tail 20 timefence
```

The bundled compose files build the image from the checkout (`build:` plus
`image: timefence:local`), so this works before any release exists. After the
code changes, `docker compose up -d --build` rebuilds and restarts TimeFence.

Open the TimeFence UI through a second tunnel and sign in as `admin` / `admin`;
it makes you choose a new password straight away:

```bash
ssh -N -L 8099:127.0.0.1:8099 you@vm.example.net
# then open http://127.0.0.1:8099/
```

In Settings, **Test connection** shows whether TimeFence can reach AdGuard Home.
Fix that before continuing. [docker.md](docker.md) covers other shapes (AdGuard Home on the
host, bridge networking).

## 3. Give the house a stable name (DDNS)

Your ISP changes the home's public address every now and then, and the VM has to
follow it. A DDNS hostname that always points at the current address solves
that.

**First check that DDNS can work at all.** Compare the WAN address on the
router's status page with what a "what is my IP" website shows from a device at
home.

- Same address: continue.
- The router shows an address starting `100.64.` to `100.127.`, or a private
  address (`10.`, `172.16.`–`172.31.`, `192.168.`): your ISP puts you behind
  carrier-grade NAT. No hostname can point at you. Ask the ISP for a public
  IPv4 address (many do for free on request) or use a VPN instead.

**Option A: the router does it.** Look for *DDNS* or *Dynamic DNS* in the
router's settings and enter the provider's hostname and credentials. Some
consumer routers have no DDNS client at all.

**Option B: a small updater on any device that is always on at home** (a PC, a
Raspberry Pi, a NAS). The example below uses DuckDNS, a free service; any
provider with an "update my address" URL works the same way. Create a subdomain
(`myhome` → `myhome.duckdns.org`) and copy your token, then on a Linux device:

```bash
echo 'YOUR_TOKEN' | sudo tee /etc/duck.token >/dev/null && sudo chmod 600 /etc/duck.token
sudo tee /usr/local/bin/duck.sh >/dev/null <<'EOF'
#!/bin/sh
curl -fsS "https://www.duckdns.org/update?domains=myhome&token=$(cat /etc/duck.token)&ip=" >/dev/null
EOF
sudo chmod 755 /usr/local/bin/duck.sh
echo '*/5 * * * * root /usr/local/bin/duck.sh' | sudo tee /etc/cron.d/duckdns >/dev/null
```

On Windows, a scheduled task that requests the same URL every five minutes does
the job. An empty `ip=` makes the provider use the address the request came
from.

Whichever option you use, keep the **TTL short (60–300 seconds)** if the
provider lets you choose, and check from the VM that the name resolves to your
home address:

```bash
getent ahostsv4 myhome.duckdns.org | head -1
```

## 4. Open port 53 to the home address, and keep it following

This is the script that follows the address. It re-resolves the hostname every
five minutes and rewrites only the port-53 firewall rules, adding the new rule
before removing the old one; it refuses private or carrier-grade addresses and
never opens DNS to the world. Details and guarantees: [remote-vm.md](remote-vm.md#dynamic-home-address-ddns).

```bash
sudo install -m 755 deploy/ddns-allowlist.sh /usr/local/bin/ddns-allowlist.sh
sudo ddns-allowlist.sh myhome.duckdns.org --dry-run    # prints the ufw commands
sudo ddns-allowlist.sh myhome.duckdns.org              # applies them

sudo install -m 644 deploy/timefence-ddns.service deploy/timefence-ddns.timer /etc/systemd/system/
sudo sed -i 's/ddns.example.org/myhome.duckdns.org/' /etc/systemd/system/timefence-ddns.service
sudo systemctl daemon-reload
sudo systemctl enable --now timefence-ddns.timer
systemctl list-timers timefence-ddns.timer
sudo ufw status numbered      # two rules, comment timefence-ddns, your home address
```

## 5. Prove the port is closed to everyone else

From a network that is **not** your home (a phone on mobile data, another VM):

```bash
dig @VM_ADDRESS example.com +time=3 +tries=1
```

It must **time out**. If it answers, port 53 is open to the world. The usual
cause is a container that publishes port 53 through Docker's bridge network;
fix that (host networking, or `DOCKER-USER` rules) before going on.

From a device at home the same command must answer.

## 6. Point the home network at the VM

In the router's DHCP settings, set the DNS server handed to devices (or set it
per device the first time you test).

- **Use one DNS server: the VM.** If a device insists on two fields, enter the
  VM twice. Never add a public resolver (Google, Cloudflare, your ISP) as the
  "secondary": devices do not treat it as a spare. Many send queries to both, and
  all of them switch to it when the first one does not answer. Every lookup that
  reaches it gets the real answer, so the block stops working, and it does so
  silently, most visibly right after your home address changes and before the
  allow-list catches up.
- **Turn off IPv6 on the home network** (router: IPv6 and, where it exists,
  prefix delegation). A device that has a public IPv6 address will often use the
  ISP's IPv6 DNS and never ask your VM at all. The allow-list covers IPv4 only.
- Reboot a device (a TV especially) so it drops old addresses and cached
  lookups, then check its network screen: only an IPv4 address (plus a harmless
  `fe80::` one) should remain.

## 7. Check that it works

1. In TimeFence, create a group (for example YouTube) and press **Block now**
   (or give it an allowed time that is not now).
2. Open the app on a device at home. It must fail.
3. In AdGuard Home's query log, look for blocked lines from your home address.
   The VM sees your whole house as that one address; the device's own
   `192.168.x.x` never shows.
4. Press **Allow freely**, or let the allowed time start, to see the other
   states.

If something does not block, go through
[Troubleshooting](troubleshooting.md): it covers IPv6, devices with their own DNS,
caches and AdGuard client settings in the order they usually apply.

## Afterwards

- **Alerts:** Settings → Alerts; Telegram, ntfy or a webhook. Press **Send test**
  once to prove the path from the VM works. See the [README](../README.md#alerts-telegram-ntfy-or-a-webhook).
- **Backups:** [backup-restore.md](backup-restore.md). The database holds password
  hashes and alert secrets.
- **Updating:** [upgrading.md](upgrading.md#manual-update-from-a-git-checkout-no-registry-no-ci)
  covers the manual route for the checkout on the VM: `sudo -E ./deploy/update.sh`
  pulls, builds, restarts and waits for `/healthz`, and `--rollback <tag>` goes
  back to an image you built earlier. Back up the data and configuration first.
  If the clone on the VM predates a rewritten history, a diverged `git pull`
  refuses to run; fix it with `git fetch origin && git reset --hard origin/main`
  instead of forcing the pull, after taking that backup (local commits are
  discarded).
- **If DNS stops working,** set the router's DNS back to the ISP's resolvers so
  the house is online, then follow "If DNS stops working" in
  [remote-vm.md](remote-vm.md#if-dns-stops-working). Keep SSH reachable and the
  VM provider's console in mind as the last resort.
- **Change the router's default admin password** if it is still the one on the
  label.
