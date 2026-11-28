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


if __name__ == "__main__":
    unittest.main()
