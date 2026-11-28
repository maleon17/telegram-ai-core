# Optional Unix tenant isolation

This module installs root-owned helpers for the Claude and Codex Telegram bridges. It is **off by default**. Installing it does not move accounts or enable isolation for any chat. Only `BRIDGE_ISOLATION=on` together with `ISOLATED_TENANTS=<comma-separated Telegram IDs>` in each bridge's service environment selects tenants.

## Threat model

1. The security boundary between ordinary tenants is the Linux UID and private `0700` home.
2. A tenant cannot read another tenant's home or the bridge's private control files.
3. The bridge user is trusted to administer tenants; a compromised bridge can call the root helpers.
4. Owner and admin agents with broad sudo are fully trusted and can read every tenant's data.
5. `tenant-run` accepts only validated IDs, a bounded environment, and installed commands or tools.
6. The firewall denies tenant traffic to loopback, LAN, tailnet and link-local ranges by group ID.
7. The firewall is an egress control; it does not isolate processes sharing a UID.
8. Optional `hidepid=2` hides other users' process listings; it does not replace UID isolation.
9. The loop image and project quotas bound disk usage; `--image-size none` has no disk quota.
10. Local backups protect against accidental deletion, not disk failure or a malicious root user.

## Requirements and quick start

Supported target: Debian or Ubuntu with systemd, Python 3.10+, sudo/visudo, ACL, nftables, and, for the default disk image, `quota` and `e2fsprogs`. Run as root for a live install. The bridge user must already exist and have a dedicated, nonprivileged primary or selected bridge group. The installer can install missing packages unless `--no-packages` is set. Network access is needed only for package or optional Codex CLI installation.

```bash
# Review the staged tree without root or system changes.
./isolation/install_isolation.sh --bridge-user bridge --bridge-group bridge \
  --claude-bridge /path/to/claude-bridge --codex-bot /path/to/codex-bot \
  --destdir /tmp/isolation-stage

# On the target server, after reviewing the options and existing mounts:
sudo ./isolation/install_isolation.sh --bridge-user bridge \
  --claude-bridge /path/to/claude-bridge --codex-bot /path/to/codex-bot --yes
sudo ./isolation/install_isolation.sh --bridge-user bridge \
  --claude-bridge /path/to/claude-bridge --codex-bot /path/to/codex-bot --check
```

Use the actual service account and repository paths. The installer does not edit either bridge repository or its environment. Put `BRIDGE_ISOLATION=on` and `ISOLATED_TENANTS=<id,...>` in each bridge's environment, and restart the bridges using their documented safe restart procedure. A selected tenant fails closed if provisioning or launch fails. Unlisted chats keep their original account paths and runtime. For bridge units that use sudo, `NoNewPrivileges=true` must not be set. Check that the selected unit can invoke the helpers with `sudo -n` before enabling a tenant.

## Options

| Option | Default | Effect |
|---|---|---|
| `--bridge-user USER` | required | Unix account running the bridges; used in sudoers and tenant exchange ACLs |
| `--bridge-group GROUP` | user's primary group | Nonprivileged group for `.exchange/` |
| `--tenants-root PATH` | `/srv/ai-tenants` | Absolute tenant home root |
| `--claude-bridge DIR` | none | Copy Claude MCP scripts and tools from this checkout |
| `--codex-bot DIR` | none | Copy Codex MCP scripts from this checkout |
| `--image-size 20G\|none` | `20G` | Sparse ext4 loop image at `/var/lib/telegram-ai/tenants.img`, or plain directory |
| `--disk-quota-gb N` | `4` | Per-tenant hard project block quota with the image; soft quota is 7/8 of this |
| `--inodes N` | `1000000` | Per-tenant hard inode quota; soft quota is 4/5 of this |
| `--memory-max SIZE` | `3G` | Per-tenant slice and process memory ceiling |
| `--tenants-memory-max SIZE` | `7G` | Combined tenant slice memory ceiling |
| `--admin-ids IDS` | none | Comma separated Telegram ids that get an administrator tenant (passwordless sudo, outside the firewall group; see below) |
| `--hidepid` | off | Add a `procview` group and mount `/proc` with `hidepid=2` |
| `--install-clis` | off | Copy the installed `claude` CLI to `/opt/claude/bin` and install `@openai/codex` via npm under `/opt/codex` |
| `--no-packages` | off | Require dependencies to be installed already |
| `--dry-run` | off | Show planned file changes without changing the system |
| `--destdir DIR` | none | Render into DIR only, without root, mounts, packages, users or services |
| `--check` | off | Report installed file differences and exit nonzero on differences |
| `--yes` | off | Skip the live install confirmation prompt |

