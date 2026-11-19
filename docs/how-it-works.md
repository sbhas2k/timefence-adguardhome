# How TimeFence works

TimeFence is a thin, opinionated controller around the AdGuard Home control
API. It stores intent (groups, budgets, owner accounts) and makes AdGuard match
that intent.

## Data model

- **groups** — name, emoji, weekday/weekend minutes, `paused` flag, optional
  `owner`, and its targets (`services` = AdGuard blocked-service ids, `domains`
  = `||domain^`).
- **usage_seconds** — metered activity per group per local day. This is the only
  source of "used today".
- **bonus** — extra seconds granted per group per local day (admin).
- **free_allow** — a group is allowed without spending budget until a timestamp.
- **group_windows** — 0..4 bedtime windows per group: start and end as minutes
  after local midnight, and the days they apply to (`all`, `weekday`,
  `weekend`).
- **alerts_sent** — one row per notification, reserved before delivery so a
  restart does not resend it and the hourly limit can be counted (a failed
  delivery keeps its row). Pruned after 14 days.
- **users** — username, role (`admin`/`user`), Argon2id hash, `disabled`,
  `session_epoch`. The epoch is bumped whenever existing cookies must stop
  working.
- **events** — append-only log of logins, admin changes, migrations.
- **sessions** — the 0.3.0 table, kept read-only as history after the upgrade;
  nothing writes or reads it for budgets.

The SQLite file is `${DATA_DIR}/timefence.db`, created and migrated atomically on
start (`PRAGMA user_version`, schema v5). An upgrade of an existing database
first writes one verified copy named `<database>.pre-v<schema>.bak` next to it. The file is mode 0600 and the data
directory 0700; the process sets `umask 077` before creating anything.

## Budget arithmetic

```
budget    = minutes_for_today(group) * 60 + bonus_seconds(group, today)
used      = seconds metered for the group's local day
remaining = max(0, budget - used)
```

`minutes_for_today` picks `weekend_minutes` on Saturday/Sunday in `TZ`,
otherwise `weekday_minutes`. "Today" is the local calendar day in `TZ`, so the
budget resets at local midnight — no cron job is involved, and the day boundary
is computed from a date, not from `now + 86400`, so daylight-saving days (23 or
25 hours) are correct.

The effective timezone is, in order: the `TZ` environment variable when set
(an unknown name stops the start with a clear error), the host's
`/etc/timezone` or the resolved `/etc/localtime`, and finally UTC. The fallback
is the only case that shows the `tz_utc` warning; an explicit `TZ=UTC` or a
system timezone that happens to be UTC is a deliberate choice. `/etc/timezone`
and `/etc/localtime` are compared against the local zoneinfo database, so a name
the system does not know is ignored.

A recorded usage value never goes down: a negative bonus can lower the cap for
*future* charges but cannot erase time that was already metered.

Each group also reports a server-side `status` with a single precedence. The
`permitted` flag is derived from the same predicate, so it is always
`status in {allowed, free}`:

| status | meaning | `blocked_until` |
|---|---|---|
| `paused` | an admin paused the group | – |
| `free` | an admin "Free until" window is open; it overrides everything | – |
| `blocked_admin` | "Block now" is active | the force-block end |
| `bedtime` | a bedtime window is open | the end of the window |
| `limit_reached` | no budget left today | next local midnight |
| `allowed` | budget left | – |

## Bedtime windows

A group can carry up to four bedtime windows: a start time, an end time and the
days it applies to (`all`, `weekday` = Monday to Friday, `weekend` = Saturday
and Sunday). While a window is open the group is blocked even if it still has
minutes, and no usage is charged (the same as `Block now`). Windows may cross
midnight (`21:00` – `07:00`), and overlapping windows are a union.

The day filter belongs to the day the window **starts** on: a `weekday`
21:00 – 07:00 window therefore covers Saturday morning, because it started on
Friday night. Everything is evaluated on the local wall clock in `TZ`, one
minute at a time, so the days a clock jumps forward or falls back are handled
without guessing a skipped or repeated local time. The end of an open window is
reported as a local time (`bedtime_until`, for example `07:00`), which the kid
screen prints verbatim. A set of windows can cover every minute of the day (for
example `00:00–23:59` plus `21:00–07:00`); there is no end to report then, so
`bedtime_until` is `null` and both screens say "Bedtime" / "Bedtime is on"
instead of a time.

