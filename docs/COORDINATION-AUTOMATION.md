# Coordination automation - PR template, branch protection, Telegram alerts

This repo now carries the handover automation shared by the Rave Player (Kodi
and web) and Control Centre workspaces.

| File | What it does |
|---|---|
| `.github/pull_request_template.md` | What/why, files, contract impact, tests, screenshots, rollback, claim row |
| `docs/BRANCH-PROTECTION.md` | The exact `main` protection settings for this repo, including admin bypass |
| `scripts/notify.sh` | Sends one line to Telegram from `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`; `--dry-run` prints the payload |
| `scripts/coordination-check.sh` | Compares live versions with `docs/COORDINATION.md`; `--notify` sends an alert; non-zero on mismatch |
| `.github/workflows/coordination.yml` | Posts `repo \| branch \| event \| actor \| sha \| title \| url` on PRs and pushes to `main`; skips safely when secrets are absent |

Nothing here deploys anything. The scripts only read versions and send text.

## 1. Create the bot (once)

1. In Telegram, open **@BotFather** -> `/newbot` -> display name -> username
   ending in `bot`. Copy the token it prints **once**; it is not shown again.
2. Optional hardening: `/setjoingroups` -> Disable if the bot should only DM
   you; `/setprivacy` -> Enable if it is in a group and should see all messages.
3. If the token ever leaks: `/revoke` in BotFather, then update the GitHub
   secret (below). Revoking invalidates the old token immediately.

Keep the token in a password manager. Never put it in a file, commit, issue,
PR, screenshot or chat message.

## 2. Get the chat id

The chat id is where messages land (your DM, a group, or a channel). It is not
secret, but keep it in the same secrets store so config lives in one place.

1. **DM:** open the bot, press **Start**, send `hi`.
2. **Group:** add the bot to the group and send a message in the group.
3. Read the id locally:

```bash
curl -sS "https://api.telegram.org/bot<bot-token>/getUpdates" \
  | jq '.result[-1].message.chat.id'
```

- Group ids start with `-` (supergroups look like `-100...`); that leading
  minus is part of the id - keep it.
- That URL contains the token. Run it in your own terminal; do not paste the
  command output into an issue, PR or doc.
- If `getUpdates` is empty, send another message to the bot/group and retry.
  A bot with a webhook set cannot use `getUpdates`; this setup does not set one.

## 3. Add the GitHub secrets

Per repository: **Settings -> Secrets and variables -> Actions -> New
repository secret**, twice:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | the id from step 2 |

Or with the GitHub CLI (prompts for the value, so it stays out of shell
history):

```bash
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret list
```

For several repos, either set the same two secrets in each, or use
**organization secrets** (Settings -> Secrets and variables -> Actions -> New
organization secret) and grant access to the three repos. One bot can serve all
of them; messages are prefixed with `owner/repo` so you can tell them apart.

## 4. Test locally

Syntax first, then a dry run (no network, no credentials needed):

```bash
bash -n scripts/notify.sh scripts/coordination-check.sh
scripts/notify.sh --dry-run "dry run from the laptop"
```

Expected dry-run output:

```
notify: dry-run, nothing sent
POST https://api.telegram.org/bot<redacted>/sendMessage
Content-Type: application/json

{"chat_id":"<chat-id>","text":"dry run from the laptop","disable_web_page_preview":true}
```

The `chat_id` shows the configured value, or `<unset>` when credentials are
absent - the dry run never needs them.

Send a real message with the token exported **for this shell only**. Use
`read -rs` so the token does not land in shell history or the process list:

```bash
read -rs TELEGRAM_BOT_TOKEN; export TELEGRAM_BOT_TOKEN   # paste at the prompt, press Enter
export TELEGRAM_CHAT_ID='<chat-id>'
scripts/notify.sh "coordination automation test"
```

Then the board check. `CC_URL`/`WEB_URL` are the live services; omit one to
skip it:

```bash
CC_URL=http://<cc-host>:8099 WEB_URL=http://<web-host>:8097 \
  scripts/coordination-check.sh
```

Expected when everything matches (`exit 0`):

```
coordination-check
  board: docs/COORDINATION.md
  Control Centre   live 2.29.0   board 2.29.0   OK
  Rave web         live 1.6.37   board 1.6.37   OK
  Kodi             live -        board 3.9.29   (board only)
```

Expected on a stale board (`exit 1`):

