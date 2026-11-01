# iPhone QA checklist — Rave Player web 1.6.24

Repeatable manual pass. Use a test owner/friends password from the host env;
never paste real credentials into this file, a ticket or a screenshot.

Preconditions: NAS/container healthy (`/healthz` → version, `data_writable:true`),
phone on the LAN/Tailscale, Safari; clear the old home-screen icon first if it
predates 1.6.19.

| # | Area | Steps | Expected |
|---|---|---|---|
| 1 | Login owner | Open `/`; you are redirected to `/login`. Enter `owner` + owner password, tap **Enter**. | Door page themed (ink/paper/acid/magenta/cyan, “THE DOOR”), no Safari auth popup unless the proxy requires it; lands on Latest with the 9-button rail. |
| 2 | Login friends | Log out, then sign in as `friends` + friends password. | Same full player; no admin/stats controls in Rave Player; session role is viewer. |
| 3 | Wrong pair | Log out; try `owner` with the friends password and `friends` with the owner password. | Generic “Invalid username or password.”; no hint which identity exists. |
| 4 | Lockout | With a throwaway identity/IP, fail 5 times; then try the correct pair. | 5th/6th attempt shows “Too many attempts. Try again in N seconds.” (HTTP 429); the other identity from the same phone is not locked. Leave it locked for the check only; it clears after 15 min. |
| 5 | Rail + back | Scroll the 9-button rail; open Tape Packs → Quick Filters; scroll the list; tap Back. | All 9 groups reachable, rail does not overflow the phone width, Back stays pinned and is at least 44×44 px; returns to the previous view. |
| 6 | Search | Paste a Mixcloud/SoundCloud set URL into Search and submit; then search a normal tape/DJ term. | Set opens the provider embed panel (✕/backdrop close); text search renders Archive rows; no Safari zoom-on-focus (16 px inputs). |
| 7 | Download foreground | Open an eligible Boot Pack row → Download; watch the panel. | Spinning head + bar/percent/bytes/ETA; completes with “Saved to Downloads”; row appears in Downloads and plays from `/api/downloads/<id>/stream`. |
| 8 | Download background | Start another eligible download, tap **Download in background**; navigate to Latest and then fully reload Safari; wait. | Panel closes; topbar spinner/percent chip appears and survives the reload; completion toast “Saved to Downloads” appears elsewhere; the row is in Downloads; active job rows had a Cancel action. |
| 9 | Cancel background | Start a background download and cancel from the Downloads job row. | State becomes cancelled, partial file does not appear in Downloads, no stuck chip/job. |
| 10 | Offline playback | Open a completed Downloads row; play/pause/seek; toggle AirPlay. | Plays the local copy with Range/seeking; progress and lock-screen controls work; AirPlay button reflects the real route. |
| 11 | PWA add to home screen | Safari → Share → Add to Home Screen from the **root**; launch from the icon. | Standalone (no Safari chrome), starts at `/`; apple meta/manifest present. Plain-HTTP LAN: service worker is not expected to register; standalone/start URL still work. Re-add after 1.6.19 if the old bookmark points elsewhere. |
| 12 | Logout | Tap **Log out** in the topbar. | Returns to the door; back button/deep link does not restore the player; `/api/menu` is 401. |
| 13 | Session expiry | With a non-remember session, leave it idle past `RAVEWEB_SESSION_HOURS`; reload. | Redirected to `/login`; old cookie cannot access pages/APIs. Remember-me uses `RAVEWEB_REMEMBER_DAYS`. |
| 14 | Admin stats (if exposed) | Control Centre Rave widget shows counts. | Owner-only endpoint; widget token is read-only; `sessions.items[]` shows **Session (masked)** only, never a raw id. |

Notes:
- 429/lockout is per identity + IP in the shared SQLite store; it should behave
  the same across both gunicorn workers.
- Background jobs are server-side; closing the tab must not stop them. They are
  not resumed after a server restart (interrupted jobs show failed).
- API errors stay JSON; browser navigations to unknown pages show the themed
  “LOST THE TAPE” page with a home link.
