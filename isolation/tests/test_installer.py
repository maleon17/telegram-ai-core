"""Root-free checks of the published isolation staging and helper contracts."""
import hashlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "isolation/install_isolation.sh"
UNINSTALL = ROOT / "isolation/uninstall_isolation.sh"


def load_helper(name):
    path = ROOT / "isolation/bin" / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.stage = self.base / "stage"
        self.claude = self.base / "claude-bridge"
        self.codex = self.base / "codex-bot"
        (self.claude / "isolation/tools").mkdir(parents=True)
        self.codex.mkdir()
        for name in ("account-limits", "list-sessions"):
            (self.claude / "isolation/tools" / name).write_text("#!/usr/bin/python3\n")
        for name in ("send_telegram_file_mcp.py", "delegate_to_codex_exchange_mcp.py"):
            (self.claude / name).write_text("#!/usr/bin/python3\n")
        for name in ("codex_send_telegram_file_mcp.py", "delegate_to_claude_exchange_mcp.py"):
            (self.codex / name).write_text("#!/usr/bin/python3\n")
        self.args = [str(INSTALL), "--bridge-user", pwd.getpwuid(os.getuid()).pw_name,
                     "--destdir", str(self.stage), "--claude-bridge", str(self.claude),
                     "--codex-bot", str(self.codex)]

    def run_installer(self, *extra):
        return subprocess.run(self.args + list(extra), text=True, capture_output=True)

    def test_stage_content_and_modes(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        conf = self.stage / "etc/telegram-ai/tenants.conf"
        self.assertEqual(stat.S_IMODE(conf.stat().st_mode), 0o600)
        self.assertIn("REQUIRE_MOUNT=1", conf.read_text())
        self.assertIn("DISK_QUOTA_KB=4194304", conf.read_text())
        self.assertNotIn("@BRIDGE_USER@", conf.read_text())
        self.assertIn("loop,prjquota,nodev,nosuid", (self.stage / "etc/fstab").read_text())
        for name in ("tenant-provision", "tenant-run", "tenant-firewall", "tenant-backup", "m1-migrate-tenant"):
            path = self.stage / "usr/local/sbin" / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            self.assertTrue(path.read_bytes().startswith(b"#!/usr/bin/python3\n"))
        for name in ("92-tenant-provision", "93-tenant-run"):
            path = self.stage / "etc/sudoers.d" / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
            self.assertIn(pwd.getpwuid(os.getuid()).pw_name, path.read_text())
        for name in ("account-limits", "list-sessions"):
            self.assertEqual(stat.S_IMODE((self.stage / "opt/telegram-ai/tools" / name).stat().st_mode), 0o755)
        self.assertTrue((self.stage / "opt/telegram-ai/mcp/delegate_to_claude_exchange_mcp.py").exists())
        self.assertIn("RequiresMountsFor=/srv/ai-tenants", (self.stage / "etc/systemd/system/ai-tenants-backup.service").read_text())

    def test_idempotence_and_check(self):
        self.assertEqual(self.run_installer().returncode, 0)
        files = [p for p in self.stage.rglob("*") if p.is_file()]
        before = {p: (hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns) for p in files}
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertEqual(before, {p: (hashlib.sha256(p.read_bytes()).digest(), p.stat().st_mtime_ns) for p in files})
        self.assertEqual(self.run_installer("--check").returncode, 0)
        (self.stage / "usr/local/sbin/tenant-run").write_text("changed\n")
        result = self.run_installer("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DIFF /usr/local/sbin/tenant-run", result.stdout)
        self.assertEqual((self.stage / "usr/local/sbin/tenant-run").read_text(), "changed\n")

    def test_required_user_and_unsafe_config(self):
        result = subprocess.run([str(INSTALL), "--destdir", str(self.stage)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--bridge-user", result.stderr)
        self.assertEqual(self.run_installer().returncode, 0)
        conf = self.stage / "etc/telegram-ai/tenants.conf"
        conf.chmod(0o666)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsafe tenants.conf", result.stderr)

    def test_no_image_and_uninstall_preserves_data(self):
        self.assertEqual(self.run_installer("--image-size", "none").returncode, 0)
        conf = self.stage / "etc/telegram-ai/tenants.conf"
        self.assertIn("REQUIRE_MOUNT=0", conf.read_text())
        self.assertIn("DISK_QUOTA_KB=0", conf.read_text())
        tenant = self.stage / "srv/ai-tenants/12345"
        tenant.mkdir()
        (tenant / "keep.txt").write_text("keep")
        result = subprocess.run([str(UNINSTALL), "--destdir", str(self.stage)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((tenant / "keep.txt").read_text(), "keep")
        self.assertTrue(conf.exists())
        self.assertFalse((self.stage / "usr/local/sbin/tenant-run").exists())

    def test_custom_limits_hidepid_and_dry_run(self):
        options = ("--tenants-root", "/srv/custom-tenants", "--disk-quota-gb", "2",
                   "--inodes", "50000", "--memory-max", "2G", "--tenants-memory-max", "5G", "--hidepid")
        self.assertEqual(self.run_installer(*options, "--dry-run").returncode, 0)
        self.assertFalse(self.stage.exists())
        result = self.run_installer(*options)
        self.assertEqual(result.returncode, 0, result.stderr)
        conf = (self.stage / "etc/telegram-ai/tenants.conf").read_text()
        self.assertIn("TENANTS_ROOT=/srv/custom-tenants", conf)
        self.assertIn("DISK_QUOTA_KB=2097152", conf)
        self.assertIn("DISK_INODES=50000", conf)
        self.assertIn("MEMORY_MAX=2G", conf)
        self.assertIn("hidepid=2,gid=99998", (self.stage / "etc/fstab").read_text())
        self.assertIn("MemoryMax=5G", (self.stage / "etc/systemd/system/ai-tenants.slice.d/10-limits.conf").read_text())

    def test_admin_ids_are_validated_and_rendered(self):
        for bad in ("abc", "123", "123456,", "123456;rm"):
            result = self.run_installer("--admin-ids", bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("--admin-ids", result.stderr)
        self.assertFalse(self.stage.exists())
        result = self.run_installer("--admin-ids", "123456,7654321")
        self.assertEqual(result.returncode, 0, result.stderr)
        conf = (self.stage / "etc/telegram-ai/tenants.conf").read_text()
        self.assertIn("ADMIN_IDS=123456,7654321", conf)
        self.assertIn("ADMIN_GROUP=aitenants-admin", conf)
        self.assertNotIn("@ADMIN_IDS@", conf)
        slice_file = self.stage / "etc/systemd/system/ai-admins.slice.d/10-limits.conf"
        self.assertIn("MemoryMax=9G", slice_file.read_text())
        # without the option the key stays empty: no administrator exists unless the owner asks for one
        other = self.base / "stage2"
        subprocess.run([str(INSTALL), "--bridge-user", pwd.getpwuid(os.getuid()).pw_name, "--destdir", str(other)],
                       text=True, capture_output=True, check=True)
        self.assertIn("ADMIN_IDS=\n", (other / "etc/telegram-ai/tenants.conf").read_text())

    def test_existing_config_only_gets_missing_keys(self):
        self.assertEqual(self.run_installer().returncode, 0)
        path = self.stage / "etc/telegram-ai/tenants.conf"
        original = path.read_text().replace("ALLOW_LOOPBACK_TCP=\n", "ALLOW_LOOPBACK_TCP=8765\n")
        original = original.replace("MEMORY_MAX=3G\n", "")
        path.write_text(original)
        path.chmod(0o600)
        self.assertEqual(self.run_installer().returncode, 0)
        result = path.read_text()
        self.assertIn("ALLOW_LOOPBACK_TCP=8765", result)
        self.assertEqual(result.count("MEMORY_MAX=3G"), 1)
        self.assertTrue(result.startswith(original))

    @unittest.skipUnless(shutil.which("visudo"), "visudo unavailable; sudoers syntax check skipped")
    def test_visudo(self):
        self.assertEqual(self.run_installer().returncode, 0)
        for name in ("92-tenant-provision", "93-tenant-run"):
            path = self.stage / "etc/sudoers.d" / name
            result = subprocess.run(["visudo", "-cf", str(path)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze unavailable; unit verification skipped")
    def test_systemd_units(self):
        self.assertEqual(self.run_installer().returncode, 0)
        units = list((self.stage / "etc/systemd/system").glob("ai-tenants-*.service"))
        units += list((self.stage / "etc/systemd/system").glob("ai-tenants-*.timer"))
        result = subprocess.run(["systemd-analyze", "verify", *map(str, units)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("nft"), "nft unavailable; nft syntax check skipped")
    def test_nft_ruleset(self):
        helper = load_helper("tenant-firewall")
        with mock.patch.object(helper.grp, "getgrnam", return_value=mock.Mock(gr_gid=30000)):
            rules = helper.render({"TENANT_GROUP": "aitenants", "ALLOW_LOOPBACK_TCP": ""})
        result = subprocess.run(["nft", "-c", "-f", "-"], input=rules, text=True, capture_output=True)
        if "Operation not permitted" in result.stderr:
            self.skipTest("nft -c requires netlink privileges on this host")
        self.assertEqual(result.returncode, 0, result.stderr)


class ProvisionLogicTests(unittest.TestCase):
    def setUp(self):
        self.helper = load_helper("tenant-provision")

    def test_quota_commands_and_inheritance(self):
        calls = []
        def fake_run(command):
            calls.append(command)
            return mock.Mock(returncode=0, stderr="")
        with mock.patch.object(self.helper, "run", side_effect=fake_run):
            self.assertTrue(self.helper.apply_disk_quota({"_quota_kb": 4096, "_inodes": 100,
                                                           "TENANTS_ROOT": "/tenants"}, "/tenants/12345", 20001, False))
        self.assertEqual(len(calls), 3)
        self.assertIn("+P", calls[1])
        self.assertEqual(calls[2][2:7], ["20001", "3584", "4096", "80", "100"])
        calls.clear()
        with mock.patch.object(self.helper, "run", side_effect=fake_run):
            self.helper.apply_disk_quota({"_quota_kb": 4096, "_inodes": 100,
                                          "TENANTS_ROOT": "/tenants"}, "/tenants/12345", 20001, True)
        self.assertEqual(len(calls), 1)

    def test_mount_requirement_and_config_validation(self):
        conf = dict(self.helper.DEFAULTS, REQUIRE_MOUNT="1", TENANTS_ROOT="/tenants")
        with mock.patch.object(self.helper.os.path, "ismount", return_value=False):
            with self.assertRaises(SystemExit):
                self.helper.validate_conf(conf)
        conf["REQUIRE_MOUNT"] = "0"
        conf["DISK_QUOTA_KB"] = "invalid"
        with self.assertRaises(SystemExit):
            self.helper.validate_conf(conf)
        conf["DISK_QUOTA_KB"] = "1024"
        fake_stat = mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o711)
        with mock.patch.object(self.helper.os, "lstat", return_value=fake_stat):
            self.helper.validate_conf(conf)
        self.assertEqual(conf["_quota_kb"], 1024)


class AdministratorRoleTests(unittest.TestCase):
    """The administrator role: set only by the root-owned configuration, passwordless sudo through a validated
    sudoers entry, outside the tenant firewall group, and with sudo actually usable (no no_new_privs)."""

    def setUp(self):
        self.provision = load_helper("tenant-provision")
        self.run_helper = load_helper("tenant-run")
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.sudoers = Path(self.temp.name)
        patcher = mock.patch.object(self.provision, "SUDOERS_DIR", str(self.sudoers))
        patcher.start()
        self.addCleanup(patcher.stop)
        chown = mock.patch.object(self.provision.os, "chown")
        chown.start()
        self.addCleanup(chown.stop)
        self.visudo_calls = []

    def fake_run(self, command):
        self.visudo_calls.append(command)
        return mock.Mock(returncode=0, stderr="")

    def conf(self, **overrides):
        conf = dict(self.provision.DEFAULTS, TENANTS_ROOT="/tenants", BRIDGE_GROUP="bridgeg", **overrides)
        fake_stat = mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o711)
        with mock.patch.object(self.provision.os, "lstat", return_value=fake_stat), \
                mock.patch.object(self.provision.pwd, "getpwnam"), \
                mock.patch.object(self.provision.grp, "getgrnam", return_value=mock.Mock(gr_gid=1234)):
            self.provision.validate_conf(conf)
        return conf

    def test_roles_come_only_from_the_configuration(self):
        conf = self.conf(ADMIN_IDS="123456, 654321")
        self.assertEqual(self.provision.role_of(conf, "123456", "main"), "admin")
        self.assertEqual(self.provision.role_of(conf, "654321", "main"), "admin")
        self.assertEqual(self.provision.role_of(conf, "999999", "main"), "member")
        # the separate delegate user of an administrator is an ordinary tenant
        self.assertEqual(self.provision.role_of(conf, "123456", "delegate"), "member")
        with self.assertRaises(SystemExit):
            self.conf(ADMIN_IDS="12ab")
        for bad in ("sudo", "aitenants", "bridgeg", "Bad Name"):
            with self.assertRaises(SystemExit, msg=bad):
                self.conf(ADMIN_GROUP=bad)

    def test_sudoers_entry_is_validated_idempotent_and_revocable(self):
        with mock.patch.object(self.provision, "run", side_effect=self.fake_run):
            self.provision.apply_sudoers("t123456", "admin")
            path = self.sudoers / "94-tenant-admin-t123456"
            self.assertEqual(path.read_text(), self.provision.sudoers_text("t123456"))
            self.assertIn("t123456 ALL=(ALL:ALL) NOPASSWD: ALL", path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o440)
            self.assertTrue(self.provision.sudoers_ok("t123456", "admin"))
            self.assertFalse(self.provision.sudoers_ok("t123456", "member"))
            checked = len(self.visudo_calls)
            self.provision.apply_sudoers("t123456", "admin")  # already correct: no rewrite
            self.assertEqual(len(self.visudo_calls), checked)
            self.provision.apply_sudoers("t123456", "member")  # revoked
            self.assertFalse(path.exists())
            self.assertTrue(self.provision.sudoers_ok("t123456", "member"))

    def test_rejected_sudoers_entry_leaves_nothing_behind(self):
        refusing = mock.Mock(returncode=1, stderr="syntax error")
        with mock.patch.object(self.provision, "run", return_value=refusing):
            with self.assertRaises(SystemExit):
                self.provision.apply_sudoers("t123456", "admin")
        self.assertEqual(list(self.sudoers.iterdir()), [])

    def test_tenant_run_gives_administrators_sudo_and_their_own_slice(self):
        conf = dict(self.run_helper.DEFAULTS)
        member = {"id": "654321", "product": "bridge", "env": [], "role": "member"}
        admin = dict(member, id="123456", role="admin")
        pw = mock.Mock(pw_dir="/srv/ai-tenants/x", pw_name="tx")
        self.assertEqual(self.run_helper.build_env(conf, member, pw)["PATH"], self.run_helper.SAFE_PATH)
        self.assertEqual(self.run_helper.build_env(conf, admin, pw)["PATH"], self.run_helper.ADMIN_PATH)
        self.assertIn("/usr/sbin", self.run_helper.ADMIN_PATH)
        commands = {}
        for name, opts in (("member", member), ("admin", admin)):
            with mock.patch.object(self.run_helper.os.path, "exists", return_value=True), \
                    mock.patch.object(self.run_helper.os, "execv", side_effect=lambda binary, cmd, n=name: commands.update({n: cmd})):
                self.run_helper.run_scope(conf, ["tenant-run", opts["id"], "--", "claude"], opts)
        self.assertIn("--slice=ai-tenants-654321.slice", commands["member"])
        self.assertIn("MemoryMax=3G", commands["member"])
        self.assertIn("--slice=ai-admins.slice", commands["admin"])
        self.assertIn("MemoryMax=8G", commands["admin"])

    def test_no_new_privs_is_kept_for_members_and_dropped_for_administrators(self):
        for role, expect_call in (("member", True), ("admin", False)):
            libc = mock.Mock()
            libc.prctl.return_value = 0
            pw = mock.Mock(pw_uid=20001, pw_gid=1002, pw_dir="/srv/ai-tenants/x", pw_name="tx")
            with mock.patch.object(self.run_helper.ctypes, "CDLL", return_value=libc), \
                    mock.patch.object(self.run_helper.os, "setgroups"), mock.patch.object(self.run_helper.os, "setgid"), \
                    mock.patch.object(self.run_helper.os, "setuid", side_effect=[None, PermissionError]), \
                    mock.patch.object(self.run_helper.os, "getuid", return_value=20001), \
                    mock.patch.object(self.run_helper.os, "geteuid", return_value=20001), \
                    mock.patch.object(self.run_helper.os, "getgid", return_value=1002), \
                    mock.patch.object(self.run_helper.os, "getppid", return_value=2), \
                    mock.patch.object(self.run_helper.os, "umask"), mock.patch.object(self.run_helper.os, "makedirs"), \
                    mock.patch.object(self.run_helper.os, "chdir"), mock.patch.object(self.run_helper.os, "closerange"), \
                    mock.patch.object(self.run_helper.resource, "setrlimit"), \
                    mock.patch.object(self.run_helper, "resolve_program", return_value="/bin/true"), \
                    mock.patch.object(self.run_helper.os, "execve") as execve:
                self.run_helper.drop_and_exec({}, pw, {"TMPDIR": "/tmp/x"}, "workspace", "claude", [], role)
            called = any(c.args and c.args[0] == self.run_helper.PR_SET_NO_NEW_PRIVS for c in libc.prctl.call_args_list)
            self.assertEqual(called, expect_call, role)
            execve.assert_called_once()


class MigratorInputTests(unittest.TestCase):
    """The migrator runs as root and copies into workspace/<project>: the project name must stay one plain component."""

    def run_plan(self, project):
        return subprocess.run(["python3", str(ROOT / "isolation/bin/m1-migrate-tenant"), "plan", "123456",
                               "--bridge-user", "nobody", "--project", project], capture_output=True, text=True)

    def test_project_name_cannot_traverse(self):
        for bad in (".", "..", ".hidden", "a/b"):
            result = self.run_plan(bad)
            self.assertNotEqual(result.returncode, 0, bad)
            self.assertIn("bad project name", result.stderr, bad)
        self.assertNotIn("bad project name", self.run_plan("Project-1.2").stderr)


class BackupGuardTests(unittest.TestCase):
    """Backups share the root disk with the system: they must refuse to eat the last free space."""

    def setUp(self):
        self.helper = load_helper("tenant-backup")
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.helper.ROOT, self.helper.DEST = str(base / "tenants"), str(base / "dest")
        home = base / "tenants" / "123456"
        (home / "workspace").mkdir(parents=True)
        (home / "workspace" / "data.bin").write_bytes(b"x" * 4096)
        (home / "cache").mkdir()
        (home / "cache" / "skipped.bin").write_bytes(b"y" * 1_000_000)

    def test_estimate_skips_cache_dirs(self):
        self.assertEqual(self.helper.estimate_bytes(str(Path(self.helper.ROOT) / "123456")), 4096)

    def test_snapshot_refused_when_free_space_is_short(self):
        usage = mock.Mock(free=self.helper.MIN_FREE_BYTES + 100)  # floor + less than the 4096 bytes of data
        with mock.patch.object(self.helper.shutil, "disk_usage", return_value=usage):
            with self.assertRaises(RuntimeError):
                self.helper.snapshot("123456", "20260101-000000")
        self.assertEqual(list(Path(self.helper.DEST, "123456").glob("*")), [])

    def test_snapshot_written_when_space_is_enough(self):
        usage = mock.Mock(free=self.helper.MIN_FREE_BYTES + 10 * 2**20)
        with mock.patch.object(self.helper.shutil, "disk_usage", return_value=usage):
            path = self.helper.snapshot("123456", "20260101-000000")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
