"""The protected-element gate (v1 scope G7), advisory until its burn-in completes.

The project declares its protected elements in a JSON file, `{"path": "sha256 hex"}`. A path whose
content no longer has its declared hash, or that is missing, is reported. It runs as an advisory
job, under a name that says so, because it has no completed burn-in (scope §2, gate rules).
Exit: 0 every element matches | 1 an element changed | 2 malformed input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HEX = re.compile(r"^[0-9a-f]{64}$")


def check(root: Path, elements: dict) -> list[str]:
    if not isinstance(elements, dict) or not elements:
        raise ValueError("the elements file must be a non-empty JSON object of path to sha256")
    findings = []
    for rel, want in sorted(elements.items()):
        if not isinstance(want, str) or not HEX.match(want):
            raise ValueError(f"{rel}: the declared hash is not 64 hex digits")
        path = Path(root) / rel
        if not path.is_file():
            findings.append(f"{rel}: missing")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != want:
            findings.append(f"{rel}: content changed")
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="protected elements keep their declared hashes")
    ap.add_argument("--root", default=".")
    ap.add_argument("--elements", required=True)
    a = ap.parse_args(argv)
    try:
        findings = check(Path(a.root), json.loads(Path(a.elements).read_text()))
    except (ValueError, OSError) as exc:
        print(f"MALFORMED: {exc}", file=sys.stderr)
        return 2
    for f in findings:
        print(f"  {f}")
    print("advisory: " + (f"{len(findings)} protected element(s) changed" if findings else "every element matches"))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
