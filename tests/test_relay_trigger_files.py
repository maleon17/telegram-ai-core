"""A trigger-fired run may send only files from the instance's uploads folder."""
import os
import time
from pathlib import Path

from test_relay_bridge import call, relay  # noqa: F401  (relay is a fixture)


def _ask(relay, request_id, requester):
    assert call(relay, "/ask", relay.userbot_token, {
        "request_id": request_id, "question": "q", "instance_id": "alice-bot", "requester_id": requester,
    })[0] == 200


def _send_file(relay, request_id, parent, path):
    return call(relay, "/tool_call", relay.userbot_token, {
        "request_id": request_id, "instance_id": "alice-bot", "parent_request_id": parent,
        "tool": "send_file", "args": {"path": str(path), "target": ""},
    })


def _result(relay, request_id):
    deadline = time.time() + 5
    while time.time() < deadline:
        data = call(relay, f"/tool_call?request_id={request_id}", relay.userbot_token)[1]
        if data.get("done"):
            return data["result"]
        time.sleep(0.05)
    raise AssertionError("no tool result")


def test_a_trigger_sends_only_files_from_the_uploads_folder(relay, tmp_path):
    uploads = Path(relay.ARTIFACT_DIR) / "alice-bot" / "uploads"
    uploads.mkdir(parents=True)
    made = uploads / "report.md"
    made.write_text("hello", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("token", encoding="utf-8")
    sneaky = uploads / "link.txt"
    os.symlink(secret, sneaky)
    _ask(relay, "trigger-ask", "trigger:abc123")

    assert _send_file(relay, "f-1", "trigger-ask", made)[0] == 200
    assert len(list(Path(relay.TOOL_QUEUE_DIR).glob("f-1.json"))) == 1

    for request_id, path in (("f-2", secret), ("f-3", sneaky), ("f-4", uploads / "missing.md")):
        assert _send_file(relay, request_id, "trigger-ask", path)[0] == 200
        assert "из триггера можно отправлять только файлы" in _result(relay, request_id)
        assert not list(Path(relay.TOOL_QUEUE_DIR).glob(f"{request_id}.json"))


def test_the_owner_may_still_send_any_registered_file(relay, tmp_path):
    outside = tmp_path / "notes.txt"
    outside.write_text("x", encoding="utf-8")
    _ask(relay, "owner-ask", "111")
    assert _send_file(relay, "f-5", "owner-ask", outside)[0] == 200
    assert len(list(Path(relay.TOOL_QUEUE_DIR).glob("f-5.json"))) == 1
