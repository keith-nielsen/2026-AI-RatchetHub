"""Preflight (v1 scope G5): the checks run before every push, locally, before anything leaves.

1. Each `[github] preflight` command, run in the project (sandboxed, as the agent runs it).
2. A trial merge against the base (`git merge-tree`), which predicts a conflict without pushing.

What it can't model locally it doesn't guess: the PR object, required checks on the server, and
anything a ruleset decides.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..declaration import load_committed

CLEAR, FINDING, UNMEASURED = "CLEAR", "FINDING", "UNMEASURED"


@dataclass
class Item:
    name: str
    result: str
    detail: str


def preflight(root: Path, *, base_ref: str | None = None, timeout: int = 1800) -> list[Item]:
    root = Path(root).resolve()
    gh = load_committed(root).github
    items: list[Item] = []
    if gh is None:
        return [Item("declaration", UNMEASURED, "no [github] section: nothing is declared to run")]
    for command in gh.preflight:
        try:
            r = subprocess.run(command, shell=True, cwd=root, capture_output=True, text=True, timeout=timeout)
            tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
            items.append(Item(f"run: {command}", CLEAR if r.returncode == 0 else FINDING,
                              f"exit {r.returncode}: {tail[0][:120]}"))
        except subprocess.TimeoutExpired:
            items.append(Item(f"run: {command}", FINDING, f"timed out after {timeout}s"))
    base = base_ref or f"origin/{gh.default_branch}"
    have = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "-q", base], capture_output=True, text=True)
    if have.returncode != 0:
        items.append(Item("trial merge", UNMEASURED, f"{base} is not known locally; fetch it first"))
        return items
    r = subprocess.run(["git", "-C", str(root), "merge-tree", "--write-tree", "--name-only", base, "HEAD"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        items.append(Item("trial merge", CLEAR, f"merges cleanly into {base}"))
    elif r.returncode == 1:
        files = [ln for ln in r.stdout.splitlines()[1:] if ln and not ln.startswith(("Auto-merging", "CONFLICT"))]
        items.append(Item("trial merge", FINDING, f"conflicts with {base}: " + ", ".join(files[:10])))
    else:
        items.append(Item("trial merge", UNMEASURED, r.stderr.strip()[:120]))
    return items
