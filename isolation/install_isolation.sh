#!/usr/bin/env bash
# Optional Unix UID isolation for Telegram AI bridges. See README.md.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
bridge_user= bridge_group= tenants_root=/srv/ai-tenants claude_bridge= codex_bot=
image_size=20G disk_quota_gb=4 inodes=1000000 memory_max=3G tenants_memory_max=7G
hidepid=0 install_clis=0 no_packages=0 dry_run=0 check=0 yes=0 destdir= admin_ids=

while (($#)); do
    case "$1" in
        --bridge-user|--bridge-group|--tenants-root|--claude-bridge|--codex-bot|--image-size|--disk-quota-gb|--inodes|--memory-max|--tenants-memory-max|--admin-ids|--destdir)
            (($# >= 2)) || die "$1 requires an argument"
            case "$1" in
                --bridge-user) bridge_user=$2;; --bridge-group) bridge_group=$2;;
                --tenants-root) tenants_root=$2;; --claude-bridge) claude_bridge=$2;;
                --codex-bot) codex_bot=$2;; --image-size) image_size=$2;;
                --disk-quota-gb) disk_quota_gb=$2;; --inodes) inodes=$2;;
                --memory-max) memory_max=$2;; --tenants-memory-max) tenants_memory_max=$2;;
                --admin-ids) admin_ids=$2;;
                --destdir) destdir=$2;;
            esac
            shift 2;;
        --hidepid) hidepid=1; shift;; --install-clis) install_clis=1; shift;;
        --no-packages) no_packages=1; shift;; --dry-run) dry_run=1; shift;;
        --check) check=1; shift;; --yes) yes=1; shift;;
        -h|--help)
            sed -n '/^## Options/,/^## /p' "$SOURCE_DIR/README.md" | head -n -1
            exit 0;;
        *) die "unknown option: $1";;
    esac
done

