"""G7 (the reusable checks) and G8 (supply-chain pins)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from ratchethub.ci import md_content, protected, scope

REPO = Path(__file__).parents[1]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

# --- scope review -------------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path):
    r = tmp_path / "r"
    run = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "docs").mkdir()
    (r / "docs" / "a.md").write_text("alpha beta gamma\n" * 5)
    (r / "keep.py").write_text("x = 1\n")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    run("checkout", "-q", "-b", "feat")
    return r, run


def test_scope_review_lists_both_sides_of_a_rename(git_repo):
    r, run = git_repo
    run("mv", "docs/a.md", "docs/b.md")
    run("commit", "-q", "-m", "rename")
    paths = scope.changed(str(r), "main", "HEAD")
    assert "docs/a.md" in paths and "docs/b.md" in paths
    assert scope.review("```scope\ndocs/b.md\n```", paths) == ["docs/a.md"]
    assert scope.review("```scope\ndocs/\n```", paths) == []


@pytest.mark.parametrize("body", ["no block", "```scope\n\n```", "```scope\n# only a comment\n```"])
def test_scope_review_fails_closed_without_a_scope(body):
    with pytest.raises(scope.Malformed):
        scope.review(body, ["x"])


def test_scope_review_folder_entries_need_the_slash(git_repo):
    assert scope.covered("docs/a.md", ["docs/"]) and not scope.covered("docs/a.md", ["docs"])
    assert not scope.covered("docsx/a.md", ["docs/"])


def test_scope_review_main_exit_codes(git_repo, tmp_path):
    r, run = git_repo
    (r / "keep.py").write_text("x = 2\n")
    run("commit", "-q", "-am", "edit")
    body = tmp_path / "body.md"
    body.write_text("```scope\ndocs/\n```")
    assert scope.main(["--repo", str(r), "--base", "main", "--body-file", str(body)]) == 1
    body.write_text("```scope\nkeep.py\n```")
    assert scope.main(["--repo", str(r), "--base", "main", "--body-file", str(body)]) == 0
    assert scope.main(["--repo", str(r), "--base", "nope", "--body-file", str(body)]) == 2


# --- markdown content ----------------------------------------------------------------------------


def test_markdown_content_selftest_passes():
    assert md_content.selftest() == 0


def test_markdown_content_catches_the_trailing_space_defect_and_ignores_reflow():
    assert md_content.compare(md_content.SELFTEST_BASE, md_content.SELFTEST_FORMATTED)[0] == "identical"
    assert md_content.compare(md_content.SELFTEST_FORMATTED, md_content.SELFTEST_DEFECT)[0] == "changed"
    assert md_content.compare("a b c", "c b a")[0] == "reordered"


def test_markdown_content_over_a_real_range(git_repo):
    r, run = git_repo
    (r / "docs" / "a.md").write_text("alpha beta\ngamma\n" * 5)
    run("commit", "-q", "-am", "reflow")
    assert md_content.check_range(str(r), "main", "HEAD")[1] == []
    (r / "docs" / "a.md").write_text("alpha delta gamma\n" * 5)
    run("commit", "-q", "-am", "edit")
    assert md_content.check_range(str(r), "main", "HEAD")[1] == ["docs/a.md"]


# --- protected elements --------------------------------------------------------------------------


def test_protected_elements(tmp_path):
    (tmp_path / "a").write_text("x")
    good = hashlib.sha256(b"x").hexdigest()
    assert protected.check(tmp_path, {"a": good}) == []
    assert protected.check(tmp_path, {"a": "0" * 64}) == ["a: content changed"]
    assert protected.check(tmp_path, {"gone": good}) == ["gone: missing"]
    for bad in ({}, {"a": "short"}, []):
        with pytest.raises(ValueError):
            protected.check(tmp_path, bad)


# --- G8: supply-chain pins -----------------------------------------------------------------------

USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$", re.M)


def test_there_are_workflows_to_check():
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "scope-review.yml", "markdown-content.yml",
                                           "protected-elements.yml"}


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_commit(wf):
    found = USES.findall(wf.read_text())
    for ref, comment in found:
        name, _, version = ref.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", version), f"{wf.name}: {ref} is not pinned to a commit"
        assert comment.strip().startswith("# v"), f"{wf.name}: {ref} has no version comment"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_every_install_is_pinned_exactly(wf):
    for line in re.findall(r"pip install[^\n]*", wf.read_text()):
        pkgs = [w for w in line.split()[2:] if not w.startswith("-")]
        assert pkgs and all("==" in p for p in pkgs), f"{wf.name}: unpinned install: {line}"


@pytest.mark.parametrize("wf", [w for w in WORKFLOWS if w.name != "ci.yml"], ids=lambda p: p.name)
def test_each_reusable_workflow_refuses_an_unpinned_ratchethub_ref(wf):
    text = wf.read_text()
    assert "workflow_call:" in text and "grep -Eq '^[0-9a-f]{40}$'" in text
    assert "persist-credentials: false" in text
    assert "${{ github.event.pull_request.body }}" not in text.split("env:")[0], "untrusted input in a run line"


def test_the_advisory_gate_says_so_and_cannot_fail_the_run():
    text = (REPO / ".github" / "workflows" / "protected-elements.yml").read_text()
    assert "name: protected elements (advisory)" in text and "continue-on-error: true" in text


def test_untrusted_pr_fields_reach_shell_only_through_env():
    for wf in WORKFLOWS:
        for line in wf.read_text().splitlines():
            if line.strip().startswith("run:"):
                assert "github.event" not in line, f"{wf.name}: {line.strip()}"


def test_python_package_has_no_runtime_dependencies_and_a_pinned_build():
    meta = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert meta["project"]["dependencies"] == []
    assert all("==" in r for r in meta["build-system"]["requires"])
