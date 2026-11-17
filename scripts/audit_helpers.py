#!/usr/bin/env python3
"""
audit_helpers.py - Common audit analysis commands.

Usage:
  python -m scripts.audit_helpers loc          # files ranked by LOC
  python -m scripts.audit_helpers churn        # files ranked by git churn
  python -m scripts.audit_helpers hotspots     # combined LOC + churn ranking
  python -m scripts.audit_helpers deps         # dependency overview (Node/Python)
  python -m scripts.audit_helpers secrets      # likely committed secrets (values masked)
"""

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path


def run(cmd):
    # Argument lists only: never a shell string, so nothing here is shell-interpreted.
    if not isinstance(cmd, (list, tuple)):
        raise TypeError("run() takes an argument list, not a shell string")
    return subprocess.run(list(cmd), capture_output=True, text=True)


def loc():
    """LOC per tracked file, sorted descending."""
    result = run(["git", "ls-files"])
    files = result.stdout.strip().split("\n")
    rows = []
    for f in files:
        p = Path(f)
        if not p.is_file():
            continue
        # skip binary/large lockfiles
        if p.suffix in (".lock", ".min.js", ".min.css", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip"):
            continue
        try:
            n = sum(1 for _ in p.open(errors="ignore"))
        except (OSError, UnicodeDecodeError):
            continue
        rows.append((n, f))
    rows.sort(reverse=True)
    print("Top 30 files by LOC:")
    for n, f in rows[:30]:
        print(f"  {n:>6}  {f}")
    return rows


def churn():
    """File change frequency from git log."""
    result = run(["git", "log", "--format=format:", "--name-only"])
    files = [f for f in result.stdout.split("\n") if f]
    counter = Counter(files)
    print("Top 30 files by change frequency:")
    for f, c in counter.most_common(30):
        print(f"  {c:>4}  {f}")
    return counter.most_common()


def hotspots():
    """Combined ranking: high LOC + high churn = hotspot."""
    loc_rows = loc()
    print()
    churn_rows = churn()
    print()

    loc_rank = {f: i for i, (_, f) in enumerate(loc_rows)}
    churn_rank = {f: i for i, (f, _) in enumerate(churn_rows)}

    common = set(loc_rank) & set(churn_rank)
    scored = []
    for f in common:
        # lower combined rank = bigger hotspot
        scored.append((loc_rank[f] + churn_rank[f], f, loc_rank[f], churn_rank[f]))
    scored.sort()
    print("Top 20 hotspots (high LOC AND high churn):")
    for score, f, l_rank, c_rank in scored[:20]:
        print(f"  combined={score:<4} LOC_rank={l_rank:<4} churn_rank={c_rank:<4} {f}")


def deps():
    """Quick dependency overview for Node and Python projects."""
    if Path("package.json").exists():
        pkg = json.loads(Path("package.json").read_text())
        deps = pkg.get("dependencies", {})
        dev = pkg.get("devDependencies", {})
        print(f"package.json: {len(deps)} deps, {len(dev)} devDeps")
        # detect duplicates by category (e.g. multiple HTTP clients)
        flags = []
        all_deps = list(deps.keys()) + list(dev.keys())
        http_clients = [d for d in all_deps if d in ("axios", "node-fetch", "got", "superagent", "ky", "isomorphic-fetch")]
        date_libs = [d for d in all_deps if d in ("moment", "date-fns", "dayjs", "luxon")]
        if len(http_clients) > 1:
            flags.append(f"Multiple HTTP clients: {http_clients}")
        if len(date_libs) > 1:
            flags.append(f"Multiple date libs: {date_libs}")
        if flags:
            print("Potential redundancy:")
            for f in flags:
                print(f"  - {f}")
    if Path("package.json").exists():
        print("Vulnerabilities: run `npm audit --omit=dev` (read-only) and record findings.")
    if Path("pyproject.toml").exists():
        print("pyproject.toml found - run `pip list` and `pip-audit` and review")
    if Path("requirements.txt").exists():
        lines = [l for l in Path("requirements.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
        print(f"requirements.txt: {len(lines)} packages")


# Patterns for likely committed secrets. Matches are reported as file:line plus the
# rule name only - the matched value is never printed, so the output is safe to
# paste into REFACTOR_AUDIT.md. This is a quick first pass, not a replacement for
# gitleaks / trufflehog (see SKILL.md Phase 1 and Phase 4).
SECRET_RULES = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}")),
    ("stripe-live-key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("assigned-secret", re.compile(
        r"(?i)\b[A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_KEY|SERVICE_ROLE_KEY|PRIVATE_KEY)"
        r"[A-Z0-9_]*\s*[:=]\s*['\"]?[^\s'\"$<{][^\s'\"]{11,}")),
]
SECRET_FILE_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519",
                     "credentials.json", "service-account.json"}


def secrets():
    """Scan tracked files (and .untangler/) for likely secrets. Exit 1 if any found."""
    files = [f for f in run(["git", "ls-files", "-z"]).stdout.split("\0") if f]
    untangler = Path(".untangler")
    if untangler.is_dir() and not untangler.is_symlink():
        files += [str(p) for p in untangler.rglob("*") if p.is_file() and not p.is_symlink()]
    findings = []
    for f in sorted(set(files)):
        p = Path(f)
        if p.name in SECRET_FILE_NAMES:
            findings.append((f, 0, "secret-file-committed"))
        if p.is_symlink() or not p.is_file() or p.stat().st_size > 2_000_000:
            continue
        if p.suffix in (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".skill", ".lock"):
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for rule, rx in SECRET_RULES:
                if rx.search(line):
                    findings.append((f, lineno, rule))
    if not findings:
        print("No likely secrets found by the quick scan (run gitleaks for a full scan).")
        return []
    print(f"{len(findings)} likely secret(s) - values not shown. Rotate anything real:")
    for f, lineno, rule in findings:
        print(f"  {f}:{lineno}  [{rule}]")
    return findings


COMMANDS = {"loc": loc, "churn": churn, "hotspots": hotspots, "deps": deps, "secrets": secrets}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    result = COMMANDS[sys.argv[1]]()
    if sys.argv[1] == "secrets" and result:
        sys.exit(1)


if __name__ == "__main__":
    main()
