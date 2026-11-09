"""[github] (schema 2), the preflight (G5) and the platform reconciler (G6)."""

from __future__ import annotations

import json
import subprocess

import pytest

from ratchethub.declaration import DeclarationError, GitHub, Label, load_committed
from ratchethub.github import platform as P
from ratchethub.github.preflight import CLEAR, FINDING, UNMEASURED, preflight
from ratchethub.github.read import Reader, ReadError

HEAD = '[ratchethub]\nschema = 2\n[project]\nname = "p"\n'


def decl(tmp_path, body):
    (tmp_path / "ratchethub.toml").write_text(HEAD + body)
    return load_committed(tmp_path, path_env="")


@pytest.mark.parametrize("body, needle", [
    ("[github]\n", "default_branch"), ('[github]\ndefault_branch = "a..b"\n', "default_branch"),
    ('[github]\ndefault_branch = "main"\nrequired_checks = "ci"\n', "required_checks"),
    ('[github]\ndefault_branch = "main"\nrulesets = ["/etc/x.json"]\n', "project-relative"),
    ('[github]\ndefault_branch = "main"\nrulesets = ["missing.json"]\n', "does not exist"),
    ('[github]\ndefault_branch = "main"\nrulesets = ["r.yaml"]\n', ".json"),
    ('[github]\ndefault_branch = "main"\nsecret_scanning = "yes"\n', "boolean"),
    ('[github]\ndefault_branch = "main"\n[[github.labels]]\nname = "x"\ncolor = "red"\n', "six hex"),
    ('[github]\ndefault_branch = "main"\n[[github.labels]]\ncolor = "ffffff"\n', "needs a name"),
    ('[github]\ndefault_branch = "main"\nextra = 1\n', "unknown key"),
])
def test_github_refusals(tmp_path, body, needle):
    with pytest.raises(DeclarationError) as info:
        decl(tmp_path, body)
    assert any(needle in e for e in info.value.errors), info.value.errors


def test_github_is_schema_2_only(tmp_path):
    (tmp_path / "ratchethub.toml").write_text(HEAD.replace("schema = 2", "schema = 1") + '[github]\ndefault_branch = "m"\n')
    with pytest.raises(DeclarationError, match="not in schema version 1"):
        load_committed(tmp_path)


def test_github_accepts_a_full_section(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "main.json").write_text("{}")
    g = decl(tmp_path, '[github]\ndefault_branch = "main"\nrequired_checks = ["test"]\nrulesets = ["rules/main.json"]\n'
                       'secret_scanning = true\npreflight = ["true"]\n[[github.labels]]\nname = "bug"\ncolor = "#D73A4A"\n').github
    assert g == GitHub("main", ("test",), (Label("bug", "d73a4a", ""),), ("rules/main.json",), True, None, ("true",))


# --- preflight ----------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    run = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
    (r / "f.txt").write_text("base\n")
    (r / "ratchethub.toml").write_text(HEAD + '[github]\ndefault_branch = "main"\npreflight = ["true", "exit 3"]\n')
    run("add", ".")
    run("commit", "-q", "-m", "base")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("checkout", "-q", "-b", "feat")
    return r, run


def test_preflight_runs_each_command_and_a_trial_merge(repo):
    r, run = repo
    items = {i.name: i for i in preflight(r)}
    assert items["run: true"].result == CLEAR and items["run: exit 3"].result == FINDING
    assert items["trial merge"].result == CLEAR


def test_preflight_predicts_a_conflict(repo):
    r, run = repo
    (r / "f.txt").write_text("feat\n")
    run("commit", "-q", "-am", "feat")
    run("checkout", "-q", "main")
    (r / "f.txt").write_text("main moved\n")
    run("commit", "-q", "-am", "main")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    run("checkout", "-q", "feat")
    trial = {i.name: i for i in preflight(r)}["trial merge"]
    assert trial.result == FINDING and "f.txt" in trial.detail


def test_preflight_without_the_base_is_unmeasured_not_clear(repo):
    r, run = repo
    run("update-ref", "-d", "refs/remotes/origin/main")
    assert {i.name: i for i in preflight(r)}["trial merge"].result == UNMEASURED


# --- the reconciler ------------------------------------------------------------------------------


def fake(routes):
    def anon(path):
        for prefix, value in routes.items():
            if path.split("?")[0] == prefix:
                if isinstance(value, Exception):
                    raise value
                return value
        raise ReadError(f"404 {path}")
    return Reader(anon=anon, gh=lambda p: (_ for _ in ()).throw(ReadError("no gh")))


GH = GitHub("main", ("test",), (Label("bug", "d73a4a", "Something broke"),), ("rules/main.json",), True, None, ())
RULESET = {"name": "main", "enforcement": "active", "rules": [{"type": "deletion"}]}


def routes(**over):
    base = {
        "/repos/o/r/rulesets": [{"id": 7, "name": "main"}],
        "/repos/o/r/rulesets/7": {**RULESET, "id": 7, "updated_at": "2026-11-08", "_links": {}},
        "/repos/o/r/rules/branches/main": [{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "test"}]}}],
        "/repos/o/r/labels": [{"name": "bug", "color": "D73A4A", "description": "Something broke"}],
        "/repos/o/r": {"security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        "/repos/o/r/branches": [{"name": "main"}, {"name": "old"}],
        "/repos/o/r/pulls": [{"number": 1, "state": "closed", "merged_at": "x"}],
        "/repos/o/r/tags": [{"name": "v1"}],
        "/repos/o/r/releases": [{"tag_name": "v1", "draft": False}],
    }
    base.update(over)
    return base


@pytest.fixture
def root(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "main.json").write_text(json.dumps(RULESET))
    return tmp_path