The admin's explicit **Free until** overrides a bedtime window — that is the
parent deciding to allow it — while the daily budget does not. A window is
enforced by the normal reconciler pass, so it starts and ends within one
`RECONCILE_INTERVAL`.

## Metering

The only usage signal available is AdGuard's query log. The reconciler reads it
in pages (`older_than`), newest first, and charges **15-second activity
buckets**:

- a bucket counts when at least one of the group's domains (or a hostname
  covered by one of its AdGuard services) was resolved inside it, by a client
  other than `127.0.0.1`/`::1`;
- answers that were *blocked* are not usage: `FilteredBlackList`,
  `FilteredBlockedService`, `FilteredSafeBrowsing`, `FilteredParental` and
  `FilteredInvalid` are ignored. SafeSearch rewrites and `Rewrite*` answers are
  answered queries and do count;
- free-allow windows, paused groups and groups inside a bedtime window are not
  metered;
- a bucket is charged once per group: the reconciler remembers the last charged
  bucket in memory, so two ticks inside one bucket cannot double-charge it (a
  restart can cost at most one bucket);
- every bucket is charged to *its own* local day, so a window that crosses
  midnight is split correctly;
- the cursor only moves forward, a short window is not dropped, and after a
  restart at most 30 minutes (`MAX_CATCHUP`) of missed time is caught up;
- a query log that is disabled, keeps less than an hour, or is too long to read
  surfaces as an admin warning (`querylog_disabled`, `querylog_retention_short`,
  `querylog_truncated`); a query-log `ignored` list that covers one of your
  groups shows up as `querylog_ignores_group`.

Metering is an approximation: a player that streams from cached IPs or bypasses
DNS (DoH/VPN/mobile data) will under-report, and background API calls may add a
few buckets that are not "watching".

## Who sees what

Each group has an optional `owner` (a user account); deleting the account clears
the owner and leaves the group in place, shared with everybody. Admins see and manage every
group; a `user` sees the groups they own plus the shared ones, on a dedicated
read-only screen, and the server refuses every mutating `/api/*` call from a
`user` session with `403 read_only`. The user state payload is reduced to time
and status: no services, domains, owner names, history or AdGuard details.

Budgets are per group, so "one budget per child" is "one group per child, owned
by that child".

## Sessions and accounts

Cookies are signed (itsdangerous) and carry the account id and its
`session_epoch`. Every request with a cookie checks that the account still
exists, is enabled, still has the same role and username, and that the epoch
matches. Disabling, deleting, demoting or changing an account's password bumps
the epoch, so every existing cookie for that account stops working on the next
request. Changing your own password re-issues your cookie in the same response,
so you stay signed in. Logging out only clears your cookie.

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
force-blocked, not inside a bedtime window, and either a free-allow window is
open or its remaining budget is positive.

## Reconciler

A background loop runs every `RECONCILE_INTERVAL` seconds (default 30) and
immediately after every action (block, bonus, free, group edit, delete,
discover-add). Each pass:

1. meters the query log (outside the database lock),
2. computes the desired service ids and domain rules,
3. compares them with what AdGuard currently reports,
4. writes **only** when they differ.

That makes it idempotent and cheap: a steady state produces no AdGuard writes
at all, and a human edit in the AdGuard UI is corrected within one interval. One
lock serialises database access (request handlers, the loop and the shutdown
path), and the AdGuard HTTP calls are made outside that lock. A failing phase
(including an unexpected error) is logged and recorded; it never skips the other
phases.

`/healthz` needs no login and no lock. It answers
`200 {"ok":true,"status":"ok","version"}` while the reconciler had a successful
sync within `3 × RECONCILE_INTERVAL`, and
`503 {"ok":false,"status":"degraded","version"}` otherwise. It deliberately does
not report why: the reason is in the admin banner and the logs. An
auto-restart tool that acts on the health check will restart TimeFence whenever
AdGuard Home is unreachable; that does not help and is not recommended.

## Alerts

One channel at a time: **ntfy** (a server base URL, a topic and an optional
access token) or a generic **webhook**. The configuration lives in the settings
table; the topic, token and webhook path are write-only through the API (reads
return a boolean and the last two characters only). `alerts_sent` is both the
de-duplication record and the rate-limit log, pruned after 14 days.

