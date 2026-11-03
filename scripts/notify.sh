#!/usr/bin/env bash
# notify.sh - send a one-line message to Telegram.
#
# Credentials come from the environment only. Nothing is read from or written
# to a file, and the bot token is never printed (not even in errors).
#
# Usage:
#   scripts/notify.sh "message"
#   printf '%s\n' "message" | scripts/notify.sh
#   scripts/notify.sh --dry-run "message"     # print the payload, send nothing
#
# Environment:
#   TELEGRAM_BOT_TOKEN  required to send (from @BotFather)
#   TELEGRAM_CHAT_ID    required to send (user, group or channel id)
#   TELEGRAM_API_BASE   optional, default https://api.telegram.org
#   NOTIFY_DRY_RUN=1    optional, same as --dry-run
#
# Exit codes: 0 sent, skipped or dry-run; 1 delivery failed; 2 usage error.
# Skipping (0) when credentials are absent is deliberate: CI and cron runs
# without secrets must stay green.
set -u

usage() {
    cat <<'EOF'
usage: notify.sh [--dry-run] MESSAGE
       printf '%s\n' MESSAGE | notify.sh [--dry-run]

Sends MESSAGE as a one-line Telegram message. Credentials are read from
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; they are never written to disk.
--dry-run prints the request body (token redacted) and sends nothing.
EOF
}

DRY_RUN="${NOTIFY_DRY_RUN:-0}"
MAX_CHARS=900

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run|-n) DRY_RUN=1; shift ;;
        --help|-h) usage; exit 0 ;;
        --) shift; break ;;
        -*) echo "notify: unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) break ;;
    esac
done

if [ "$#" -gt 0 ]; then
    text="$*"
else
    if [ -t 0 ]; then
        echo "notify: no message (pass text as arguments or pipe it on stdin)" >&2
        usage >&2
        exit 2
    fi
    text=$(cat)
fi

# Normalise to a single line: newlines/tabs become spaces, runs of spaces are
# squeezed, and control characters that would break the JSON body are dropped.
text=$(printf '%s' "$text" | tr '\n\r\t' '   ' | LC_ALL=C tr -d '\001-\010\013\014\016-\037' \
    | sed -e 's/  */ /g' -e 's/^ //' -e 's/ $//')

if [ -z "$text" ]; then
    echo "notify: message is empty" >&2
    exit 2
fi

if [ "${#text}" -gt "$MAX_CHARS" ]; then
    text="${text:0:${MAX_CHARS}}..."
fi

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

token="${TELEGRAM_BOT_TOKEN:-}"
chat_id="${TELEGRAM_CHAT_ID:-}"
api_base="${TELEGRAM_API_BASE:-https://api.telegram.org}"
api_base="${api_base%/}"

escaped_chat=$(json_escape "${chat_id:-<unset>}")
escaped_text=$(json_escape "$text")
payload="{\"chat_id\":\"${escaped_chat}\",\"text\":\"${escaped_text}\",\"disable_web_page_preview\":true}"

if [ "$DRY_RUN" = "1" ]; then
    echo "notify: dry-run, nothing sent"
    echo "POST ${api_base}/bot<redacted>/sendMessage"
    echo "Content-Type: application/json"
    echo
    echo "$payload"
    exit 0
fi

if [ -z "$token" ] || [ -z "$chat_id" ]; then
    # Do not treat "not configured" as a delivery failure: a workflow or cron
    # run on a repo without the secrets should skip cleanly.
    echo "notify: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set, skipping" >&2
    exit 0
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "notify: curl is not installed" >&2
    exit 1
fi

tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/notify.XXXXXX") || {
    echo "notify: could not create a temporary directory" >&2
    exit 1
}
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT INT TERM

# -sS keeps the request quiet; stderr is filtered before printing in case curl
# ever includes the URL (which carries the token) in a message.
if ! http_code=$(curl -sS -m 10 -o "$tmpdir/response.json" -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' \
        --data-binary "$payload" \
        "${api_base}/bot${token}/sendMessage" 2>"$tmpdir/error.txt"); then
    echo "notify: delivery failed (network error)" >&2
    sed "s|${token}|<redacted>|g" "$tmpdir/error.txt" >&2
    exit 1
fi

case "$http_code" in
    2??)
        echo "notify: sent"
        exit 0
        ;;
    *)
        echo "notify: Telegram rejected the message (HTTP ${http_code})" >&2
        # Telegram's error description never contains the token; still strip it
        # defensively and cap the body so logs stay short.
        sed "s|${token}|<redacted>|g" "$tmpdir/response.json" 2>/dev/null | head -c 300 >&2
        echo >&2
        exit 1
        ;;
esac