def by_item(rows):
    return {r.item: r for r in rows}


def test_reconcile_matches_by_content_not_timestamps(root):
    rows = by_item(P.reconcile(fake(routes()), "o/r", GH, root))
    assert rows["ruleset main"].result == P.OK and rows["required checks"].result == P.OK
    assert rows["label bug"].result == P.OK and rows["secret_scanning"].result == P.OK
    assert rows["tags and releases"].result == P.OK
    assert rows["stale merged branches"].result == P.DRIFT and "old" in rows["stale merged branches"].detail


@pytest.mark.parametrize("over, item", [
    ({"/repos/o/r/rulesets/7": {**RULESET, "enforcement": "disabled"}}, "ruleset main"),
    ({"/repos/o/r/rulesets": []}, "ruleset main"),
    ({"/repos/o/r/rules/branches/main": []}, "required checks"),
    ({"/repos/o/r/labels": [{"name": "bug", "color": "000000", "description": "Something broke"}]}, "label bug"),
    ({"/repos/o/r/labels": []}, "label bug"),
    ({"/repos/o/r": {"security_and_analysis": {"secret_scanning": {"status": "disabled"}}}}, "secret_scanning"),
    ({"/repos/o/r/releases": []}, "tags and releases"),
])
def test_reconcile_reports_drift(root, over, item):
    assert by_item(P.reconcile(fake(routes(**over)), "o/r", GH, root))[item].result == P.DRIFT


def test_an_undeclared_ruleset_is_drift(root):
    r = routes(**{"/repos/o/r/rulesets": [{"id": 7, "name": "main"}, {"id": 8, "name": "extra"}],
                  "/repos/o/r/rulesets/8": {"name": "extra"}})
    assert by_item(P.reconcile(fake(r), "o/r", GH, root))["ruleset extra"].result == P.DRIFT


def test_security_hidden_from_an_anonymous_read_is_unmeasured(root):
    rows = by_item(P.reconcile(fake(routes(**{"/repos/o/r": {}})), "o/r", GH, root))
    assert rows["secret_scanning"].result == P.UNMEASURED


def test_a_failed_read_is_unmeasured_never_ok(root):
    rows = by_item(P.reconcile(fake(routes(**{"/repos/o/r/labels": ReadError("down")})), "o/r", GH, root))
    assert rows["labels"].result == P.UNMEASURED


def test_ruleset_change_sends_the_whole_ruleset_with_one_field_changed():
    full = {**RULESET, "id": 7, "updated_at": "t"}
    reader = fake({"/repos/o/r/rulesets/7": full})
    payload = P.ruleset_change(reader, "o/r", 7, "enforcement", "evaluate")
    assert payload == {**RULESET, "enforcement": "evaluate"}
    with pytest.raises(KeyError):
        P.ruleset_change(reader, "o/r", 7, "enforcment", "evaluate")


# --- what GitHub hides from an anonymous read, and rule order ----------------------------------


def _declare(root, ruleset):
    (root / "rules" / "main.json").write_text(json.dumps(ruleset))


def test_bypass_actors_hidden_from_the_read_are_unmeasured_not_drift_and_not_ok(root):
    """REST 'Get a repository ruleset': bypass_actors is returned only to a caller with write access
    to the ruleset. An anonymous read lacks it; that is no evidence either way."""
    _declare(root, {**RULESET, "bypass_actors": []})
    rows = by_item(P.reconcile(fake(routes()), "o/r", GH, root))
    assert rows["ruleset main"].result == P.OK, rows["ruleset main"].detail
    assert rows["ruleset main bypass_actors"].result == P.UNMEASURED
    assert "write access" in rows["ruleset main bypass_actors"].detail


def test_bypass_actors_that_are_visible_are_compared(root):
    _declare(root, {**RULESET, "bypass_actors": []})
    live = {**RULESET, "id": 7, "bypass_actors": [{"actor_type": "RepositoryRole", "actor_id": 5}]}
    rows = by_item(P.reconcile(fake(routes(**{"/repos/o/r/rulesets/7": live})), "o/r", GH, root))
    assert rows["ruleset main"].result == P.DRIFT and "bypass_actors" in rows["ruleset main"].detail
    assert "ruleset main bypass_actors" not in rows


def test_rule_order_is_not_drift_but_a_changed_rule_is(root):
    rules = [{"type": "deletion"}, {"type": "non_fast_forward"}]
    _declare(root, {**RULESET, "rules": rules})
    swapped = {**RULESET, "id": 7, "rules": rules[::-1]}
    assert by_item(P.reconcile(fake(routes(**{"/repos/o/r/rulesets/7": swapped})), "o/r", GH, root))[
        "ruleset main"].result == P.OK
    changed = {**RULESET, "id": 7, "rules": [{"type": "deletion"}, {"type": "creation"}]}
    assert by_item(P.reconcile(fake(routes(**{"/repos/o/r/rulesets/7": changed})), "o/r", GH, root))[
        "ruleset main"].result == P.DRIFT


def test_the_committed_ruleset_is_what_the_declaration_names():
    """RH's own repository: the declared ruleset file exists and requires the CI job by its real
    name, from GitHub Actions (app id 15368, read from GET /apps/github-actions)."""
    from pathlib import Path
    import tomllib
    repo = Path(__file__).resolve().parents[1]
    rs = json.loads((repo / ".github/rulesets/main.json").read_text())
    checks = [r for r in rs["rules"] if r["type"] == "required_status_checks"][0]["parameters"]
    assert checks["required_status_checks"] == [{"context": "test", "integration_id": 15368}]
    ci = (repo / ".github/workflows/ci.yml").read_text()
    assert "\n  test:\n" in ci, "the required context is the CI job's name"