Each reconcile run calls a cheap **alert probe** after metering and before the
AdGuard phases. It takes the store lock once, builds the per-group facts,
classifies the events and enqueues them; its own errors are logged once and
never touch the run result or the event log. Delivery happens in a separate
daemon thread, so no request or reconcile run waits for it. Shutdown stops the
worker and joins it (up to 2.5 s) before it blocks the groups and closes the
database, because the worker writes its bookkeeping through the same lock.

| event | fires | default | de-duplicated by |
|---|---|---|---|
| `group_low` | `0 < remaining ≤ 10 min` and the group is allowed (not free) | on | `low:<group>:<local day>` |
| `group_exhausted` | the run that reaches 0 minutes | on | `ex:<group>:<local day>` |
| `bedtime_started` | the first run inside a bedtime window | off | `bedtime:<group>:<window end>`, so a window that crosses midnight fires once |
| `sync_failing` | three runs in a row without a successful AdGuard sync | on | the outage start |
| `sync_recovered` | the first run after that outage | on | the outage start |
| `weekly_summary` | Monday, local 08:00 or later | off | `weekly:<local Monday>` |

- A group that is paused, free, force-blocked or in a bedtime window never
  produces `group_low` or `group_exhausted`. Exhausted supersedes low, and a
  top-up after an exhausted message does not send a second one (the parent knows
  they topped up).
- An outage needs three consecutive stale runs; any success resets the count, so
  a flapping AdGuard never alerts. The outage start is persisted, so a restart
  during an outage does not repeat the message. Durations use the monotonic
  clock; the persisted stamps use wall time.
- The weekly summary is one message: the 7 local dates ending yesterday, one
  line per non-deleted group with `round(seconds / 60, 1)` minutes. If Monday
  was missed, the next run records the key without sending and logs one line.
- Everything uses local dates and the local wall clock (`TZ`): no `86400`
  arithmetic, so a week with a daylight-saving change totals the same seconds.
- At most 20 messages go out per rolling hour. `group_exhausted` and
  `sync_failing` are protected: they are dropped only when the hour already
  holds 20 messages of their own class. A full queue drops the oldest message of
  the incoming class (or the newcomer). Drops increment a counter that survives
  restarts and is shown in the alert settings.
- One attempt, no retry, no redirects; the queue holds 50 messages; the response
  body is never logged, and a delivery is abandoned after a hard 5 s
  wall-clock deadline. The **Test** button sends one message directly, outside
  the queue and the hourly limit, with the same transport rules; only one test
  runs at a time (`429 busy` otherwise).
- Webhooks receive JSON with `event`, `time` (ISO-8601 with the local offset)
  and `message`, plus `group`/`minutes_left`, `group`, `outage_seconds` or
  `days`/`groups` depending on the event. ntfy receives a plain-text body with
  `Title`, `Priority` and `Tags` headers. A message only ever contains a group
  name, numbers and a time — never a domain, client address or username.

## Usage history and export

The History tab is served by one query function, `budget.usage_range`: it reads
the metered and bonus tables over the local dates of the range (7, 14 or 30
days, built from a date and `timedelta`, never `86400` offsets) and fills zeros
in Python. It is exactly two statements whatever the number of groups and days.
Non-deleted groups only, ordered by name; each row carries the metered seconds,
the bonus seconds, both as minutes rounded to one decimal, and the minutes the
group is allowed on that date.

`GET /api/usage?days=7|14|30` returns `{"days", "rows"}` (admin only,
`no-store`); the range is validated as a string so anything else is a `400`
with an i18n message rather than a framework `422`. The tab fetches it when it
opens and when the range selector changes, and the 5 s poll never resets the
selector. `GET /api/usage.csv?days=` writes the same rows with
`csv.writer`: the header is `group,date,used_minutes,bonus_minutes`, the file is
UTF-8 without a BOM, named `timefence-usage.csv`, and every group name has
control characters removed and gets a leading `'` when it would otherwise start
with `=`, `+`, `-`, `@` or `'`, so a spreadsheet treats it as text. The
"minutes allowed" column is the group's *current* weekday/weekend setting
applied to that date — it is shown in the table and deliberately left out of
the CSV, which is a record of what was used. `/api/state.history` keeps its old
7-day shape for compatibility.

The weekly summary alert uses the same `usage_range`, so the table, the CSV and
the message can never disagree about a number.

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
needs a reload.
