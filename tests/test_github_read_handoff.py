"""G4 (read channel) and G2 (handoff file). The handoff script is run for real, under bash."""

from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

from ratchethub import guard
from ratchethub.github import handoff as h
from ratchethub.github.read import ANON, GH, Reader, ReadError, slug_from_remote, summarize_checks

# --- G4 --------------------------------------------------------------------------------------------


def http_error(code, headers=None):
    return urllib.error.HTTPError("u", code, "x", headers or {}, io.BytesIO(b""))


def test_anonymous_answers_first_and_names_its_channel():
    r = Reader(anon=lambda p: {"ok": p}, gh=lambda p: pytest.fail("gh must not be asked"))
    assert r.get("/x") == ({"ok": "/x"}, ANON)


@pytest.mark.parametrize("err", [http_error(404), http_error(403, {"X-RateLimit-Remaining": "0"}),
                                 urllib.error.URLError("offline"), TimeoutError()])
def test_falls_back_to_gh_and_names_it(err):
    def anon(p):
        raise err
    r = Reader(anon=anon, gh=lambda p: {"via": "gh"})
    assert r.get("/x") == ({"via": "gh"}, GH)


def test_a_rate_limited_error_records_the_budget():
    r = Reader(anon=lambda p: (_ for _ in ()).throw(http_error(403, {"X-RateLimit-Remaining": "0",
                                                                    "X-RateLimit-Limit": "60"})),
               gh=lambda p: {})
    r.get("/x")
    assert r.budget.report().startswith("0/60")


def test_both_channels_failing_is_an_error_never_an_empty_answer():
    def fail(p):
        raise ReadError("no")
    r = Reader(anon=lambda p: (_ for _ in ()).throw(urllib.error.URLError("offline")), gh=fail)
    with pytest.raises(ReadError, match="both channels failed"):
        r.get("/x")


def test_pulls_for_branch_asks_for_every_state():
    seen = []
    Reader(anon=lambda p: seen.append(p) or [], gh=None).pulls_for_branch("o/r", "feat/x")
    assert "state=all" in seen[0] and "head=o:feat/x" in seen[0]


@pytest.mark.parametrize("payload", [{}, {"check_runs": None}, [], None])
def test_summarize_refuses_an_empty_read(payload):
    with pytest.raises(ReadError):
        summarize_checks(payload)


def test_summarize_counts_skipped_and_neutral_as_passing():
    runs = [{"name": "a", "status": "completed", "conclusion": "success"},
            {"name": "b", "status": "completed", "conclusion": "skipped"},
            {"name": "c", "status": "completed", "conclusion": "neutral"},
            {"name": "d", "status": "in_progress"},
            {"name": "e", "status": "completed", "conclusion": "failure"},
            {"name": "e", "status": "completed", "conclusion": "cancelled"}]
    assert summarize_checks({"check_runs": runs}) == (6, ["d"], ["e"])


@pytest.mark.parametrize("url, slug", [
    ("https://github.com/o/r.git", "o/r"), ("git@github.com:o/r.git", "o/r"), ("https://github.com/o/r", "o/r"),
    ("https://example.com/o/r.git", None)])
def test_slug_comes_from_the_remote(tmp_path, url, slug):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", url], check=True)
    assert slug_from_remote(str(tmp_path)) == slug


