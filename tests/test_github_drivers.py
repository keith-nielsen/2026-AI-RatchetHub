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
    o = world.run(mnemonic="s8")
    assert o.code == D.HANDOFF and "Bad credentials" in " ".join(world.lines)
    assert "repos/o/r/pulls" in handoff.plan_path(world.root).read_text()
    relayed = (world.root / guard.RELAY_LINE).read_text().strip()
    assert relayed.endswith("# s8 create-pr"), "the change's tag survives a failed write"
    assert guard.relay("\n".join(world.lines), str(world.root)) is None


def test_the_push_is_not_claimed_for_the_agent(world):
    """Where the agent's channel has no GitHub credential the push is the person's, so the
    driver's line can't name the agent as the one who runs it."""
    world.run()
    text = " ".join(world.lines)
    assert "(agent)" not in text and "otherwise a person does" in text


def test_a_plan_verifies_with_the_same_ratchethub_without_it_on_path(world, tmp_path):
    """The first real handoff's verify line called `ratchethub`, which wasn't installed: the PR
    would have opened and the plan then failed. The line must run from anywhere, clean env."""
    from ratchethub.cli import self_command
    cmd = self_command(world.root, "pr") + " --help"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    r = subprocess.run(["bash", "-c", cmd], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "--assert-head" in r.stdout, r.stderr
    assert str(world.root.resolve()) in cmd


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
    assert handoff.verified_marker(w.root, "tag", "v1.0.0").exists(), "the tag is on GitHub: its plan is spent"


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


@pytest.mark.parametrize("merge_state, want", [
    ("clean", S.MERGEABLE), ("has_hooks", S.MERGEABLE),
    ("blocked", S.NOT_MERGEABLE), ("behind", S.NOT_MERGEABLE), ("dirty", S.NOT_MERGEABLE),
    ("unstable", S.NOT_MERGEABLE), ("unknown", S.CHECKS_PENDING), (None, S.CHECKS_PENDING),
])
def test_mergeable_needs_a_clean_merge_state_not_just_no_conflicts(world, merge_state, want):
    """`mergeable: true` means no conflict; a ruleset can still refuse the merge (blocked), or the
    head is out of date (behind). Values: GitHub's MergeStateStatus enum, lower-cased in REST."""
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1, mergeable_state=merge_state)])
    st = S.derive(Reader(anon=offline), SLUG, "feat-x", "main", world.head)
    assert st.state == want, (st.state, st.detail)
    if want == S.NOT_MERGEABLE:
        assert merge_state in st.detail


