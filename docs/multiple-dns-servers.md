# Multiple AdGuard Home servers

If more than one AdGuard Home serves your network (load-balanced at the
router, a server in another room, a failover box), do **not** run TimeFence
on each of them. TimeFence writes exactly two AdGuard objects — the set of
blocked services (`blocked_services/update`) and the user rules
(`filtering/set_rules`). [AdGuard Sync](https://github.com/atoy3731/adguard-sync)
replicates exactly those, one-way, from a primary to a secondary. So the
shape is: TimeFence on the primary, Sync to the replica, and the router
points clients at both boxes.

```
 devices ──► home router ──► primary AdGuard Home (port 53)
             (DNS: both boxes)        ▲
                                      │ TimeFence (ADGUARD_URL = the primary)
             you ──► TimeFence UI ────┘

 AdGuard Sync (its own container):
   primary ──► secondary AdGuard Home   every REFRESH_INTERVAL_SECS
   (one-way — the replica never pushes back; it runs no TimeFence)
```

## Run TimeFence on the primary only

Point `ADGUARD_URL` at the primary's AdGuard Home and keep TimeFence there.
The replica's blocks and rules arrive by sync; it has no TimeFence of its
own. A second TimeFence on the secondary would be a second writer, and the
one-way loop means its changes go nowhere but its own box — and when the
next sync overwrites them, the two schedules fight.

## Running AdGuard Sync

AdGuard Sync is a **long-running container with its own poll loop — there is
no cron and nothing to schedule**. It wakes up every `REFRESH_INTERVAL_SECS`
(default 60), reads the primary, and pushes to the secondary. That is the
whole mechanism; the settings that matter for TimeFence are:

| Variable | Meaning | Default |
|---|---|---|
| `ADGUARD_PRIMARY` | the AdGuard Home TimeFence manages (use an IP, not a hostname) | required |
| `ADGUARD_SECONDARY` | the replica | required |
| `REFRESH_INTERVAL_SECS` | how often it copies primary → secondary | `60` |
| `SYNC_BLOCKED_SERVICES` | replicate the blocked-services set | `true` |
| `SYNC_CUSTOM_RULES` | replicate the user rules | `true` |

A standalone container:

```bash
docker run -d --name adguard-sync --restart=always \
  -e ADGUARD_PRIMARY=http://192.168.1.2 \
  -e ADGUARD_SECONDARY=http://192.168.1.3 \
  -e ADGUARD_USER=admin \
  -e ADGUARD_PASS='<password>' \
  atoy3731/adguard-sync:2.1
```

or the same as a service in a compose file. The primary and secondary can
have different admin credentials: `SECONDARY_ADGUARD_USER` and
`SECONDARY_ADGUARD_PASS` (they default to `ADGUARD_USER`/`ADGUARD_PASS`).

## What to know before you rely on it

- **The replica lags by one sync cycle.** A "block everything now" lands on
  the primary immediately and on the secondary within `REFRESH_INTERVAL_SECS`.
  TimeFence's reconciler keeps re-pushing the current state to the primary
  every `RECONCILE_INTERVAL`, so the replica converges to the current
  decision rather than chasing a stale one. For a fallback box, sixty
  seconds is fine; lower `REFRESH_INTERVAL_SECS` if you want it tighter —
  just remember each wake-up polls both AdGuard instances.
- **Everything `true` gets copied, not just TimeFence's two objects.**
  By default AdGuard Sync also carries rewrite entries, block/allow lists,
  the general settings and the DNS settings. If the replica's AdGuard has
  settings you want to keep different, turn those toggles off; keep
  `SYNC_BLOCKED_SERVICES` and `SYNC_CUSTOM_RULES` on.
- **Keep the two AdGuard Home instances the same version.** TimeFence blocks
  by service id and by rule text; the ids have to match on both boxes.
- **Sync stays one-way.** "Release everything" in TimeFence's UI lifts the
  blocks on the primary, and the sync carries that lifting to the replica.
  Nothing you do in the replica's UI is authoritative — the next sync
  overwrites it.
- **Leave `SYNC_ENCRYPTION_SETTINGS` off** (its default). Certificates and
  keys do not travel that way; the files would have to exist on both boxes.
- **Check it on the real replica:** after a time window opens or closes,
  confirm the secondary enforces it within a sync cycle: from the router,
  `dig @<replica-address> youtube.com` should give no real address while
  the group is closed (a sinkhole/empty answer, whatever your AdGuard
  blocked-mode setting produces) and a real IP once it opens. That is the
  one behaviour this page does not promise — the block set and rules are
  what sync copies, and TimeFence keeps the set current, but the first run
  on your hardware is the proof.

## Failover

The model above is "TimeFence on the primary, Sync to the secondary." If your
boxes swap roles after a failure (the secondary becomes the one devices
point at, or a new box takes over as primary), point TimeFence's
`ADGUARD_URL` at the box that is now primary and run AdGuard Sync the other
way. TimeFence's reconciler only ever pushes the current decision to the
box it points at, so nothing else changes — and you must not run TimeFence
on *both* sides at once.