[[ -n $bridge_user ]] || die '--bridge-user is required'
[[ $bridge_user =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid bridge user'
[[ -z $bridge_group || $bridge_group =~ ^[a-z_][a-z0-9_-]*$ ]] || die 'invalid bridge group'
[[ $image_size == 20G || $image_size == none ]] || die '--image-size must be 20G or none'
[[ $disk_quota_gb =~ ^[0-9]+$ && $disk_quota_gb -gt 0 ]] || die 'invalid disk quota'
[[ $inodes =~ ^[0-9]+$ && $inodes -gt 0 ]] || die 'invalid inode limit'
[[ $memory_max =~ ^[0-9]+[GM]$ && $tenants_memory_max =~ ^[0-9]+[GM]$ ]] || die 'memory limits require a number and G or M'
[[ -z $admin_ids || $admin_ids =~ ^[0-9]{5,15}(,[0-9]{5,15})*$ ]] || die '--admin-ids must be comma separated ids of 5 to 15 digits'
for path in "$tenants_root" "$claude_bridge" "$codex_bot" "$destdir"; do
    [[ -z $path || ( $path =~ ^/[A-Za-z0-9_./+-]+$ && $path != */../* && $path != */./* && $path != *'/..' && $path != *'/.' && $path != *'//'*) ]] || die "unsafe path: $path"
done
[[ $tenants_root != / && $tenants_root != /proc && $tenants_root != /etc ]] || die 'unsafe tenants root'
[[ $destdir != / ]] || die 'unsafe staging root'
if [[ -z $bridge_group ]]; then
    bridge_group="$(id -gn "$bridge_user" 2>/dev/null)" || die "user $bridge_user does not exist; specify --bridge-group for staging"
fi
[[ $bridge_group != aitenants ]] || die 'bridge group must differ from tenant group'
case "$bridge_group" in root|sudo|adm|docker|lxd|shadow|disk|wheel|admin|kvm|libvirt) die 'bridge group must be nonprivileged';; esac
if [[ $image_size == none ]]; then require_mount=0; quota_kb=0; else require_mount=1; quota_kb=$((disk_quota_gb * 1024 * 1024)); fi
[[ $quota_kb -lt 2147483647 ]] || die 'disk quota is too large'

to_megabytes() { local n=${1%[GM]}; if [[ $1 == *G ]]; then echo $((n * 1024)); else echo "$n"; fi; }
tenant_memory_high="$(( $(to_megabytes "$memory_max") * 5 / 6 ))M"
tenants_memory_high="$(( $(to_megabytes "$tenants_memory_max") * 6 / 7 ))M"
if [[ $image_size == none ]]; then mount_dependency=; else mount_dependency="RequiresMountsFor=$tenants_root"; fi

target() { printf '%s%s' "$destdir" "$1"; }
render() {
    local src=$1
    sed -e "s|@BRIDGE_USER@|$bridge_user|g" -e "s|@BRIDGE_GROUP@|$bridge_group|g" \
        -e "s|@TENANTS_ROOT@|$tenants_root|g" -e "s|@REQUIRE_MOUNT@|$require_mount|g" \
        -e "s|@DISK_QUOTA_KB@|$quota_kb|g" -e "s|@DISK_INODES@|$inodes|g" \
        -e "s|@MEMORY_MAX@|$memory_max|g" -e "s|@TENANTS_MEMORY_MAX@|$tenants_memory_max|g" \
        -e "s|@TENANT_MEMORY_HIGH@|$tenant_memory_high|g" \
        -e "s|@TENANTS_MEMORY_HIGH@|$tenants_memory_high|g" \
        -e "s|@MOUNT_DEPENDENCY@|$mount_dependency|g" -e "s|@ADMIN_IDS@|$admin_ids|g" "$src"
}
write_file() {
    local src=$1 path=$2 mode=$3 dst tmp
    dst="$(target "$path")"
    [[ ! -L $dst ]] || die "unsafe symlink: $dst"
    if [[ -f $dst ]] && cmp -s "$dst" <(render "$src") && [[ $(stat -c %a "$dst") == "${mode#0}" ]] \
       && [[ -n $destdir || $(stat -c %u:%g "$dst") == 0:0 ]]; then
        note "OK $path"
        return
    fi
    if ((check)); then note "DIFF $path"; differences=1; return; fi
    if ((dry_run)); then note "PLAN $path"; return; fi
    mkdir -p "$(dirname "$dst")"
    tmp="$(mktemp "$(dirname "$dst")/.isolation.XXXXXXXX")"
    trap 'rm -f "$tmp"' RETURN
    render "$src" > "$tmp"
    if [[ $path == /etc/sudoers.d/* ]] && command -v visudo >/dev/null; then
        visudo -cf "$tmp" >/dev/null || die "visudo rejected $path"
    elif [[ $path == /etc/sudoers.d/* && -z $destdir ]]; then
        die 'visudo is required before installing sudoers'
    fi
    install -m "$mode" "$tmp" "$dst"
    rm -f "$tmp"
    trap - RETURN
    note "WROTE $path"
}
config_file() {
    local src="$SOURCE_DIR/tenants.conf.example" dst tmp key line current_mode desired actual
    dst="$(target /etc/telegram-ai/tenants.conf)"
    if [[ -e $dst ]]; then
        [[ -f $dst && ! -L $dst ]] || die "unsafe tenants.conf: $dst"
        current_mode="$(stat -c %a "$dst")"
        [[ $(( 8#$current_mode & 8#022 )) -eq 0 ]] || die "unsafe tenants.conf permissions: $dst"
        if [[ -z $destdir ]]; then [[ $(stat -c %u:%g "$dst") == 0:0 ]] || die "unsafe tenants.conf owner: $dst"; fi
        if (( ! check && ! dry_run )); then
            tmp="$(mktemp "$(dirname "$dst")/.tenants.conf.XXXXXXXX")"
            cp "$dst" "$tmp"
        fi
        while IFS= read -r line; do
            [[ $line == *=* && $line != \#* ]] || continue
            key=${line%%=*}
            if ! grep -Eq "^[[:space:]]*$key[[:space:]]*=" "$dst"; then
                if ((check)); then note "DIFF /etc/telegram-ai/tenants.conf (missing $key)"; differences=1
                elif ((dry_run)); then note "PLAN append $key to tenants.conf"
                else render_line="$(printf '%s\n' "$line" | sed -e "s|@BRIDGE_USER@|$bridge_user|g" -e "s|@BRIDGE_GROUP@|$bridge_group|g" -e "s|@TENANTS_ROOT@|$tenants_root|g" -e "s|@REQUIRE_MOUNT@|$require_mount|g" -e "s|@DISK_QUOTA_KB@|$quota_kb|g" -e "s|@DISK_INODES@|$inodes|g" -e "s|@MEMORY_MAX@|$memory_max|g" -e "s|@ADMIN_IDS@|$admin_ids|g")"; printf '%s\n' "$render_line" >> "$tmp"; fi
            else
                desired="$(printf '%s\n' "$line" | sed -e "s|@BRIDGE_USER@|$bridge_user|g" -e "s|@BRIDGE_GROUP@|$bridge_group|g" -e "s|@TENANTS_ROOT@|$tenants_root|g" -e "s|@REQUIRE_MOUNT@|$require_mount|g" -e "s|@DISK_QUOTA_KB@|$quota_kb|g" -e "s|@DISK_INODES@|$inodes|g" -e "s|@MEMORY_MAX@|$memory_max|g" -e "s|@ADMIN_IDS@|$admin_ids|g")"
                actual="$(sed -n -E "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*//p" "$dst" | tail -n 1)"
                if [[ $actual != "${desired#*=}" ]]; then
                    case $key in
                        TENANTS_ROOT|TENANT_GROUP|BRIDGE_USER|BRIDGE_GROUP|REQUIRE_MOUNT|DISK_QUOTA_KB|DISK_INODES|MEMORY_MAX)
                            if ((check)); then note "DIFF tenants.conf: $key"; differences=1
                            elif ((dry_run)); then note "PLAN conflict in tenants.conf: $key"
                            else die "existing tenants.conf differs for $key; edit it manually or use matching options"; fi;;
                    esac
                fi
            fi
        done < "$src"
        if (( ! check && ! dry_run )); then
            if ! cmp -s "$tmp" "$dst"; then install -m 0600 "$tmp" "$dst"; note 'UPDATED tenants.conf (missing keys only)'; else note 'OK tenants.conf'; fi
            rm -f "$tmp"
        fi
    else
        write_file "$src" /etc/telegram-ai/tenants.conf 0600
    fi
}
ensure_fstab_entry() {
    local entry=$1 marker=$2 file
    file="$(target /etc/fstab)"
    if [[ -f $file ]] && grep -Fq "$marker" "$file"; then
        if grep -Fxq "$entry" "$file"; then note "OK fstab: $marker"; return; fi
        if ((check || dry_run)); then note "DIFF fstab: $marker"; differences=1; return; fi
        die "existing fstab entry differs for $marker; edit it manually"
    fi
    if ((check)); then note "DIFF fstab: $marker"; differences=1; return; fi
    if ((dry_run)); then note "PLAN fstab: $entry"; return; fi
    mkdir -p "$(dirname "$file")"
    printf '%s\n' "$entry" >> "$file"
    chmod 0644 "$file"
    note "ADDED fstab: $marker"
}
differences=0

# Staging and read-only modes never call package managers or system services.
if [[ -z $destdir && $check -eq 0 && $dry_run -eq 0 ]]; then
    ((EUID == 0)) || die 'run as root, or use --destdir/--dry-run/--check'
    [[ -d /run/systemd/system ]] || die 'systemd is required'
    python3 -c 'import sys; assert sys.version_info >= (3, 10)' || die 'Python 3.10 or newer is required'
    command -v visudo >/dev/null || die 'visudo is required'
    command -v systemctl >/dev/null || die 'systemctl is required'
    id "$bridge_user" >/dev/null || die "bridge user $bridge_user does not exist"
    if ((install_clis)); then
        command -v claude >/dev/null || die 'claude CLI is missing'
        command -v npm >/dev/null || die 'npm is missing'
    fi
    if [[ $image_size != none && -d $tenants_root ]] && ! mountpoint -q "$tenants_root"; then
        [[ -z $(find "$tenants_root" -mindepth 1 -maxdepth 1 -print -quit) ]] || die "tenants root is nonempty; migrate data before mounting an image"
    fi
    if (( ! yes )); then
        [[ -t 0 ]] || die 'use --yes for non-interactive installation'
        read -r -p 'Install optional tenant isolation system files? [y/N] ' answer
        [[ $answer == y || $answer == Y ]] || die 'cancelled'
    fi
    packages=(acl nftables)
    if [[ $image_size != none ]]; then packages+=(quota e2fsprogs); fi
    missing=()
    for package in "${packages[@]}"; do dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed' || missing+=("$package"); done
    if ((${#missing[@]})); then
        ((no_packages == 0)) || die "required packages are missing: ${missing[*]}"
        apt-get install -y "${missing[@]}"
    fi
    getent group aitenants >/dev/null || groupadd --system aitenants
    # administrator tenants live in their own group, which the network rules do not cover
    if [[ -n $admin_ids ]]; then getent group aitenants-admin >/dev/null || groupadd --system aitenants-admin; fi
fi

if (( ! check && ! dry_run )); then
    for directory in "$(target /etc/telegram-ai)" "$(target /var/lib/telegram-ai/tenants)" "$(target "$tenants_root")" "$(target /var/backups/ai-tenants/daily)"; do
        [[ ! -L $directory ]] || die "unsafe symlink: $directory"
    done
    mkdir -p "$(target /etc/telegram-ai)" "$(target /var/lib/telegram-ai/tenants)" "$(target "$tenants_root")" "$(target /var/backups/ai-tenants/daily)"
    chmod 0700 "$(target /etc/telegram-ai)" "$(target /var/lib/telegram-ai/tenants)" "$(target /var/backups/ai-tenants/daily)"
    chmod 0711 "$(target "$tenants_root")"
fi
config_file
if [[ -z $destdir && $check -eq 0 && $dry_run -eq 0 && $image_size != none ]]; then
    image=/var/lib/telegram-ai/tenants.img
    if [[ ! -e $image ]]; then
        truncate -s "$image_size" "$image"
        chmod 0600 "$image"
        mkfs.ext4 -F -L aitenants -O quota,project -E quotatype=prjquota "$image" >/dev/null
    fi
fi

if [[ $image_size != none ]]; then
    ensure_fstab_entry "/var/lib/telegram-ai/tenants.img $tenants_root ext4 loop,prjquota,nodev,nosuid 0 0" " $tenants_root ext4 "
fi
if ((hidepid)) && [[ -n $destdir || $check -eq 1 || $dry_run -eq 1 ]]; then
    if [[ -n $destdir ]]; then proc_gid=99998; else proc_gid="$(getent group procview 2>/dev/null | cut -d: -f3)"; fi
    proc_gid=${proc_gid:-99998}
    ensure_fstab_entry "proc /proc proc defaults,hidepid=2,gid=$proc_gid 0 0" ' /proc proc '
fi

if [[ -z $destdir && $check -eq 0 && $dry_run -eq 0 && $image_size != none ]]; then
    mountpoint -q "$tenants_root" || mount "$tenants_root"
    chmod 0711 "$tenants_root"
fi

for helper in tenant-provision tenant-run tenant-firewall tenant-backup tenant-role m1-migrate-tenant; do
    write_file "$SOURCE_DIR/bin/$helper" "/usr/local/sbin/$helper" 0755
done
write_file "$SOURCE_DIR/sudoers/92-tenant-provision.in" /etc/sudoers.d/92-tenant-provision 0440
write_file "$SOURCE_DIR/sudoers/93-tenant-run.in" /etc/sudoers.d/93-tenant-run 0440
write_file "$SOURCE_DIR/sudoers/95-tenant-role.in" /etc/sudoers.d/95-tenant-role 0440
for unit in ai-tenants-firewall.service ai-tenants-backup.service ai-tenants-backup.timer; do
    write_file "$SOURCE_DIR/systemd/$unit" "/etc/systemd/system/$unit" 0644
done
write_file "$SOURCE_DIR/systemd/ai-tenants.slice.d-10-limits.conf" /etc/systemd/system/ai-tenants.slice.d/10-limits.conf 0644
write_file "$SOURCE_DIR/systemd/ai-tenants-.slice.d-10-limits.conf" /etc/systemd/system/ai-tenants-.slice.d/10-limits.conf 0644
write_file "$SOURCE_DIR/systemd/ai-admins.slice.d-10-limits.conf" /etc/systemd/system/ai-admins.slice.d/10-limits.conf 0644

if [[ $image_size != none ]]; then
    for kind in claude codex; do
        if [[ $kind == claude ]]; then bridge=$claude_bridge; fallback=claude-telegram-bridge.service; else bridge=$codex_bot; fallback=codex-telegram-bot.service; fi
        [[ -n $bridge ]] || continue
        units=()
        if [[ -n $destdir ]]; then units=("$fallback")
        else
            for file in /etc/systemd/system/*.service; do
                [[ -f $file ]] && grep -Fq "$bridge" "$file" && units+=("$(basename "$file")")
            done
        fi
        for unit in "${units[@]}"; do
            dropin="/etc/systemd/system/$unit.d/isolation-mount.conf"
            content="[Unit]"$'\n'"RequiresMountsFor=$tenants_root"$'\n'
            if ((check)); then
                dst="$(target "$dropin")"
                [[ -f $dst && ! -L $dst && $(cat "$dst") == "${content%$'\n'}" && $(stat -c %a "$dst") == 644 ]] || { note "DIFF $dropin"; differences=1; }
            elif ((dry_run)); then note "PLAN $dropin"
            else
                dst="$(target "$dropin")"; mkdir -p "$(dirname "$dst")"
                [[ ! -L $dst ]] || die "unsafe symlink: $dst"
                if [[ ! -f $dst || $(cat "$dst") != "${content%$'\n'}" ]]; then printf '%s' "$content" > "$dst"; chmod 0644 "$dst"; fi
            fi
        done
    done
fi

if [[ -n $claude_bridge ]]; then
    for tool in account-limits list-sessions session-title; do
        src="$claude_bridge/isolation/tools/$tool"
        [[ -f $src ]] || die "missing bridge tool: $src"
        write_file "$src" "/opt/telegram-ai/tools/$tool" 0755
    done
    for script in send_telegram_file_mcp.py delegate_to_codex_exchange_mcp.py brokered_mcp_exchange_mcp.py; do
        src="$claude_bridge/$script"; [[ -f $src ]] || continue
        write_file "$src" "/opt/telegram-ai/mcp/$script" 0644
    done
fi
if [[ -n $codex_bot ]]; then
    for script in codex_send_telegram_file_mcp.py codex_rename_session_mcp.py delegate_to_claude_exchange_mcp.py; do
        src="$codex_bot/$script"; [[ -f $src ]] || die "missing bridge MCP: $src"
        write_file "$src" "/opt/telegram-ai/mcp/$script" 0644
    done
fi
if [[ -z $destdir && $check -eq 0 && $dry_run -eq 0 ]]; then
    chown root:root /etc/telegram-ai /etc/telegram-ai/tenants.conf /var/lib/telegram-ai/tenants "$tenants_root"
    for path in /opt/telegram-ai /opt/telegram-ai/mcp /opt/telegram-ai/tools; do [[ -d $path ]] && chown root:root "$path" && chmod 0755 "$path"; done
    if ((install_clis)); then
        mkdir -p /opt/claude/bin /opt/codex
        claude_source="$(readlink -f "$(command -v claude)")"
        if [[ $claude_source != /opt/claude/bin/claude ]]; then install -m 0755 "$claude_source" /opt/claude/bin/claude; fi
        npm install --global --prefix /opt/codex @openai/codex
    fi
    if ((hidepid)); then
        getent group procview >/dev/null || groupadd --system procview
        usermod -aG procview "$bridge_user"
        gid="$(getent group procview | cut -d: -f3)"
        ensure_fstab_entry "proc /proc proc defaults,hidepid=2,gid=$gid 0 0" ' /proc proc '
        mount -o remount,hidepid=2,gid="$gid" /proc
    fi
    systemctl daemon-reload
    systemctl enable --now ai-tenants-firewall.service ai-tenants-backup.timer
fi
if ((check)) && [[ -z $destdir ]]; then
    if getent group aitenants >/dev/null; then note 'OK group aitenants'; else note 'DIFF group aitenants missing'; differences=1; fi
    if [[ $image_size != none ]]; then
        if [[ -f /var/lib/telegram-ai/tenants.img ]] && mountpoint -q "$tenants_root"; then note 'OK tenant image mounted'
        else note 'DIFF tenant image or mount missing'; differences=1; fi
    fi
    for unit in ai-tenants-firewall.service ai-tenants-backup.timer; do
        if systemctl is-enabled --quiet "$unit"; then note "OK enabled $unit"
        else note "DIFF disabled $unit"; differences=1; fi
    done
    if ((install_clis)); then
        for path in /opt/claude/bin/claude /opt/codex/bin/codex; do
            if [[ -x $path ]]; then note "OK CLI $path"; else note "DIFF CLI $path missing"; differences=1; fi
        done
    fi
fi
if ((check)); then
    if ((differences)); then note 'Installed files differ.'; exit 1; fi
    note 'Installed files match.'
else
    note 'Isolation remains off until BRIDGE_ISOLATION=on and ISOLATED_TENANTS=<id,...> are set in each bridge environment and the bridges are restarted.'
    note 'For existing users, run m1-migrate-tenant plan/backup/copy/verify with --bridge-user and both bridge paths. To revert, remove those flags, restart bridges, then run uninstall_isolation.sh.'
fi
