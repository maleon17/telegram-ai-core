#!/usr/bin/env bash
# Installs the shared queue relay (cmd_queue.py) as a systemd service and
# records it in the component registry.
#
# The ClaudeAsk and CodexAsk setup scripts call this for you; it is also safe
# to run by hand. Non-interactive and idempotent: a relay that is already
# running is left running and keeps its own configuration.
#
# Environment:
#   JARVIS_RELAY_QUEUE_SERVICE  unit name   (default jarvis-ask-cmd-queue.service)
#   TELEGRAM_AI_CONFIG_DIR      credentials and registry (default ~/.config/telegram-ai)
#   JARVIS_STATE_DIR            queues and uploads       (default ~/.local/state/telegram-ai)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

die() { echo "ERROR: $*" >&2; exit 1; }
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
running_env() {  # value of one variable in a running service's environment
    local pid
    pid="$(systemctl show -p MainPID --value "$1" 2>/dev/null || true)"
    [[ -n "$pid" && "$pid" != 0 && -r "/proc/$pid/environ" ]] || return 0
    tr '\0' '\n' < "/proc/$pid/environ" | sed -n "s/^$2=//p" | tail -n 1
}

command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v systemctl >/dev/null 2>&1 || die "systemd (systemctl) not found"
[[ $EUID -eq 0 ]] || command -v sudo >/dev/null 2>&1 || die "sudo not found (needed to install the service unit)"
python3 -c 'import ensurepip, venv' >/dev/null 2>&1 \
    || die "python3 venv support is missing (Debian/Ubuntu: sudo apt install python3-venv)"

UNIT="${JARVIS_RELAY_QUEUE_SERVICE:-jarvis-ask-cmd-queue.service}"
[[ "$UNIT" =~ ^[A-Za-z0-9_.@-]+\.service$ ]] || die "invalid unit name: $UNIT"

VENV="$ROOT/.venv"
PYTHON="$VENV/bin/python"
if ! "$PYTHON" -c 'import cryptography, mcp' >/dev/null 2>&1; then
    echo "Installing relay dependencies into $VENV"
    python3 -m venv "$VENV"
    "$PYTHON" -m pip install --quiet --upgrade pip
    "$PYTHON" -m pip install --quiet -r requirements.txt
fi

ACTIVE=0
systemctl is-active --quiet "$UNIT" 2>/dev/null && ACTIVE=1
if (( ACTIVE )); then
    # A running relay is the source of truth for where its files live.
    for name in TELEGRAM_AI_CONFIG_DIR JARVIS_RELAY_CONFIG_DIR JARVIS_STATE_DIR; do
        live_value="$(running_env "$UNIT" "$name")"
        [[ -n "$live_value" ]] && export "$name=$live_value"
    done
fi
CONFIG_DIR="${TELEGRAM_AI_CONFIG_DIR:-${JARVIS_RELAY_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/telegram-ai}}"
STATE_DIR="${JARVIS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/telegram-ai}"
mkdir -p "$CONFIG_DIR" "$STATE_DIR"
chmod 700 "$CONFIG_DIR" "$STATE_DIR"
export TELEGRAM_AI_CONFIG_DIR="$CONFIG_DIR" JARVIS_STATE_DIR="$STATE_DIR"

"$PYTHON" registry.py register core "path=$ROOT" "python=$PYTHON"
"$PYTHON" registry.py register relay "path=$ROOT" "unit=$UNIT" \
    "url=http://127.0.0.1:${JARVIS_QUEUE_PORT:-9092}" "config_dir=$CONFIG_DIR" "state_dir=$STATE_DIR"

if (( ACTIVE )); then
    RUNNING_FROM="$(systemctl show -p ExecStart --value "$UNIT" 2>/dev/null | grep -o '[^ ]*cmd_queue\.py' | head -n 1 || true)"
    if [[ -n "$RUNNING_FROM" && "$RUNNING_FROM" != "$ROOT/cmd_queue.py" ]]; then
        echo "NOTE: $UNIT is running $RUNNING_FROM, not this checkout."
        echo "      To move it here, re-run with JARVIS_RELAY_REINSTALL=1 once no request is in flight."
    fi
    if [[ "${JARVIS_RELAY_REINSTALL:-}" != 1 ]]; then
        echo "$UNIT is already running; left as is."
        exit 0
    fi
fi

RENDERED="$(mktemp)"
trap 'rm -f "$RENDERED"' EXIT
sed \
    -e "s|__USER__|$(id -un)|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__INSTALL_DIR__|$ROOT|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__CONFIG_DIR__|$CONFIG_DIR|g" \
    -e "s|__STATE_DIR__|$STATE_DIR|g" \
    relay.service.example > "$RENDERED"
echo "Installing /etc/systemd/system/$UNIT (sudo required)"
as_root install -m 0644 "$RENDERED" "/etc/systemd/system/$UNIT"
as_root systemctl daemon-reload
as_root systemctl enable "$UNIT" >/dev/null 2>&1 || as_root systemctl enable "$UNIT"
# The relay refuses to start without at least one registered instance, so it
# is started here only if one exists; register_instance.sh starts it otherwise.
if "$PYTHON" cmd_queue.py --check-config >/dev/null 2>&1; then
    as_root systemctl restart "$UNIT"
    echo "$UNIT started."
else
    echo "$UNIT installed; it starts when the first instance is registered."
fi
