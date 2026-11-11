#!/usr/bin/env python3
"""Registry of the Telegram AI components installed on this host.

Every component (the relay, ClaudeAsk, CodexAsk, the two Telegram bridges)
records where it lives in one small JSON file when its setup.sh runs, and
looks its neighbours up there instead of guessing directory names:

    ~/.config/telegram-ai/registry.json
    {"version": 1, "components": {"claude-bridge": {"path": "/srv/cb", ...}}}

The file format is the contract. It is deliberately trivial so that a
component which does not ship this module (the bridges are fully standalone)
can read and write it with a dozen lines of its own.

Component names: core, relay, claude-ask, codex-ask, claude-bridge,
codex-bridge. Every entry has "path" (its checkout); other fields are
component-specific and documented in the README.

CLI:
    registry.py register NAME key=value [key=value ...]   merge fields into NAME
    registry.py unregister NAME
    registry.py get NAME [KEY]                             exit 1 if absent
    registry.py list
    registry.py file                                       path of the registry
"""
import contextlib
import fcntl
import json
import os
import re
import sys
import tempfile

REGISTRY_VERSION = 1
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def config_dir():
    """The per-user configuration directory shared by all components."""
    base = os.environ.get("TELEGRAM_AI_CONFIG_DIR")
    if not base:
        base = os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "telegram-ai",
        )
    return os.path.abspath(os.path.expanduser(base))


def registry_file():
    return os.path.join(config_dir(), "registry.json")


def _read():
    try:
        with open(registry_file(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    components = data.get("components") if isinstance(data, dict) else None
    if not isinstance(components, dict):
        return {}
    return {
        name: dict(entry) for name, entry in components.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


@contextlib.contextmanager
def _locked():
    """Serialise writers: two setup scripts may register at the same moment."""
    directory = config_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(directory, ".registry.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(lock_fd)


def _write(components):
    path = registry_file()
    fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": REGISTRY_VERSION, "components": components},
                handle, indent=2, sort_keys=True, ensure_ascii=False,
            )
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def components():
    """Every registered component: {name: {field: value}}."""
    return _read()


def component(name):
    """One component's fields, or None when it is not registered or its
    checkout has disappeared (a stale entry must not send callers to a
    directory that is no longer there)."""
    entry = _read().get(name)
    if not entry:
        return None
    path = entry.get("path")
    if not isinstance(path, str) or not os.path.isdir(path):
        return None
    return entry


def register(name, **fields):
    """Merge fields into a component's entry; None removes a field."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError(f"invalid component name: {name!r}")
    for key in fields:
        if not _KEY_RE.fullmatch(key):
            raise ValueError(f"invalid field name: {key!r}")
    with _locked():
        current = _read()
        entry = current.get(name, {})
        for key, value in fields.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise ValueError("a component needs a 'path'")
        entry["path"] = os.path.abspath(os.path.expanduser(entry["path"]))
        current[name] = entry
        _write(current)
    return entry


def unregister(name):
    with _locked():
        current = _read()
        if current.pop(name, None) is not None:
            _write(current)
            return True
    return False


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0] if argv else ""
    if command == "file" and len(argv) == 1:
        print(registry_file())
        return 0
    if command == "list" and len(argv) == 1:
        for name, entry in sorted(components().items()):
            print(f"{name}\t{entry.get('path', '')}")
        return 0
    if command == "get" and len(argv) in (2, 3):
        entry = component(argv[1])
        if entry is None:
            return 1
        if len(argv) == 2:
            print(json.dumps(entry, ensure_ascii=False, sort_keys=True))
            return 0
        value = entry.get(argv[2])
        if value is None:
            return 1
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
        return 0
    if command == "register" and len(argv) >= 3:
        fields = {}
        for assignment in argv[2:]:
            key, separator, value = assignment.partition("=")
            if not separator:
                print(f"registry: expected key=value, got {assignment!r}", file=sys.stderr)
                return 2
            fields[key] = value
        try:
            register(argv[1], **fields)
        except ValueError as exc:
            print(f"registry: {exc}", file=sys.stderr)
            return 2
        return 0
    if command == "unregister" and len(argv) == 2:
        unregister(argv[1])
        return 0
    print(__doc__.strip().split("CLI:")[1].strip() if "CLI:" in __doc__ else __doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
