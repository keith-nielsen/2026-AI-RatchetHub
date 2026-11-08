"""The read channel (v1 scope G4): every GitHub read tries anonymous REST first, then `gh api`.

Why anonymous first: a sandboxed `gh` can't reach the system keyring and reports a stale token as
`HTTP 401`, while `api.github.com` answers anonymously. A reader that fails exactly when the agent
needs it gets replaced by a hand-rolled `curl`. Every read returns the channel that answered, and
callers print it: a fact without its channel can't be told from a verified one.

Reads only. Mutations never come through here; they go through REST paths the `gh` guard
sanctions, never `gh pr edit`.

The anonymous budget is 60 requests an hour, and a `304 Not Modified` still spends one (measured
in VMM, 2026-08-04), so the budget headers of every response are recorded for the report.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

API = "https://api.github.com"
TIMEOUT = 30
ANON, GH = "anon-rest", "gh-api"


class ReadError(RuntimeError):
    """A read failed on every channel. An empty or failed read is no answer (G1)."""


@dataclass
class Budget:
    remaining: int | None = None
    limit: int | None = None
    reset: int | None = None

    def record(self, headers) -> None:
        for key, header in (("remaining", "X-RateLimit-Remaining"), ("limit", "X-RateLimit-Limit"),
                            ("reset", "X-RateLimit-Reset")):
            try:
                setattr(self, key, int(headers.get(header)))
            except (TypeError, ValueError, AttributeError):
                pass

    def report(self) -> str | None:
        if self.remaining is None:
            return None
        line = f"{self.remaining}/{self.limit} anonymous reads remaining"
        if self.reset:
            line += f", resets in about {max(0, round((self.reset - time.time()) / 60))} min"
        return line


def _urlopen_json(path: str, budget: Budget, opener=urllib.request.urlopen):
    req = urllib.request.Request(f"{API}{path}", headers={"Accept": "application/vnd.github+json",
                                                         "User-Agent": "ratchethub-read"})
    with opener(req, timeout=TIMEOUT) as resp:
        budget.record(resp.headers)
        return json.loads(resp.read().decode())


def _gh_json(path: str):
    if not shutil.which("gh"):
        raise ReadError("gh is not installed")
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=TIMEOUT,
                       env={**os.environ, "GH_PROMPT_DISABLED": "1"})
    if r.returncode != 0:
        raise ReadError(f"gh api failed: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


@dataclass
class Reader:
    """`anon` and `gh` are injectable so tests reach the real call path with a fake network."""

    anon: Callable | None = None
    gh: Callable | None = None
    budget: Budget = field(default_factory=Budget)

    def get(self, path: str) -> tuple[object, str]:
        anon = self.anon or (lambda p: _urlopen_json(p, self.budget))
        gh = self.gh or _gh_json
        try:
            return anon(path), ANON
        except urllib.error.HTTPError as exc:
            self.budget.record(exc.headers)
            anon_err: Exception = exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            anon_err = exc
        try:
            return gh(path), GH
        except (ReadError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ReadError(f"both channels failed for {path}: anonymous: {anon_err}; gh: {exc}") from exc

    # --- the reads the drivers use ---

    def pull_request(self, slug: str, number: int):
        """One PR. The list endpoint omits `mergeable`, so mergeability is read only here."""
        return self.get(f"/repos/{slug}/pulls/{number}")

    def pulls_for_branch(self, slug: str, branch: str, state: str = "all"):
        """`state=all` by default: a closed, unmerged PR is invisible to an open-only query, and a
        driver that sees "no PR" proposes a duplicate."""
        owner = slug.split("/")[0]
        return self.get(f"/repos/{slug}/pulls?state={state}&head={owner}:{branch}&per_page=100")

    def open_children(self, slug: str, branch: str):
        """Open PRs stacked on `branch`. Merging the parent with branch deletion closes them for
        good, so they must be seen, and retargeted, before the merge."""
        return self.get(f"/repos/{slug}/pulls?state=open&base={branch}&per_page=100")

    def check_runs(self, slug: str, sha: str):
        return self.get(f"/repos/{slug}/commits/{sha}/check-runs?per_page=100")

    def workflow_runs(self, slug: str, sha: str):
        return self.get(f"/repos/{slug}/actions/runs?head_sha={sha}&per_page=100")

    def jobs(self, slug: str, run_id: int):
        """The job-level view, cross-checked against pending checks (G1)."""
        return self.get(f"/repos/{slug}/actions/runs/{run_id}/jobs?per_page=100")

    def branch(self, slug: str, name: str):
        return self.get(f"/repos/{slug}/branches/{name}")

    def branches(self, slug: str):
        return self.get(f"/repos/{slug}/branches?per_page=100")


def slug_from_remote(root: str, remote: str = "origin") -> str | None:
    """owner/repo from the remote URL, never from the folder name, which can differ."""
    r = subprocess.run(["git", "-C", str(root), "remote", "get-url", remote], capture_output=True, text=True,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        return None
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", r.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def summarize_checks(payload) -> tuple[int, list[str], list[str]]:
    """(total, pending, failing). `skipped` and `neutral` pass: a burn-in job reports `skipped`
    on one trigger, and failing it gets a gate switched off rather than obeyed."""
    if not isinstance(payload, dict) or not isinstance(payload.get("check_runs"), list):
        raise ReadError("the check-runs payload has no check_runs list; an empty read is no answer")
    runs = payload["check_runs"]
    pending = sorted(r.get("name", "?") for r in runs if r.get("status") != "completed")
    failing = sorted({r.get("name", "?") for r in runs if r.get("status") == "completed"
                      and r.get("conclusion") not in ("success", "neutral", "skipped")})
    return len(runs), pending, failing