def test_cli_pr_plans_verify_with_the_same_ratchethub(world):
    """Through the CLI, a handed-over write's plan verifies with `self_command`, not a bare
    `ratchethub` that may not be on the person's PATH."""
    from ratchethub.cli import main, self_command
    subprocess.run(["git", "-C", str(world.root), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    (world.root / "ratchethub.toml").write_text('[ratchethub]\nschema = 2\n[project]\nname = "p"\n'
                                                '[github]\ndefault_branch = "main"\n')
    _pushed(world, GREEN)
    world.set(unauthenticated=True)
    import ratchethub.github.read as rd
    orig = rd.Reader.__init__
    rd.Reader.__init__ = lambda self, anon=offline, gh=None, budget=None: orig(self, anon, gh, budget or rd.Budget())
    try:
        assert main(["--root", str(world.root), "pr", "--title", "T", "--body-file", world.body]) == D.HANDOFF
    finally:
        rd.Reader.__init__ = orig
    plan = handoff.plan_path(world.root).read_text().splitlines()
    assert self_command(world.root, "pr") + " --branch feat-x --after create-pr" in plan


def test_cli_release_plans_verify_with_the_same_ratchethub(main_repo):
    from ratchethub.cli import main, self_command
    w, notes = main_repo
    subprocess.run(["git", "-C", str(w.root), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    (w.root / "ratchethub.toml").write_text('[ratchethub]\nschema = 2\n[project]\nname = "p"\n'
                                            '[github]\ndefault_branch = "main"\n')
    subprocess.run(["git", "-C", str(w.root), "add", "ratchethub.toml"], check=True)
    subprocess.run(["git", "-C", str(w.root), "commit", "-q", "-m", "declare"], check=True)
    import ratchethub.github.read as rd
    orig = rd.Reader.__init__
    rd.Reader.__init__ = lambda self, anon=offline, gh=None, budget=None: orig(self, anon, gh, budget or rd.Budget())
    try:
        assert main(["--root", str(w.root), "release", "--tag", "v1.0.0", "--notes", notes]) == D.HANDOFF
    finally:
        rd.Reader.__init__ = orig
    plan = handoff.plan_path(w.root).read_text().splitlines()
    assert any(line.startswith(self_command(w.root, "release") + " --tag v1.0.0") for line in plan), plan


# --- a plan's verification confirms its own step and does nothing else ---------------------------


def _cli_world(w):
    """Make `world` a repo the CLI accepts, and drive it through `self_command`, as a real plan does."""
    from ratchethub.cli import self_command
    subprocess.run(["git", "-C", str(w.root), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
    (w.root / ".git" / "info" / "exclude").write_text("ratchethub.toml\n")
    (w.root / "ratchethub.toml").write_text('[ratchethub]\nschema = 2\n[project]\nname = "p"\n'
                                            '[github]\ndefault_branch = "main"\n')
    w.driver.driver_cmd = self_command(w.root, "pr")


def _run_plan(w):
    """Run next.sh as the person does. The anonymous channel is pointed at a closed port, so every
    read the plan's own driver run makes falls back to the fake `gh`."""
    env = {**os.environ, "HTTPS_PROXY": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
           "NO_PROXY": "", "no_proxy": ""}
    return subprocess.run(["bash", str(handoff.plan_path(w.root))], env=env, capture_output=True, text=True,
                          timeout=120)


def _no_sleep_ladder(monkeypatch):
    """In-process only: a plan's own driver run is a subprocess and waits on the real ladder."""
    monkeypatch.setattr(D, "LADDER", (0, 0, 0))


def test_the_merge_plan_survives_a_lagging_read_and_does_only_the_merge(world, monkeypatch):
    """PR #1's real run: the read straight after the merge still showed the PR open, so the plan's
    verification wrote a fresh merge plan; on a current read it would have deleted the branch,
    a mutation the plan never announced. Now it waits out the lag and only confirms."""
    _no_sleep_ladder(monkeypatch)
    _cli_world(world)
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1)], lag_on_merge=2)
    assert world.run(mnemonic="t").code == D.HANDOFF
    r = _run_plan(world)
    assert r.returncode == 0, r.stdout + r.stderr
    g = world.gh()
    assert g["pulls"][0]["merged"] and "feat-x" in g["branches"], "merged, and the branch left for the next run"
    writes = [c for c in world.calls() if "-X" in c]
    assert [c[2] for c in writes] == ["PUT"], writes
    assert "verified: merge (PR #1)" in r.stdout
    assert "[gh-api]" in r.stdout and "[anon-rest]" not in r.stdout, "the plan's reads reached the fake, not GitHub"
    log = [json.loads(x) for x in (world.root / D.LAG_LOG).read_text().splitlines() if '"merge"' in x]
    assert log[0]["outcome"] == "lagging" and log[-1]["outcome"] == "confirmed", "the lag was real and waited out"
    assert handoff.verified_marker(world.root, "merge", 1).exists()
    assert not handoff.plan_path(world.root).exists() and not (world.root / guard.RELAY_LINE).exists()


def test_the_next_run_after_a_verified_merge_deletes_the_branch(world, monkeypatch):
    _no_sleep_ladder(monkeypatch)
    _cli_world(world)
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1)])
    world.run()
    assert _run_plan(world).returncode == 0
    o = world.run()
    assert o.code == D.DONE and "feat-x" not in world.gh()["branches"]


