"""Tailscale for a remote userbot: installed into /data, started, logged in
with a link the owner can open, and never touched by a same-host setup."""
import hashlib
import io
import json
import tarfile
from unittest.mock import AsyncMock, Mock

import pytest

from test_module_config import FLAVORS, ask, message, run  # noqa: F401  (ask is a fixture)


@pytest.fixture
def flavor(request):
    return FLAVORS["claude"][0]


def _status(state, **extra):
    return {"BackendState": state, **extra}


class FakeTailscale:
    """Scripted `tailscale status` answers plus a record of spawned processes."""

    def __init__(self, module, monkeypatch, tmp_path, statuses, binaries=True):
        self.statuses = list(statuses)
        self.spawned = []
        self.installed = 0
        monkeypatch.setattr(module, "TAILSCALE_STATE_DIR", str(tmp_path / "tailscale"))
        monkeypatch.setattr(module, "TAILSCALE_SOCKET", str(tmp_path / "run" / "tailscaled.sock"))
        monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
        self.have = binaries
        monkeypatch.setattr(module, "_tailscale_binary", lambda name: f"/bin/{name}" if self.have else None)
        monkeypatch.setattr(module, "_tailscale_status", self.status)
        monkeypatch.setattr(module, "_tailscale_detached", lambda argv, log: self.spawned.append(argv))
        monkeypatch.setattr(module, "_install_tailscale", self.install)

    def status(self, cli):
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def install(self):
        self.installed += 1
        self.have = True


def test_running_node_is_left_alone(flavor, monkeypatch, tmp_path):
    fake = FakeTailscale(flavor, monkeypatch, tmp_path, [
        _status("Running", Self={"TailscaleIPs": ["fd7a::1", "100.64.0.9"]}),
    ])
    assert flavor._ensure_tailnet("userbot-1") == ("running", "100.64.0.9")
    assert fake.spawned == [] and fake.installed == 0


def test_fresh_container_gets_installed_started_and_a_login_link(flavor, monkeypatch, tmp_path):
    fake = FakeTailscale(flavor, monkeypatch, tmp_path, [
        None, None, _status("NeedsLogin"),
        _status("NeedsLogin", AuthURL="https://login.tailscale.com/a/abc"),
    ], binaries=False)
    assert flavor._ensure_tailnet("userbot-1") == ("login", "https://login.tailscale.com/a/abc")
    assert fake.installed == 1
    daemon, up = fake.spawned
    assert daemon[0] == "/bin/tailscaled" and "--tun=userspace-networking" in daemon
    assert "--outbound-http-proxy-listen=localhost:1056" in daemon
    assert any(arg.startswith("--state=") and str(tmp_path) in arg for arg in daemon)
    assert up[0] == "/bin/tailscale" and "up" in up and "--hostname=userbot-1" in up


def test_pending_login_is_not_requested_twice(flavor, monkeypatch, tmp_path):
    fake = FakeTailscale(flavor, monkeypatch, tmp_path, [
        _status("NeedsLogin", AuthURL="https://login.tailscale.com/a/abc"),
    ])
    assert flavor._ensure_tailnet("h") == ("login", "https://login.tailscale.com/a/abc")
    assert fake.spawned == []


def test_logged_in_but_stopped_node_is_brought_up(flavor, monkeypatch, tmp_path):
    fake = FakeTailscale(flavor, monkeypatch, tmp_path, [
        _status("Stopped"), _status("Running", Self={"TailscaleIPs": ["100.64.0.3"]}),
    ])
    assert flavor._ensure_tailnet("h") == ("running", "100.64.0.3")
    assert len(fake.spawned) == 1 and "up" in fake.spawned[0]


def test_failures_are_reported_not_raised(flavor, monkeypatch, tmp_path):
    fake = FakeTailscale(flavor, monkeypatch, tmp_path, [None], binaries=False)
    fake.install = Mock(side_effect=RuntimeError("нет сети"))
    monkeypatch.setattr(flavor, "_install_tailscale", fake.install)
    state, detail = flavor._ensure_tailnet("h")
    assert state == "error" and "нет сети" in detail

    fake = FakeTailscale(flavor, monkeypatch, tmp_path, [None])
    state, detail = flavor._ensure_tailnet("h")
    assert state == "error" and "tailscaled.log" in detail


