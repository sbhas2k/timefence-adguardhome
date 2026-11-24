# How TimeFence works

TimeFence is a thin, opinionated controller around the AdGuard Home control
API. It stores intent (groups, allowed times, owner accounts) and makes AdGuard
match that intent.

## Data model

- **groups** — name, emoji, `paused` flag, optional `owner`, and its targets
  (`services` = AdGuard blocked-service ids, `domains` = `||domain^`).
- **free_allow** — a group is allowed whatever its schedule says until a
  timestamp.
- **group_windows** — 0..4 **allowed** windows per group: start and end as
  minutes after local midnight, and the days they apply to (`all`, `weekday`,
  `weekend`). Inside a window the group is open; outside every window it is
  blocked. No rows = always open.
- **alerts_sent** — one row per notification, reserved before delivery so a
  restart does not resend it and the hourly limit can be counted (a failed
  delivery keeps its row). Pruned after 14 days.
- **users** — username, role (`admin`/`user`), Argon2id hash, `disabled`,
  `session_epoch`. The epoch is bumped whenever existing cookies must stop
  working.
- **events** — append-only log of logins, admin changes, migrations.
- **sessions** — the 0.3.0 table, kept read-only after the upgrade; nothing
  writes or reads it.

The SQLite file is `${DATA_DIR}/timefence.db`, created and migrated atomically on
start (`PRAGMA user_version`, schema v6). An upgrade of an existing database
first writes one verified copy named `<database>.pre-v<schema>.bak` next to it. The file is mode 0600 and the data
directory 0700; the process sets `umask 077` before creating anything.

## Allowed times

A group is open when *at least one* of its windows contains the local time, and
the day filter of a window belongs to the day the window **starts**: a
`weekday` 21:00 – 07:00 window covers Saturday morning, because it started on
Friday night. Windows may cross midnight and overlapping windows are a union; a
`weekend` window only ever applies to Saturday and Sunday. A group with no
windows has no restriction and is always open — it can still be blocked, paused
or freed like any other group.

To allow a group all day, give it no windows: a window cannot end at 24:00, so
`00:00 – 23:59` would close the group for the last minute of the day.

```
open_now = any(window contains (local date, local minute-of-day))
```

Everything is evaluated on the local wall clock in `TZ`. The next change is
found by walking candidate local dates and window edges forward (never by
assuming a day is 86400 seconds), so the days a clock jumps forward or falls
back are handled without guessing a skipped or repeated local time. A window is
enforced by the normal reconciler pass, so it opens and closes within one
`RECONCILE_INTERVAL`.

The effective timezone is, in order: the `TZ` environment variable when set
(an unknown name stops the start with a clear error), the host's
`/etc/timezone` or the resolved `/etc/localtime`, and finally UTC. The fallback
is the only case that shows the `tz_utc` warning; an explicit `TZ=UTC` or a
system timezone that happens to be UTC is a deliberate choice. `/etc/timezone`
and `/etc/localtime` are compared against the local zoneinfo database, so a name
the system does not know is ignored.

Each group reports a server-side `status` with a single precedence, and
`permitted` is derived from the same predicate, so it is always
`status in {allowed, free}`:

| status | meaning | `blocked_until` |
|---|---|---|
| `paused` | an admin paused the group (or it is deleted) | – |
| `free` | an admin "Free until" window is open; it disables the windows | – |
| `blocked_admin` | "Block now" is active | the force-block end |
| `outside_hours` | no allowed window is open right now | the next opening |
| `allowed` | inside an allowed window, or the group has none | – |

"Free until" wins over a closed window and over "Block now"; a "Block now"
wins over an open window. Both are deliberate admin actions, so neither depends
on the schedule. The API also reports `open_now` and `next_change`, plus
`opens_at` (when `outside_hours`) and `open_until` (when `allowed` and the group
has windows) as local `HH:MM` — the kid screen prints those verbatim, and has no
clock of its own.

## Who sees what

