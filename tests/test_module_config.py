"""Module settings: config-backed, migrated from the old layout, never assumed."""
import asyncio
import json
import types
import urllib.error
from unittest.mock import AsyncMock, Mock

import pytest

import test_module_claude_triggers as claude_harness
import test_module_codex_triggers as codex_harness

FLAVORS = {
    "claude": (claude_harness.claude_ask, "ClaudeAsk", ""),
    "codex": (codex_harness.codex_ask, "CodexAsk", "x"),
}
SETTINGS = (
    "BACKEND_URL", "HTTP_PROXY", "INSTANCE_ID", "RELAY_TOKEN", "RELAY_OPENER",
    "TEST_CHANNEL_BOT_ID", "MISTRAL_API_KEY",
)


class WorkMessage:
    def __init__(self):
        self.id = 9
        self.edits = []

    async def edit(self, text, **kwargs):
        self.edits.append(text)

    @property
    def last(self):
        return self.edits[-1]


@pytest.fixture(params=sorted(FLAVORS))
def ask(request, monkeypatch):
    """An unconfigured module instance of one flavor, with every module-level
    setting restored afterwards."""
    module, class_name, prefix = FLAVORS[request.param]
    for name in SETTINGS:
        monkeypatch.setattr(module, name, getattr(module, name))
    for name in ("_ENV_BACKEND_URL", "_ENV_HTTP_PROXY", "_ENV_INSTANCE_ID", "_ENV_RELAY_TOKEN"):
        monkeypatch.setattr(module, name, "")
    monkeypatch.setattr(module, "_load_mistral_api_key", lambda: "")
    monkeypatch.setattr(module.utils, "get_args_raw", lambda message: message.args, raising=False)
    bot = getattr(module, class_name)()
    bot.db = claude_harness.FakeDB()
    bot._owner_id_cache = 1000
    bot._ensure_topics = AsyncMock()
    bot._apply_config()
    work = WorkMessage()
    bot._work_message = AsyncMock(return_value=work)
    return types.SimpleNamespace(
        module=module, bot=bot, work=work, prefix=prefix, name=class_name,
        command=lambda name: getattr(bot, prefix + name),
    )


def run(awaitable):
    return asyncio.run(awaitable)


def message(args="", sender_id=1000, chat_id=1000):
    return types.SimpleNamespace(args=args, sender_id=sender_id, chat_id=chat_id, id=5)


def test_fresh_module_assumes_nothing_and_says_how_to_set_up(ask):
    module, bot = ask.module, ask.bot
    assert (module.BACKEND_URL, module.INSTANCE_ID, module.RELAY_TOKEN, module.HTTP_PROXY) == ("", "", "", "")
    assert module.TEST_CHANNEL_BOT_ID == 0 and not module._configured()

    bot._enqueue = Mock()
    run(bot._do_ask(message(), "hello"))
    bot._enqueue.assert_not_called()
    assert f".{ask.prefix}asknet token" in ask.work.last

    bot._fetch_pending_tool_call = Mock()
    run(bot.tool_call_watcher())
    bot._fetch_pending_tool_call.assert_not_called()

    run(ask.command("new")(message()))
    assert f".{ask.prefix}asknet" in ask.work.last


def test_settings_from_the_old_layout_are_carried_over_once(ask):
    module, bot = ask.module, ask.bot
    bot.db.set(ask.name, "network", {
        "instance_id": "mybot", "backend_url": "http://10.0.0.5:9092/",
        "http_proxy": "http://localhost:1056", "relay_token": "old-token",
    })
    run(bot.client_ready())
    assert (module.INSTANCE_ID, module.BACKEND_URL, module.HTTP_PROXY, module.RELAY_TOKEN) == (
        "mybot", "http://10.0.0.5:9092", "http://localhost:1056", "old-token",
    )
    assert bot.config["relay_token"] == "old-token" and module._configured()

    # Once the config holds values, a stale legacy dict no longer overrides them.
    bot.config["relay_token"] = "new-token"
    run(bot.client_ready())
    assert module.RELAY_TOKEN == "new-token"


def test_asknet_modes(ask):
    module, bot, asknet = ask.module, ask.bot, ask.command("asknet")

    run(asknet(message("token secret-token")))
    assert module.RELAY_TOKEN == "secret-token" and "secret-token" not in ask.work.last

    run(asknet(message("local mybot")))
    assert (module.INSTANCE_ID, module.BACKEND_URL, module.HTTP_PROXY) == ("mybot", "http://127.0.0.1:9092", "")
    assert module._configured() and "secret-token" not in ask.work.last

    run(asknet(message("tailnet mybot http://100.64.0.1:9092")))
    assert (module.BACKEND_URL, module.HTTP_PROXY) == ("http://100.64.0.1:9092", "http://localhost:1056")

    run(asknet(message("custom other https://relay.example none")))
    assert (module.INSTANCE_ID, module.BACKEND_URL, module.HTTP_PROXY) == ("other", "https://relay.example", "")

    before = (module.INSTANCE_ID, module.BACKEND_URL)
    for bad in ("local bad/id", "tailnet mybot ftp://nope", "local", "nonsense x"):
        run(asknet(message(bad)))
        assert (module.INSTANCE_ID, module.BACKEND_URL) == before, bad

    run(asknet(message("")))
    assert "instance_id: <code>other</code>" in ask.work.last and "secret-token" not in ask.work.last

    run(asknet(message("testbot 777")))
    assert module.TEST_CHANNEL_BOT_ID == 777
    run(asknet(message("testbot off")))
    assert module.TEST_CHANNEL_BOT_ID == 0


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._payload


def test_asknet_check_reports_what_is_actually_wrong(ask):
    module, bot, asknet = ask.module, ask.bot, ask.command("asknet")
    run(asknet(message("check")))
    assert "asknet token" in ask.work.last  # not configured yet

    run(asknet(message("token t")))
    run(asknet(message("local mybot")))

    bot._relay_open = lambda request, timeout: _Response({"status": "ok", "instance_id": "mybot"})
    run(asknet(message("check")))
    assert ask.work.last.startswith("✅")

    bot._relay_open = lambda request, timeout: _Response({"status": "ok", "instance_id": "someone-else"})
    run(asknet(message("check")))
    assert ask.work.last.startswith("❌") and "someone-else" in ask.work.last

    def unauthorized(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    bot._relay_open = unauthorized
    run(asknet(message("check")))
    assert ask.work.last.startswith("❌") and "401" in ask.work.last

    def unreachable(request, timeout):
        raise OSError("connection refused")

    bot._relay_open = unreachable
    run(asknet(message("check")))
    assert ask.work.last.startswith("❌") and "connection refused" in ask.work.last


def test_test_channel_is_off_by_default_and_scoped_to_its_own_chat(ask):
    module, bot = ask.module, ask.bot
    stranger_bot = 555
    assert run(bot._requester_for(message(sender_id=stranger_bot, chat_id=stranger_bot))) == stranger_bot

    bot.config["test_bot_id"] = stranger_bot
    bot._apply_config()
    assert run(bot._requester_for(message(sender_id=stranger_bot, chat_id=stranger_bot))) == 1000
    # The same bot writing in a group is an ordinary sender.
    assert run(bot._requester_for(message(sender_id=stranger_bot, chat_id=-100123))) == stranger_bot
    assert run(bot._requester_for(message(sender_id=42, chat_id=stranger_bot))) == 42

    # No code path grants tools to a bare bot id any more.
    assert run(bot._tool_request_is_authorized(
        str(stranger_bot), "1000", "send_message", {"target": "x"}, owner_authorized=None,
    )) is False
