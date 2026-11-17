#!/usr/bin/env python3
"""
update_state.py - Update fields in state.json and regenerate STATE.md mirror.

Usage:
  python -m scripts.update_state set <path.to.field> <value>
  python -m scripts.update_state advance <phase>          # mark phase done, advance to next
  python -m scripts.update_state override <phase> <gate> "reason"   # HUMAN ONLY (needs a terminal)
  python -m scripts.update_state signoff                  # HUMAN ONLY: sign off the Top 5
  python -m scripts.update_state record-tests <passing> <failing>
  python -m scripts.update_state regen                    # just regenerate STATE.md from JSON

Safety rules enforced here:
  - `set` cannot touch workflow-control fields (phase, phases, sign-off, overrides,
    last test run, baseline tag). Those change only through dedicated commands.
  - `advance` refuses unless the phase gate passes or a human override was recorded.
  - `signoff` and `override` require an interactive terminal; agent shells have none.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guard  # noqa: E402

STATE_DIR = _guard.STATE_DIR
STATE_JSON = _guard.STATE_JSON
STATE_MD = _guard.STATE_MD
OVERRIDES_LOG = _guard.OVERRIDES_LOG

_PATH_RE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,5}$")


def load():
    try:
        return _guard.load_json(STATE_JSON)
    except _guard.GuardError as exc:
        _guard.fail(str(exc))


def save(state):
    _guard.atomic_write_text(STATE_JSON, json.dumps(state, indent=2) + "\n")
    _guard.atomic_write_text(STATE_MD, render_md(state))


def render_md(state):
    lines = [
        "# Codebase Untangler - State",
        "",
        f"**Started**: {state['started_at']}",
        f"**Current phase**: {state['current_phase']} ({state['phases'][str(state['current_phase'])]['name']})",
        f"**Branch**: {state['baseline']['branch'] or '(not set)'}",
        f"**Stack**: {', '.join(state['stack']) if state['stack'] else '(not detected)'}",
        "",
        "## Phase progress",
        ""
    ]
    for i in range(6):
        p = state["phases"][str(i)]
        marker = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}.get(p["status"], "[?]")
        lines.append(f"- {marker} Phase {i}: {p['name']} ({p['status']})")
    lines.extend([
        "",
        "## Baseline",
        "",
        f"- Total LOC: {state['baseline']['total_loc'] or 'tbd'}",
        f"- File count: {state['baseline']['file_count'] or 'tbd'}",
        f"- Tests: {state['baseline']['test_count'] or 'tbd'} "
        f"({state['baseline']['tests_passing'] or 0} passing, {state['baseline']['tests_failing'] or 0} failing)",
        "",
        "## Overrides used",
        ""
    ])
    if state["overrides"]:
        for o in state["overrides"]:
            lines.append(f"- {_guard.one_line(o.get('timestamp'))} Phase {_guard.one_line(o.get('phase'))}: "
                         f"{_guard.one_line(o.get('gate'))} - {_guard.one_line(o.get('reason'))}")
    else:
        lines.append("(none)")
    lines.extend(["", "---", "*Auto-generated from state.json.*"])
    return "\n".join(lines) + "\n"


def set_nested(d, path, value):
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    # try to parse value as number/bool/null
    try:
        value = json.loads(value)
    except (ValueError, TypeError):
        pass
    d[keys[-1]] = value


def cmd_set(args):
    if len(args) != 2:
        print("Usage: set <path> <value>")
        sys.exit(2)
    if not _PATH_RE.match(args[0]):
        _guard.fail(f"Invalid field path: {args[0]!r}")
    if _guard.is_protected(args[0]):
        _guard.fail(f"'{args[0]}' is a workflow-control field and cannot be changed with `set`. "
                    "Use advance / signoff / override / record-tests.")
    state = load()
    set_nested(state, args[0], args[1])
    save(state)
    print(f"Set {args[0]} = {args[1]}")


def cmd_advance(args):
    if len(args) != 1:
        print("Usage: advance <phase>")
        sys.exit(2)
    phase = args[0]
    state = load()
    if phase not in state["phases"]:
        print(f"Unknown phase: {phase}")
        sys.exit(2)
    if int(phase) != state.get("current_phase"):
        _guard.fail(f"Phase {phase} is not the current phase ({state.get('current_phase')}). "
                    "Phases advance in order.")
    import check_gate  # same directory; imported lazily to keep `set` cheap
    overridden = any(str(o.get("phase")) == phase for o in state.get("overrides", []))
    if not check_gate.gate_passes(int(phase), state) and not overridden:
        _guard.fail(f"Phase {phase} gate does not pass. Run check_gate.py {phase}; "
                    "only a human override can skip it.", code=1)
    state["phases"][phase]["status"] = "done"
    state["phases"][phase]["completed_at"] = datetime.now(timezone.utc).isoformat()
    next_phase = str(int(phase) + 1)
    if next_phase in state["phases"]:
        state["current_phase"] = int(next_phase)
        state["phases"][next_phase]["status"] = "in_progress"
        state["phases"][next_phase]["started_at"] = datetime.now(timezone.utc).isoformat()
    save(state)
    print(f"Phase {phase} marked done. Now at phase {state['current_phase']}.")


def cmd_override(args):
    if len(args) != 3:
        print('Usage: override <phase> <gate> "<reason>"')
        sys.exit(2)
    phase, gate, reason = (_guard.one_line(a) for a in args)
    if phase not in {str(i) for i in range(6)}:
        _guard.fail(f"Unknown phase: {phase}")
    if not gate or not reason:
        _guard.fail("Gate and reason must be non-empty.")
    state = load()
    try:
        confirmed = _guard.require_human(
            f'Override phase {phase} gate "{gate}".\nType exactly: {_guard.MAGIC_PHRASE}',
            _guard.MAGIC_PHRASE)
    except _guard.GuardError as exc:
        _guard.fail(str(exc), code=1)
    if not confirmed:
        _guard.fail("Phrase did not match exactly. No override recorded.", code=1)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase": phase,
        "gate": gate,
        "reason": reason,
        "via": "interactive",
    }
    state["overrides"].append(entry)
    save(state)
    _guard.append_line(OVERRIDES_LOG,
                       f"{entry['timestamp']} phase={phase} gate={gate} reason={reason}")
    print(f"Override recorded for phase {phase} gate '{gate}'.")


def cmd_signoff(args):
    if args:
        print("Usage: signoff")
        sys.exit(2)
    state = load()
    audit = _guard.load_json(_guard.AUDIT_JSON, required=False) or {}
    top5 = [_guard.one_line(i, 40) for i in audit.get("top_5", [])]
    if len(top5) != 5:
        _guard.fail("audit.json must list exactly 5 Top 5 issue ids before sign-off.", code=1)
    try:
        confirmed = _guard.require_human(
            "Top 5 to be fixed in Phase 3: " + ", ".join(top5) + "\nType exactly: yes",
            "yes")
    except _guard.GuardError as exc:
        _guard.fail(str(exc), code=1)
    if not confirmed:
        _guard.fail("Not confirmed. No sign-off recorded.", code=1)
    now = datetime.now(timezone.utc).isoformat()
    state["top_5_signed_off_at"] = now
    state["top_5_signoff"] = {"at": now, "via": "interactive", "top_5": top5}
    save(state)
    print("Top 5 sign-off recorded.")


def cmd_record_tests(args):
    if len(args) != 2:
        print("Usage: record-tests <passing> <failing>")
        sys.exit(2)
    try:
        passing, failing = int(args[0]), int(args[1])
    except ValueError:
        _guard.fail("passing and failing must be integers")
    if passing < 0 or failing < 0:
        _guard.fail("passing and failing must be >= 0")
    state = load()
    state["baseline"]["tests_passing"] = passing
    state["baseline"]["tests_failing"] = failing
    state["baseline"]["test_count"] = passing + failing
    state["last_test_run"] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "passing": failing == 0,
        "passing_count": passing,
        "failing_count": failing
    }
    save(state)
    print(f"Recorded: {passing} passing, {failing} failing.")


def cmd_regen(args):
    state = load()
    save(state)
    print("STATE.md regenerated.")


COMMANDS = {
    "set": cmd_set,
    "advance": cmd_advance,
    "override": cmd_override,
    "signoff": cmd_signoff,
    "record-tests": cmd_record_tests,
    "regen": cmd_regen,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    try:
        _guard.check_state_dir()
    except _guard.GuardError as exc:
        _guard.fail(str(exc))
    if not STATE_JSON.exists():
        print(f"ERROR: {STATE_JSON} not found. Run init_state.py first.")
        sys.exit(2)
    try:
        COMMANDS[sys.argv[1]](sys.argv[2:])
    except _guard.GuardError as exc:
        _guard.fail(str(exc))


if __name__ == "__main__":
    main()
