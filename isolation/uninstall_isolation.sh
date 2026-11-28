#!/usr/bin/env bash
# Remove the optional isolation wiring. Tenant accounts and data are retained.
set -euo pipefail
destdir= yes=0
while (($#)); do
    case "$1" in
        --destdir) (($# >= 2)) || { echo 'missing --destdir path' >&2; exit 2; }; destdir=$2; shift 2;;
        --yes) yes=1; shift;;
        *) echo "unknown option: $1" >&2; exit 2;;
    esac
done
[[ -z $destdir || ( $destdir == /* && $destdir != / && $destdir != *'/../'* ) ]] || { echo 'unsafe --destdir' >&2; exit 2; }
if [[ -z $destdir ]]; then
    ((EUID == 0)) || { echo 'run as root or use --destdir' >&2; exit 1; }
    if (( ! yes )); then
        [[ -t 0 ]] || { echo 'use --yes for non-interactive uninstall' >&2; exit 1; }
        read -r -p 'Remove isolation helpers and units? Tenant data remains. [y/N] ' answer
        [[ $answer == y || $answer == Y ]] || exit 1
    fi
    systemctl disable --now ai-tenants-backup.timer ai-tenants-firewall.service 2>/dev/null || true
fi
files=(
    /etc/sudoers.d/92-tenant-provision /etc/sudoers.d/93-tenant-run
    /usr/local/sbin/tenant-provision /usr/local/sbin/tenant-run
    /usr/local/sbin/tenant-firewall /usr/local/sbin/tenant-backup
    /usr/local/sbin/m1-migrate-tenant
    /etc/systemd/system/ai-tenants-firewall.service
    /etc/systemd/system/ai-tenants-backup.service
    /etc/systemd/system/ai-tenants-backup.timer
    /etc/systemd/system/ai-tenants.slice.d/10-limits.conf
    /etc/systemd/system/ai-tenants-.slice.d/10-limits.conf
    /opt/telegram-ai/tools/account-limits /opt/telegram-ai/tools/list-sessions
    /opt/telegram-ai/mcp/send_telegram_file_mcp.py
    /opt/telegram-ai/mcp/delegate_to_codex_exchange_mcp.py
    /opt/telegram-ai/mcp/brokered_mcp_exchange_mcp.py
    /opt/telegram-ai/mcp/codex_send_telegram_file_mcp.py
    /opt/telegram-ai/mcp/delegate_to_claude_exchange_mcp.py
)
for path in "${files[@]}"; do
    if [[ -e $destdir$path ]]; then rm -f -- "$destdir$path"; printf 'REMOVED %s\n' "$path"; fi
done
for path in "$destdir"/etc/systemd/system/*.service.d/isolation-mount.conf; do
    [[ -f $path ]] || continue
    rm -f -- "$path"
    printf 'REMOVED %s\n' "${path#"$destdir"}"
done
if [[ -z $destdir ]]; then systemctl daemon-reload; fi
cat <<'EOF'
Tenant users, /srv/ai-tenants (or your custom root), /var/lib/telegram-ai/tenants.img,
/etc/telegram-ai/tenants.conf, fstab entries, quotas and local backups were kept.
Disable BRIDGE_ISOLATION and ISOLATED_TENANTS in each bridge environment before restarting it.
After verifying the old accounts and backups, remove retained data, users, mount and
fstab entries manually if desired. No tenant data was deleted by this script.
EOF
