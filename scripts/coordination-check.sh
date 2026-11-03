#!/usr/bin/env bash
# coordination-check.sh - compare live Rave versions with the coordination board.
#
# Reads the board ("Right now" bullets) and asks each live service for its
# version. Exits non-zero when they disagree, so cron/CI can catch a board that
# was not updated (or a deploy that did not happen). With --notify it also
# sends a one-line Telegram alert through scripts/notify.sh.
#
# Usage:
#   scripts/coordination-check.sh [--notify] [--board FILE]
#
# Environment:
#   CC_URL         base URL of the live Control Centre (e.g. http://host:8099)
#   WEB_URL        base URL of the live Rave web build
#   BOARD_FILE     board path, default docs/COORDINATION.md
#   NOTIFY_SCRIPT  default scripts/notify.sh
#   NOTIFY_DRY_RUN=1  print the alert instead of sending it
#
# Exit codes:
#   0  versions match, or no live endpoint was configured to compare
#   1  at least one live version differs from the board
#   2  could not verify (endpoint unreachable, or board missing while a URL is set)
set -u

usage() {
    cat <<'EOF'
usage: coordination-check.sh [--notify] [--board FILE]

Compares the live Control Centre and Rave web versions with the versions
recorded in docs/COORDINATION.md. Set CC_URL and/or WEB_URL to the live
services; without them only the board is printed.

  --notify   send a Telegram alert on mismatch/unverifiable (uses notify.sh)
  --board    board file to read (default docs/COORDINATION.md)

Exit: 0 match, 1 mismatch, 2 could not verify.
EOF
}

NOTIFY=0
BOARD_FILE_OVERRIDE=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --notify) NOTIFY=1; shift ;;
        --board)
            [ "$#" -ge 2 ] || { echo "coordination-check: --board needs a path" >&2; exit 2; }
            BOARD_FILE_OVERRIDE="$2"; shift 2 ;;
        --board=*) BOARD_FILE_OVERRIDE="${1#--board=}"; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "coordination-check: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BOARD_FILE="${BOARD_FILE_OVERRIDE:-${BOARD_FILE:-$ROOT/docs/COORDINATION.md}}"
CC_URL="${CC_URL:-}"
WEB_URL="${WEB_URL:-}"
TIMEOUT="${COORDINATION_TIMEOUT:-5}"
NOTIFY_SCRIPT="${NOTIFY_SCRIPT:-$ROOT/scripts/notify.sh}"

if [ ! -f "$BOARD_FILE" ]; then
    echo "coordination-check: board not found: $BOARD_FILE" >&2
    echo "  set BOARD_FILE=/path/to/COORDINATION.md (or pass --board FILE)" >&2
fi

# "  - Control Centre: **v2.28.1** live, ..." -> 2.28.1
board_version() {
    [ -f "$BOARD_FILE" ] || return 0
    sed -n "s/^- ${1}:[[:space:]]*\*\*v\{0,1\}\([^ *]*\)\*\*.*/\1/p" "$BOARD_FILE" | head -1
}

# First "version" value in a JSON body (the endpoints return one flat object).
fetch_version() {
    body=$(curl -fsS -m "$TIMEOUT" "$1" 2>/dev/null) || return 1
    printf '%s' "$body" \
        | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1
}

cc_board=$(board_version "Control Centre")
web_board=$(board_version "Rave web")
kodi_board=$(board_version "Kodi")

problems=0
alert=""

# check_service NAME LIVE STATE BOARD STATE_LABEL URL_VAR
check_service() {
    name="$1" live="$2" state="$3" board="$4" var="$5"
    board_display="${board:--}"

    if [ "$state" = "unset" ]; then
        printf '  %-16s live %-10s board %-10s (set %s to compare)\n' \
            "$name" "-" "$board_display" "$var"
        return
    fi

    if [ "$state" != "live" ]; then
        printf '  %-16s live %-10s board %-10s CANNOT VERIFY\n' \
            "$name" "-" "$board_display"
        problems=$((problems + 1))
        alert="${alert}${alert:+; }${name} unreachable (board ${board_display})"
        return
    fi

    if [ -z "$board" ]; then
        printf '  %-16s live %-10s board %-10s BOARD MISSING\n' \
            "$name" "$live" "-"
        problems=$((problems + 1))
        alert="${alert}${alert:+; }${name} live ${live}, board missing"
        return
    fi

    if [ "$live" = "$board" ]; then
        printf '  %-16s live %-10s board %-10s OK\n' "$name" "$live" "$board"
    else
        printf '  %-16s live %-10s board %-10s MISMATCH\n' "$name" "$live" "$board"
        problems=$((problems + 1))
        alert="${alert}${alert:+; }${name} live ${live} != board ${board}"
    fi
}

cc_live=""
cc_state="unset"
if [ -n "$CC_URL" ]; then
    cc_state="unreachable"
    if cc_live=$(fetch_version "${CC_URL%/}/api/version") && [ -n "$cc_live" ]; then
        cc_state="live"
    fi
fi

web_live=""
web_state="unset"
if [ -n "$WEB_URL" ]; then
    web_state="unreachable"
    if web_live=$(fetch_version "${WEB_URL%/}/healthz") && [ -n "$web_live" ]; then
        web_state="live"
    fi
fi

echo "coordination-check"
echo "  board: ${BOARD_FILE}"
check_service "Control Centre" "$cc_live" "$cc_state" "$cc_board" "CC_URL"
check_service "Rave web" "$web_live" "$web_state" "$web_board" "WEB_URL"
printf '  %-16s live %-10s board %-10s (board only)\n' "Kodi" "-" "${kodi_board:--}"

if [ "$problems" -gt 0 ]; then
    if [ -n "$alert" ]; then
        echo "  alert: ${alert}"
    fi
    if [ "$NOTIFY" -eq 1 ]; then
        if [ -f "$NOTIFY_SCRIPT" ]; then
            if ! bash "$NOTIFY_SCRIPT" "coordination: ${alert}"; then
                echo "coordination-check: notify failed (see above)" >&2
            fi
        else
            echo "coordination-check: notify script not found: $NOTIFY_SCRIPT" >&2
        fi
    fi
    if [ "$cc_state" = "unreachable" ] || [ "$web_state" = "unreachable" ] \
        || { [ -z "$cc_board" ] && [ "$cc_state" = "live" ]; } \
        || { [ -z "$web_board" ] && [ "$web_state" = "live" ]; }; then
        exit 2
    fi
    exit 1
fi

if [ "$cc_state" = "unset" ] && [ "$web_state" = "unset" ]; then
    echo "  no live endpoints configured, nothing compared"
fi
exit 0