def _tarball(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _pkgs(monkeypatch, module, archive, checksum):
    index = {"Tarballs": {"amd64": "tailscale_1.2.3_amd64.tgz"}}
    answers = {
        "/?mode=json": json.dumps(index).encode(),
        "/tailscale_1.2.3_amd64.tgz": archive,
        "/tailscale_1.2.3_amd64.tgz.sha256": f"{checksum}\n".encode(),
    }
    fetched = []

    def download(url, timeout=60):
        fetched.append(url)
        return answers[url[len(module.TAILSCALE_PKGS):]]

    monkeypatch.setattr(module, "_download", download)
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    return fetched


def test_install_puts_verified_binaries_into_the_volume(flavor, monkeypatch, tmp_path):
    monkeypatch.setattr(flavor, "TAILSCALE_STATE_DIR", str(tmp_path / "tailscale"))
    archive = _tarball({
        "tailscale_1.2.3_amd64/tailscale": b"cli", "tailscale_1.2.3_amd64/tailscaled": b"daemon",
        "tailscale_1.2.3_amd64/systemd/tailscaled.service": b"unit",
    })
    _pkgs(monkeypatch, flavor, archive, hashlib.sha256(archive).hexdigest())
    flavor._install_tailscale()
    bin_dir = tmp_path / "tailscale" / "bin"
    assert sorted(p.name for p in bin_dir.iterdir()) == ["tailscale", "tailscaled"]
    assert (bin_dir / "tailscaled").read_bytes() == b"daemon"
    assert flavor._tailscale_binary("tailscale") == str(bin_dir / "tailscale")


def test_install_refuses_a_tampered_archive_and_a_missing_volume(flavor, monkeypatch, tmp_path):
    monkeypatch.setattr(flavor, "TAILSCALE_STATE_DIR", str(tmp_path / "tailscale"))
    archive = _tarball({"x/tailscale": b"cli", "x/tailscaled": b"daemon"})
    _pkgs(monkeypatch, flavor, archive, "0" * 64)
    with pytest.raises(RuntimeError, match="контрольная сумма"):
        flavor._install_tailscale()
    assert not (tmp_path / "tailscale" / "bin").exists()

    monkeypatch.setattr(flavor, "TAILSCALE_STATE_DIR", str(tmp_path / "absent" / "tailscale"))
    with pytest.raises(RuntimeError, match="постоянного тома"):
        flavor._install_tailscale()


def test_asknet_tailnet_shows_the_login_link_and_local_never_touches_tailscale(ask, monkeypatch):
    calls = []

    def ensure(hostname):
        calls.append(hostname)
        return "login", "https://login.tailscale.com/a/xyz"

    monkeypatch.setattr(ask.module, "_ensure_tailnet", ensure)
    asknet = ask.command("asknet")
    run(asknet(message("local mybot")))
    assert calls == []

    run(asknet(message("tailnet mybot http://100.64.0.1:9092")))
    assert calls == ["userbot-1000"]
    assert "https://login.tailscale.com/a/xyz" in ask.work.last and "применена" in ask.work.last

    run(asknet(message("token t")))
    run(asknet(message("check")))
    assert "https://login.tailscale.com/a/xyz" in ask.work.last


def test_module_load_sends_each_login_link_to_saved_messages_once(ask, monkeypatch):
    link = ["https://login.tailscale.com/a/one"]
    monkeypatch.setattr(ask.module, "_ensure_tailnet", lambda hostname: ("login", link[0]))
    ask.bot.config["http_proxy"] = "http://localhost:1056"
    ask.bot.config["backend_url"] = "http://100.64.0.1:9092"
    ask.bot._client = Mock(send_message=AsyncMock())
    run(ask.bot.client_ready())
    run(ask.bot.client_ready())
    link[0] = "https://login.tailscale.com/a/two"
    run(ask.bot.client_ready())
    sent = [call.args for call in ask.bot._client.send_message.await_args_list]
    assert [target for target, _ in sent] == ["me", "me"]
    assert "a/one" in sent[0][1] and "a/two" in sent[1][1]
