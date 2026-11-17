"""Security regression tests for the untangler scripts.

Each test runs the real scripts as subprocesses inside a throwaway git repo, with no
terminal attached (stdin is not a TTY) - the same situation as an agent's tool shell.
All data is fictitious.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def run(script, *args, cwd):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    env_name = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    os.environ.update(env_name)
    git("init", "-q", "-b", "main", cwd=tmp_path)
    (tmp_path / "app.py").write_text("print('hello')\n")
    git("add", ".", cwd=tmp_path)
    git("commit", "-q", "-m", "init", cwd=tmp_path)
    git("checkout", "-q", "-b", "refactor/untangle-test", cwd=tmp_path)
    git("tag", "pre-refactor-baseline", cwd=tmp_path)
    res = run("init_state.py", cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    return tmp_path


def state(repo):
    return json.loads((repo / ".untangler" / "state.json").read_text())


def write_state(repo, data):
    (repo / ".untangler" / "state.json").write_text(json.dumps(data))


# --- workflow-control fields cannot be forged with `set` -------------------------------

@pytest.mark.parametrize("field", [
    "top_5_signed_off_at", "top_5_signoff.via", "current_phase", "phases.1.status",
    "overrides", "last_test_run.passing", "baseline.git_tag", "schema_version",
])
def test_set_refuses_protected_fields(repo, field):
    res = run("update_state.py", "set", field, "1", cwd=repo)
    assert res.returncode != 0
    assert "workflow-control" in res.stderr
    assert state(repo).get("top_5_signed_off_at") is None


def test_set_allows_ordinary_fields(repo):
    res = run("update_state.py", "set", "baseline.total_loc", "123", cwd=repo)
    assert res.returncode == 0, res.stderr
    assert state(repo)["baseline"]["total_loc"] == 123


def test_set_rejects_odd_paths(repo):
    res = run("update_state.py", "set", "a..b", "1", cwd=repo)
    assert res.returncode != 0


# --- human-only commands refuse a non-interactive shell ---------------------------------

def test_override_requires_terminal(repo):
    res = run("update_state.py", "override", "0", "tests", "agent wants to skip", cwd=repo)
    assert res.returncode != 0
    assert "human-only" in res.stderr
    assert state(repo)["overrides"] == []
    assert (repo / ".untangler" / "overrides.log").read_text() == ""


def test_signoff_requires_terminal(repo):
    audit = {"schema_version": 1, "issues": [], "top_5": ["I-1", "I-2", "I-3", "I-4", "I-5"],
             "deferred": []}
    (repo / ".untangler" / "audit.json").write_text(json.dumps(audit))
    res = run("update_state.py", "signoff", cwd=repo)
    assert res.returncode != 0
    assert "human-only" in res.stderr
    assert state(repo)["top_5_signed_off_at"] is None


def test_hand_written_signoff_does_not_pass_gate(repo):
    data = state(repo)
    data["top_5_signed_off_at"] = "2026-01-01T00:00:00Z"  # forged without the command
    write_state(repo, data)
    res = run("check_gate.py", "1", cwd=repo)
    assert res.returncode == 1
    assert "[FAIL] Top 5 signed off" in res.stdout


# --- advance needs a passing gate -------------------------------------------------------

def test_advance_refuses_failing_gate(repo):
    res = run("update_state.py", "advance", "0", cwd=repo)
    assert res.returncode == 1
    assert state(repo)["current_phase"] == 0


def test_advance_refuses_skipping_phases(repo):
    res = run("update_state.py", "advance", "3", cwd=repo)
    assert res.returncode != 0
    assert state(repo)["current_phase"] == 0


def test_advance_allowed_when_gate_passes(repo):
    running = repo / ".untangler" / "RUNNING.md"
    running.write_text(running.read_text() + "python3 app.py starts the fictitious demo app locally.\n")
    for field, value in (("baseline.total_loc", "1"), ("baseline.test_count", "0"),
                         ("stack", '["python"]')):
        assert run("update_state.py", "set", field, value, cwd=repo).returncode == 0
    res = run("update_state.py", "advance", "0", cwd=repo)
    assert res.returncode == 0, res.stdout + res.stderr
    assert state(repo)["current_phase"] == 1


def test_running_md_template_alone_does_not_pass(repo):
    res = run("check_gate.py", "0", cwd=repo)
    assert "[FAIL] App run command documented" in res.stdout


# --- git argument injection via state.json ---------------------------------------------

def test_option_like_tag_is_rejected(repo):
    data = state(repo)
    data["baseline"]["git_tag"] = "--output=pwned.txt"
    write_state(repo, data)
    res = run("check_gate.py", "1", cwd=repo)
    assert res.returncode == 2
    assert "Unsafe or invalid git ref" in res.stderr
    assert not (repo / "pwned.txt").exists()


def test_audit_gate_sees_untracked_code(repo):
    (repo / "new_module.py").write_text("x = 1\n")  # untracked, not in git diff
    res = run("check_gate.py", "1", cwd=repo)
    assert "[FAIL] No code changes during audit (got 1 unexpected)" in res.stdout


# --- filesystem safety ------------------------------------------------------------------

def test_symlinked_state_dir_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / ".untangler").symlink_to(outside, target_is_directory=True)
    res = run("init_state.py", cwd=work)
    assert res.returncode != 0
    assert "symlink" in res.stderr
    assert list(outside.iterdir()) == []


def test_corrupt_state_fails_cleanly(repo):
    (repo / ".untangler" / "state.json").write_text("{not json")
    res = run("check_gate.py", "0", cwd=repo)
    assert res.returncode == 2
    assert "not valid JSON" in res.stderr


# --- secret scanner never prints values -------------------------------------------------

def test_secret_scan_masks_values(repo):
    fake = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # fictitious token shape
    (repo / "config.py").write_text(f'TOKEN = "{fake}"\n')
    git("add", "config.py", cwd=repo)
    res = run("audit_helpers.py", "secrets", cwd=repo)
    assert res.returncode == 1
    assert "config.py:1" in res.stdout
    assert fake not in res.stdout and fake not in res.stderr


def test_secret_scan_clean_repo(repo):
    res = run("audit_helpers.py", "secrets", cwd=repo)
    assert res.returncode == 0, res.stdout


def test_run_rejects_shell_strings():
    sys.path.insert(0, str(SCRIPTS))
    import audit_helpers
    with pytest.raises(TypeError):
        audit_helpers.run("echo hi; touch /tmp/x")


# --- override log cannot be forged with newlines ---------------------------------------

def test_one_line_strips_control_characters():
    sys.path.insert(0, str(SCRIPTS))
    import _guard
    assert _guard.one_line("ok\n2026-01-01 phase=5 gate=fake") == "ok 2026-01-01 phase=5 gate=fake"
    assert "\x1b" not in _guard.one_line("\x1b[31mred")


# --- the shipped bundle matches the reviewed source ------------------------------------

def test_bundle_matches_source():
    res = subprocess.run([sys.executable, str(ROOT / "tools" / "build_bundle.py"), "--check"],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


# --- the human path still works on a real terminal --------------------------------------

def _drive_on_pty(args, answer, cwd):
    pty = pytest.importorskip("pty")
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvp(sys.executable, [sys.executable, *args])
    out, sent = b"", False
    while True:
        try:
            data = os.read(fd, 1024)
        except OSError:
            break
        if not data:
            break
        out += data
        if b"> " in out and not sent:
            os.write(fd, (answer + "\n").encode())
            sent = True
    os.waitpid(pid, 0)
    return out.decode(errors="replace")


def test_human_override_needs_exact_phrase(repo):
    script = str(SCRIPTS / "update_state.py")
    out = _drive_on_pty([script, "override", "2", "safety", "time-boxed"],
                        "I understand the risk, override gate", repo)  # missing the dot
    assert "did not match exactly" in out
    assert state(repo)["overrides"] == []
    out = _drive_on_pty([script, "override", "2", "safety", "time-boxed"],
                        "I understand the risk, override gate.", repo)
    assert "Override recorded" in out
    assert state(repo)["overrides"][0]["via"] == "interactive"


def test_human_signoff_on_terminal(repo):
    audit = {"schema_version": 1, "issues": [], "top_5": ["I-1", "I-2", "I-3", "I-4", "I-5"],
             "deferred": []}
    (repo / ".untangler" / "audit.json").write_text(json.dumps(audit))
    out = _drive_on_pty([str(SCRIPTS / "update_state.py"), "signoff"], "yes", repo)
    assert "sign-off recorded" in out
    assert state(repo)["top_5_signoff"]["via"] == "interactive"