The CLIs must exist at `/opt/claude/bin/claude` and `/opt/codex/bin/codex` before selected tenant turns start. With `--install-clis` omitted, provide them separately as root-owned executable files. Auto-update inside tenant accounts is disabled; update these copies as the server owner. The mount root must be empty for a new image. An existing `tenants.conf` is preserved, and only missing keys are appended; inspect existing values before using different options. A loop image is not resized by a repeated install.

## Layout and roles

```text
/srv/ai-tenants/<id>/          tenant UID, private home
  .auth/                       credentials
  .claude-bridge/              Claude config and sessions
  .codex-bridge/               Codex config and sessions
  .claude-delegate/            delegate config
  .codex-delegate/             delegate config
  .private/                    tenant private files
  .exchange/{inbox,outbox,queue,result}/  shared only with the bridge
  workspace/                   shared project working directory
/var/lib/telegram-ai/tenants/  root-owned provision records
/var/backups/ai-tenants/daily/ local tenant archives
```

`tenant-provision` creates a locked Unix account with no privileged groups for each selected Telegram ID. Ordinary tenant delegates use the same UID and separate CLI config. The bridge's service user needs only the two narrow sudoers entries installed here.

**Administrator tenants.** `--admin-ids ID,ID` (stored as `ADMIN_IDS` in the root-owned `tenants.conf`, which only root can edit, so a compromised bridge cannot promote anybody) lists people who are trusted with the server itself. Their tenant has the same layout and is still a separate Unix user, but `tenant-provision` gives that user passwordless sudo through a `visudo`-checked `/etc/sudoers.d/94-tenant-admin-<user>` entry, puts it in the `aitenants-admin` group (not covered by the firewall table) and `tenant-run` starts its processes without `no_new_privs` (otherwise sudo would not work), with `sudo` on `PATH`, in the larger `ai-admins.slice`. **An administrator is root-equivalent**: isolation between administrators and from the server does not exist by design; this role is for the owner and co-administrators and nobody else. `ADMIN_DISK_QUOTA_KB` (default: same as `DISK_QUOTA_KB`) gives administrators a larger disk quota. A directory containing a `.nobackup` file is skipped by the daily backup (use it for code checkouts). Removing an id from `ADMIN_IDS` and running `tenant-provision ensure` removes the sudoers entry again; `tenant-run` refuses a tenant whose sudoers entry does not match its role. The separate delegate user of an administrator stays an ordinary tenant. For administrators `workspace/` and `.private/` are kept at mode 0750 (members: 0700): the service account reaches code placed there through the administrator group or an ACL entry, and a chmod 0700 on every `ensure` would zero the ACL mask and cut that access off.

With the image, ext4 project ID equals the tenant UID. `tenant-provision ensure` sets the project ID and inheritance on the tree, then applies block and inode limits. It refuses to provision if the required mount is absent. The root-owned `ai-tenants.slice` and per-tenant slice drop-ins limit memory, CPU and tasks; `tenant-run` also starts each process in a scope with `MemoryMax`. These are per-process and per-tenant ceilings, not fair scheduling guarantees. On systems where AppArmor blocks unprivileged user namespaces, Codex's `bwrap` sandbox may not work; the UID remains the separation boundary.

The firewall service applies its own `inet ai_tenants` table. It permits internet destinations and the local DNS stub, while rejecting private and local destinations. Confirm that this matches the site's network policy before enabling tenant turns. `ALLOW_LOOPBACK_TCP` in `tenants.conf` allows selected loopback TCP ports if needed. `--hidepid` changes host-wide `/proc` visibility and grants the bridge user `procview` membership.

## Migration, backups, rollback

For an existing chat, run `m1-migrate-tenant` as root with `--bridge-user USER`, `--claude-bridge DIR`, `--codex-bot DIR`, and, if different, `--tenants-root PATH`. Its `plan <id>` is read-only; then run `backup <id>`, `copy <id> --yes`, and `verify <id>`. `--legacy-home PATH` sets the old workspace path and project slug; it defaults to the bridge user's home. `--project NAME` copies one project directory from the old Claude account. The migration copies old data and does not delete it. Do this before adding the ID to `ISOLATED_TENANTS`. Back up and verify application state before switching live chats.

To roll a chat back, remove its ID from `ISOLATED_TENANTS` in both bridges and restart them safely. Once no tenant processes are running, `m1-migrate-tenant rollback <id> --yes` copies newer sessions, logins and Codex state back to the old accounts after making a safety archive. The old accounts and tenant tree remain. To remove installed wiring after all isolated chats are disabled:

```bash
sudo ./isolation/uninstall_isolation.sh --yes
```

Uninstall leaves tenant users, homes, the loop image, mount and fstab entries, quotas, configuration and backups untouched. Remove those manually only after verifying copies. The daily backup timer writes seven compressed local snapshots per tenant under `/var/backups/ai-tenants/daily/`, excluding caches and `.exchange/`. Send copies to external storage yourself if disk loss must be covered.
