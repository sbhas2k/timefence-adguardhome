# Screenshots

These images are captured from the development server (`python
scripts/dev_server.py`), which runs TimeFence against a **fake AdGuard Home
with sample data** — no real household, DNS logs or credentials are shown. They
are light theme unless the file name ends in `-dark`. There is no browser chrome
in any capture.

| file | size | shows |
|---|---|---|
| `kid-390.png` | 390×844 | the read-only kid screen on a phone: one line per group saying opens at 21:00, opens tomorrow at 16:00, open all day or free time until, with the closed groups set off in a warm highlight |
| `kid-390-dark.png` | 390×844 | the same kid screen in dark mode |
| `kid-1280.png` | 1280×834 | the kid screen at desktop width |
| `admin-today-390.png` | 390×1205 | the admin Today list on a phone: the All groups Block / Auto / Free row, then each group's live line with the day of its next opening and its allowed times, Block / Auto / Free, and the free group's Extra time and Clear buttons |
| `admin-today-1280.png` | 1280×1151 | the same Today list at desktop width |
| `admin-today-1280-dark.png` | 1280×1151 | the same view in dark mode |
| `groups-1280.png` | 1280×800 | the Groups tab: every group with its allowed times, its targets, the "Shared (all users)" line and Edit / pause, above the Discover domains card |
| `groups-editor-390.png` | 390×844 | the new-group editor on a phone: the two starter presets, emoji and name, an allowed time with the seven day chips on one line and "Every day" below them, Remove / Add a time, and the AdGuard services picker button |
| `alerts-1280.png` | 748×732 | Settings → Alerts, card only: the Telegram channel selected and enabled, its bot token and chat id fields, and the three event toggles |

## Regenerating them

Start the development server (it uses a temporary database and a fake AdGuard
Home, so it is safe to click around):

```bash
TZ=Europe/London python scripts/dev_server.py   # http://127.0.0.1:8099; TZ=UTC when unset
```

Add accounts and sample groups, then capture at 390×844 (phone) and 1280×800
(desktop), light and dark. Keep each PNG small (well under 150 KB) and make sure
no browser chrome, address bar or real data is in the frame.

Most captures are full-page screenshots, so a page taller than its viewport
gives a taller PNG than the 390×844 or 1280×800 window. Two are not page
captures: the group editor is a fixed sheet, so its shot is the viewport itself
(390×844), and the Alerts card is cropped to the card (748×732), with no page
background around it.
