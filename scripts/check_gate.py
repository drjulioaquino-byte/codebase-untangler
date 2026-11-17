#!/usr/bin/env python3
"""
check_gate.py <phase> - Validate the exit gate for a given phase.

Exit codes:
  0 - all gate checks passed
  1 - one or more checks failed
  2 - invalid usage or state file missing

Usage:
  python -m scripts.check_gate 0
  python -m scripts.check_gate 1
  etc.
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guard  # noqa: E402

STATE_DIR = _guard.STATE_DIR
STATE_JSON = _guard.STATE_JSON
AUDIT_JSON = _guard.AUDIT_JSON


def load_state():
    try:
        return _guard.load_json(STATE_JSON)
    except _guard.GuardError as exc:
        _guard.fail(str(exc))


def load_audit():
    try:
        return _guard.load_json(AUDIT_JSON, required=False)
    except _guard.GuardError as exc:
        _guard.fail(str(exc))


def baseline_tag(state):
    try:
        return _guard.validate_ref(state["baseline"]["git_tag"])
    except (KeyError, TypeError):
        _guard.fail("state.json has no baseline.git_tag")
    except _guard.GuardError as exc:
        _guard.fail(str(exc))


def git_diff_empty():
    """True if there are no uncommitted changes."""
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == ""


def git_branch():
    result = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def git_tag_exists(tag):
    # `tag` is validated by baseline_tag(); `--` stops option parsing anyway.
    result = subprocess.run(["git", "tag", "-l", "--", tag], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == tag


def running_md_filled():
    """RUNNING.md must contain real instructions, not just the template headings/notes."""
    path = STATE_DIR / "RUNNING.md"
    if path.is_symlink() or not path.is_file():
        return False
    body = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip() and not ln.lstrip().startswith(("#", ">"))
            and ln.strip() != "Document the exact steps to run locally."]
    return len("".join(body)) > 40


def check_phase_0(state):
    checks = []
    checks.append(("App run command documented", running_md_filled()))
    checks.append(("Baseline LOC captured", state["baseline"]["total_loc"] is not None))
    checks.append(("Baseline test count captured", state["baseline"]["test_count"] is not None))
    checks.append(("Stack detected", len(state["stack"]) > 0))
    checks.append(("Baseline git tag exists", git_tag_exists(baseline_tag(state))))
    branch = git_branch()
    checks.append(("On a refactor branch", branch is not None and branch.startswith("refactor/")))
    return checks


def check_phase_1(state):
    audit = load_audit()
    checks = []
    checks.append(("REFACTOR_AUDIT.md exists", Path("REFACTOR_AUDIT.md").exists()))
    checks.append(("REFACTOR_AUDIT.md has substance (>100 lines)",
                   Path("REFACTOR_AUDIT.md").exists() and
                   len(Path("REFACTOR_AUDIT.md").read_text().splitlines()) > 100))
    checks.append(("audit.json has >= 5 issues", audit is not None and len(audit.get("issues", [])) >= 5))
    checks.append(("Top 5 selected in audit.json", audit is not None and len(audit.get("top_5", [])) == 5))
    signoff = state.get("top_5_signoff") or {}
    checks.append(("Top 5 signed off by user (interactive `update_state.py signoff`)",
                   state.get("top_5_signed_off_at") is not None
                   and signoff.get("via") == "interactive"))

    # Verify no code changes happened during audit phase
    # (only state files and audit doc should have changed)
    # Tracked changes since the baseline tag, plus untracked files (git diff alone
    # would not see a brand-new source file). Fail closed if git errors.
    tag = baseline_tag(state)
    diff = subprocess.run(["git", "diff", "--name-only", tag, "--"],
                          capture_output=True, text=True)
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True)
    if diff.returncode != 0 or untracked.returncode != 0:
        checks.append(("No code changes during audit (git diff failed)", False))
        return checks
    changed = [f for f in (diff.stdout + "\n" + untracked.stdout).split("\n") if f]
    allowed = lambda f: f.startswith(".untangler/") or f == "REFACTOR_AUDIT.md"
    code_changes = sorted({f for f in changed if not allowed(f)})
    checks.append((f"No code changes during audit (got {len(code_changes)} unexpected)",
                   len(code_changes) == 0))
    return checks


def check_phase_2(state):
    audit = load_audit()
    checks = []
    if audit is None or not audit.get("top_5"):
        checks.append(("audit.json with Top 5 exists", False))
        return checks

    for issue_id in audit["top_5"]:
        issue = next((i for i in audit["issues"] if i["id"] == issue_id), None)
        if not issue:
            checks.append((f"Issue {issue_id} found in issues list", False))
            continue
        safety = issue.get("safety_net", {})
        checks.append((f"Issue {issue_id} has safety net plan", "type" in safety))
        checks.append((f"Issue {issue_id} safety net verified", safety.get("status") == "verified"))

    checks.append(("At least one safety net deliberately broken-then-fixed",
                   any(i.get("safety_net", {}).get("broken_then_fixed") for i in audit["issues"]
                       if i["id"] in audit["top_5"])))
    return checks


def check_phase_3(state):
    audit = load_audit()
    checks = []
    if audit is None or not audit.get("top_5"):
        checks.append(("audit.json with Top 5 exists", False))
        return checks

    for issue_id in audit["top_5"]:
        issue = next((i for i in audit["issues"] if i["id"] == issue_id), None)
        if not issue:
            continue
        checks.append((f"Issue {issue_id} marked done", issue.get("status") == "done"))

    # Tests must pass (we record the result of the last test run in state.json)
    last_test = state.get("last_test_run", {})
    checks.append(("Last test run passing", last_test.get("passing") is True))
    checks.append(("Git working tree clean", git_diff_empty()))
    return checks


def check_phase_4(state):
    checks = []
    checks.append(("ARCHITECTURE.md exists", Path("ARCHITECTURE.md").exists()))
    # Look for lint/boundary config - this varies by stack so check for common indicators
    boundary_indicators = [
        Path(".eslintrc.json"), Path(".eslintrc.js"), Path("eslint.config.js"),
        Path(".importlinter"), Path("pyproject.toml"),
    ]
    has_lint_config = any(p.exists() for p in boundary_indicators)
    checks.append(("Lint config present", has_lint_config))

    hook_indicators = [Path(".husky"), Path(".pre-commit-config.yaml"), Path(".git/hooks/pre-commit")]
    checks.append(("Pre-commit hooks configured", any(p.exists() for p in hook_indicators)))

    claude_md = Path("CLAUDE.md")
    checks.append(("CLAUDE.md has guard rails section",
                   claude_md.exists() and "Codebase Conventions" in claude_md.read_text()))
    return checks


def check_phase_5(state):
    checks = []
    checks.append(("HANDOVER.md exists", (STATE_DIR / "HANDOVER.md").exists()))
    # Check tag exists
    result = subprocess.run(["git", "tag", "-l", "--", "refactor-complete-*"],
                            capture_output=True, text=True)
    checks.append(("refactor-complete tag set", bool(result.stdout.strip())))
    return checks


PHASE_CHECKERS = {
    0: check_phase_0,
    1: check_phase_1,
    2: check_phase_2,
    3: check_phase_3,
    4: check_phase_4,
    5: check_phase_5,
}


def run_checks(phase, state):
    """Return [(name, ok)] for a phase. Used by update_state.py advance as well."""
    return PHASE_CHECKERS[phase](state)


def gate_passes(phase, state):
    return all(ok for _, ok in run_checks(phase, state))


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.check_gate <phase>")
        sys.exit(2)
    try:
        phase = int(sys.argv[1])
    except ValueError:
        print("Phase must be an integer 0-5")
        sys.exit(2)
    if phase not in PHASE_CHECKERS:
        print(f"Unknown phase: {phase}")
        sys.exit(2)

    state = load_state()
    checks = run_checks(phase, state)

    print(f"\nGate check for Phase {phase} ({state['phases'][str(phase)]['name']}):\n")
    passed = 0
    failed_items = []
    for name, ok in checks:
        marker = "[PASS]" if ok else "[FAIL]"
        print(f"  {marker} {name}")
        if ok:
            passed += 1
        else:
            failed_items.append(name)

    print(f"\n{passed}/{len(checks)} checks passed.\n")

    if failed_items:
        print("Failed items:")
        for f in failed_items:
            print(f"  - {f}")
        print()
        print("To override, the human runs this in an interactive terminal and types the phrase:")
        print(f'  python3 scripts/update_state.py override {phase} "<gate>" "<reason>"')
        print(f'  phrase: "{_guard.MAGIC_PHRASE}"')
        print()
        sys.exit(1)

    print(f"Phase {phase} gate PASSED. You may proceed to Phase {phase + 1}." if phase < 5
          else "Phase 5 complete. Refactor handover done.")
    sys.exit(0)


if __name__ == "__main__":
    main()
