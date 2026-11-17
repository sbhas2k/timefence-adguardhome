# How TimeFence works

TimeFence is a thin, opinionated controller around the AdGuard Home control
API. It stores intent (groups, budgets, sessions) and makes AdGuard match that
intent.

## Data model

- **groups** — name, emoji, weekday/weekend minutes, `paused` flag, and its
  targets (`services` = AdGuard blocked-service ids, `domains` = `||domain^`).
- **sessions** — one open session per group at most; `planned_seconds`,
  `started_at`, `ended_at`, `used_seconds`, `end_reason`.
- **bonus** — extra seconds granted per group per local day (admin).
- **free_allow** — a group is allowed without spending budget until a timestamp.
- **events** — append-only log of logins, sessions, admin changes, migrations.

The SQLite file is `${DATA_DIR}/timefence.db`, created and migrated on start
(`PRAGMA user_version`).

## Budget arithmetic

```
budget   = minutes_for_today(group) * 60 + bonus_seconds(group, today)
used     = sum(used_seconds of today's sessions)  (+ live part of an active one)
remaining= max(0, budget - used)
```

`minutes_for_today` picks `weekend_minutes` on Saturday/Sunday in `TZ`,
otherwise `weekday_minutes`. "Today" is the local calendar day in `TZ`, so the
budget resets at local midnight — no cron job is involved.

A session's `planned_seconds` is `min(requested_minutes * 60, remaining)`
rounded down to whole minutes. When it ends by expiry the full planned time is
charged; when the user presses **Stop**, only `now - started_at` is charged.

## Two modes

Every group has a `mode`:

- **auto** (default) — allowed while the daily budget lasts. Nothing is blocked
  up front. Usage is metered (below); when the budget is exhausted the group is
  blocked until local midnight. `Block now` force-blocks it earlier, `Allow
  freely until` overrides everything.
- **session** — blocked unless a session is running. `15/30/60` buttons start a
  session capped at the remaining budget; `Stop` bills only the elapsed
  seconds; expiry charges the planned window. On restart open sessions are
  closed (`service_reason`) and the group re-blocks.

## Metering (auto mode)

The only usage signal available is AdGuard's query log. Once per reconcile tick
(30 s) TimeFence looks at the queries that arrived since the previous tick and,
for every **auto** group that is currently permitted, checks whether any of its
domains (or the domains covered by its AdGuard services) were resolved. If so,
the elapsed window is charged to that group's day, capped at the budget.

Details that matter:

- loopback queries (`127.0.0.1`, `::1`) are ignored, so TimeFence's own health
  checks never consume budget;
- a group only accrues usage while it is permitted, so a blocked group cannot
  run up a debt through retries;
- the first tick after a start only sets the cursor (no back-fill);
- metering is an approximation: a player that streams from cached IPs or
  bypasses DNS (DoH/VPN/mobile data) will under-report, and background API
  calls may add a few seconds that are not "watching".

## Intended state → AdGuard

Two independent things are managed:

1. **Blocked services.** `services` of every group that is *not* permitted are
   added to AdGuard's blocked-services list. TimeFence only ever removes
   services it owns (tracked in `owned_services`, so deleting a group unblocks
   its service again instead of orphaning it). The rest of the list and the
   `schedule` object are preserved byte-for-byte through a
   GET → modify → PUT cycle.
2. **User rules.** Domains of every non-permitted group become `||domain^`
   lines inside

   ```
   # timefence:begin
   ||example.com^
   # timefence:end
   ```

   The section is rebuilt on every change; all other lines (including a
   `# budget-app` section from the old app, which is migrated silently) are
   kept verbatim.

A group is *permitted* when it is not paused and either an active session has
not expired or a free-allow window is open.

## Reconciler

A background loop runs every `RECONCILE_INTERVAL` seconds (default 30) and
immediately after every action (start, stop, block, bonus, free, group edit,
delete, discover-add). Each pass:

1. closes expired sessions (charging the planned time),
2. computes the desired service ids and domain rules,
3. compares them with what AdGuard currently reports,
4. writes **only** when they differ.

That makes it idempotent and cheap: a steady state produces no AdGuard writes
at all, and a human edit in the AdGuard UI is corrected within one interval.

## Fail-safety

- On startup every session with `ended_at IS NULL` is closed with
  `end_reason = "service_restart"` (billed up to the planned time), then a full
  reconcile runs. A crash, kill or reboot therefore ends in "everything
  blocked" and never in "left unblocked".
- If AdGuard is unreachable the reconciler logs the error, keeps the intent in
  SQLite and retries on the next pass; the UI shows a banner.

## Why the DNS cache TTL is capped at 60 s

AdGuard caches answers. A device that just lost (or gained) access would keep
using a cached answer until it expires. TimeFence sets `cache_ttl_max = 60`
(through `dns_config`, preserving every other DNS setting) so the change lands
within about a minute. Resolvers and browsers may additionally cache, which is
why a page sometimes needs a reload.
