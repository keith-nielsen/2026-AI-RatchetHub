"""The hooks (A3–A5). Refusals first; each pass case is a lesson where an earlier guard over-fired."""

from __future__ import annotations

import io
import json
import time

import pytest

from ratchethub import guard as g

# --- the tokenizer: what is and isn't a command -----------------------------------------------------


@pytest.mark.parametrize("text, expect", [
    ("git push origin main", [["git", "push", "origin", "main"]]),
    ("cd /x && git push", [["cd", "/x"], ["git", "push"]]),
    ("FOO=1 BAR=2 git push", [["git", "push"]]),
    ("env -u X FOO=1 git push", [["git", "push"]]),
    ("sudo -u bob git push", [["git", "push"]]),
    ("timeout 30 git push", [["git", "push"]]),
    ("timeout -s KILL 30 git push", [["git", "push"]]),
    ("nohup git push &", [["git", "push"]]),
    ("echo a#b # git push", [["echo", "a#b"]]),
    ("echo 'git push'", [["echo", "git push"]]),
    ('git commit -m "converted the git push call"', [["git", "commit", "-m", "converted the git push call"]]),
    ("cat <<EOF\ngit push\nEOF", [["cat"]]),
    ("cat <<'EOF' > f\ngit push\nEOF\necho done", [["cat"], ["echo", "done"]]),
    ("if true; then git push; fi", [["true"], ["git", "push"]]),
    ("{ git push; }", [["git", "push"]]),
    ("(git push)", [["git", "push"]]),
    ("a | b || c; d", [["a"], ["b"], ["c"], ["d"]]),
    ("git \\\n push", [["git", "push"]]),
    ("/usr/bin/git push", [["/usr/bin/git", "push"]]),
])
def test_commands(text, expect):
    assert g.commands(text) == expect


@pytest.mark.parametrize("text", [
    'echo "$(git push)"', "echo $(git push)", "echo `git push`", 'x="`git push`"',
    "bash -c 'git push'", "sh -ec 'git push'", "eval git push", "bash <<EOF\ngit push\nEOF",
    "cat <(git push)", "xargs -n1 git push < refs", "command git push", "exec git push",
])
def test_hidden_commands_are_found(text):
    assert any(a[:2] == ["git", "push"] for a in g.commands(text)), g.commands(text)


@pytest.mark.parametrize("text", ["echo 'unterminated", 'echo "x', "echo $(x", "echo `x", "cat <<EOF\nx", "cat <<EOF"])
def test_unreadable_commands_raise(text):
    with pytest.raises(g.ParseError):
        g.commands(text)


# --- A3: outbound --------------------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "git push --tags", "git push origin refs/tags/v1.0", "git push --follow-tags", "git push --mirror",
    "git -C /r remote add backup https://example.invalid/x.git", "git remote set-url origin x",
    "gh repo create o/new", "gh repo fork o/r", "gh release create v1 --notes x",
    "gh release upload v1 a.tgz", "gh repo edit --visibility public",
    "gh api -X POST repos/o/r/releases -f tag_name=v1", "gh api --method=POST repos/o/r/releases",
    "gh api -XPOST repos/o/r/git/refs -f ref=refs/tags/v1", "gh api repos/o/r/releases -f tag_name=v1",
    "gh api -X PUT repos/o/r/contents/README.md -f message=m", "gh api -X POST user/repos -f name=x",
    "gh api -X DELETE /repos/o/r/releases/123", "gh api -X PUT repos/o/r/contents/docs/deep/a.md -f message=m",
    "gh api -X POST https://uploads.github.com/repos/o/r/releases/1/assets?name=a.tgz --input a.tgz",
    "curl -X POST https://uploads.github.com/repos/o/r/releases/1/assets", "npm publish", "pnpm publish",
    "python3 -m twine upload dist/*", "docker push img", "cargo publish", "uv publish",
    "bash -c 'git push --tags'", 'echo "$(npm publish)"',
])
def test_outbound_denies(cmd):
    v = g.outbound(cmd, "/nonexistent", no_remotes=False)
    assert v and v[0] == "deny", (cmd, v)


@pytest.mark.parametrize("cmd", ["git push origin feat/x", "git -C /r push -u origin feat/x",
                                 "cd /r && git push", "timeout 60 git push"])
def test_branch_push_asks(cmd):
    assert g.outbound(cmd, "/nonexistent", no_remotes=False)[0] == "ask"


