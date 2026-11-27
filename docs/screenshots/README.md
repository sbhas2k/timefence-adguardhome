# Screenshots

These images are captured from the development server (`python
scripts/dev_server.py`), which runs TimeFence against a **fake AdGuard Home
with sample data** — no real household, DNS logs or credentials are shown. They
are light theme unless the file name ends in `-dark`. There is no browser chrome
in any capture.

| file | shows |
|---|---|
| `kid-390.png` | the read-only kid screen on a phone: one line per group saying open until / opens at / free time until, plus a closed group muted |
| `kid-390-dark.png` | the same kid screen in dark mode |
| `kid-1280.png` | the kid screen at desktop width |
| `admin-today-390.png` | the admin Today card on a phone: the live schedule line with its per-day times, Block / Auto / Free, the free group's Extra time and Clear buttons, and the Block everything now / Release everything actions |
| `admin-today-1280.png` | the admin Today view at desktop width |
| `admin-today-1280-dark.png` | the same view in dark mode |
| `groups-1280.png` | the Groups tab: allowed times, targets, owners and the AdGuard services picker |
| `groups-editor-390.png` | the new-group editor on a phone: the preset row, time windows with day-of-week chips, services and owner |
| `alerts-1280.png` | Settings → Alerts: one Telegram, ntfy or webhook channel and the event toggles |

## Regenerating them

Start the development server (it uses a temporary database and a fake AdGuard
Home, so it is safe to click around):

```bash
TZ=Europe/London python scripts/dev_server.py   # http://127.0.0.1:8099; TZ=UTC when unset
```

Add accounts and sample groups, then capture at 390×844 (phone) and 1280×800
(desktop), light and dark. Keep each PNG small (well under 150 KB) and make sure
no browser chrome, address bar or real data is in the frame.

Five captures are taller or narrower than those two sizes, because the content
does not fit the viewport:

- `admin-today-1280.png` and `admin-today-1280-dark.png` are full-page desktop
  captures (1280×927), not viewport crops.
- `admin-today-390.png` (390×968) and `groups-editor-390.png` (390×889) are
  full-page phone captures.
- `alerts-1280.png` is a card crop of the Alerts settings panel (748×732), not a
  full-width desktop frame.
