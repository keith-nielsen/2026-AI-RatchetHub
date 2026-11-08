"""The scope review (v1 scope G7): every file a PR changes must be in the scope its body declares.

The PR body carries a fenced block:

    ```scope
    src/ratchethub/guard.py
    tests/
    ```

A line ending in `/` covers a folder; any other line is one exact path. A rename is reported by both
its old and new path, and each must be in scope: a rename moves content out of one place and into
another, and a scope that names only the new path hides what left.

Fails closed: a body with no scope block, or a diff git can't produce, is a failure, never a pass.
Exit: 0 every changed path is in scope | 1 out of scope | 2 malformed input.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

FENCE = re.compile(r"```scope[ \t]*\r?\n(.*?)```", re.DOTALL)


class Malformed(Exception):
    pass


def declared(body: str) -> list[str]:
    blocks = FENCE.findall(body)
    if not blocks:
        raise Malformed("the PR body has no ```scope block")
    entries = []
    for block in blocks:
        for line in block.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                entries.append(line.lstrip("./") if line.startswith("./") else line)
    if not entries:
        raise Malformed("the ```scope block is empty")
    return entries


def changed(repo: str, base: str, head: str) -> list[str]:
    """Every path the change touches. A rename (R) and a copy (C) give both sides."""
    mb = subprocess.run(["git", "-C", repo, "merge-base", base, head], capture_output=True, text=True)
    if mb.returncode != 0:
        raise Malformed(f"no merge base for {base} and {head}: {mb.stderr.strip()}")
    r = subprocess.run(["git", "-C", repo, "diff", "--name-status", "-M", "-C", f"{mb.stdout.strip()}..{head}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Malformed(r.stderr.strip() or "git diff failed")
    paths = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if parts[0][:1] in "RC":
            paths += parts[1:3]
        elif len(parts) >= 2:
            paths.append(parts[1])
    return list(dict.fromkeys(paths))


def covered(path: str, scope: list[str]) -> bool:
    return any(path == s or (s.endswith("/") and path.startswith(s)) for s in scope)


def review(body: str, paths: list[str]) -> list[str]:
    scope = declared(body)
    return [p for p in paths if not covered(p, scope)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="every changed path must be in the PR body's scope block")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--body-file", required=True)
    a = ap.parse_args(argv)
    try:
        outside = review(open(a.body_file, encoding="utf-8").read(), changed(a.repo, a.base, a.head))
    except (Malformed, OSError) as exc:
        print(f"MALFORMED: {exc}", file=sys.stderr)
        return 2
    if outside:
        print("outside the declared scope:\n" + "\n".join(f"  {p}" for p in outside))
        return 1
    print("every changed path is in the declared scope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