def test_no_remotes_denies_a_branch_push():
    assert g.outbound("git push origin feat/x", "/nonexistent", no_remotes=True)[0] == "deny"


@pytest.mark.parametrize("cmd", [
    'git commit -m "converted the gh release create call"', "echo git push --tags",
    "git log --oneline", "gh api repos/o/r/releases", "gh api -X GET repos/o/r/releases/1",
    "grep -r 'npm publish' docs", "cat <<EOF > notes.md\ngit push --tags\nEOF",
    "git remote -v", "git pushd", "gh api repos/o/r/pulls -X GET -f state=open",
])
def test_outbound_passes(cmd):
    assert g.outbound(cmd, "/nonexistent", no_remotes=False) is None, cmd


def test_outbound_unreadable_is_denied():
    assert g.outbound("git push 'x", "/n", False)[0] == "deny"


def _repo(tmp_path, branch="feat/x"):
    (tmp_path / ".git" / "ratchethub").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    return tmp_path


def _emit(root, command, branch="feat/x", expires=None):
    rec = {"command": command, "step": "push", "branch": branch, "expires": expires or time.time() + 600}
    (root / g.EMISSION).write_text(json.dumps(rec))


def test_driver_emission_exact_match_allows(tmp_path):
    root = _repo(tmp_path)
    _emit(root, "git push -u origin feat/x")
    assert g.outbound("git push -u origin feat/x", str(root), False)[0] == "allow"


@pytest.mark.parametrize("ran", ["git push -u origin feat/x ", "timeout 9 git push -u origin feat/x"])
def test_driver_emission_mismatch_asks_and_shows_both(tmp_path, ran):
    root = _repo(tmp_path)
    _emit(root, "git push -u origin feat/x")
    v = g.outbound(ran, str(root), False)
    assert v[0] == "ask" and "printed: git push -u origin feat/x" in v[1]


@pytest.mark.parametrize("kw", [{"expires": 1.0}, {"branch": "other"}])
def test_stale_emission_is_ignored(tmp_path, kw):
    root = _repo(tmp_path)
    _emit(root, "git push", **kw)
    v = g.outbound("git push", str(root), False)
    assert v[0] == "ask" and "printed" not in v[1]


def test_emission_never_allows_an_irreversible_push(tmp_path):
    root = _repo(tmp_path)
    _emit(root, "git push --tags")
    assert g.outbound("git push --tags", str(root), False)[0] == "deny"


def test_emission_never_allows_under_no_remotes(tmp_path):
    root = _repo(tmp_path)
    _emit(root, "git push")
    assert g.outbound("git push", str(root), True)[0] == "deny"


# --- A4: gh invocation forms ----------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [
    "gh pr list", "gh issue list", "gh run list", "gh repo view", "cd /tmp && gh pr list", "/usr/bin/gh pr view 1",
    "gh api graphql", "gh api -X GET graphql", "gh api graphql -f query=x", "gh api -X POST graphql -f query=x", "gh api --method=POST graphql",
    "gh api -XPOST graphql", "gh api", "gh api -X DELETE repos/o/r", "gh api --method DELETE repos/o/r/hooks/12",
    "gh api -X PATCH /repos/o/r/actions/permissions", "gh api -X PUT /repos/o/r/rulesets/1 --input p.json",
    "gh api -X PATCH repos/o/r/rulesets/1", "gh api -X POST repos/o/r/actions/runners/registration-token",
    "gh api repos/o/r/hooks -f url=x", "bash -c 'gh pr merge 1'", "echo $(gh pr list)",
])
def test_gh_denies(cmd):
    assert g.gh(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "gh auth status", "gh api repos/o/r/pulls", "gh api /repos/o/r/actions/runs?head_sha=abc",
    "gh api -X GET repos/o/r/rulesets", "gh api --method GET /repos/o/r/rulesets/1",
    "gh api -X POST repos/o/r/pulls -f title=t -f head=h -f base=main -F body=@b.md",
    "gh api -X PATCH /repos/o/r/pulls/51 -f base=main", "gh api -X PUT /repos/o/r/pulls/51/merge -f sha=abc",
    "gh api -X POST repos/o/r/labels -f name=x", "gh api repos/{owner}/{repo}/pulls",
    "gh api -X DELETE repos/o/r/git/refs/heads/feat-x", "echo gh pr list", "git commit -m 'gh pr list'",
    "gh api -X PATCH /repos/X/pulls/X -f base=X",
])
def test_gh_passes(cmd):
    assert g.gh(cmd) is None, (cmd, g.gh(cmd))