```
  Control Centre   live 2.29.0   board 2.28.1   MISMATCH
  alert: Control Centre live 2.29.0 != board 2.28.1
```

Test the alert path without sending:

```bash
NOTIFY_DRY_RUN=1 CC_URL=... WEB_URL=... scripts/coordination-check.sh --notify
```

Exit codes: `0` match (or nothing to compare), `1` mismatch, `2` could not
verify (endpoint unreachable, or board missing while a URL is set).

## 5. Test in GitHub Actions

The workflow only appears in the Actions tab once it is on the default branch,
so after this PR is merged:

1. **Actions -> coordination -> Run workflow** (branch `main`), or open a
   draft PR from any branch.
2. With secrets set: one message arrives and the log ends with
   `Telegram alert sent (HTTP 200).`
3. Without secrets: the log says `Telegram secrets are not configured ...
   skipping alert.` and the job is green.
4. Pull requests from forks never receive secrets, so they skip by design.

The workflow posts on `opened`, `synchronize`, `reopened` and
`ready_for_review` only, so editing a description or adding a label does not
spam the chat.

## 6. Sample messages

```
<owner>/raveplayer-web | feature/1.6.38 | pull_request | <actor> | 94cc40fa1b2c | player: keep the AirPlay session on route change | https://github.com/<owner>/raveplayer-web/pull/12
<owner>/raveplayer-web | main | push | <actor> | 94cc40fa1b2c | 1.6.38: AirPlay route-change fix | https://github.com/<owner>/raveplayer-web/commit/94cc40f
<owner>/control-centre | docs/coordination-automation | pull_request | <actor> | b200e46f0a11 | docs: branch protection + Telegram alerts | https://github.com/<owner>/control-centre/pull/34
coordination: Control Centre live 2.29.0 != board 2.28.1; Rave web unreachable (board 1.6.37)
```

## 7. Privacy rules

- No bot tokens, session ids, private/LAN addresses, hostnames or internal
  service URLs in messages, docs, commits, screenshots or PR bodies.
- The only URLs allowed in an alert are the GitHub PR/commit links GitHub
  itself generates (the workflow's `url` field).
- The Telegram API URL embeds the bot token. The scripts never print it and
  redact it from errors and dry-run output.
- GitHub masks Action secrets in logs, but do not print them anyway.
- If a token leaks: `/revoke` in BotFather -> update `TELEGRAM_BOT_TOKEN` in
  every repo/org secret -> send one test message.
- Do not commit a `.env` with real values; if you keep one locally, confirm it
  is git-ignored before adding it.

## 8. Operations

| Task | How |
|---|---|
| Silence alerts for one repo | Delete its two secrets (workflow skips) or disable the workflow |
| Change destination chat | Update `TELEGRAM_CHAT_ID` only |
| Add the automation to another repo | Copy `scripts/notify.sh`, `scripts/coordination-check.sh`, `.github/workflows/coordination.yml` and this doc; add the two secrets |
| Board alert is wrong | Fix the board line (`- Control Centre: **vX.Y.Z** live`) or deploy; the check only knows what the board says |
| Rotate the bot | BotFather `/revoke`, update the secret everywhere, run one test |

The board parser expects the existing format in `docs/COORDINATION.md`:

```
- Control Centre: **v2.29.0** live, ...
- Rave web: **1.6.37** live, ...
- Kodi: **3.9.29** published ...
```

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 Unauthorized` from Telegram | wrong or revoked token | copy the token again or `/revoke` + update the secret |
| `400 Bad Request: chat not found` | wrong chat id, or the DM was never started | press Start in the DM, re-read `getUpdates`, keep the leading `-` for groups |
| `403 Forbidden: bot is not a member` | bot removed from the group, or not admin in a channel | re-add it / grant post rights |
| Actions job green, no message | secrets absent (fork PR or not configured) | check the job log; set both secrets |
| Local `notify.sh` prints "skipping" | variables set in another shell | `export` both in the current shell |
| Workflow missing from Actions | file is not on the default branch yet | merge the PR; then test with Run workflow |
| Alert says mismatch but versions look fine | board line format changed | restore `**vX.Y.Z**` on the `- Control Centre:`/`- Rave web:` lines |

## 10. Rollback

1. Actions -> coordination -> **Disable workflow** (instant, no code change).
2. Delete `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` from the repo secrets.
3. Revert this commit or delete the branch.

There is no runtime or data impact: the automation is read-only plus outbound
Telegram messages.