def test_a_handed_over_delete_plan_verifies_and_is_then_spent(world, monkeypatch):
    _no_sleep_ladder(monkeypatch)
    _cli_world(world)
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1, state="closed", merged=True, merged_at="2026-11-08T00:00:00Z")],
              unauthenticated=True)
    assert world.run(mnemonic="t").code == D.HANDOFF
    plan = handoff.plan_path(world.root).read_text()
    assert "--after delete-branch --pr 1" in plan
    world.set(unauthenticated=False)
    r = _run_plan(world)
    assert r.returncode == 0 and "verified: delete-branch (PR #1)" in r.stdout, r.stdout + r.stderr
    handoff.plan_path(world.root).write_text(plan)  # a kept copy of the spent plan
    calls = len(world.calls())
    again = _run_plan(world)
    assert again.returncode == 1 and "already verified" in again.stderr
    assert len(world.calls()) == calls, "the spent copy made no call at all"


def test_a_handed_over_create_pr_plan_is_spent_once_the_pr_exists(world, monkeypatch):
    """PR #1's create plan was never marked spent: the PR went straight to checks-pending, and
    only the no-PR path could have marked it. The plan's own verification marks it now."""
    _no_sleep_ladder(monkeypatch)
    _cli_world(world)
    _pushed(world, [{"name": "ci", "status": "in_progress"}])
    world.set(unauthenticated=True)
    assert world.run(mnemonic="t").code == D.HANDOFF
    plan = handoff.plan_path(world.root).read_text()
    world.set(unauthenticated=False)
    r = _run_plan(world)
    assert r.returncode == 0 and "verified: create-pr (PR #1)" in r.stdout, r.stdout + r.stderr
    assert len(world.gh()["pulls"]) == 1
    handoff.plan_path(world.root).write_text(plan)
    calls = len(world.calls())
    again = _run_plan(world)
    assert again.returncode == 1 and "already verified" in again.stderr
    assert len(world.calls()) == calls and len(world.gh()["pulls"]) == 1, "the kept copy opened no second PR"


def test_a_spent_create_pr_does_not_spend_another_branchs(world, monkeypatch):
    """Keyed `create-pr-none`, the first verified create would have refused every later branch's."""
    _no_sleep_ladder(monkeypatch)
    _cli_world(world)
    _pushed(world, GREEN)
    world.set(unauthenticated=True)
    world.run()
    world.set(unauthenticated=False)
    assert _run_plan(world).returncode == 0
    subprocess.run(["git", "-C", str(world.root), "checkout", "-q", "-b", "feat-y"], check=True)
    subprocess.run(["git", "-C", str(world.root), "commit", "-q", "--allow-empty", "-m", "c2"], check=True)
    y = subprocess.run(["git", "-C", str(world.root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    g = world.gh(); g["branches"]["feat-y"] = y; g["checks"][y] = GREEN; g["unauthenticated"] = True
    world.state_file.write_text(json.dumps(g))
    assert world.driver.run("feat-y", "main", title="T2", body_file=world.body).code == D.HANDOFF
    world.set(unauthenticated=False)
    r = _run_plan(world)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [p["head"]["ref"] for p in world.gh()["pulls"]] == ["feat-x", "feat-y"]


def test_a_step_github_never_shows_is_handed_back_unchanged(world, monkeypatch):
    _no_sleep_ladder(monkeypatch)
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1)])
    o = world.driver.verify_after("feat-x", "main", "merge", pr=1)
    assert o.code == D.PENDING and not [c for c in world.calls() if "-X" in c]
    assert not handoff.plan_path(world.root).exists(), "a verification never writes a plan"
    assert not handoff.verified_marker(world.root, "merge", 1).exists()
    assert any(line.endswith("--branch feat-x --after merge --pr 1") for line in world.lines)


def test_a_merge_of_another_pr_does_not_verify_this_one(world, monkeypatch):
    _no_sleep_ladder(monkeypatch)
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 2, state="closed", merged=True, merged_at="2026-11-08T00:00:00Z")])
    assert world.driver.verify_after("feat-x", "main", "merge", pr=1).code == D.PENDING
    assert world.driver.verify_after("feat-x", "main", "merge", pr=2).code == D.DONE