def test_gh_refusal_says_not_to_relay_it():
    assert "Don't hand this command to a person" in g.gh("gh pr list")


def test_ruleset_refusal_says_excluded_by_decision():
    assert "excluded by decision" in g.gh("gh api -X PUT repos/o/r/rulesets/9")


# --- A5: relay -------------------------------------------------------------------------------------------

LINE = "bash /p/.git/ratchethub/next.sh  # rh:push:feat-x"


def _relay_repo(tmp_path):
    (tmp_path / ".git" / "ratchethub").mkdir(parents=True)
    (tmp_path / g.RELAY_LINE).write_text(LINE + "\n")
    return str(tmp_path)


def _msg(line):
    return f"Run this:\n\nSTART COPY\n```bash\n{line}\n```\nEND COPY\n"


def test_exact_relay_passes(tmp_path):
    assert g.relay(_msg(LINE), _relay_repo(tmp_path)) is None


@pytest.mark.parametrize("line", [LINE.replace("  # rh:push:feat-x", ""), "  " + LINE,
                                  LINE.replace("/p/", "~/p/"), "bash next.sh"])
def test_mangled_relay_blocks_once(tmp_path, line):
    root = _relay_repo(tmp_path)
    reason = g.relay(_msg(line), root)
    assert reason and LINE in reason
    assert g.relay(_msg(line), root) is None  # the loop guard: never block twice


def test_a_correct_relay_rearms_the_guard(tmp_path):
    root = _relay_repo(tmp_path)
    assert g.relay(_msg("bash x"), root)
    assert g.relay(_msg(LINE), root) is None
    assert g.relay(_msg("bash x"), root)


def test_no_copy_block_or_no_sidecar_passes(tmp_path):
    assert g.relay("no block here", _relay_repo(tmp_path)) is None
    assert g.relay(_msg("bash x"), str(tmp_path / "elsewhere")) is None


# --- the entry point -------------------------------------------------------------------------------------


def _run(monkeypatch, capsys, mode, payload, *extra):
    monkeypatch.setattr("sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload)))
    code = g.main([mode, *extra])
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def test_main_denies_as_pretooluse_json(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    code, out, _ = _run(monkeypatch, capsys, "gh", {"tool_name": "Bash", "tool_input": {"command": "gh pr list"}})
    assert code == 0 and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_passes_silently(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    code, out, _ = _run(monkeypatch, capsys, "outbound", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert code == 0 and out is None


def test_main_no_remotes_flag(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    payload = {"tool_name": "Bash", "tool_input": {"command": "git push"}}
    _, out, _ = _run(monkeypatch, capsys, "outbound", payload, "--no-remotes")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("payload", ["not json", "[]", {"tool_name": "Bash", "tool_input": {"command": 3}}])
def test_main_fails_closed(monkeypatch, capsys, payload):
    code, _, err = _run(monkeypatch, capsys, "outbound", payload)
    assert code == 2 and "fail closed" in err


def test_main_relay_blocks_with_exit_2(monkeypatch, capsys, tmp_path):
    root = _relay_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", root)
    code, _, err = _run(monkeypatch, capsys, "relay", {"last_assistant_message": _msg("bash x")})
    assert code == 2 and LINE in err


def test_main_relay_stands_aside_when_stop_hook_active(monkeypatch, capsys, tmp_path):
    root = _relay_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", root)
    code, _, _ = _run(monkeypatch, capsys, "relay", {"last_assistant_message": _msg("x"), "stop_hook_active": True})
    assert code == 0


def test_guard_is_standalone():
    """Generate copies the file on its own, so it may import only the standard library."""
    import ast
    import sys as _sys

    tree = ast.parse(open(g.__file__).read())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names - {"__future__"} <= set(_sys.stdlib_module_names), names


def test_residual_rules_doc_matches_the_code():
    import re
    from pathlib import Path

    from ratchethub.residue import RULES

    doc = (Path(__file__).parents[1] / "docs" / "residual-rules.md").read_text()
    assert re.findall(r"^## \d+\. (.+)$", doc, re.M) == [t for t, _ in RULES]
    for _, body in RULES:
        assert body in doc.replace("\n", " ")
