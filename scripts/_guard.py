#!/usr/bin/env python3
"""
_guard.py - Shared safety helpers for the untangler scripts.

Everything here exists to keep the state machine honest and the scripts inert:
- state lives only in a real `.untangler/` directory inside the current repo
  (no symlinks pointing elsewhere);
- git refs read from state are validated before reaching the git command line;
- writes are atomic, so a crash never leaves half-written JSON;
- free text written to logs is reduced to a single safe line;
- human-only actions (sign-off, override) need an interactive terminal.

The gates are guard rails against drift, not a lock against a hostile agent
with shell access. See SECURITY.md for the threat model.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

STATE_DIR = Path(".untangler")
STATE_JSON = STATE_DIR / "state.json"
AUDIT_JSON = STATE_DIR / "audit.json"
STATE_MD = STATE_DIR / "STATE.md"
OVERRIDES_LOG = STATE_DIR / "overrides.log"

MAGIC_PHRASE = "I understand the risk, override gate."

# Conservative git ref name: must start with an alphanumeric (so it can never be
# parsed as an option such as --output=...), no "..", no "@{", no spaces.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")

# Keys that only dedicated commands may change. `update_state.py set` refuses them.
PROTECTED_TOP_LEVEL = {
    "schema_version",
    "started_at",
    "current_phase",
    "phases",
    "top_5_signed_off_at",
    "top_5_signoff",
    "overrides",
    "last_test_run",
}
PROTECTED_PATHS = {"baseline.git_tag"}


class GuardError(Exception):
    """Raised when a safety check fails. Callers print it and exit non-zero."""


def fail(message, code=2):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def check_state_dir(create=False):
    """Refuse to work with a `.untangler` that is a symlink or escapes the repo."""
    if STATE_DIR.is_symlink():
        raise GuardError(f"{STATE_DIR} is a symlink; refusing to read or write through it.")
    if create:
        STATE_DIR.mkdir(exist_ok=True)
    if STATE_DIR.exists():
        if not STATE_DIR.is_dir():
            raise GuardError(f"{STATE_DIR} exists but is not a directory.")
        cwd = Path.cwd().resolve()
        resolved = STATE_DIR.resolve()
        if resolved.parent != cwd:
            raise GuardError(f"{STATE_DIR} resolves outside the current directory ({resolved}).")


def check_regular_file(path):
    if path.is_symlink():
        raise GuardError(f"{path} is a symlink; refusing to use it.")
    if path.exists() and not path.is_file():
        raise GuardError(f"{path} is not a regular file.")


def atomic_write_text(path, text):
    """Write via a temp file in the same directory, then rename over the target."""
    check_state_dir()
    check_regular_file(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_line(path, line):
    check_state_dir()
    check_regular_file(path)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def load_json(path, required=True):
    check_state_dir()
    if not path.exists():
        if required:
            raise GuardError(f"{path} not found. Run init_state.py first.")
        return None
    check_regular_file(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GuardError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GuardError(f"{path} must contain a JSON object.")
    return data


def validate_ref(name):
    """Return `name` if it is a safe git ref, else raise."""
    if not isinstance(name, str) or not _REF_RE.match(name) or ".." in name or "@{" in name \
            or name.endswith((".", "/", ".lock")):
        raise GuardError(f"Unsafe or invalid git ref name in state: {name!r}")
    return name


def one_line(text, limit=300):
    """Collapse free text to one printable line (prevents log/markdown injection)."""
    text = "" if text is None else str(text)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def is_protected(path):
    top = path.split(".", 1)[0]
    return top in PROTECTED_TOP_LEVEL or path in PROTECTED_PATHS


def require_human(prompt, expected):
    """
    Ask the person at an interactive terminal to type `expected` exactly.

    Agent tool shells normally have no controlling terminal, so this cannot be
    satisfied by a non-interactive command. Returns True only on an exact match.
    """
    if not sys.stdin.isatty():
        raise GuardError("This action is human-only: run it yourself in an interactive terminal.")
    if not sys.stdout.isatty():
        raise GuardError("This action is human-only: run it yourself in an interactive terminal.")
    try:
        # Prefer the controlling terminal; fall back to the (verified TTY) stdin/stdout.
        try:
            tty = open("/dev/tty", "r+", encoding="utf-8")
        except OSError:
            tty = None
        if tty is not None:
            with tty:
                tty.write(prompt + "\n> ")
                tty.flush()
                answer = tty.readline()
        else:
            sys.stdout.write(prompt + "\n> ")
            sys.stdout.flush()
            answer = sys.stdin.readline()
    except OSError as exc:
        raise GuardError("This action is human-only: no interactive terminal available.") from exc
    return answer.rstrip("\r\n") == expected
