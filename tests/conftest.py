"""Keep every test run away from the real relay state, credentials and registry."""
import os
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="telegram-ai-core-tests-")
os.environ["JARVIS_STATE_DIR"] = os.path.join(_SANDBOX, "state")
os.environ["TELEGRAM_AI_CONFIG_DIR"] = os.path.join(_SANDBOX, "config")
os.environ.pop("JARVIS_RELAY_CONFIG_DIR", None)
for _name in (
    "JARVIS_RELAY_TOKENS_FILE", "JARVIS_RELAY_OWNERS_FILE", "JARVIS_RELAY_TOKEN",
    "JARVIS_RELAY_TOKENS_JSON", "JARVIS_PERSONA_DIR", "JARVIS_XPERSONA_DIR",
    "JARVIS_OWNER_NAME", "JARVIS_OWNER_TG_ID",
):
    os.environ.pop(_name, None)
