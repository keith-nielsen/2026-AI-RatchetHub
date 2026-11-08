"""Live Verify (with a fake runner that acts like the runtime), Approve, the probe and the CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ratchethub import live
from ratchethub import selftest as st
from ratchethub.approve import ApproveError, approve
from ratchethub.cli import main
from ratchethub.generate import COMMITTED_RECORD, generate
from ratchethub.probe import ABSENT, AUTHENTICATED, STATES, UNAUTHENTICATED, ProbeRow, probe
from ratchethub.verify import record_verify, verify_static

ITEM = re.compile(r"^\d+\. (Bash) tool, command exactly: (.*)$|^\d+\. (Write) tool, file_path exactly (\S+) with content")


class FakeRuntime:
    """Acts out a headless session: the governed copy (it has settings) refuses protected writes
    with the runtime's own error texts; the baseline lands them. `breaks` names a misbehaviour."""

    def __init__(self, breaks: str = ""):
        self.breaks = breaks
        self.calls = []

    def __call__(self, argv, cwd, stdin=""):
        self.calls.append((argv, cwd))
        prompt = self._prompt(argv, stdin)
        if prompt is None:
            return "", "Error: Input must be provided either through stdin or as a prompt argument when using --print", 1
        governed = (Path(cwd) / ".claude" / "settings.json").exists()
        events = [{"type": "system", "subtype": "init", "permissionMode": argv[argv.index("--permission-mode") + 1]}]
        for n, line in enumerate(prompt.splitlines()):
            m = ITEM.match(line)
            if not m:
                continue
            kind, arg = ("bash", m.group(2)) if m.group(1) else ("write", m.group(4))
            if self.breaks == "skip-writes" and kind == "write":
                continue
            uid = f"t{n}"
            inp = {"command": arg} if kind == "bash" else {"file_path": arg, "content": live.MARK}
            events.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": uid, "input": inp}]}})
            err, text = self._act(kind, arg, governed, cwd)
            events.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": uid, "is_error": err, "content": text}]}})
        events.append({"type": "system", "subtype": "permission_denied", "message": "a string, not a dict"})
        return "\n".join(json.dumps(e) for e in events) + "\nnot json\n", "", 0

    @staticmethod
    def _prompt(argv, stdin):
        """Parse argv the way the real CLI does: a variadic option (`--add-dir <directories...>`)
        takes every following argument that isn't an option, so only what's left is the prompt."""
        variadic, takes_one = {"--add-dir"}, {"--model", "--max-turns", "--output-format", "--permission-mode"}
        positional, i = [], 1
        while i < len(argv):
            tok = argv[i]
            if tok in variadic:
                i += 1
                while i < len(argv) and not argv[i].startswith("-"):
                    i += 1
                continue
            if tok in takes_one:
                i += 2
                continue
            if not tok.startswith("-"):
                positional.append(tok)
            i += 1
        return positional[0] if positional else (stdin or None)

    def _act(self, kind, arg, governed, cwd):
        if arg.startswith(("git push", "gh ")):
            hooked = (governed and self.breaks != "no-hooks") or self.breaks == "baseline-hooked"
            return True, ("RatchetHub: refused" if hooked else "fatal: no remote")
        path = Path(arg.split("touch ", 1)[1] if kind == "bash" else arg)
        protected = path.parent.resolve() != Path(cwd).resolve()
        refuse = governed and protected and self.breaks != "governed-lands"
        if not governed and self.breaks == "baseline-holds":
            refuse = True
        if refuse:
            if self.breaks == "classifier" and governed:
                return True, "This action was blocked by the auto mode classifier."
            return True, (f"touch: cannot touch '{path}': Read-only file system" if kind == "bash"
                          else "File is in a directory that is denied by your permission settings.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("" if kind == "bash" else live.MARK)
        return False, "ok"


@pytest.fixture
def project(tmp_path):
    root, _, _ = st.build(tmp_path)
    return root


def test_targets_come_from_the_declaration(project):
    names = [t.name for t in live.targets(project)]
    assert "data (bash)" in names and "data (write)" in names and ".claude (bash)" in names
    assert ".git/hooks (write)" in names and ".git/hooks (bash)" not in names
    assert {"outbound guard", "gh guard"} <= set(names)


def test_a_clean_run_reports_every_row_enforced(project, tmp_path):
    result = live.run_live(project, tmp_path / "scratch", claude_version="v", runner=FakeRuntime())
    assert result.ok, [(v.target, v.verdict) for v in result.verdicts]
    assert {v.verdict for v in result.verdicts} == {"ENFORCED", "HOOK FIRES", "CONTROL LANDS"}
    assert not list((tmp_path / "scratch").rglob(".rh-live-*")), "probe files left behind"


@pytest.mark.parametrize("breaks, verdict", [
    ("governed-lands", "NOT ENFORCED"), ("baseline-holds", "HELD BY MODE"), ("classifier", "INCONCLUSIVE"),
    ("skip-writes", "INCONCLUSIVE"), ("no-hooks", "HOOK SILENT"), ("baseline-holds", "CONTROL FAILED"),
    ("baseline-hooked", "INCONCLUSIVE"),
])
def test_each_broken_session_fails_the_run(project, tmp_path, breaks, verdict):
    result = live.run_live(project, tmp_path / "scratch", claude_version="v", runner=FakeRuntime(breaks))
    assert not result.ok and verdict in {v.verdict for v in result.verdicts}


def test_both_sessions_run_in_the_same_mode_with_the_same_add_dirs(project, tmp_path):
    ext = tmp_path / "external"
    ext.mkdir()
    (project / "ratchethub.local.toml").write_text(f'[protect]\nfolders = ["{ext}"]\n')
    rt = FakeRuntime()
    result = live.run_live(project, tmp_path / "scratch", claude_version="v", runner=rt, mode="auto")
    (gov_argv, gov_cwd), (base_argv, base_cwd) = rt.calls
    assert gov_argv[:-1] == base_argv[:-1] and "--strict-mcp-config" in gov_argv
    assert ["--add-dir", str(ext)] == gov_argv[gov_argv.index("--add-dir"):gov_argv.index("--add-dir") + 2]
    assert (gov_cwd / ".claude" / "settings.json").exists() and not (base_cwd / ".claude" / "settings.json").exists()
    assert result.ok and not list(ext.iterdir()), "external probe files must be deleted by name"


def test_live_refuses_an_existing_scratch(project, tmp_path):
    (tmp_path / "scratch").mkdir()
    with pytest.raises(FileExistsError):
        live.run_live(project, tmp_path / "scratch", claude_version="v", runner=FakeRuntime())


def test_cleanup_reports_residue(tmp_path, monkeypatch):
    p = tmp_path / ".rh-live-x"
    p.write_text("x")
    monkeypatch.setattr(live.os, "unlink", lambda path: None)
    assert live.cleanup([str(p)]) == [str(p)]


def test_plan_lists_external_paths_before_the_run(project, tmp_path):
    ext = tmp_path / "external"
    ext.mkdir()
    (project / "ratchethub.local.toml").write_text(f'[protect]\nfolders = ["{ext}"]\n')
    p = live.plan(project, tmp_path / "scratch", "tag")
    assert p["outside_scratch"] and all(x.startswith(str(ext)) for x in p["outside_scratch"])
    assert not (tmp_path / "scratch").exists(), "planning writes nothing"


# --- Approve ----------------------------------------------------------------------------------------


def _verified(project, tmp_path, version="v", live_too=True):
    report = verify_static(project, claude_version=version, home=tmp_path / "home", managed=())
    assert report.ok, report.failures
    record_verify(project, report, now="t1")
    if live_too:
        result = live.run_live(project, tmp_path / "scratch", claude_version=version, runner=FakeRuntime())
        live.record_live(project, result, now="t2")


def test_approve_records_who_and_both_verify_hashes(project, tmp_path):
    _verified(project, tmp_path)
    entry = approve(project, who="A Person", now="t3", claude_version="v")
    last = json.loads((project / COMMITTED_RECORD).read_text())["entries"][-1]
    assert last == entry and entry["who"] == "A Person" and entry["live_verify"] and not entry["static_only"]


@pytest.mark.parametrize("setup, needle", [
    (lambda p, t: None, "no static verify entry"),
    (lambda p, t: _verified(p, t, live_too=False), "no live verify entry"),
    (lambda p, t: (_verified(p, t), (p / ".claude/settings.local.json").write_text("{}\n")), "changed since"),
    (lambda p, t: (_verified(p, t), (p / "ratchethub.toml").write_text(st.DECLARATION + "#\n")), "ratchethub.toml"),
    (lambda p, t: (_verified(p, t), generate(p, claude_version="v", now="t9")), "no static verify entry since"),
])
def test_approve_refuses(project, tmp_path, setup, needle):
    setup(project, tmp_path)
    with pytest.raises(ApproveError) as info:
        approve(project, who="A Person", now="t3", claude_version="v")
    assert any(needle in r for r in info.value.reasons), info.value.reasons


def test_approve_refuses_a_stale_verify(project, tmp_path):
    _verified(project, tmp_path, version="2.1.310")
    with pytest.raises(ApproveError, match="stale"):
        approve(project, who="A Person", now="t3", claude_version="2.1.319")


def test_approve_refuses_a_failed_live_run(project, tmp_path):
    record_verify(project, verify_static(project, claude_version="v", home=tmp_path / "h", managed=()), now="t1")
    live.record_live(project, live.run_live(project, tmp_path / "s", claude_version="v",
                                            runner=FakeRuntime("governed-lands")), now="t2")
    with pytest.raises(ApproveError, match="live verify entry did not pass"):
        approve(project, who="A Person", now="t3", claude_version="v")


def test_approve_refuses_without_a_name(project, tmp_path):
    _verified(project, tmp_path)
    with pytest.raises(ApproveError, match="who"):
        approve(project, who=" ", now="t3", claude_version="v")


def test_static_only_approval_says_so(project, tmp_path):
    _verified(project, tmp_path, live_too=False)
    entry = approve(project, who="A Person", now="t3", claude_version="v", static_only=True)
    assert entry["static_only"] and entry["live_verify"] is None


# --- the probe -------------------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_opener(statuses):
    import urllib.error

    def opener(req, timeout):
        auth = req.get_header("Authorization")
        code = statuses.get("token" if auth else "anon")
        if code is None:
            raise urllib.error.URLError("offline")
        if code >= 400:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
        return FakeResponse(code)
    return opener


def _rows(project, tmp_path, env_extra=None, statuses=None):
    import os

    bin_ = tmp_path / "bin"  # git only: no gh, whatever this machine has
    bin_.mkdir(exist_ok=True)
    if not (bin_ / "git").exists():
        (bin_ / "git").symlink_to(shutil.which("git"))
    keep = {k: v for k, v in os.environ.items() if k.startswith("GIT_") and not k.startswith("GIT_CONFIG_KEY")
            and not k.startswith("GIT_CONFIG_VALUE") and k not in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS")}
    env = {**keep, "PATH": str(bin_), "HOME": str(tmp_path / "home"), **(env_extra or {})}
    return {r.channel: r for r in probe(project, opener=fake_opener(statuses or {"anon": 200}), env=env)}


def test_probe_with_nothing_reports_absent_never_unauthenticated(project, tmp_path):
    rows = _rows(project, tmp_path)
    assert rows["gh credential"].state == ABSENT and "unknown" in rows["gh credential"].note
    assert rows["env GH_TOKEN"].state == ABSENT and rows["env GH_TOKEN"].evidence == "inspected"
    assert rows["git credential.helper"].state == ABSENT
    assert rows["github api (anonymous)"].evidence == "attempted:rest-anonymous"


@pytest.mark.parametrize("code, state", [(200, AUTHENTICATED), (401, UNAUTHENTICATED)])
def test_probe_tries_a_token_and_never_prints_it(project, tmp_path, code, state):
    rows = _rows(project, tmp_path, {"GH_TOKEN": "ghp_secretvalue"}, {"anon": 200, "token": code})
    assert rows["env GH_TOKEN"].state == state and rows["env GH_TOKEN"].evidence == "attempted:rest-token"
    assert "ghp_secretvalue" not in json.dumps([vars(r) for r in rows.values()])


def test_probe_enumerates_credential_helpers(project, tmp_path):
    subprocess.run(["git", "-C", str(project), "config", "credential.helper", "store --file=/nonexistent"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "credential.https://github.com.helper", "cache"], check=True)
    rows = _rows(project, tmp_path)
    assert rows["git credential.helper"].note == "store --file=/nonexistent"
    assert "git credential.https://github.com.helper" in rows
    assert rows["git credential fill github.com"].evidence == "attempted:git-credential"


def test_probe_refuses_an_undeclared_state():
    with pytest.raises(AssertionError):
        ProbeRow("x", "MAYBE", "inspected")
    assert all(" " not in s for s in STATES)


# --- the CLI ---------------------------------------------------------------------------------------


def test_cli_round_trip(project, tmp_path, capsys):
    base = ["--root", str(project), "--claude-version", "v"]
    assert main([*base, "report"]) == 0
    assert "tripwire" in capsys.readouterr().out
    assert main([*base, "selftest"]) == 0
    assert main([*base, "approve", "--who", "X"]) == 1
    assert "REFUSED" in capsys.readouterr().out
    assert main([*base, "live"]) == 2, "live must not start without --yes"
    assert "--yes" in capsys.readouterr().out
    assert main([*base, "generate"]) == 0, "Generate again with no drift succeeds"


def test_cli_generate_refuses_drift(project, capsys):
    (project / ".claude" / "settings.json").write_text("{}\n")
    assert main(["--root", str(project), "--claude-version", "v", "generate"]) == 1
    assert "drift" in capsys.readouterr().out


def test_residue_fails_the_run():
    assert not live.LiveResult("auto", [], ["/x/.rh-live-1"], "v").ok


def test_approve_refuses_a_failed_static_verify(project, tmp_path):
    st._local({"sandbox": {"enabled": False}})(project, tmp_path / "home", tmp_path / "m.json")
    report = verify_static(project, claude_version="v", home=tmp_path / "home", managed=())
    record_verify(project, report, now="t1")
    live.record_live(project, live.run_live(project, tmp_path / "s", claude_version="v", runner=FakeRuntime()), now="t2")
    with pytest.raises(ApproveError, match="static verify entry did not pass"):
        approve(project, who="A Person", now="t3", claude_version="v")


@pytest.mark.parametrize("static_v, live_v, needle", [
    ("old", "new", "static verify entry is stale"), ("new", "old", "live verify entry is stale")])
def test_approve_names_which_verify_is_stale(project, tmp_path, static_v, live_v, needle):
    record_verify(project, verify_static(project, claude_version=static_v, home=tmp_path / "h", managed=()), now="t1")
    live.record_live(project, live.run_live(project, tmp_path / "s", claude_version=live_v, runner=FakeRuntime()),
                     now="t2")
    with pytest.raises(ApproveError) as info:
        approve(project, who="A Person", now="t3", claude_version="new")
    assert [r for r in info.value.reasons if needle in r] and len(info.value.reasons) == 1, info.value.reasons


def test_approve_refuses_a_live_entry_from_before_the_last_generate(project, tmp_path):
    live.record_live(project, live.run_live(project, tmp_path / "s", claude_version="v", runner=FakeRuntime()), now="t0")
    generate(project, claude_version="v", now="t1")
    record_verify(project, verify_static(project, claude_version="v", home=tmp_path / "h", managed=()), now="t2")
    with pytest.raises(ApproveError, match="no live verify entry since"):
        approve(project, who="A Person", now="t3", claude_version="v")


def test_approve_refuses_files_changed_since_the_live_entry(project, tmp_path):
    _verified(project, tmp_path)
    (project / ".claude/settings.local.json").write_text("{}\n")
    record_verify(project, verify_static(project, claude_version="v", home=tmp_path / "h", managed=()), now="t4")
    with pytest.raises(ApproveError, match="changed since the live verify entry"):
        approve(project, who="A Person", now="t5", claude_version="v")


def test_the_committed_record_never_names_this_machine(project, tmp_path, monkeypatch):
    """RH is published: the committed record carries no repository path, home folder or local
    protected folder, from any of the four writers. The local record keeps the full detail."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    estate = tmp_path / "private-estate"
    estate.mkdir()
    (project / "ratchethub.local.toml").write_text(f'[protect]\nfolders = ["{estate}"]\n')
    generate(project, claude_version="v", now="t0")
    _verified(project, tmp_path)
    approve(project, who="A Person", now="t3", claude_version="v")
    committed = (project / COMMITTED_RECORD).read_text()
    for private in (str(tmp_path), str(project), str(estate)):
        assert private not in committed, private
    assert "<local folder 1>" in committed, "the live row for the estate folder is still recorded"
    local = (project / ".claude/ratchethub-record.local.json").read_text()
    assert str(estate) in local and str(tmp_path / "scratch") in local, "the local record keeps the real paths"


def test_portable_replaces_whole_paths_only(tmp_path):
    from ratchethub.generate import portable

    root = tmp_path / "repo"
    root.mkdir()
    got = portable({"a": [f"{root}/x", f"{root}-copy/x", f"{tmp_path}/h/.ssh"]}, root, home=tmp_path / "h")
    assert got == {"a": ["./x", f"{root}-copy/x", "~/.ssh"]}
    nested = tmp_path / "h" / "repo"
    nested.mkdir(parents=True)
    assert portable(f"{nested}/y", nested, home=tmp_path / "h") == "./y", "the repository wins over home"


def test_a_session_that_never_starts_is_named_and_fails(project, tmp_path):
    """The first real run: the prompt was swallowed by `--add-dir`, both sessions exited before
    the start event, and nothing was measured. That must read as a non-start, never as verdicts."""
    dead = lambda argv, cwd, prompt: ("", "Error: Input must be provided", 1)
    result = live.run_live(project, tmp_path / "scratch", claude_version="v", runner=dead)
    assert result.not_started == ["governed", "baseline"] and not result.ok


def test_the_prompt_survives_the_add_dir_list(project, tmp_path):
    estate = tmp_path / "estate"
    estate.mkdir()
    (project / "ratchethub.local.toml").write_text(f'[protect]\nfolders = ["{estate}"]\n')
    generate(project, claude_version="v", now="t0")
    rt = FakeRuntime()
    live.run_live(project, tmp_path / "scratch", claude_version="v", runner=rt)
    argv, _ = rt.calls[0]
    assert "--add-dir" in argv, "the test project has an absolute folder, so --add-dir is in play"
    assert FakeRuntime._prompt(argv + ["the prompt"], "") is None, "a trailing prompt is eaten, as in the CLI"


def test_the_real_runner_sends_the_prompt_on_stdin(tmp_path):
    out, _, rc = live.claude_runner(["sh", "-c", "cat"], tmp_path, "the prompt")
    assert (out, rc) == ("the prompt", 0)
