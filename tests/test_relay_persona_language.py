"""The persona follows the userbot module's interface language."""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EN = "# Persona\nYou are Jarvis of {{OWNER_NAME}}. \"🎭 Sticker\" lines are stickers.\n"
RU = "# Персона\nТы Jarvis владельца {{OWNER_NAME}}. Строки \"🎭 Стикер\" это стикеры.\n"
CUSTOM = "# Моя персона\nТы мой Jarvis, {{OWNER_NAME}}. Отвечай коротко и по делу, без лишних слов.\n"
MARKERS = {"en": ["🎭 Sticker", "Current time"], "ru": ["🎭 Стикер", "Текущее время"]}


@pytest.fixture
def relay(tmp_path, monkeypatch):
    name = "_cmd_queue_persona_language_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "cmd_queue.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "default.md.example").write_text(EN, encoding="utf-8")
    (personas / "default.ru.md.example").write_text(RU, encoding="utf-8")
    monkeypatch.setenv("JARVIS_PERSONA_DIR", str(personas))
    monkeypatch.setenv("JARVIS_OWNER_NAME", "Andrey")
    monkeypatch.setenv("JARVIS_OWNER_TG_ID", "1")
    for attribute in ("ASK_QUEUE_DIR", "ASK_RESULT_DIR"):
        folder = tmp_path / attribute.lower()
        folder.mkdir()
        monkeypatch.setattr(module, attribute, str(folder))
    module.personas = personas
    return module


def _worker(relay, answer):
    """Stand-in for claude_watcher.py: answers the first queued request."""
    seen = {}

    def run():
        queue_dir = Path(relay.ASK_QUEUE_DIR)
        deadline = time.time() + 5
        while time.time() < deadline:
            for item in queue_dir.glob("*.json"):
                request = json.loads(item.read_text(encoding="utf-8"))
                seen.update(request)
                item.unlink()
                result = Path(relay.ASK_RESULT_DIR) / f"{request['request_id']}.json"
                result.write_text(json.dumps({"done": True, "answer": answer(request)}), encoding="utf-8")
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, seen


def _wait(relay, job):
    deadline = time.time() + 5
    while time.time() < deadline:
        response = relay._persona_language_job("/persona", "andrey", job)
        if response.get("status") != "translating":
            return response
        time.sleep(0.02)
    raise AssertionError("translation did not finish")


def test_without_an_own_persona_the_template_of_the_language_is_served(relay):
    assert relay._persona_set_language("/persona", "andrey", "ru", MARKERS) == {"status": "ok", "action": "template"}
    text, source = relay._persona_read(str(relay.personas), "andrey", ("Andrey", "1"), "ru")
    assert source == "template" and text == RU.replace("{{OWNER_NAME}}", "Andrey")
    text, _ = relay._persona_read(str(relay.personas), "andrey", ("Andrey", "1"), "kk")
    assert text == EN.replace("{{OWNER_NAME}}", "Andrey")  # no Kazakh template: English


def test_an_unedited_copy_of_a_template_is_swapped_for_the_new_language(relay):
    # The editor shows the template with the owner filled in; saving it back
    # unchanged still counts as the default persona.
    (relay.personas / "andrey.md").write_text(RU.replace("{{OWNER_NAME}}", "Andrey"), encoding="utf-8")
    assert relay._persona_set_language("/persona", "andrey", "en", MARKERS)["action"] == "swapped"
    assert not (relay.personas / "andrey.md").exists()


def test_a_persona_from_before_languages_is_adopted_not_translated(relay):
    (relay.personas / "andrey.md").write_text(CUSTOM, encoding="utf-8")
    assert relay._persona_set_language("/persona", "andrey", "ru", MARKERS)["action"] == "adopted"
    assert relay._persona_meta(str(relay.personas), "andrey") == {"language": "ru", "source_language": "ru"}
    assert (relay.personas / "andrey.md").read_text(encoding="utf-8") == CUSTOM
    assert relay._persona_set_language("/persona", "andrey", "ru", MARKERS)["action"] == "unchanged"
    assert not list(Path(relay.ASK_QUEUE_DIR).iterdir())


def test_an_edited_persona_is_translated_from_its_original_and_restored_on_return(relay):
    (relay.personas / "andrey.md").write_text(CUSTOM, encoding="utf-8")
    relay._persona_remember(str(relay.personas), "andrey", "ru", source_text=CUSTOM)
    translated = "# My persona\nYou are my Jarvis, {{OWNER_NAME}}. Answer briefly and to the point, no extra words.\n"
    thread, seen = _worker(relay, lambda request: translated)

    started = relay._persona_set_language("/persona", "andrey", "en", MARKERS)
    assert started["status"] == "translating"
    # A second request for the same switch joins the running job.
    assert relay._persona_set_language("/persona", "andrey", "en", MARKERS) == started
    assert _wait(relay, started["job"]) == {"status": "ok", "action": "translated"}
    thread.join(timeout=2)

    assert seen["mode"] == "persona_translate" and seen["language"] == "en"
    assert "from Russian into English" in seen["question"]
    assert "«🎭 Стикер» -> «🎭 Sticker»" in seen["question"]
    assert "«Таймаут: результат неизвестен» -> «Timeout: result unknown»" in seen["question"]
    assert seen["question"].endswith(CUSTOM)
    assert (relay.personas / "andrey.md").read_text(encoding="utf-8") == translated
    assert relay._persona_meta(str(relay.personas), "andrey")["language"] == "en"
    # The relay forgets a finished job once it has been read.
    assert relay._persona_language_job("/persona", "andrey", started["job"])["code"] == "job_lost"

    assert relay._persona_set_language("/persona", "andrey", "ru", MARKERS) == {"status": "ok", "action": "restored"}
    assert (relay.personas / "andrey.md").read_text(encoding="utf-8") == CUSTOM


def test_a_bad_translation_leaves_the_persona_alone(relay):
    (relay.personas / "andrey.md").write_text(CUSTOM, encoding="utf-8")
    relay._persona_remember(str(relay.personas), "andrey", "ru", source_text=CUSTOM)
    thread, _ = _worker(relay, lambda request: "Sorry")
    started = relay._persona_set_language("/persona", "andrey", "de", MARKERS)
    assert _wait(relay, started["job"])["code"] == "persona_translate_bad"
    thread.join(timeout=2)
    assert (relay.personas / "andrey.md").read_text(encoding="utf-8") == CUSTOM
    assert relay._persona_meta(str(relay.personas), "andrey")["language"] == "ru"


def test_translation_problems_are_recognised(relay):
    original = CUSTOM
    good = "# Meine Persona\nDu bist mein Jarvis, {{OWNER_NAME}}. Antworte kurz und sachlich, ohne viele Worte.\n"
    assert relay._persona_translation_problem(original, good) is None
    assert relay._persona_translation_problem(original, "```\n" + good + "\n```") == "wrapped in a code fence"
    assert relay._persona_translation_problem(original, good.replace("{{OWNER_NAME}}", "Andrey")) == "{{OWNER_NAME}} was lost"
    assert relay._persona_translation_problem(original, good.replace("# ", "")) == "headings were lost"
    assert relay._persona_translation_problem(original, "Du bist Jarvis.") == "too short"


def test_an_unknown_language_and_an_unknown_job_are_refused(relay):
    assert relay._persona_set_language("/persona", "andrey", "fr", MARKERS)["code"] == "bad_language"
    assert relay._persona_language_job("/persona", "andrey", "nope")["code"] == "job_lost"
