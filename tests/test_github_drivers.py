"""G1 and G3 through the real call path: the driver runs `gh` as a subprocess, and `gh` on PATH is
tests/fakes/gh, which keeps GitHub's state in a file. Reads go to `gh` too (the anonymous channel is
made to fail), so every read and write is the command the driver really runs."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
from pathlib import Path

import pytest

from ratchethub import guard
from ratchethub.github import driver as D
from ratchethub.github import handoff
from ratchethub.github import state as S
from ratchethub.github.read import Reader

FAKES = Path(__file__).parent / "fakes"
SLUG = "o/r"


def offline(path):
    raise urllib.error.URLError("anonymous channel unreachable in this test")


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "feat-x", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "c1"], check=True)
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    state = tmp_path / "gh.json"
    state.write_text(json.dumps({"branches": {"main": "m0"}, "pulls": [], "checks": {}}))
    monkeypatch.setenv("PATH", f"{FAKES}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    body = tmp_path / "body.md"
    body.write_text("why this change\n")
    sleeps = []

    class W:
        pass
    w = W()
    w.root, w.head, w.state_file, w.body, w.sleeps = root, head, state, str(body), sleeps
    w.lines = []
    w.driver = D.Driver(root, SLUG, Reader(anon=offline), sleep=sleeps.append, out=w.lines.append)
    w.gh = lambda: json.loads(state.read_text())
    w.set = lambda **kw: state.write_text(json.dumps({**w.gh(), **kw}))
    w.calls = lambda: [json.loads(x) for x in Path(str(state) + ".calls").read_text().splitlines()] \
        if Path(str(state) + ".calls").exists() else []
    w.run = lambda **kw: w.driver.run("feat-x", "main", title="T", body_file=w.body, **kw)
    return w


def _pushed(w, checks=None):
    g = w.gh()
    g["branches"]["feat-x"] = w.head
    if checks is not None:
        g["checks"][w.head] = checks
    w.state_file.write_text(json.dumps(g))


GREEN = [{"name": "ci", "status": "completed", "conclusion": "success"}]


def test_not_pushed_prints_the_push_for_the_agent_and_records_it_for_the_guard(world):
    o = world.run()
    assert o.code == D.ACT and o.state.state == S.NOT_PUSHED
    cmd = D.push_command(world.root, "feat-x")
    assert cmd in world.lines
    assert guard.outbound(cmd, str(world.root), False)[0] == "allow", "the exact printed push is allowed"
    assert guard.outbound(cmd + " --force", str(world.root), False)[0] == "ask"


def test_no_pr_opens_one_through_a_sanctioned_rest_write(world):
    _pushed(world, GREEN)
    o = world.run()
    writes = [c for c in world.calls() if "-X" in c]
    assert writes and writes[0][:4] == ["api", "-X", "POST", "repos/o/r/pulls"]
    assert guard.gh_verdict(["gh", *writes[0]]) is None, "the driver's own write passes the gh guard"
    assert world.gh()["pulls"][0]["body"] == "why this change\n"
    assert o.code == D.PENDING and o.state.pr == 1


def test_a_lagging_read_never_opens_a_duplicate(world):
    _pushed(world, GREEN)
    world.set(lag_on_create=10)
    world.run()
    world.run()  # the list still hides PR #1: the mutation's response and the record suppress a repeat
    assert len(world.gh()["pulls"]) == 1
    assert any("not creating another" in line or "PR #1" in line for line in world.lines)


def test_the_lag_log_partitions(world):
    _pushed(world, GREEN)
    world.set(lag_on_create=2)
    world.run()
    log = [json.loads(x) for x in (world.root / D.LAG_LOG).read_text().splitlines()]
    assert all(e["outcome"] in D.OUTCOMES for e in log)
    terminal = [e for e in log if e["outcome"] != "lagging"]
    assert len(terminal) == 1 and log[-1] is terminal[0], log


def test_pending_checks_run_the_ladder_then_hand_to_a_monitor(world):
    _pushed(world, [{"name": "ci", "status": "in_progress"}])
    world.set(pulls=[_pr(world, 1)])
    o = world.run()
    assert world.sleeps == list(D.LADDER) and o.code == D.PENDING
    assert any(line.startswith("ratchethub pr --branch feat-x") for line in world.lines)


def test_failing_checks_are_named(world):
    _pushed(world, [{"name": "lint", "status": "completed", "conclusion": "failure"}])
    world.set(pulls=[_pr(world, 1)])
    o = world.run()
    assert o.code == D.BLOCKED and "failing checks: lint" in world.lines


def test_no_checks_yet_is_pending_not_green(world):
    _pushed(world, [])
    world.set(pulls=[_pr(world, 1)])
    assert world.run().code == D.PENDING


def test_mergeable_hands_the_merge_to_a_person_with_a_pinned_plan(world):
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1)])
    o = world.run(mnemonic="docs")
    assert o.code == D.HANDOFF and not [c for c in world.calls() if "PUT" in c], "the driver never merges"
    plan = handoff.plan_path(world.root).read_text()
    assert D.merge_command(SLUG, 1, world.head) in plan and f"--assert-head {world.head} --assert-pr 1" in plan
    assert guard.relay("\n".join(world.lines), str(world.root)) is None, "the printed block is the canonical one"
    assert guard.outbound(f"bash {handoff.plan_path(world.root)}", str(world.root), False)[0] == "deny"


@pytest.mark.parametrize("pr_kw, state", [({"draft": True}, S.DRAFT), ({"mergeable": False}, S.NOT_MERGEABLE),
                                          ({"mergeable_state": "queued"}, S.QUEUED)])
def test_states_that_are_not_ready(world, pr_kw, state):
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1, **pr_kw)])
    st = S.derive(Reader(anon=offline), SLUG, "feat-x", "main", world.head)
    assert st.state == state


def test_a_queued_merge_is_not_merged(world):
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1, mergeable_state="queued")])
    o = world.run()
    assert o.code == D.PENDING and "a queued merge is not merged" in " ".join(world.lines)


def test_merged_with_the_branch_present_deletes_it_and_finishes(world):
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1, state="closed", merged=True, merged_at="2026-11-08T00:00:00Z")])
    handoff.write_plan(world.root, step="merge", command="true", branch="feat-x", pr=1, verify_lines=["true"])
    o = world.run()
    assert "feat-x" not in world.gh()["branches"] and o.code == D.DONE
    delete = [c for c in world.calls() if "DELETE" in c][0]
    assert guard.gh_verdict(["gh", *delete]) is None
    assert not handoff.plan_path(world.root).exists() and handoff.verified_marker(world.root, "merge", 1).exists()


def test_a_closed_unmerged_pr_is_never_duplicated(world):
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1, state="closed")])
    o = world.run()
    assert o.code == D.BLOCKED and len(world.gh()["pulls"]) == 1


def test_a_failed_read_is_no_answer(world, monkeypatch):
    monkeypatch.setenv("FAKE_GH_STATE", str(world.state_file) + ".missing")
    o = world.run()
    assert o.state.state == S.UNREAD and o.code == D.BLOCKED and not world.gh()["pulls"]


def test_an_unauthenticated_write_is_handed_to_a_person(world):
    _pushed(world, GREEN)
    world.set(unauthenticated=True)
    o = world.run()
    assert o.code == D.HANDOFF and "Bad credentials" in " ".join(world.lines)
    assert "repos/o/r/pulls" in handoff.plan_path(world.root).read_text()


def test_assert_state_refuses_a_moved_head(world):
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1)])
    assert world.driver.assert_state("feat-x", "main", head=world.head, pr=1)
    assert not world.driver.assert_state("feat-x", "main", head="0" * 40, pr=1)


def _pr(w, n, **kw):
    pr = {"number": n, "state": "open", "draft": False, "merged": False, "merged_at": None, "mergeable": True,
          "mergeable_state": "clean", "head": {"ref": "feat-x", "sha": w.head}, "base": {"ref": "main"}}
    pr.update(kw)
    return pr


# --- G3 ----------------------------------------------------------------------------------------------


@pytest.fixture
def main_repo(world):
    subprocess.run(["git", "-C", str(world.root), "checkout", "-q", "-b", "main"], check=True)
    notes = world.root.parent / "notes.md"
    notes.write_text("notes\n")
    return world, str(notes)


def _release(w, notes, tag="v1.0.0"):
    return D.release(w.root, SLUG, Reader(anon=offline), tag=tag, notes_file=notes, default_branch="main",
                     out=w.lines.append)


def test_release_tags_first_as_a_person_step_with_prose_and_command_apart(main_repo):
    w, notes = main_repo
    assert _release(w, notes) == D.HANDOFF
    text = "\n".join(w.lines)
    assert text.index("What this step does") < text.index("What to run")
    assert "refs/tags/v1.0.0" in handoff.plan_path(w.root).read_text()


def test_release_publishes_once_tagged(main_repo):
    w, notes = main_repo
    w.set(tags=[{"name": "v1.0.0"}])
    assert _release(w, notes) == D.HANDOFF
    assert "repos/o/r/releases -f tag_name=v1.0.0" in handoff.plan_path(w.root).read_text()


def test_release_done_when_tag_and_release_exist(main_repo):
    w, notes = main_repo
    w.set(tags=[{"name": "v1.0.0"}], releases=[{"tag_name": "v1.0.0", "draft": False}])
    assert _release(w, notes) == D.DONE


def test_release_refuses_off_the_default_branch_or_dirty(world, tmp_path):
    assert D.release(world.root, SLUG, Reader(anon=offline), tag="v1", notes_file="n", default_branch="main",
                     out=world.lines.append) == D.BLOCKED
    subprocess.run(["git", "-C", str(world.root), "checkout", "-q", "-b", "main"], check=True)
    (world.root / "dirty.txt").write_text("x")
    assert D.release(world.root, SLUG, Reader(anon=offline), tag="v1", notes_file="n", default_branch="main",
                     out=world.lines.append) == D.BLOCKED


def test_cli_pr_runs_the_driver_and_asserts(world, capsys):
    from ratchethub.cli import main

    subprocess.run(["git", "-C", str(world.root), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    (world.root / "ratchethub.toml").write_text('[ratchethub]\nschema = 2\n[project]\nname = "p"\n'
                                                '[github]\ndefault_branch = "main"\n')
    import ratchethub.github.read as rd
    orig = rd.Reader.__init__
    rd.Reader.__init__ = lambda self, anon=offline, gh=None, budget=None: orig(self, anon, gh, budget or rd.Budget())
    try:
        assert main(["--root", str(world.root), "pr"]) == D.ACT
        _pushed(world, GREEN)
        world.set(pulls=[_pr(world, 1)])
        assert main(["--root", str(world.root), "pr", "--assert-head", world.head, "--assert-pr", "1"]) == 0
        assert main(["--root", str(world.root), "pr", "--assert-head", "f" * 40, "--assert-pr", "1"]) == 1
    finally:
        rd.Reader.__init__ = orig
