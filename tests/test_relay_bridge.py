"""Bridge tokens: an instance owner's Telegram actions from their bridge chat,
and nothing else."""
import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def relay(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("JARVIS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TELEGRAM_AI_CONFIG_DIR", str(tmp_path / "config"))
    for name in ("JARVIS_RELAY_CONFIG_DIR", "JARVIS_RELAY_TOKENS_FILE", "JARVIS_RELAY_OWNERS_FILE",
                 "JARVIS_RELAY_BRIDGE_TOKENS_FILE", "JARVIS_PERSONA_DIR"):
        monkeypatch.delenv(name, raising=False)
    name = "_cmd_queue_bridge_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "cmd_queue.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    def cli(*argv):
        assert module.main(list(argv)) == 0
        return capsys.readouterr().out.strip()

    cli("--set-owner", "alice-bot", "111")
    cli("--set-owner", "bob-bot", "222")
    module.userbot_token = cli("--add-token", "alice-bot")
    module.bob_userbot_token = cli("--add-token", "bob-bot")
    module.bridge_token = cli("--add-bridge-token", "alice-bot")
    module.cli = cli
    module._load_relay_config()
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Queue)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    module.base = f"http://127.0.0.1:{server.server_port}"
    yield module
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def call(relay, path, token, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(relay.base + path, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_bridge_call_runs_as_the_instance_owner_whatever_the_caller_claims(relay):
    status, _ = call(relay, "/tool_call", relay.bridge_token, {
        "request_id": "direct-1", "instance_id": "alice-bot", "tool": "send_message",
        "args": {"target": "@friend", "text": "hi"},
        # Everything below is caller-supplied and must not be believed.
        "requester_id": "999", "chat_id": "-100500", "owner_authorized": False,
        "parent_request_id": "made-up",
    })
    assert status == 200
    queued = json.loads((Path(relay.TOOL_QUEUE_DIR) / "direct-1.json").read_text())
    assert queued["requester_id"] == "111" and queued["owner_authorized"] is True
    assert queued["direct"] is True and queued["chat_id"] == ""
    assert queued["instance_id"] == "alice-bot" and queued["tool"] == "send_message"

    # The userbot of that instance picks it up; the bridge reads the result.
    status, pending = call(relay, "/tool_call_pending?instance_id=alice-bot", relay.userbot_token)
    assert status == 200 and pending["request_id"] == "direct-1" and pending["direct"] is True
    call(relay, "/tool_call_result", relay.userbot_token, {"request_id": "direct-1", "result": "✅ sent"})
    status, result = call(relay, "/tool_call?request_id=direct-1", relay.bridge_token)
    assert status == 200 and result["result"] == "✅ sent"


def test_bridge_token_reaches_nothing_but_tool_calls(relay):
    token = relay.bridge_token
    assert call(relay, "/whoami", token) == (200, {
        "status": "ok", "instance_id": "alice-bot", "owner_registered": True, "role": "bridge",
    })
    forbidden_posts = {
        "/ask": {"request_id": "x1", "question": "q", "instance_id": "alice-bot"},
        "/xask": {"request_id": "x2", "question": "q", "instance_id": "alice-bot"},
        "/persona": {"instance_id": "alice-bot", "persona": "evil"},
        "/reset": {"chat_id": "1", "instance_id": "alice-bot"},
        "/cancel": {"request_id": "x1", "instance_id": "alice-bot"},
        "/tool_call_result": {"request_id": "x1", "result": "forged"},
        "/classify": {"text": "t", "condition": "c", "instance_id": "alice-bot"},
    }
    for path, body in forbidden_posts.items():
        assert call(relay, path, token, body)[0] == 403, path
    for path in ("/tool_call_pending?instance_id=alice-bot", "/persona?instance_id=alice-bot",
                 "/ask?request_id=x1", "/download?path=/etc/passwd"):
        assert call(relay, path, token)[0] == 403, path
    assert not list(Path(relay.ASK_QUEUE_DIR).glob("*.json"))

    # ...and only for its own instance.
    assert call(relay, "/tool_call", token, {
        "request_id": "direct-2", "instance_id": "bob-bot", "tool": "send_message", "args": {},
    })[0] == 403


def test_a_userbot_token_still_needs_a_real_chat_request(relay):
    status, _ = call(relay, "/tool_call", relay.userbot_token, {
        "request_id": "no-parent", "instance_id": "alice-bot", "tool": "send_message", "args": {},
        "direct": True,
    })
    assert status == 403
    assert call(relay, "/whoami", relay.userbot_token)[1]["role"] == "userbot"


def test_bridge_tokens_are_managed_like_the_others(relay, tmp_path):
    assert relay.cli("--owned-by", "111") == "alice-bot"
    assert relay.cli("--owned-by", "333") == ""
    relay.cli("--set-owner", "alice-second", "111")
    assert relay.cli("--owned-by", "111").split() == ["alice-bot", "alice-second"]
    assert "bridge_tokens=1" in relay.cli("--list")

    with pytest.raises(RuntimeError):
        relay.main(["--add-bridge-token", "nobody-owns-this"])

    # A new bridge token works without a restart; revoking drops both kinds.
    second = relay.cli("--add-bridge-token", "bob-bot")
    assert call(relay, "/whoami", second)[1]["instance_id"] == "bob-bot"
    relay.cli("--revoke-tokens", "alice-bot")
    assert call(relay, "/whoami", relay.bridge_token)[0] == 401
    assert call(relay, "/whoami", relay.userbot_token)[0] == 401
    assert call(relay, "/whoami", second)[0] == 200
    stored = json.loads((tmp_path / "config" / "relay-bridge-tokens.json").read_text())
    assert list(stored.values()) == ["bob-bot"] and second not in json.dumps(stored)