Each group has an optional `owner` (a user account); deleting the account clears
the owner and leaves the group in place, shared with everybody. Admins see and manage every
group; a `user` sees the groups they own plus the shared ones, on a dedicated
read-only screen, and the server refuses every mutating `/api/*` call from a
`user` session with `403 read_only`. The user state payload is reduced to the
group's identity and schedule — `id`, `name`, `emoji`, `permitted`, `status`,
`open_now`, `opens_at`, `open_until`, `free_until`, `paused` — with no windows,
services, domains, owner names or AdGuard details.

Allowed times are per group, so "one schedule per child" is "one group per
child, owned by that child".

## Sessions and accounts

Cookies are signed (itsdangerous) and carry the account id and its
`session_epoch`. Every request with a cookie checks that the account still
exists, is enabled, still has the same role and username, and that the epoch
matches. Disabling, deleting, demoting or changing an account's password bumps
the epoch, so every existing cookie for that account stops working on the next
request. Changing your own password re-issues your cookie in the same response,
so you stay signed in. Logging out bumps the account's `session_epoch` before it
clears your cookie, so it signs that account out on every device, not only in the
browser you used.

Sign-in is JSON-only (415 otherwise). The order is: address lockout → body
validation → username format (invalid usernames are counted on the address key
only) → account+address lockout → hash verification. Unknown and disabled
accounts still run a dummy Argon2 verification, so their timing matches a real
account. Key derivation is limited to four concurrent sign-ins and two
concurrent hashes.

## Intended state → AdGuard

Two independent things are managed:

1. **Blocked services.** `services` of every group that is *not* permitted are
   added to AdGuard's blocked-services list. TimeFence only ever removes
   services it owns (tracked in `owned_services`, so deleting a group unblocks
   its service again instead of orphaning it). The rest of the list and the
   `schedule` object are preserved through a GET → modify → PUT cycle.
2. **User rules.** Domains of every non-permitted group become `||domain^`
   lines inside

   ```
   # timefence:begin
   ||example.com^
   # timefence:end
   ```

   The section is rebuilt on every change; all other lines (including a
   `# budget-app` section from the old app, which is migrated silently) are
   kept verbatim. An unbalanced marker is dropped on its own and never deletes
   the lines around it; the admin banner then says the section is damaged.

A group is *permitted* when its status is `allowed` or `free`: not paused, not
force-blocked, and either a free-allow window is open or one of its allowed
times is open now.

## Reconciler

A background loop runs every `RECONCILE_INTERVAL` seconds (default 30) and
immediately after every action (block, free, pause, group edit, delete,
discover-add). Each pass:

1. computes the desired service ids and domain rules,
2. compares them with what AdGuard currently reports,
3. writes **only** when they differ.

That makes it idempotent and cheap: a steady state produces no AdGuard writes
at all, and a human edit in the AdGuard UI is corrected within one interval. One
lock serialises database access (request handlers, the loop and the shutdown
path), and the AdGuard HTTP calls are made outside that lock. A failing phase
(including an unexpected error) is logged and recorded; it never skips the other
phases.

**Rejected credentials are special.** AdGuard Home counts failed authentications
per source address and, after five, stops answering for `block_auth_min`
(15 minutes by default) — even to the right password, for TimeFence and for
every other client on that address. So a `401`/`403` ends the pass at the first
call (no second phase, no shutdown re-block, no `timefence release` retry) and
starts a back-off: 30 s, then doubling, capped at 15 minutes, reset by the first
success. The admin banner names the rejection and the next attempt, and
`/healthz` stays `503` while it lasts. The admin connection test
(`POST /api/adguard/test`) clears the back-off so a corrected password is tried
immediately.

`/healthz` needs no login and no lock. It answers
`200 {"ok":true,"status":"ok","version"}` while the reconciler had a successful
sync within `3 × RECONCILE_INTERVAL`, and
`503 {"ok":false,"status":"degraded","version"}` otherwise. It deliberately does
not report why: the reason is in the admin banner and the logs. An
auto-restart tool that acts on the health check will restart TimeFence whenever
AdGuard Home is unreachable; that does not help and is not recommended.

## Alerts

One channel at a time: **Telegram** (a bot token and a numeric chat id),
**ntfy** (a server base URL, a topic and an optional access token) or a generic
**webhook**. The configuration lives in the settings table; the bot token, chat
id, topic, token and webhook path are write-only through the API (reads return a
boolean and the last two characters only). `alerts_sent` is both the
de-duplication record and the rate-limit log, pruned after 14 days.