def test_no_remote_is_no_slug(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert slug_from_remote(str(tmp_path)) is None


# --- G2 --------------------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "feat-x", str(r)], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    return r


def run_plan(repo):
    return subprocess.run(["bash", str(h.plan_path(repo))], capture_output=True, text=True)


def plan(repo, **kw):
    args = dict(step="merge", command="echo MUTATED > mutated.txt", branch="feat-x", pr=7,
                verify_lines=["echo VERIFIED"])
    args.update(kw)
    return h.write_plan(repo, **args)


def test_the_plan_runs_the_mutation_then_its_verification(repo):
    plan(repo)
    r = run_plan(repo)
    assert r.returncode == 0 and "VERIFIED" in r.stdout and (repo / "mutated.txt").exists()
    text = h.plan_path(repo).read_text()
    assert "step merge, branch feat-x, PR 7" in text


def test_the_plan_refuses_another_branch(repo):
    plan(repo, branch="other")
    r = run_plan(repo)
    assert r.returncode == 1 and "for branch other" in r.stderr and not (repo / "mutated.txt").exists()


def test_the_plan_refuses_once_expired(repo):
    plan(repo, now=1.0)
    r = run_plan(repo)
    assert r.returncode == 1 and "expired" in r.stderr and not (repo / "mutated.txt").exists()


def test_the_plan_refuses_once_its_step_verified(repo):
    plan(repo)
    marker = h.verified_marker(repo, "merge", 7)
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    r = run_plan(repo)
    assert r.returncode == 1 and "already verified" in r.stderr and not (repo / "mutated.txt").exists()


def test_mark_verified_removes_the_plan_and_the_emission(repo):
    plan(repo)
    h.write_emission(repo, "merge", "git push", "feat-x")
    h.mark_verified(repo, "merge", 7)
    assert not h.plan_path(repo).exists() and not (repo / guard.EMISSION).exists()
    assert h.verified_marker(repo, "merge", 7).exists()


def test_a_failing_assertion_stops_before_the_mutation(repo):
    plan(repo, assert_lines=["false"])
    assert run_plan(repo).returncode != 0 and not (repo / "mutated.txt").exists()


@pytest.mark.parametrize("kw, needle", [({"branch": ""}, "branch"), ({"step": "Merge Now"}, "step id"),
                                        ({"foreign": True}, "foreign"), ({"verify_lines": []}, "verify")])
def test_write_plan_refuses_a_plan_it_cant_guard(repo, kw, needle):
    with pytest.raises(ValueError, match=needle):
        plan(repo, **kw)


def test_a_foreign_plan_skips_the_checkout_comparison_but_keeps_the_assertion(repo):
    plan(repo, branch="dependabot/x", foreign=True, assert_lines=["echo ASSERTED"])
    r = run_plan(repo)
    assert r.returncode == 0 and "ASSERTED" in r.stdout


def test_emission_lets_the_outbound_guard_allow_the_exact_command(repo):
    h.write_emission(repo, "push", "git push -u origin feat-x", "feat-x")
    assert guard.outbound("git push -u origin feat-x", str(repo), False)[0] == "allow"
    assert guard.outbound("git push origin feat-x", str(repo), False)[0] == "ask"


def test_the_emitted_block_satisfies_the_relay_guard(repo):
    out = []
    line = h.emit(repo, "merge", 7, mnemonic="docs", has_assertion=True, out=out.append)
    message = "\n".join(out)
    assert line.startswith("bash ") and line.split("  # ", 1)[1] in ("docs merge", "docs merge #7")
    assert guard.relay(message, str(repo)) is None
    assert guard.relay(message.replace("# docs merge", "# merge"), str(repo)), "a dropped tag must be caught"


def test_relay_line_shortens_home_paths(monkeypatch, repo):
    monkeypatch.setenv("HOME", str(repo.parent))
    assert h.relay_line(repo, "merge", None).startswith("bash ~/repo/.git/ratchethub/next.sh")


def test_a_long_line_says_so(monkeypatch, tmp_path):
    deep = tmp_path / ("d" * 90)
    deep.mkdir()
    out = []
    h.emit(deep, "merge", 1, has_assertion=False, out=out.append)
    assert any("columns" in line for line in out)


def test_a_real_length_line_fits_with_the_run_prefix(monkeypatch, tmp_path):
    """This repository's own path, under a home folder, with the operator's `! ` prefix: the
    first real run printed 90 columns and asked the person to widen the terminal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "Documents" / "repo" / "ratchethub"
    repo.mkdir(parents=True)
    for step, pr in (("create-pr", None), ("delete-branch", 123), ("merge", 9999)):
        line = h.relay_line(repo, step, pr, mnemonic="s8")
        assert len(h.RUN_PREFIX + line) <= h.MAX_WIDTH, (len(h.RUN_PREFIX + line), line)
        out = []
        h.emit(repo, step, pr, mnemonic="s8", has_assertion=False, out=out.append)
        assert not any("columns" in o for o in out)
    assert h.relay_line(repo, "merge", 7, mnemonic="s8").endswith("# s8 merge #7"), "the PR joins when it fits"


def test_the_width_warning_counts_the_run_prefix(monkeypatch, tmp_path):
    """A line that fits only without the `! ` prefix still wraps when run with it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "r"
    repo.mkdir()
    base = len(h.relay_line(repo, "merge", None, mnemonic=""))
    tag = "x" * (h.MAX_WIDTH - 1 - base)  # the bare line is MAX_WIDTH - 1 columns
    assert len(h.relay_line(repo, "merge", None, mnemonic=tag)) == h.MAX_WIDTH - 1
    out = []
    h.emit(repo, "merge", None, mnemonic=tag, has_assertion=False, out=out.append)
    assert any("columns" in o for o in out)