def test_a_verification_waits_on_the_ladder(world):
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1)])
    def merge_during_the_wait(seconds):
        world.sleeps.append(seconds)
        world.set(pulls=[_pr(world, 1, state="closed", merged=True, merged_at="2026-11-08T00:00:00Z")])
    world.driver.sleep = merge_during_the_wait
    assert world.driver.verify_after("feat-x", "main", "merge", pr=1).code == D.DONE
    assert world.sleeps == [D.LADDER[0]]


def test_an_unread_state_never_verifies(world, monkeypatch):
    _no_sleep_ladder(monkeypatch)
    monkeypatch.setenv("FAKE_GH_STATE", str(world.state_file) + ".missing")
    assert world.driver.verify_after("feat-x", "main", "merge", pr=1).code == D.BLOCKED


def test_an_unknown_step_is_refused(world):
    with pytest.raises(ValueError):
        world.driver.verify_after("feat-x", "main", "push")


def test_cli_after_and_pr_flags(world, monkeypatch):
    from ratchethub.cli import main
    _no_sleep_ladder(monkeypatch)
    _cli_world(world)
    _pushed(world, GREEN)
    world.set(pulls=[_pr(world, 1, state="closed", merged=True, merged_at="2026-11-08T00:00:00Z")])
    import ratchethub.github.read as rd
    orig = rd.Reader.__init__
    rd.Reader.__init__ = lambda self, anon=offline, gh=None, budget=None: orig(self, anon, gh, budget or rd.Budget())
    try:
        assert main(["--root", str(world.root), "pr", "--after", "merge", "--pr", "1"]) == D.DONE
        assert "feat-x" in world.gh()["branches"]
        with pytest.raises(SystemExit):
            main(["--root", str(world.root), "pr", "--pr", "1"])
        with pytest.raises(SystemExit):
            main(["--root", str(world.root), "pr", "--after", "push"])
    finally:
        rd.Reader.__init__ = orig


def _release_after(w, notes, after, tag="v1.0.0", sleep=None):
    return D.release(w.root, SLUG, Reader(anon=offline), tag=tag, notes_file=notes, default_branch="main",
                     out=w.lines.append, after=after, sleep=sleep or w.sleeps.append)


def test_release_plans_verify_their_own_step(main_repo):
    w, notes = main_repo
    _release(w, notes)
    assert "--after tag" in handoff.plan_path(w.root).read_text()
    w.set(tags=[{"name": "v1.0.0"}])
    _release(w, notes)
    assert "--after release" in handoff.plan_path(w.root).read_text()


def test_a_tag_verification_waits_out_the_lag_and_plans_nothing(main_repo):
    w, notes = main_repo
    def tag_lands(seconds):
        w.sleeps.append(seconds)
        w.set(tags=[{"name": "v1.0.0"}])
    assert _release_after(w, notes, "tag", sleep=tag_lands) == D.DONE
    assert w.sleeps == [D.LADDER[0]] and not handoff.plan_path(w.root).exists()
    assert handoff.verified_marker(w.root, "tag", "v1.0.0").exists()


def test_a_release_github_never_shows_is_handed_back_unchanged(main_repo):
    w, notes = main_repo
    w.set(tags=[{"name": "v1.0.0"}])
    assert _release_after(w, notes, "release") == D.PENDING
    assert w.sleeps == list(D.LADDER) and not handoff.plan_path(w.root).exists()
    assert not handoff.verified_marker(w.root, "release", "v1.0.0").exists()


def test_one_release_does_not_spend_the_next(main_repo):
    """Keyed `release-none`, the first finished release would have refused every later one."""
    w, notes = main_repo
    w.set(tags=[{"name": "v1.0.0"}], releases=[{"tag_name": "v1.0.0", "draft": False}])
    assert _release(w, notes) == D.DONE
    assert _release(w, notes, tag="v1.0.1") == D.HANDOFF
    plan = handoff.plan_path(w.root).read_text()
    assert str(handoff.verified_marker(w.root, "tag", "v1.0.1")) in plan
    assert not handoff.verified_marker(w.root, "tag", "v1.0.1").exists()
