"""`.stop` / `.xstop`: abandoning a request in flight, on both flavors."""
import json
import types
from unittest.mock import AsyncMock, Mock

from test_module_config import WorkMessage, ask, message, run  # noqa: F401  (ask is a fixture)


def _configure(ask):
    run(ask.command("asknet")(message("token t")))
    run(ask.command("asknet")(message("local mybot")))


def _command(chat_id=1000, reply_to=None):
    command = message(chat_id=chat_id)
    command.reply_to_msg_id = reply_to
    command.delete = AsyncMock()
    return command


def test_nothing_to_stop(ask):
    _configure(ask)
    command = _command()
    run(ask.command("stop")(command))
    assert "нечего останавливать" in ask.work.last
    command.delete.assert_not_called()


def test_stop_cancels_on_the_relay_then_abandons_the_wait(ask):
    _configure(ask)
    module, bot = ask.module, ask.bot
    seen = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status": "ok", "state": "cancelling"}'

    bot._relay_open = lambda request, timeout: seen.append((request.full_url, json.loads(request.data))) or Response()
    bot._active_requests = {
        "here-1": {"chat_id": "1000", "work_message_id": 11},
        "here-2": {"chat_id": "1000", "work_message_id": 12},
        "elsewhere": {"chat_id": "2000", "work_message_id": 13},
    }
    command = _command(chat_id=1000)
    run(ask.command("stop")(command))

    assert bot._cancelled_requests == {"here-1", "here-2"}
    assert sorted(body["request_id"] for _, body in seen) == ["here-1", "here-2"]
    assert all(url == f"http://127.0.0.1:9092/{ask.prefix}cancel" for url, _ in seen)
    assert all(body["instance_id"] == "mybot" for _, body in seen)
    command.delete.assert_awaited_once()

    # The wait loop gives up at once, with the sentinel rather than an answer.
    bot._fetch_ask_status = Mock(side_effect=AssertionError("must not poll a stopped request"))
    work = WorkMessage()
    _, answer, thoughts, images = run(bot._poll_progress_and_result(work, "here-1"))
    assert answer is module.CANCELLED and thoughts == [] and images == []
    answer, _ = run(bot._poll_result_silent("here-2", timeout_s=3))
    assert answer is module.CANCELLED


def test_reply_stops_only_that_request(ask):
    _configure(ask)
    bot = ask.bot
    bot._cancel_on_relay = Mock(return_value={"status": "ok"})
    bot._active_requests = {
        "first": {"chat_id": "1000", "work_message_id": 11},
        "second": {"chat_id": "1000", "work_message_id": 12},
    }
    run(ask.command("stop")(_command(chat_id=1000, reply_to=12)))
    assert bot._cancelled_requests == {"second"}
    # A reply to something that is not a pending request stops the whole chat.
    run(ask.command("stop")(_command(chat_id=1000, reply_to=999)))
    assert bot._cancelled_requests == {"first", "second"}


def test_unconfirmed_backend_stop_is_reported_honestly(ask):
    _configure(ask)
    module, bot = ask.module, ask.bot
    bot._cancel_on_relay = Mock(side_effect=OSError("relay unreachable"))
    bot._active_requests = {"r": {"chat_id": "1000", "work_message_id": 11}}
    run(ask.command("stop")(_command(chat_id=1000)))
    assert "r" in bot._cancelled_requests
    assert bot._finish_request("r").get("cancel_error") is True
    assert "r" not in bot._cancelled_requests and "r" not in bot._active_requests
    assert module._t("stopped_unconfirmed") != module._t("stopped")


def test_a_cancelled_result_from_the_backend_is_not_an_answer(ask):
    _configure(ask)
    module, bot = ask.module, ask.bot
    bot._fetch_ask_status = Mock(return_value={
        "request_id": "r", "done": True, "cancelled": True, "answer": "",
    })
    module_sleep = AsyncMock()
    original, module.asyncio.sleep = module.asyncio.sleep, module_sleep
    try:
        _, answer, _, _ = run(bot._poll_progress_and_result(WorkMessage(), "r"))
        assert answer is module.CANCELLED
        answer, _ = run(bot._poll_result_silent("r", timeout_s=2))
        assert answer is module.CANCELLED
    finally:
        module.asyncio.sleep = original
