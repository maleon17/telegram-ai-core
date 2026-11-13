"""Interface language of the userbot modules."""
import asyncio
import json
import re
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

import test_module_claude_triggers as claude_harness
import test_module_codex_triggers as codex_harness

ROOT = Path(__file__).resolve().parents[1]
FLAVORS = {
    "claude": (claude_harness.claude_ask, "ClaudeAsk", ""),
    "codex": (codex_harness.codex_ask, "CodexAsk", "x"),
}


class PointerDB(claude_harness.FakeDB):
    def __init__(self):
        super().__init__()
        self.pointers = {}

    def pointer(self, namespace, key, default):
        return self.pointers.setdefault((namespace, key), default)


@pytest.fixture(params=sorted(FLAVORS))
def ask(request, monkeypatch):
    module, class_name, prefix = FLAVORS[request.param]
    monkeypatch.setattr(module, "LANG", module.LANG)
    bot = getattr(module, class_name)()
    bot.db = PointerDB()
    return types.SimpleNamespace(module=module, bot=bot, prefix=prefix)


def run(awaitable):
    return asyncio.run(awaitable)


def test_every_locale_has_the_reference_keys_and_placeholders():
    locales = {path.stem: json.loads(path.read_text(encoding="utf-8"))
               for path in (ROOT / "module" / "locales").glob("*.json")}
    assert set(locales) == {"en", "ru", "uk", "kk", "de"}
    placeholder = re.compile(r"\{(\w+)\}")
    for language, strings in locales.items():
        assert set(strings) == set(locales["en"]), language
        for key, text in strings.items():
            assert set(placeholder.findall(text)) == set(placeholder.findall(locales["en"][key])), (language, key)


def test_the_setting_switches_texts_and_help(ask):
    module, bot = ask.module, ask.bot
    bot.config["language"] = "en"
    bot._apply_language()
    assert module._t("stopped") == "⏹ Stopped."
    command = getattr(type(bot), ask.prefix + "ask")
    assert command.__doc__.startswith("<question>") and bot.__doc__.startswith("Jarvis in your chats")
    bot.config["language"] = "de"
    assert bot._apply_language() is True
    assert module._t("stopped") != "⏹ Stopped." and module.LANG == "de"
    assert bot._apply_language() is False  # unchanged


def test_auto_follows_the_userbot_language_and_falls_back_to_english(ask):
    module, bot = ask.module, ask.bot
    bot.config["language"] = "auto"
    bot.db.set("heroku.translations", "lang", "ua")
    assert bot._resolve_language() == "uk"
    bot.db.set("heroku.translations", "lang", "ja")
    assert bot._resolve_language() == "en"


def test_a_missing_string_falls_back_to_english(ask, monkeypatch):
    module = ask.module
    monkeypatch.setitem(module._LOCALES, "de", {})
    monkeypatch.setattr(module, "LANG", "de")
    assert module._t("stopped") == "⏹ Stopped."


def test_markers_are_aligned_and_present_in_every_language(ask):
    markers = ask.module._persona_markers()
    lengths = {len(values) for values in markers.values()}
    assert len(lengths) == 1 and all(all(values) for values in markers.values())
    assert markers["ru"][0] == "🎭 Стикер" and markers["en"][0] == "🎭 Sticker"
    assert markers["ru"][4] == "ИСТОРИЯ ЕЩЁ НЕ ПРОЧИТАНА" and markers["en"][4] == "HISTORY NOT READ YET"


def test_topics_found_under_another_language_are_renamed_not_duplicated(ask, monkeypatch):
    module, bot = ask.module, ask.bot
    monkeypatch.setattr(module, "LANG", "en")
    entity = types.SimpleNamespace(title="heroku-content")
    cache = bot.db.pointer("heroku.forums", "forums_cache", {})
    cache["heroku-content"] = {"🛡 ClaudeAsk: Модерация": 7, "💬 ClaudeAsk: Trash": 9}
    renamed = []
    bot._rename_topic = AsyncMock(side_effect=lambda entity, topic_id, title: renamed.append((topic_id, title)))

    async def scenario():
        bot._rename_topics(entity)
        await asyncio.sleep(0)

    run(scenario())
    assert cache["heroku-content"] == {"🛡 ClaudeAsk: Moderation": 7, "💬 ClaudeAsk: Trash": 9}
    assert renamed == [(7, "🛡 ClaudeAsk: Moderation")]


def _configure(module, monkeypatch):
    for name, value in (("BACKEND_URL", "http://relay"), ("INSTANCE_ID", "andrey"), ("RELAY_TOKEN", "t")):
        monkeypatch.setattr(module, name, value)


def test_a_translation_is_reported_once_and_remembered(ask, monkeypatch):
    module, bot = ask.module, ask.bot
    _configure(module, monkeypatch)
    monkeypatch.setattr(module, "LANG", "de")
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    replies = iter([
        {"status": "translating", "job": "j"},
        {"status": "translating", "job": "j"},
        {"status": "ok", "action": "translated"},
    ])
    bot._persona_http = Mock(side_effect=lambda *args: next(replies))
    note = types.SimpleNamespace(edit=AsyncMock())
    bot._client = types.SimpleNamespace(send_message=AsyncMock(return_value=note))
    run(bot._sync_persona_language())
    method, path, payload = bot._persona_http.call_args_list[0].args
    assert (method, path) == ("POST", f"/{ask.prefix}persona_language")
    assert payload["language"] == "de" and set(payload["markers"]) == set(module.LANGUAGES)
    assert bot._client.send_message.await_count == 1  # the progress note...
    assert note.edit.await_count == 1                  # ...edited into the result
    assert bot.db.get(bot.strings["name"], "persona_language") == "de"
    run(bot._sync_persona_language())  # done for this language: nothing more
    assert bot._persona_http.call_count == 3


def test_an_interrupted_translation_is_retried_later(ask, monkeypatch):
    module, bot = ask.module, ask.bot
    _configure(module, monkeypatch)
    monkeypatch.setattr(module, "LANG", "uk")
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    replies = iter([{"status": "translating", "job": "j"}, {"status": "error", "code": "job_lost"}])
    bot._persona_http = Mock(side_effect=lambda *args: next(replies))
    bot._client = types.SimpleNamespace(send_message=AsyncMock(return_value=None))
    run(bot._sync_persona_language())
    assert bot.db.get(bot.strings["name"], "persona_language") is None


def test_a_quiet_outcome_sends_nothing(ask, monkeypatch):
    module, bot = ask.module, ask.bot
    _configure(module, monkeypatch)
    monkeypatch.setattr(module, "LANG", "en")
    bot._persona_http = Mock(return_value={"status": "ok", "action": "template"})
    bot._client = types.SimpleNamespace(send_message=AsyncMock())
    run(bot._sync_persona_language())
    bot._client.send_message.assert_not_awaited()
    assert bot.db.get(bot.strings["name"], "persona_language") == "en"


def test_relay_codes_are_shown_in_the_interface_language(ask, monkeypatch):
    module, bot = ask.module, ask.bot
    monkeypatch.setattr(module, "LANG", "en")
    reply = {"status": "ok", "code": "relay_session_reset", "values": {"chat": "5"}, "message": "Сессия чата 5 сброшена"}
    assert bot._relay_message(reply, "new_session") == "The session of chat 5 was reset"
    assert bot._relay_message({"message": "старое"}, "new_session") == "старое"
    assert bot._relay_message({}, "new_session") == "New session"
