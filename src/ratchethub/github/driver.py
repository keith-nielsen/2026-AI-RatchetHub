"""The PR lifecycle driver (v1 scope G1) and the release driver (G3).

**Level-triggered.** Each run derives the route state from GitHub and the local repository
(`state.derive`) and does the one step that state calls for. Nothing is remembered between runs
except an unconfirmed mutation, which suppresses only a repeat of that same mutation while the
read lags.

Who runs each step:

- agent: push the branch (printed, recorded for the outbound guard, run by the agent as Bash),
  open the PR and delete a merged branch (REST writes the `gh` guard sanctions). Where the agent's
  channel holds no GitHub credential, the push is the person's, and a failed write is handed over;
- person: merge, tag and release, through the handoff file (G2), because each is irreversible.

A plan's verification line (`--after <step>`) only confirms its own step: it re-reads on the
ladder, marks the step verified, and never writes to GitHub or plans the next step, so a plan does
exactly the one mutation it announces.

A mutation's own response outranks a lagging read. Verify reads retry on the measured ladder
(5, 8 and 13 seconds) and then hand over to a monitor. Every command the driver runs or prints is
built here, in its final shape, so the tests check the shape the agent and the person actually see.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..guard import RELAY_DIR
from . import handoff, state as S
from .read import Reader

LADDER = (5, 8, 13)
DONE, ACT, PENDING, HANDOFF, BLOCKED = 0, 10, 11, 12, 1
UNCONFIRMED = Path(RELAY_DIR) / "unconfirmed.json"
LAG_LOG = Path(RELAY_DIR) / "lag.jsonl"
SUPPRESS_SECONDS = 120
OUTCOMES = ("confirmed", "lagging", "exhausted")  # one sequence: lagging*, then exactly one terminal
# A person's step, and the route states that show it took effect. The create-pr marker is keyed
# without a PR number, because the plan was written before the PR had one.
AFTER = {
    "create-pr": (S.DRAFT, S.CHECKS_PENDING, S.CHECKS_FAILING, S.NOT_MERGEABLE, S.MERGEABLE, S.QUEUED,
                  S.MERGED_BRANCH_PRESENT, S.DONE),
    "merge": (S.MERGED_BRANCH_PRESENT, S.DONE),
    "delete-branch": (S.DONE,),
}


class MutationError(RuntimeError):
    pass


def gh_write(argv: list[str]) -> dict:
    """Run one `gh api` write. Returns its JSON response: the mutation's own answer."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise MutationError((r.stderr or r.stdout).strip()[:300])
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        return {}


def create_pr_argv(slug: str, branch: str, base: str, title: str, body_file: str) -> list[str]:
    return ["gh", "api", "-X", "POST", f"repos/{slug}/pulls", "-f", f"title={title}", "-f", f"head={branch}",
            "-f", f"base={base}", "-F", f"body=@{body_file}"]


def delete_branch_argv(slug: str, branch: str) -> list[str]:
    return ["gh", "api", "-X", "DELETE", f"repos/{slug}/git/refs/heads/{branch}"]


def merge_command(slug: str, pr: int, sha: str) -> str:
    return f"gh api -X PUT repos/{slug}/pulls/{pr}/merge -f merge_method=merge -f sha={sha}"


def push_command(root: Path, branch: str) -> str:
    return f"git -C {shlex.quote(str(root))} push -u origin {shlex.quote(branch)}"


@dataclass
class Outcome:
    code: int
    state: S.RouteState
    lines: list[str] = field(default_factory=list)