Telegram is the only channel with a fixed destination: the request goes to
`https://api.telegram.org/bot<token>/sendMessage` with a JSON body
(`chat_id`, `text`, `disable_notification` for the default priority),
so no admin-supplied URL is parsed for it. The token travels in the request
path, which is why the `urllib3` logger is pinned to `WARNING` at import: at
`DEBUG` it would print the path (and with it the token, or a webhook secret).
The same transport rules apply to every channel — no redirects, no proxy from
the environment, one attempt, a hard 5 s deadline — and a failure leaves the
module as a short category (`http_401` for a bad bot token, `http_400`/`403`/`404`
for a bad chat id or a bot you never pressed Start on), never as a message or a
traceback.

Each reconcile run calls a cheap **alert probe** before the AdGuard phases. It
takes the store lock once, builds the per-group facts, classifies the events and
enqueues them; its own errors are logged once and never touch the run result or
the event log. Delivery happens in a separate daemon thread, so no request or
reconcile run waits for it. Shutdown stops the worker and joins it (up to 2.5 s)
before it blocks the groups and closes the database, because the worker writes
its bookkeeping through the same lock.

| event | fires | default | de-duplicated by |
|---|---|---|---|
| `group_closed` | the first run whose status is `outside_hours` | off | `closed:<group>:<next opening>`, so one closing sends one message and a window that crosses midnight cannot repeat it after 00:00 |
| `sync_failing` | three runs in a row without a successful AdGuard sync | on | the outage start |
| `sync_recovered` | the first run after that outage | on | the outage start |

- Only the schedule closes a group. A paused, free or force-blocked group never
  produces `group_closed`: those are deliberate admin states, not the clock.
- An outage needs three consecutive stale runs; any success resets the count, so
  a flapping AdGuard never alerts. The outage start is persisted, so a restart
  during an outage does not repeat the message. Durations use the monotonic
  clock; the persisted stamps use wall time.
- At most 20 messages go out per rolling hour. `sync_failing` is protected: it is
  dropped only when the hour already holds 20 messages of its own class. A full
  queue drops the oldest message of the incoming class (or the newcomer). Drops
  increment a counter that survives restarts and is shown in the alert settings.
- One attempt, no retry, no redirects; the queue holds 50 messages; the response
  body is never logged, and a delivery is abandoned after a hard 5 s
  wall-clock deadline. The **Test** button sends one message directly, outside
  the queue and the hourly limit, with the same transport rules; only one test
  runs at a time (`429 busy` otherwise).
- Webhooks receive JSON with `event`, `time` (ISO-8601 with the local offset)
  and `message`, plus `group` for `group_closed` or `outage_seconds` for the two
  sync events. ntfy receives a plain-text body with `Title`, `Priority` and
  `Tags` headers; Telegram receives the same message text in its own JSON body.
  A message only ever contains a group name, numbers and a time — never a domain,
  client address or username.

## Fail-safety

- **Clean stop blocks every managed group.** On shutdown the loop stops, then
  TimeFence blocks the services and domains of every non-deleted group before it
  closes the database. Use `timefence release` when you stop TimeFence on
  purpose and want the blocks lifted (the uninstaller does this for you).
- **A crash or `kill -9` cannot block.** AdGuard keeps whatever state TimeFence
  last wrote, so a crash usually leaves groups unblocked until the service
  starts again (and re-applies the true state).
- If AdGuard is unreachable the reconciler logs the error, keeps the intent in
  SQLite and retries on the next pass; the UI shows a banner.

## DNS caching

AdGuard caches answers. A device that just lost (or gained) access may keep
using a cached answer until that entry expires. TimeFence does not change any of
AdGuard's DNS settings (including its maximum cache TTL). Lower AdGuard's
maximum cache TTL under *Settings → DNS settings* if you want faster changes;
resolvers and browsers may additionally cache, which is why a page sometimes
needs a reload. DNS only affects *new* lookups: a running game or stream keeps
playing until it reconnects and resolves something again. The full list of what
DNS-level blocking cannot do is in [limitations](limitations.md).
