"""PR state per layer (v1 scope G5), and the route state the PR driver derives from it (G1).

Level-triggered: every run derives the state from GitHub and the local repository, and never from
a previous run's memory. Each layer is reported with the channel that answered. A layer that
couldn't be read is UNREAD, never assumed: an empty or failed read is no answer.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from .read import Reader, ReadError, summarize_checks

# Route states, in lifecycle order.

# REST mergeable_state carries GitHub's MergeStateStatus values, lower-cased (GraphQL schema enum;
# the REST description gives only the example "clean"). Only these two say a merge will go through:
# `mergeable: true` alone means no conflict, while a ruleset can still refuse the merge.
READY_MERGE_STATES = ("clean", "has_hooks")
MERGE_STATE_MEANING = {
    "behind": "the head ref is out of date",
    "blocked": "the merge is blocked (a ruleset or a required check)",
    "dirty": "the merge commit cannot be cleanly created",
    "unstable": "mergeable with a non-passing commit status",
}

NOT_PUSHED = "not-pushed"            # the branch has commits GitHub doesn't
NO_PR = "no-pr"
CLOSED_UNMERGED = "closed-unmerged"  # a closed PR exists: never propose a duplicate
DRAFT = "draft"
CHECKS_PENDING = "checks-pending"
CHECKS_FAILING = "checks-failing"
NOT_MERGEABLE = "not-mergeable"
MERGEABLE = "mergeable"              # green and mergeable: the merge is a person's step
QUEUED = "queued"                    # in a merge queue: a queued merge is not merged
MERGED_BRANCH_PRESENT = "merged-branch-present"
DONE = "done"
UNREAD = "unread"


@dataclass
class Layer:
    name: str
    value: str
    channel: str


@dataclass
class RouteState:
    state: str
    branch: str
    pr: int | None = None
    head_sha: str | None = None
    layers: list[Layer] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    failing: list[str] = field(default_factory=list)
    detail: str = ""


def git(root, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin"})


def local_head(root) -> str | None:
    r = git(root, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def derive(reader: Reader, slug: str, branch: str, base: str, head: str | None,
           remote_override: dict | None = None) -> RouteState:
    """`remote_override` holds a mutation's own response for this run: it outranks a lagging read
    (a just-created PR, a just-merged one)."""
    st = RouteState(UNREAD, branch, head_sha=head)
    try:
        remote_branch, ch = reader.branch(slug, branch)
        remote_sha = remote_branch.get("commit", {}).get("sha") if isinstance(remote_branch, dict) else None
        st.layers.append(Layer("branch", remote_sha or "absent", ch))
    except ReadError as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            remote_sha = None
            st.layers.append(Layer("branch", "absent", "anon-rest"))
        else:
            st.detail = f"branch read failed: {exc}"
            return st

    try:
        pulls, ch = reader.pulls_for_branch(slug, branch)
    except ReadError as exc:
        st.detail = f"PR list read failed: {exc}"
        return st
    if not isinstance(pulls, list):
        st.detail = "the PR list read was not a list; no answer"
        return st
    if remote_override and remote_override.get("number"):
        pulls = [remote_override] + [p for p in pulls if p.get("number") != remote_override["number"]]
    st.layers.append(Layer("pulls", f"{len(pulls)} for {branch}", ch))
    open_ = [p for p in pulls if p.get("state") == "open"]
    merged = [p for p in pulls if p.get("merged_at") or p.get("merged")]

    if merged and not open_:
        st.pr = merged[0].get("number")
        st.state = MERGED_BRANCH_PRESENT if remote_sha else DONE
        return st
    if head and remote_sha != head and not merged:
        st.state = NOT_PUSHED
        st.detail = f"local {head[:12]}, GitHub {(remote_sha or 'absent')[:12]}"
        if not open_:
            return st
    if not open_:
        closed = [p for p in pulls if p.get("state") == "closed"]
        st.state = CLOSED_UNMERGED if closed else (NO_PR if st.state != NOT_PUSHED else NOT_PUSHED)
        st.pr = closed[0].get("number") if closed else None
        return st

    number = open_[0].get("number")
    st.pr = number
    if st.state == NOT_PUSHED:
        return st
    try:
        pr, ch = reader.pull_request(slug, number)
    except ReadError as exc:
        st.state, st.detail = UNREAD, f"PR read failed: {exc}"
        return st
    if remote_override and remote_override.get("number") == number:
        pr = {**pr, **remote_override}
    st.layers.append(Layer("pr", f"#{number} draft={pr.get('draft')} mergeable={pr.get('mergeable')} "
                                 f"state={pr.get('mergeable_state')}", ch))
    if pr.get("merged") or pr.get("merged_at"):
        st.state = MERGED_BRANCH_PRESENT if remote_sha else DONE
        return st
    if pr.get("auto_merge") or pr.get("mergeable_state") == "queued":
        st.state = QUEUED
        return st
    if pr.get("draft"):
        st.state = DRAFT
        return st
    merge_state = (pr.get("mergeable_state") or None)
    merge_state = merge_state.lower() if isinstance(merge_state, str) else None
    sha = (pr.get("head") or {}).get("sha") or remote_sha
    st.head_sha = sha
    try:
        checks, ch = reader.check_runs(slug, sha)
        total, pending, failing = summarize_checks(checks)
    except ReadError as exc:
        st.state, st.detail = UNREAD, f"checks read failed: {exc}"
        return st
    st.layers.append(Layer("checks", f"{total} runs, {len(pending)} pending, {len(failing)} failing", ch))
    pending = _cross_check_jobs(reader, slug, sha, pending, st)
    st.pending, st.failing = pending, failing
    if failing:
        st.state = CHECKS_FAILING
    elif pending or total == 0:
        st.state = CHECKS_PENDING
        st.detail = "no check has reported yet" if total == 0 else ""
    elif pr.get("mergeable") is None or (pr.get("mergeable") and merge_state in (None, "unknown")):
        st.state, st.detail = CHECKS_PENDING, "GitHub is still computing mergeability"
    elif pr.get("mergeable") is True and merge_state in READY_MERGE_STATES:
        st.state = MERGEABLE
    else:
        st.state = NOT_MERGEABLE
        st.detail = (f"merge state {merge_state}: {MERGE_STATE_MEANING.get(merge_state, 'not ready')}"
                     if pr.get("mergeable") else "the merge commit can't be cleanly created")
    return st


def _cross_check_jobs(reader: Reader, slug: str, sha: str, pending: list[str], st: RouteState) -> list[str]:
    """A check can read pending while its job has finished (lag), or the reverse. The job-level
    view is read and both are reported; a check stays pending unless its job says completed."""
    if not pending:
        return pending
    try:
        runs, ch = reader.workflow_runs(slug, sha)
        jobs = []
        for run in (runs or {}).get("workflow_runs", []):
            payload, _ = reader.jobs(slug, run["id"])
            jobs += payload.get("jobs", [])
    except (ReadError, KeyError, TypeError, AttributeError) as exc:
        st.layers.append(Layer("jobs", f"unread: {exc}", "none"))
        return pending
    done = {j.get("name") for j in jobs if j.get("status") == "completed"}
    st.layers.append(Layer("jobs", f"{len(jobs)} jobs, {len(done)} completed", ch))
    return [p for p in pending if p not in done]