@dataclass
class Driver:
    root: Path
    slug: str
    reader: Reader
    write: Callable[[list[str]], dict] = gh_write
    sleep: Callable[[float], None] = time.sleep
    out: Callable[[str], None] = print
    driver_cmd: str = "ratchethub pr"

    def _say(self, outcome: Outcome, line: str) -> None:
        outcome.lines.append(line)
        self.out(line)

    # --- the unconfirmed-mutation record and the lag log ---

    def _unconfirmed(self) -> dict:
        try:
            rec = json.loads((self.root / UNCONFIRMED).read_text())
            return rec if time.time() - rec.get("at", 0) < SUPPRESS_SECONDS else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def _set_unconfirmed(self, step: str, branch: str, response: dict) -> None:
        (self.root / RELAY_DIR).mkdir(parents=True, exist_ok=True)
        (self.root / UNCONFIRMED).write_text(json.dumps({"step": step, "branch": branch, "at": time.time(),
                                                         "response": response}))

    def _clear_unconfirmed(self) -> None:
        try:
            (self.root / UNCONFIRMED).unlink()
        except FileNotFoundError:
            pass

    def _lag(self, step: str, attempt: int, outcome: str) -> None:
        assert outcome in OUTCOMES
        (self.root / RELAY_DIR).mkdir(parents=True, exist_ok=True)
        with open(self.root / LAG_LOG, "a") as fh:
            fh.write(json.dumps({"step": step, "attempt": attempt, "outcome": outcome, "at": time.time()}) + "\n")

    def _confirm(self, step: str, derive: Callable[[], S.RouteState], confirmed: Callable[[S.RouteState], bool]):
        """Re-read on the ladder until the state shows the step done. Logs each attempt."""
        st = derive()
        for i, wait in enumerate(LADDER, 1):
            if confirmed(st):
                self._lag(step, i, "confirmed")
                return st, True
            self._lag(step, i, "lagging")
            self.sleep(wait)
            st = derive()
        ok = confirmed(st)
        self._lag(step, len(LADDER) + 1, "confirmed" if ok else "exhausted")
        return st, ok

    # --- the run ---

    def run(self, branch: str, base: str, *, title: str = "", body_file: str = "", mnemonic: str = "change") -> Outcome:
        head = S.local_head(self.root)
        unconfirmed = self._unconfirmed()
        override = unconfirmed.get("response") if unconfirmed.get("branch") == branch else None
        st = S.derive(self.reader, self.slug, branch, base, head, remote_override=override)
        o = Outcome(BLOCKED, st)
        self._say(o, f"state: {st.state}" + (f" (PR #{st.pr})" if st.pr else "") + (f": {st.detail}" if st.detail else ""))
        for layer in st.layers:
            self._say(o, f"  {layer.name}: {layer.value} [{layer.channel}]")

        if st.state == S.UNREAD:
            self._say(o, "a read failed, so there is no state to act on; nothing was done")
            return o
        if st.state == S.NOT_PUSHED:
            cmd = push_command(self.root, branch)
            handoff.write_emission(self.root, "push", cmd, branch)
            self._say(o, "next: push the branch with exactly this command, then run the driver again. The agent "
                         "runs it if its channel holds a GitHub credential; otherwise a person does:")
            self._say(o, cmd)
            o.code = ACT
            return o
        if st.state == S.NO_PR:
            if unconfirmed.get("step") == "create-pr" and unconfirmed.get("branch") == branch:
                self._say(o, "a PR was created moments ago and the read doesn't show it yet; not creating "
                             "another. Run the driver again shortly.")
                o.code = PENDING
                return o
            if not (title and body_file and Path(body_file).is_file()):
                self._say(o, "no PR yet: give a title and an existing body file to open one")
                return o
            try:
                resp = self.write(create_pr_argv(self.slug, branch, base, title, body_file))
            except MutationError as exc:
                return self._mutation_failed(o, "create-pr", branch, None,
                                             " ".join(map(shlex.quote, create_pr_argv(self.slug, branch, base, title,
                                                                                      body_file))), exc, mnemonic)
            self._set_unconfirmed("create-pr", branch, resp)
            self._say(o, f"opened PR #{resp.get('number')} (from the mutation's own response)")
            st, ok = self._confirm("create-pr", lambda: S.derive(self.reader, self.slug, branch, base, head, resp),
                                   lambda s: s.pr == resp.get("number"))
            o.state = st
            o.code = PENDING
            return o
        if st.state == S.CLOSED_UNMERGED:
            self._say(o, f"PR #{st.pr} for this branch is closed and unmerged. Not opening a duplicate: "
                         "reopen it or close the branch, a person's call.")
            return o
        if st.state in (S.DRAFT, S.NOT_MERGEABLE):
            self._say(o, "the PR is a draft" if st.state == S.DRAFT else "GitHub reports the PR not mergeable")
            return o
        if st.state == S.CHECKS_FAILING:
            self._say(o, "failing checks: " + ", ".join(st.failing))
            return o
        if st.state in (S.CHECKS_PENDING, S.QUEUED):
            if st.state == S.QUEUED:
                self._say(o, "the PR is queued to merge; a queued merge is not merged")
            self._say(o, f"waiting for checks, re-reading on the {'/'.join(map(str, LADDER))}s ladder")
            st, ok = self._confirm("checks", lambda: S.derive(self.reader, self.slug, branch, base, head),
                                   lambda s: s.state not in (S.CHECKS_PENDING, S.QUEUED))
            o.state = st
            # The state block above was read before the wait: show the read the result stands on.
            self._say(o, f"after the wait: {st.state}" + (f" (PR #{st.pr})" if st.pr else "")
                      + (f": {st.detail}" if st.detail else ""))
            for layer in st.layers:
                self._say(o, f"  {layer.name}: {layer.value} [{layer.channel}]")
            if not ok:
                self._say(o, f"still {st.state} after the {'/'.join(map(str, LADDER))}s ladder; hand over to a monitor:")
                self._say(o, f"{self.driver_cmd} --branch {shlex.quote(branch)}")
                o.code = PENDING
                return o
            self._say(o, f"state is now {st.state}; run the driver again")
            o.code = ACT
            return o
        if st.state == S.MERGEABLE:
            self._clear_unconfirmed()
            cmd = merge_command(self.slug, st.pr, st.head_sha)
            assert_line = f"{self.driver_cmd} --branch {shlex.quote(branch)} --assert-head {st.head_sha} --assert-pr {st.pr}"
            verify_line = f"{self.driver_cmd} --branch {shlex.quote(branch)} --after merge --pr {st.pr}"
            handoff.write_plan(self.root, step="merge", command=cmd, branch=branch, pr=st.pr,
                               verify_lines=[verify_line], assert_lines=[assert_line])
            self._say(o, f"PR #{st.pr} is green and mergeable at {st.head_sha[:12]}. Merging is a person's step.")
            self._say(o, f"The plan runs: {cmd}")
            handoff.emit(self.root, "merge", st.pr, mnemonic=mnemonic, has_assertion=True, out=lambda s: self._say(o, s))
            o.code = HANDOFF
            return o
        if st.state == S.MERGED_BRANCH_PRESENT:
            handoff.mark_verified(self.root, "merge", st.pr)
            try:
                self.write(delete_branch_argv(self.slug, branch))
            except MutationError as exc:
                return self._mutation_failed(o, "delete-branch", branch, st.pr,
                                             " ".join(delete_branch_argv(self.slug, branch)), exc, mnemonic)
            self._say(o, f"PR #{st.pr} is merged; deleted the branch {branch} on GitHub")
            st, ok = self._confirm("delete-branch", lambda: S.derive(self.reader, self.slug, branch, base, None),
                                   lambda s: s.state == S.DONE)
            o.state = st
            o.code = DONE if ok else PENDING
            return o
        if st.state == S.DONE:
            handoff.mark_verified(self.root, "merge", st.pr)
            self._clear_unconfirmed()
            self._say(o, f"done: PR #{st.pr} is merged and its branch is gone")
            o.code = DONE
            return o
        return o

    def _mutation_failed(self, o: Outcome, step: str, branch: str, pr, cmd: str, exc: Exception,
                         mnemonic: str = "change") -> Outcome:
        """The agent's channel couldn't write (in a sandbox, `gh` often has no credential). Hand
        the same command to a person rather than retrying by another route."""
        self._say(o, f"the {step} write failed on the agent's channel: {exc}")
        verify = f"{self.driver_cmd} --branch {shlex.quote(branch)} --after {step}" + (f" --pr {pr}" if pr is not None else "")
        handoff.write_plan(self.root, step=step, command=cmd, branch=branch, pr=pr, verify_lines=[verify],
                           key=pr if pr is not None else branch)
        handoff.emit(self.root, step, pr, mnemonic=mnemonic, has_assertion=False, out=lambda s: self._say(o, s))
        o.code = HANDOFF
        return o

    def verify_after(self, branch: str, base: str, step: str, *, pr: int | None = None) -> Outcome:
        """A plan's verification line: confirm that the person's step took effect, and nothing
        else. It re-reads on the ladder (a read straight after a write lags), marks the step
        verified, and never writes to GitHub or plans the next step, so the plan does exactly the
        one mutation it announces. The agent's next run carries on from the new state."""
        want = AFTER.get(step)
        if want is None:
            raise ValueError(f"no verification for step {step!r}; known: {', '.join(AFTER)}")
        head = S.local_head(self.root)
        st, ok = self._confirm(step, lambda: S.derive(self.reader, self.slug, branch, base, head),
                               lambda s: s.state in want and s.pr is not None and (pr is None or s.pr == pr))
        o = Outcome(PENDING, st)
        self._say(o, f"after {step}: state {st.state}" + (f" (PR #{st.pr})" if st.pr else "")
                  + (f": {st.detail}" if st.detail else ""))
        for layer in st.layers:
            self._say(o, f"  {layer.name}: {layer.value} [{layer.channel}]")
        if not ok:
            self._say(o, f"GitHub doesn't show the {step} yet (after the {'/'.join(map(str, LADDER))}s ladder). "
                         "Nothing was changed; run this same line again shortly:")
            self._say(o, f"{self.driver_cmd} --branch {shlex.quote(branch)} --after {step}"
                      + (f" --pr {pr}" if pr is not None else ""))
            o.code = BLOCKED if st.state == S.UNREAD else PENDING
            return o
        handoff.mark_verified(self.root, step, branch if step == "create-pr" else st.pr)
        self._clear_unconfirmed()
        self._say(o, f"verified: {step}" + (f" (PR #{st.pr})" if st.pr else "")
                  + ". Nothing more was done here; the driver's next run takes the next step.")
        o.code = DONE
        return o

    def assert_state(self, branch: str, base: str, *, head: str, pr: int) -> bool:
        """The plan's precondition: GitHub still shows the state the person approved."""
        st = S.derive(self.reader, self.slug, branch, base, None)
        return st.state == S.MERGEABLE and st.pr == pr and st.head_sha == head


