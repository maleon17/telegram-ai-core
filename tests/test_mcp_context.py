"""Telegram-action MCP server: per-turn context and relay request shaping.

The MCP SDK is replaced by a local stub: these tests run from a clean checkout
without the SDK, Telegram or a live relay.
"""
import importlib.util
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_mcp(monkeypatch, tmp_path):
    class Server:
        def __init__(self, name):
            self.name = name
        def tool(self):
            return lambda function: function

    mcp = types.ModuleType("mcp")
    server = types.ModuleType("mcp.server")
    mcpserver = types.ModuleType("mcp.server.mcpserver")
    mcpserver.MCPServer = Server
    monkeypatch.setitem(sys.modules, "mcp", mcp)
    monkeypatch.setitem(sys.modules, "mcp.server", server)
    monkeypatch.setitem(sys.modules, "mcp.server.mcpserver", mcpserver)
    monkeypatch.setenv("CODEX_TELEGRAM_INSTANCE_ID", "instance")
    monkeypatch.setenv("CODEX_TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("CODEX_TELEGRAM_CONTEXT_DIR", str(tmp_path))
    for key in ("CODEX_TELEGRAM_TOPIC_ID", "CODEX_TELEGRAM_EXCLUDE_MSG_ID", "TOPIC_ID", "EXCLUDE_MSG_ID"):
        monkeypatch.delenv(key, raising=False)
    spec = importlib.util.spec_from_file_location("review_mcp", ROOT / "telegram_actions_mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mcp_reads_topic_and_excluded_message_from_dynamic_turn_context(monkeypatch, tmp_path):
    module = load_mcp(monkeypatch, tmp_path)
    path = tmp_path / (module.hashlib.sha256(b"instance\0" + b"42").hexdigest() + ".json")
    seen = []
    monkeypatch.setattr(module, "_call_tool", lambda tool, args: seen.append((tool, args)) or "ok")

    path.write_text(json.dumps({"requester_id": "owner", "topic_id": 11, "message_id": 101}))
    module.read_history()
    path.write_text(json.dumps({"requester_id": "owner", "topic_id": 22, "message_id": 202}))
    module.read_history()

    assert seen == [
        ("read_history", {"count": 50, "direction": None, "reply_id": None, "until_id": None,
                          "topic_id": 11, "exclude_id": 101, "chat": ""}),
        ("read_history", {"count": 50, "direction": None, "reply_id": None, "until_id": None,
                          "topic_id": 22, "exclude_id": 202, "chat": ""}),
    ]


def test_mcp_uses_observed_chat_from_turn_context_with_env_fallback(monkeypatch, tmp_path):
    module = load_mcp(monkeypatch, tmp_path)
    path = tmp_path / (module.hashlib.sha256(b"instance\0" + b"42").hexdigest() + ".json")
    bodies = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b"{}"

    monkeypatch.setattr(module, "POLL_TIMEOUT_S", 0)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda request, **_: bodies.append(request.data) or Response())

    path.write_text(json.dumps({"chat_id": "99"}))
    module._call_tool("send_message", {})
    path.unlink()
    module._call_tool("send_message", {})

    assert [json.loads(body)["chat_id"] for body in bodies] == ["99", "42"]


