# Screenshots

These images are captured from the development server (`python
scripts/dev_server.py`), which runs TimeFence against a **fake AdGuard Home
with sample data** — no real household, DNS logs or credentials are shown. They
are light theme unless the file name ends in `-dark`. There is no browser chrome
in any capture.

| file | shows |
|---|---|
| `kid-390.png` | the read-only kid screen on a phone: one line per group saying open until / opens at, plus a closed group muted |
| `kid-390-dark.png` | the same kid screen in dark mode |
| `kid-1280.png` | the kid screen at desktop width |
| `admin-today-390.png` | the admin Today card on a phone: the live schedule line, Block / Auto / Free and quick actions |
| `admin-today-1280.png` | the admin Today view at desktop width |
| `admin-today-1280-dark.png` | the same view in dark mode |
| `groups-1280.png` | the Groups tab: allowed times, targets, owners and the AdGuard services picker |
| `groups-editor-390.png` | the group editor on a phone, with the allowed times |
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
