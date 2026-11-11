#!/usr/bin/env bash
# Registers one userbot instance with the relay and prints its new token.
#
#   register_instance.sh <instance_id> <owner_telegram_id> [worker-env-file]
#
# The token goes to stdout (and nothing else does), so callers can capture it.
# With a worker-env-file, the token is also recorded there for that worker's
# own Telegram MCP client. Starts the relay if it is installed but not running
# yet. No sudo is needed for the credentials themselves, and a running relay
# picks the new instance up without a restart.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { echo "ERROR: $*" >&2; exit 1; }
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
running_env() {
    local pid
    pid="$(systemctl show -p MainPID --value "$1" 2>/dev/null || true)"
    [[ -n "$pid" && "$pid" != 0 && -r "/proc/$pid/environ" ]] || return 0
    tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$2=//p" | tail -n 1
}

[[ $# -ge 2 && $# -le 3 ]] || die "usage: register_instance.sh <instance_id> <owner_telegram_id> [worker-env-file]"
INSTANCE_ID="$1" OWNER_TG_ID="$2" WORKER_ENV="${3:-}"
[[ "$INSTANCE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || die "invalid instance id: $INSTANCE_ID"
[[ "$OWNER_TG_ID" =~ ^[0-9]+$ ]] || die "owner Telegram ID must be numeric"

PYTHON="$ROOT/.venv/bin/python"
[[ -x "$PYTHON" ]] || die "the relay is not installed yet; run $ROOT/install_relay.sh first"
UNIT="${JARVIS_RELAY_QUEUE_SERVICE:-jarvis-ask-cmd-queue.service}"

# Write where the running relay reads: its own environment wins.
if systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    for name in TELEGRAM_AI_CONFIG_DIR JARVIS_RELAY_CONFIG_DIR JARVIS_RELAY_TOKENS_FILE JARVIS_RELAY_OWNERS_FILE; do
        live_value="$(running_env "$UNIT" "$name")"
        [[ -n "$live_value" ]] && export "$name=$live_value"
    done
fi

"$PYTHON" "$ROOT/cmd_queue.py" --set-owner "$INSTANCE_ID" "$OWNER_TG_ID" >&2
if [[ -n "$WORKER_ENV" ]]; then
    TOKEN="$("$PYTHON" "$ROOT/cmd_queue.py" --add-token "$INSTANCE_ID" --worker-env "$WORKER_ENV")"
else
    TOKEN="$("$PYTHON" "$ROOT/cmd_queue.py" --add-token "$INSTANCE_ID")"
fi

if ! systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    if systemctl cat "$UNIT" >/dev/null 2>&1; then
        as_root systemctl start "$UNIT" >&2
    else
        echo "NOTE: $UNIT is not installed; run $ROOT/install_relay.sh" >&2
    fi
fi
printf '%s\n' "$TOKEN"
