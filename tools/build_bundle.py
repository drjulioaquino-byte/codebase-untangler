#!/usr/bin/env python3
"""
build_bundle.py - Build codebase-untangler.skill deterministically from the repo source.

Usage:
  python3 tools/build_bundle.py           # rebuild codebase-untangler.skill
  python3 tools/build_bundle.py --check   # exit 1 if the bundle differs from the source

The bundle is what people install, so CI runs --check: a .skill that does not match the
reviewed source (extra files, different scripts) fails the build.
"""

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "codebase-untangler.skill"
PREFIX = "codebase-untangler"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def source_files():
    files = [ROOT / "SKILL.md"]
    files += sorted((ROOT / "references").rglob("*.md"))
    files += sorted((ROOT / "scripts").glob("*.py"))
    return [f for f in files if f.is_file() and not f.is_symlink()]


def build_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in source_files():
            arc = f"{PREFIX}/{f.relative_to(ROOT).as_posix()}"
            info = zipfile.ZipInfo(arc, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, f.read_bytes())
    return buf.getvalue()


def bundle_contents(data):
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.is_dir()}


def main():
    expected = build_bytes()
    if "--check" in sys.argv[1:]:
        if not BUNDLE.exists():
            print("FAIL: codebase-untangler.skill is missing")
            sys.exit(1)
        want = bundle_contents(expected)
        got = bundle_contents(BUNDLE.read_bytes())
        extra = sorted(set(got) - set(want))
        missing = sorted(set(want) - set(got))
        changed = sorted(k for k in set(want) & set(got) if want[k] != got[k])
        if extra or missing or changed:
            for label, items in (("extra", extra), ("missing", missing), ("changed", changed)):
                for item in items:
                    print(f"FAIL: {label} in bundle: {item}")
            print("Rebuild with: python3 tools/build_bundle.py")
            sys.exit(1)
        print(f"OK: bundle matches source ({len(want)} files)")
        return
    BUNDLE.write_bytes(expected)
    print(f"Wrote {BUNDLE.name} ({len(source_files())} files)")


if __name__ == "__main__":
    main()
