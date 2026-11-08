"""The Generate step (docs/change-protocol.md §3–§4).

Adversarial cases first. Every refusal is also checked to have written nothing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ratchethub.generate import (
    COMMITTED_RECORD,
    LOCAL_RECORD,
    SETTINGS,
    SETTINGS_LOCAL,
    GenerateError,
    generate,
    rh_local_keys,
)

HEADER = '[ratchethub]\nschema = 1\n\n[project]\nname = "p"\n'
IGNORE = "ratchethub.local.toml\n.claude/settings.local.json\n.claude/ratchethub-record.local.json\n"


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / ".gitignore").write_text(IGNORE, encoding="utf-8")
    (tmp_path / "ratchethub.toml").write_text(HEADER, encoding="utf-8")
    return tmp_path


def run(root: Path, **kw):
    kw.setdefault("claude_version", "2.1.310")
    kw.setdefault("now", "2026-11-08T00:00:00Z")
    return generate(root, **kw)


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts[:1]
    }


def refuse(root: Path, needle: str, **kw) -> None:
    before = snapshot(root)
    with pytest.raises(GenerateError) as info:
        run(root, **kw)
    assert any(needle in r for r in info.value.reasons), info.value.reasons
    assert snapshot(root) == before, "a refused Generate wrote something"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- preconditions ----------------------------------------------------------------------------------


def test_refuses_outside_a_git_repository(tmp_path):
    (tmp_path / "ratchethub.toml").write_text(HEADER, encoding="utf-8")
    refuse(tmp_path, "git repository")


def test_refuses_when_the_local_declaration_is_not_ignored(repo):
    (repo / ".gitignore").write_text(".claude/settings.local.json\n.claude/ratchethub-record.local.json\n")
    refuse(repo, "ratchethub.local.toml is not ignored")


def test_refuses_when_the_local_declaration_is_tracked(repo):
    (repo / "ratchethub.local.toml").write_text('[protect]\nfolders = []\n', encoding="utf-8")
    git(repo, "add", "-f", "ratchethub.local.toml")
    refuse(repo, "ratchethub.local.toml is not ignored")


@pytest.mark.parametrize("name", [".claude/settings.local.json", ".claude/ratchethub-record.local.json"])
def test_refuses_when_a_generated_local_file_is_not_ignored(repo, name):
    (repo / ".gitignore").write_text(IGNORE.replace(name + "\n", ""), encoding="utf-8")
    refuse(repo, f"{name} is not ignored")


def test_refuses_a_project_root_below_the_repository_root(repo):
    sub = repo / "sub"
    sub.mkdir()
    (sub / "ratchethub.toml").write_text(HEADER, encoding="utf-8")
    refuse(sub, "not the root of its git repository")


def test_refuses_an_invalid_declaration(repo):
    (repo / "ratchethub.toml").write_text(HEADER + "[sandbox]\nenabled = false\n", encoding="utf-8")
    refuse(repo, "floor")


def test_refuses_an_invalid_local_declaration(repo):
    (repo / "ratchethub.local.toml").write_text('[network]\nallow = ["x.com"]\n', encoding="utf-8")
    refuse(repo, "local declaration")


def test_refuses_a_writable_folder_the_local_declaration_protects(repo):
    (repo / "cache").mkdir()
    (repo / "ratchethub.toml").write_text(HEADER.replace("schema = 1", "schema = 2")
                                          + '[writable]\nfolders = ["cache"]\n', encoding="utf-8")
    (repo / "ratchethub.local.toml").write_text('[protect]\nfolders = ["cache"]\n', encoding="utf-8")
    refuse(repo, "overlaps")


def test_refuses_without_a_claude_code_version(repo):
    refuse(repo, "Claude Code version", claude_version="")


# --- first run and adoption -------------------------------------------------------------------------


def test_first_run_refuses_to_take_over_an_existing_settings_file(repo):
    (repo / ".claude").mkdir()
    (repo / SETTINGS).write_text('{"permissions": {"deny": []}}\n', encoding="utf-8")
    refuse(repo, "adopt")


def test_adoption_refuses_to_drop_an_existing_deny_entry(repo):
    (repo / ".claude").mkdir()
    (repo / SETTINGS).write_text('{"permissions": {"deny": ["Bash(git push *)"]}}\n', encoding="utf-8")
    refuse(repo, "Bash(git push *)", adopt=True)


def test_adoption_refuses_to_drop_an_existing_deny_write_entry(repo):
    (repo / ".claude").mkdir()
    (repo / SETTINGS).write_text('{"sandbox": {"filesystem": {"denyWrite": ["./secret"]}}}\n', encoding="utf-8")
    refuse(repo, "./secret", adopt=True)


def test_first_run_refuses_to_drop_existing_local_restrictions(repo):
    # Even with no settings.json to adopt: RH would replace these keys, a silent loosening.
    (repo / ".claude").mkdir()
    (repo / SETTINGS_LOCAL).write_text('{"permissions": {"deny": ["Edit(//srv/x/**)"]}}\n', encoding="utf-8")
    refuse(repo, "Edit(//srv/x/**)", adopt=True)


def test_first_run_refuses_a_local_record_without_a_committed_one(repo):
    (repo / ".claude").mkdir()
    (repo / LOCAL_RECORD).write_text('{"entries": []}\n', encoding="utf-8")
    refuse(repo, "record")


# --- drift ------------------------------------------------------------------------------------------


def test_refuses_a_hand_edited_settings_file(repo):
    run(repo)
    path = repo / SETTINGS
    path.write_text(path.read_text().replace('"disable"', '"enable"'), encoding="utf-8")
    refuse(repo, "drift")


def test_refuses_a_reformatted_settings_file(repo):
    # Whole-file ownership: even a change with no effect is a hand edit.
    run(repo)
    path = repo / SETTINGS
    path.write_text(json.dumps(load(path)), encoding="utf-8")
    refuse(repo, "drift")


def test_refuses_a_deleted_settings_file(repo):
    run(repo)
    (repo / SETTINGS).unlink()
    refuse(repo, "drift")


def test_refuses_a_hand_edited_rh_key_in_the_local_file(repo):
    (repo / "data").mkdir()
    (repo / "ratchethub.local.toml").write_text('[protect]\nfolders = ["data"]\n', encoding="utf-8")
    run(repo)
    path = repo / SETTINGS_LOCAL
    data = load(path)
    data["permissions"]["deny"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    refuse(repo, "drift")


def test_refuses_rh_keys_added_by_hand_to_a_local_file_rh_left_empty(repo):
    run(repo)
    (repo / SETTINGS_LOCAL).write_text('{"sandbox": {"filesystem": {"denyWrite": ["/x"]}}}\n')
    refuse(repo, "drift")


def test_refuses_an_unreadable_record(repo):
    run(repo)
    (repo / COMMITTED_RECORD).write_text("{not json", encoding="utf-8")
    refuse(repo, "record")


def test_refuses_a_record_with_no_generate_entry(repo):
    run(repo)
    (repo / COMMITTED_RECORD).write_text('{"entries": []}\n', encoding="utf-8")
    refuse(repo, "generate entry")


def test_refuses_a_missing_local_record_when_the_committed_one_exists(repo):
    run(repo)
    (repo / LOCAL_RECORD).unlink()
    refuse(repo, "record")


def test_a_deleted_record_makes_the_next_run_a_first_run(repo):
    run(repo)
    (repo / COMMITTED_RECORD).unlink()
    (repo / LOCAL_RECORD).unlink()
    refuse(repo, "adopt")


# --- confirming -------------------------------------------------------------------------------------


def test_first_run_writes_settings_and_both_records(repo):
    from ratchethub.declaration import load_committed
    from ratchethub.render import render_committed

    result = run(repo)
    assert load(repo / SETTINGS) == render_committed(load_committed(repo)).settings
    entry = load(repo / COMMITTED_RECORD)["entries"][-1]
    assert entry["step"] == "generate" and entry["adopted"] is False
    assert entry["claude_code"] == "2.1.310" and entry["at"] == "2026-11-08T00:00:00Z"
    assert entry["files"][SETTINGS] == result.hashes[SETTINGS]
    local_entry = load(repo / LOCAL_RECORD)["entries"][-1]
    assert local_entry["declaration"] is None  # no local declaration
    assert result.rows


def test_settings_hash_in_record_matches_the_bytes_on_disk(repo):
    import hashlib

    run(repo)
    digest = hashlib.sha256((repo / SETTINGS).read_bytes()).hexdigest()
    assert load(repo / COMMITTED_RECORD)["entries"][-1]["files"][SETTINGS] == digest


def test_rerun_after_a_declaration_change_rewrites_and_appends(repo):
    run(repo)
    (repo / "data").mkdir()
    (repo / "ratchethub.toml").write_text(HEADER + '[protect]\nfolders = ["data"]\n', encoding="utf-8")
    run(repo, now="2026-11-09T00:00:00Z")
    assert "Edit(/data/**)" in load(repo / SETTINGS)["permissions"]["deny"]
    entries = load(repo / COMMITTED_RECORD)["entries"]
    assert [e["at"] for e in entries] == ["2026-11-08T00:00:00Z", "2026-11-09T00:00:00Z"]
    assert entries[0]["declaration"] != entries[1]["declaration"]


def test_local_keys_are_merged_and_other_keys_kept(repo):
    (repo / ".claude").mkdir()
    (repo / SETTINGS_LOCAL).write_text(
        '{"permissions": {"allow": ["Bash(ls)"]}, "sandbox": {"enabled": true}}\n', encoding="utf-8"
    )
    (repo / "shared").mkdir()
    (repo / "ratchethub.local.toml").write_text(f'[protect]\nfolders = ["{repo}/shared"]\n', encoding="utf-8")
    run(repo)
    data = load(repo / SETTINGS_LOCAL)
    assert data["permissions"]["allow"] == ["Bash(ls)"] and data["sandbox"]["enabled"] is True
    assert data["permissions"]["deny"] == [f"Edit(/{repo}/shared/**)"]
    assert data["sandbox"]["filesystem"]["denyWrite"] == [f"{repo}/shared"]


def test_runtime_writes_to_non_rh_local_keys_are_not_drift(repo):
    run(repo)
    path = repo / SETTINGS_LOCAL
    data = load(path) if path.exists() else {}
    data.setdefault("permissions", {})["allow"] = ["Bash(npm test)"]
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    run(repo)
    assert load(path)["permissions"]["allow"] == ["Bash(npm test)"]


def test_no_local_file_is_created_when_there_is_nothing_to_write(repo):
    run(repo)
    assert not (repo / SETTINGS_LOCAL).exists()


def test_adoption_succeeds_when_nothing_is_dropped_and_is_recorded(repo):
    (repo / ".claude").mkdir()
    (repo / SETTINGS).write_text('{"permissions": {"deny": ["Edit(/.claude/**)"]}, "env": {"A": "1"}}\n')
    result = run(repo, adopt=True)
    assert load(repo / COMMITTED_RECORD)["entries"][-1]["adopted"] is True
    assert "env" not in load(repo / SETTINGS)
    assert any("env" in note for note in result.notes)  # dropped non-restriction keys are reported


def test_rh_local_keys_reads_absent_as_empty(tmp_path):
    assert rh_local_keys(tmp_path / "missing.json") == {"permissions.deny": [], "sandbox.filesystem.denyWrite": []}


# --- the guard file ---------------------------------------------------------------------------------


def test_generate_installs_the_guard_and_records_its_hash(repo):
    from ratchethub.render import GUARD_PATH
    from ratchethub.generate import GUARD_SOURCE

    generate(repo, claude_version="v", now="t")
    assert (repo / GUARD_PATH).read_bytes() == GUARD_SOURCE.read_bytes()
    entry = json.loads((repo / ".claude/ratchethub-record.json").read_text())["entries"][-1]
    assert GUARD_PATH in entry["files"]


def test_an_edited_guard_is_drift(repo):
    from ratchethub.render import GUARD_PATH

    generate(repo, claude_version="v", now="t")
    (repo / GUARD_PATH).write_text("# edited\n")
    refuse(repo, "ratchethub_guard.py differs")


def test_first_run_refuses_an_unrecorded_guard_without_adopt(repo):
    from ratchethub.render import GUARD_PATH

    (repo / GUARD_PATH).parent.mkdir(parents=True)
    (repo / GUARD_PATH).write_text("# someone else's\n")
    refuse(repo, "pass adopt")
