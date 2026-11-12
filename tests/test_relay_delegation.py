"""delegate_to_bridge: only the instance's owner, in their own request."""
import json
import threading
import time
from pathlib import Path

import pytest

import registry
from test_relay_bridge import call, relay  # noqa: F401  (relay is a fixture)


@pytest.fixture
def bridge(relay, tmp_path, monkeypatch):
    """A stand-in Claude bridge checkout, registered like a real one."""
    monkeypatch.setattr(relay, "DELEGATE_ACCEPT_TIMEOUT_S", 1.5)
    monkeypatch.setattr(relay, "DELEGATE_POLL_S", 0.05)
    path = tmp_path / "claude-telegram-bridge"
    (path / "cross_delegate_queue").mkdir(parents=True)
    (path / "cross_delegate_result").mkdir()
    registry.register("claude-bridge", path=str(path))
    return path


def _ask(relay, token, request_id, requester):
    assert call(relay, "/ask", token, {
        "request_id": request_id, "question": "q", "instance_id": "alice-bot", "requester_id": requester,
    })[0] == 200


def _tool(relay, token, request_id, parent, args=None):
    return call(relay, "/tool_call", token, {
        "request_id": request_id, "instance_id": "alice-bot", "parent_request_id": parent,
        "tool": "delegate_to_bridge", "args": args or {"prompt": "fix the build", "agent": "claude"},
    })


def _result(relay, token, request_id, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data = call(relay, f"/tool_call?request_id={request_id}", token)
        if data.get("done"):
            return data["result"]
        time.sleep(0.05)
    raise AssertionError("no tool result")


def _play_bridge(path, final_text=None, accept=True, reason="accepted"):
    """Consume one delegation like the real bridge watcher would."""
    seen = {}

    def run():
        queue, results = path / "cross_delegate_queue", path / "cross_delegate_result"
        deadline = time.time() + 5
        while time.time() < deadline:
            names = sorted(queue.glob("*.json"))
            if names:
                seen["request"] = json.loads(names[0].read_text())
                names[0].unlink()
                final_path = path / "final.json"
                payload = {"done": True, "ok": accept, "text": reason}
                if accept and final_text is not None:
                    payload["final"] = {"path": str(final_path), "after_ts": time.time(), "consume": True}
                (results / names[0].name).write_text(json.dumps(payload))
                if accept and final_text is not None:
                    time.sleep(0.2)
                    final_path.write_text(json.dumps({"text": final_text, "ok": True, "ts": time.time()}))
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, seen


def test_owner_request_reaches_the_owners_own_bridge_tenant(relay, bridge):
    _ask(relay, relay.userbot_token, "owner-ask", "111")
    player, seen = _play_bridge(bridge, final_text="build fixed")
    assert _tool(relay, relay.userbot_token, "d-1", "owner-ask")[0] == 200
    result = _result(relay, relay.userbot_token, "d-1")
    player.join()
    # The task goes to the tenant with the owner's own Telegram id -- taken
    # from the relay's owners map, not from the request.
    assert seen["request"] == {"chat_id": 111, "text": "fix the build"}
    assert result.endswith("build fixed") and "claude" in result
    assert not (bridge / "final.json").exists()
    # It never entered the userbot's tool queue.
    assert not list(Path(relay.TOOL_QUEUE_DIR).glob("*.json"))


def test_everyone_but_the_owner_is_refused(relay, bridge):
    # A stranger using .ask in a chat, and a trigger-fired run.
    _ask(relay, relay.userbot_token, "stranger-ask", "555")
    _ask(relay, relay.userbot_token, "trigger-ask", "trigger:abc123")
    for request_id, parent in (("d-2", "stranger-ask"), ("d-3", "trigger-ask")):
        assert _tool(relay, relay.userbot_token, request_id, parent)[0] == 200
        assert "только владелец" in _result(relay, relay.userbot_token, request_id)
    # A bridge token (the owner acting from a bridge) must use the bridge's
    # own delegation tool instead.
    assert _tool(relay, relay.bridge_token, "d-4", "")[0] == 200
    assert "из бриджа" in _result(relay, relay.bridge_token, "d-4")
    # Another instance cannot piggyback on this owner's request.
    assert call(relay, "/tool_call", relay.bob_userbot_token, {
        "request_id": "d-5", "instance_id": "bob-bot", "parent_request_id": "stranger-ask",
        "tool": "delegate_to_bridge", "args": {"prompt": "x"},
    })[0] == 403
    assert not list((bridge / "cross_delegate_queue").glob("*.json"))


def test_bridge_refusal_absence_and_silence_are_told_apart(relay, bridge, tmp_path):
    _ask(relay, relay.userbot_token, "owner-ask", "111")

    player, _ = _play_bridge(bridge, accept=False, reason="Claude-аккаунт не готов")
    _tool(relay, relay.userbot_token, "d-6", "owner-ask")
    assert "отклонил" in _result(relay, relay.userbot_token, "d-6") and True
    player.join()

    # Installed but its service is not consuming the queue.
    _tool(relay, relay.userbot_token, "d-7", "owner-ask")
    result = _result(relay, relay.userbot_token, "d-7")
    assert "НЕ начата" in result
    assert not list((bridge / "cross_delegate_queue").glob("*.json"))

    # Accepted, not waited for.
    player, _ = _play_bridge(bridge, final_text="later")
    _tool(relay, relay.userbot_token, "d-8", "owner-ask", {"prompt": "long job", "wait_seconds": 0})
    assert _result(relay, relay.userbot_token, "d-8").startswith("Задача принята")
    player.join()

    # The Codex bridge is not installed; a bad agent name; an empty task.
    _tool(relay, relay.userbot_token, "d-9", "owner-ask", {"prompt": "x", "agent": "codex"})
    assert "не установлен" in _result(relay, relay.userbot_token, "d-9")
    _tool(relay, relay.userbot_token, "d-10", "owner-ask", {"prompt": "x", "agent": "gemini"})
    assert "claude" in _result(relay, relay.userbot_token, "d-10")
    _tool(relay, relay.userbot_token, "d-11", "owner-ask", {"prompt": "  "})
    assert "пуст" in _result(relay, relay.userbot_token, "d-11")

    # A registered bridge that never ran: nothing is created inside it.
    fresh = tmp_path / "codex-telegram-bridge"
    fresh.mkdir()
    registry.register("codex-bridge", path=str(fresh))
    _tool(relay, relay.userbot_token, "d-12", "owner-ask", {"prompt": "x", "agent": "codex"})
    assert "ни разу не запускался" in _result(relay, relay.userbot_token, "d-12")
    assert list(fresh.iterdir()) == []
