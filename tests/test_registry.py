"""Component registry, and the relay finding its neighbours through it."""
import importlib.util
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import registry  # noqa: E402


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_AI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "state"))
    for name in ("JARVIS_RELAY_CONFIG_DIR", "JARVIS_PERSONA_DIR", "JARVIS_XPERSONA_DIR",
                 "JARVIS_OWNER_NAME", "JARVIS_OWNER_TG_ID"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "config"


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_register_merge_get_and_unregister(config, tmp_path):
    checkout = tmp_path / "bridge"
    checkout.mkdir()
    registry.register("claude-bridge", path=str(checkout), unit="x.service")
    registry.register("claude-bridge", owner_id="7", unit=None)
    assert registry.component("claude-bridge") == {"path": str(checkout), "owner_id": "7"}
    assert _mode(config) == 0o700 and _mode(config / "registry.json") == 0o600
    stored = json.loads((config / "registry.json").read_text())
    assert stored["version"] == 1 and set(stored["components"]) == {"claude-bridge"}
    assert registry.unregister("claude-bridge") and registry.component("claude-bridge") is None
    assert not registry.unregister("claude-bridge")


def test_stale_or_malformed_entries_are_not_returned(config, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    registry.register("codex-bridge", path=str(gone))
    gone.rmdir()
    assert registry.component("codex-bridge") is None
    (config / "registry.json").write_text("{ not json")
    assert registry.components() == {} and registry.component("codex-bridge") is None
    # A broken file does not block the next registration.
    registry.register("core", path=str(tmp_path))
    assert registry.component("core")["path"] == str(tmp_path)


def test_rejects_bad_names_and_missing_path(config, tmp_path):
    for name in ("../evil", "Upper", "", "a" * 40):
        with pytest.raises(ValueError):
            registry.register(name, path=str(tmp_path))
    with pytest.raises(ValueError):
        registry.register("core", **{"bad-key": "x", "path": str(tmp_path)})
    with pytest.raises(ValueError):
        registry.register("core", unit="x.service")


def test_cli(config, tmp_path, capsys):
    assert registry.main(["register", "relay", f"path={tmp_path}", "url=http://127.0.0.1:9092"]) == 0
    assert registry.main(["get", "relay", "url"]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:9092"
    assert registry.main(["get", "relay", "missing"]) == 1
    assert registry.main(["get", "absent"]) == 1
    assert registry.main(["list"]) == 0
    assert capsys.readouterr().out.strip() == f"relay\t{tmp_path}"
    assert registry.main(["file"]) == 0
    assert capsys.readouterr().out.strip() == str(config / "registry.json")
    assert registry.main(["register", "relay", "novalue"]) == 2
    assert registry.main(["unregister", "relay"]) == 0 and registry.component("relay") is None


def _relay(tmp_path):
    name = "_cmd_queue_registry_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "cmd_queue.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _call(url, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_relay_serves_each_products_persona_from_the_registry(config, tmp_path, capsys):
    relay = _relay(tmp_path)
    relay.main(["--set-owner", "mybot", "111"])
    relay.main(["--add-token", "mybot"])
    token = capsys.readouterr().out.strip()
    relay._load_relay_config()

    claude = tmp_path / "claude-ask"
    (claude / "personas").mkdir(parents=True)
    (claude / "personas" / "default.md.example").write_text("Hello {{OWNER_NAME}} #{{OWNER_TG_ID}}\n")
    registry.register("claude-ask", path=str(claude), owner_name="Tester", owner_tg_id="111")

    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body = _call(f"{base}/persona?instance_id=mybot", token)
        assert (status, body["source"], body["persona"]) == (200, "template", "Hello Tester #111\n")

        # CodexAsk is not installed: its endpoint says so instead of silently
        # reading or writing ClaudeAsk's persona.
        assert _call(f"{base}/xpersona?instance_id=mybot", token)[0] == 404
        assert _call(f"{base}/xpersona", token, {"instance_id": "mybot", "persona": "x"})[0] == 404

        status, body = _call(f"{base}/persona", token, {"instance_id": "mybot", "persona": "Mine"})
        assert status == 200 and body["status"] == "ok"
        assert (claude / "personas" / "mybot.md").read_text() == "Mine\n"

        codex = tmp_path / "codex-ask"
        codex.mkdir()
        registry.register("codex-ask", path=str(codex), persona_dir=str(codex / "custom-personas"))
        assert _call(f"{base}/xpersona", token, {"instance_id": "mybot", "persona": "X"})[0] == 200
        assert (codex / "custom-personas" / "mybot.md").read_text() == "X\n"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_reset_clears_the_session_the_claude_worker_actually_uses(config, tmp_path, capsys, monkeypatch):
    """.new must reach the worker's own session map, wherever it keeps it."""
    import base64
    from cryptography.fernet import Fernet

    monkeypatch.delenv("JARVIS_ASK_DIR", raising=False)
    relay = _relay(tmp_path)
    relay.main(["--set-owner", "mybot", "111"])
    relay.main(["--add-token", "mybot"])
    token = capsys.readouterr().out.strip()
    relay._load_relay_config()

    sessions_dir = tmp_path / "worker-sessions"
    sessions_dir.mkdir()
    key = Fernet.generate_key()
    (sessions_dir / ".session_key_mybot").write_bytes(key)
    raw = base64.urlsafe_b64decode(Fernet(key).encrypt(json.dumps({"42": "s1", "43": "s2"}).encode()))
    (sessions_dir / "ask_sessions_mybot.enc").write_bytes(raw)
    registry.register("claude-ask", path=str(tmp_path), sessions_dir=str(sessions_dir))

    server = relay.ThreadingHTTPServer(("127.0.0.1", 0), relay.Queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _call(f"http://127.0.0.1:{server.server_port}/reset", token,
                             {"chat_id": "42", "instance_id": "mybot"})
        assert status == 200 and "сброшена" in body["message"]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    stored = Fernet(key).decrypt(base64.urlsafe_b64encode((sessions_dir / "ask_sessions_mybot.enc").read_bytes()))
    assert json.loads(stored) == {"43": "s2"}