# --- G3: the release driver ----------------------------------------------------------------------


def release(root: Path, slug: str, reader: Reader, *, tag: str, notes_file: str, default_branch: str,
            out: Callable[[str], None] = print, driver_cmd: str = "ratchethub release",
            after: str | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Level-triggered like the PR driver. Tagging and releasing are both a person's steps.

    `after` is a plan's verification of its own step (`tag` or `release`): it re-reads on the
    ladder until GitHub shows the step, marks it verified, and plans nothing, so a lagging read
    straight after the step can't hand the same step over again."""
    if after is not None and after not in ("tag", "release"):
        raise ValueError(f"no release verification for step {after!r}; known: tag, release")
    root = Path(root).resolve()
    branch = subprocess.run(["git", "-C", str(root), "branch", "--show-current"], capture_output=True,
                            text=True).stdout.strip()
    if branch != default_branch:
        out(f"a release is cut from {default_branch}; this checkout is on {branch or '(detached)'}")
        return BLOCKED
    dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
    if dirty.strip():
        out("the working tree has uncommitted changes; a release is cut from a clean tree")
        return BLOCKED

    def read():
        tags, ch = reader.get(f"/repos/{slug}/tags?per_page=100")
        releases, _ = reader.get(f"/repos/{slug}/releases?per_page=100")
        return (any(t.get("name") == tag for t in tags),
                any(r.get("tag_name") == tag and not r.get("draft") for r in releases), ch)

    def shows(has_tag: bool, has_release: bool) -> bool:
        return has_tag and (after == "tag" or has_release)

    try:
        has_tag, has_release, ch = read()
        if after is not None:
            for wait in LADDER:
                if shows(has_tag, has_release):
                    break
                sleep(wait)
                has_tag, has_release, ch = read()
    except Exception as exc:  # noqa: BLE001 - a failed read is no answer
        out(f"a read failed, so there is no release state to act on: {exc}")
        return BLOCKED
    out(f"release state for {tag}: tag {'present' if has_tag else 'absent'}, release "
        f"{'present' if has_release else 'absent'} [{ch}]")
    if after is not None:
        if not shows(has_tag, has_release):
            out(f"GitHub doesn't show the {after} for {tag} yet (after the {'/'.join(map(str, LADDER))}s "
                "ladder). Nothing was changed; run this same line again shortly:")
            out(f"{driver_cmd} --tag {shlex.quote(tag)} --notes {shlex.quote(notes_file)} --after {after}")
            return PENDING
        handoff.mark_verified(root, after, tag)
        out(f"verified: {after} for {tag}. Nothing more was done here; the driver's next run takes the next step.")
        return DONE
    if has_tag and has_release:
        handoff.mark_verified(root, "tag", tag)
        handoff.mark_verified(root, "release", tag)
        out(f"done: {tag} is tagged and released")
        return DONE
    if not has_tag:
        step, cmd = "tag", (f"git -C {shlex.quote(str(root))} tag -a {shlex.quote(tag)} -m {shlex.quote(tag)} && "
                            f"git -C {shlex.quote(str(root))} push origin refs/tags/{shlex.quote(tag)}")
        prose = f"Tag {tag} on {default_branch} and push the tag. A pushed tag is permanent."
    else:
        handoff.mark_verified(root, "tag", tag)  # the tag is on GitHub: its plan is spent
        if not Path(notes_file).is_file():
            out(f"the release notes file {notes_file} doesn't exist")
            return BLOCKED
        step, cmd = "release", (f"gh api -X POST repos/{slug}/releases -f tag_name={shlex.quote(tag)} "
                                f"-f name={shlex.quote(tag)} -F body=@{shlex.quote(notes_file)}")
        prose = f"Publish the GitHub release for {tag}. A published release is cached and mirrored at once."
    verify = f"{driver_cmd} --tag {shlex.quote(tag)} --notes {shlex.quote(notes_file)} --after {step}"
    handoff.write_plan(root, step=step, command=cmd, branch=default_branch, pr=None, verify_lines=[verify], key=tag)
    out("What this step does (prose, not to run):")
    out(f"  {prose}")
    out("What to run (the executable line):")
    handoff.emit(root, step, None, mnemonic=tag, has_assertion=False, out=out)
    return HANDOFF
