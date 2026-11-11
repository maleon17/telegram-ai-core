#!/usr/bin/env python3
"""Build the ClaudeAsk and CodexAsk userbot modules from one source.

Heroku loads a module as a single file, and the two products differ only in
names (module, commands, relay endpoints, environment prefix) and in a few
backend-specific lines. Keeping them as two hand-edited files let them drift;
they are now generated from ask_module.py.tmpl:

    @@TOKEN@@                      replaced per flavor (see FLAVORS)
    #@if claude | #@if codex       keep the following lines for that flavor only
    #@else / #@endif

    build.py            write dist/claude_ask.py and dist/codex_ask.py
    build.py --check    exit 1 if dist/ is not what the template produces
    build.py --install  also copy each module into its product checkout
                        (found through the component registry or as a sibling)

Never edit the generated files: change the template and rebuild.
"""
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "ask_module.py.tmpl")
DIST = os.path.join(HERE, "dist")

FLAVORS = {
    "claude": {
        "OUTPUT": "claude_ask.py",
        "MODULE": "ClaudeAsk",
        "X": "",
        "ENGINE": "claude",
        "ENV": "CLAUDE_JARVIS",
        "BACKEND": "Claude Code",
        "WATCHER": "claude_watcher.py",
        "VIA": "",
        "PRODUCT": "claude-ask",
        "CHECKOUTS": ("claude-ask", "claude-jarvis"),
    },
    "codex": {
        "OUTPUT": "codex_ask.py",
        "MODULE": "CodexAsk",
        "X": "x",
        "ENGINE": "codex",
        "ENV": "CODEX_JARVIS",
        "BACKEND": "Codex",
        "WATCHER": "codex_ask_watcher.py",
        "VIA": " (Codex)",
        "PRODUCT": "codex-ask",
        "CHECKOUTS": ("codex-ask", "codex-jarvis"),
    },
}
_TOKEN_RE = re.compile(r"@@([A-Z_]+)@@")
_DIRECTIVE_RE = re.compile(r"^\s*#@(if|else|endif)\b\s*(\w*)\s*$")


class TemplateError(Exception):
    pass


def render(template, flavor):
    """One flavor's module source. Raises TemplateError on a malformed template."""
    values = FLAVORS[flavor]
    output = []
    branch = None  # None outside a block, else True/False = "these lines are kept"
    for number, line in enumerate(template.splitlines(), 1):
        directive = _DIRECTIVE_RE.match(line)
        if directive:
            keyword, argument = directive.groups()
            if keyword == "if":
                if branch is not None:
                    raise TemplateError(f"line {number}: nested #@if is not supported")
                if argument not in FLAVORS:
                    raise TemplateError(f"line {number}: unknown flavor {argument!r}")
                branch = argument == flavor
            elif branch is None:
                raise TemplateError(f"line {number}: #@{keyword} without #@if")
            elif keyword == "else":
                branch = not branch
            else:
                branch = None
            continue
        if branch is False:
            continue

        def substitute(match):
            name = match.group(1)
            if name not in values or not isinstance(values[name], str):
                raise TemplateError(f"line {number}: unknown token @@{name}@@")
            return values[name]

        output.append(_TOKEN_RE.sub(substitute, line))
    if branch is not None:
        raise TemplateError("unterminated #@if at end of template")
    digest = hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]
    banner = (
        f"# GENERATED FILE -- do not edit. Built from telegram-ai-core "
        f"module/ask_module.py.tmpl (sha256 {digest});\n"
        f"# change the template there and run module/build.py.\n"
    )
    source = banner + "\n".join(output) + "\n"
    compile(source, values["OUTPUT"], "exec")  # a syntax error fails the build
    return source


def build():
    with open(TEMPLATE, encoding="utf-8") as handle:
        template = handle.read()
    return {values["OUTPUT"]: render(template, flavor) for flavor, values in FLAVORS.items()}


def _write(path, text):
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def _product_checkout(values):
    """Where a product's repository lives: the registry first, then a sibling."""
    sys.path.insert(0, os.path.dirname(HERE))
    try:
        import registry
        entry = registry.component(values["PRODUCT"])
    except Exception:
        entry = None
    candidates = [entry["path"]] if entry else []
    parent = os.path.dirname(os.path.dirname(HERE))
    candidates += [os.path.join(parent, name) for name in values["CHECKOUTS"]]
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, values["OUTPUT"])):
            return candidate
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(argument not in ("--check", "--install") for argument in argv):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        modules = build()
    except (TemplateError, SyntaxError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    if "--check" in argv:
        stale = []
        for name, text in modules.items():
            try:
                with open(os.path.join(DIST, name), encoding="utf-8") as handle:
                    if handle.read() != text:
                        stale.append(name)
            except FileNotFoundError:
                stale.append(name)
        if stale:
            print("stale (run module/build.py): " + ", ".join(stale), file=sys.stderr)
            return 1
        print("dist/ is up to date")
        return 0
    os.makedirs(DIST, exist_ok=True)
    for name, text in modules.items():
        changed = _write(os.path.join(DIST, name), text)
        print(f"{'wrote' if changed else 'unchanged'} dist/{name}")
    if "--install" in argv:
        for values in FLAVORS.values():
            checkout = _product_checkout(values)
            if checkout is None:
                print(f"{values['PRODUCT']}: no checkout found, skipped")
                continue
            changed = _write(os.path.join(checkout, values["OUTPUT"]), modules[values["OUTPUT"]])
            print(f"{'installed' if changed else 'unchanged'} {os.path.join(checkout, values['OUTPUT'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
