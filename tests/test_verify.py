"""Static Verify. The broken inputs come first: each must fail its named check."""

from __future__ import annotations

import json
import shutil

import pytest

from ratchethub import selftest as st
from ratchethub.generate import COMMITTED_RECORD, LOCAL_RECORD, SETTINGS_LOCAL
from ratchethub.verify import record_verify, verify_static


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    base = tmp_path_factory.mktemp("template")
    st.build(base)
    return base


@pytest.fixture
def copy(template, tmp_path):
    dest = tmp_path / "copy"
    shutil.copytree(template, dest, symlinks=True)
    return dest / "project", dest / "home", dest / "managed-settings.json"


def failed_ids(report):
    return {c.id for c in report.failures}


@pytest.mark.parametrize("case", st.CASES, ids=[c.name for c in st.CASES])
def test_each_broken_input_fails_its_check(copy, case):
    report = st.run_case(*copy, case)
    assert case.expect in failed_ids(report), [(c.id, c.detail) for c in report.checks]


def test_clean_project_passes(copy):
    report = st.run_case(*copy, None)
    assert report.ok, [(c.id, c.detail) for c in report.failures]


def test_an_older_guard_fails_only_its_own_check(copy):
    """The realistic case: the record and the hook agree, so no drift; only the source moved."""
    from ratchethub.selftest import _older_guard
    root, home, managed = copy
    _older_guard(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    assert failed_ids(report) == {"guard-current"}, [(c.id, c.detail) for c in report.failures]
    assert "cycle is owed" in next(c.detail for c in report.checks if c.id == "guard-current")


def test_a_current_guard_passes_its_check(copy):
    report = st.run_case(*copy, None)
    check = next(c for c in report.checks if c.id == "guard-current")
    assert check.ok is True


def test_selftest_passes():
    assert st.selftest() == []


def test_selftest_catches_a_check_that_never_fails(monkeypatch):
    import ratchethub.verify as v

    monkeypatch.setattr(v, "_check_loosening", lambda sources, checks: None)
    problems = st.selftest()
    assert any("loosening" in p for p in problems), problems


# --- precedence --------------------------------------------------------------------------------


def test_a_lower_file_overridden_by_a_higher_one_passes_and_is_reported(copy):
    root, home, managed = copy
    st._user({"sandbox": {"enabled": False}})(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    check = next(c for c in report.checks if c.id == "floor:sandbox.enabled")
    assert check.ok is True and "user sets False" in check.detail


def test_managed_outranks_the_project_file(copy):
    root, home, managed = copy
    st._managed({"sandbox": {"enabled": False}})(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    assert "floor:sandbox.enabled" in failed_ids(report)


def test_managed_directory_files_are_read(copy, tmp_path):
    root, home, _ = copy
    d = tmp_path / "managed.d"
    d.mkdir()
    (d / "10-x.json").write_text(json.dumps({"sandbox": {"allowUnsandboxedCommands": True}}))
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(d,))
    assert "floor:sandbox.allowUnsandboxedCommands" in failed_ids(report)


# --- write allows: what is and isn't a loosening -----------------------------------------------


@pytest.mark.parametrize("patch", [
    {"sandbox": {"filesystem": {"allowWrite": ["/tmp/build"]}}},
    {"permissions": {"allow": ["Edit(/src/**)"]}},
    {"permissions": {"additionalDirectories": ["../sibling"]}},
])
def test_an_allow_away_from_protected_paths_passes(copy, patch):
    root, home, managed = copy
    st._local(patch)(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    assert "write-allow" not in failed_ids(report)


def test_an_allow_of_a_parent_folder_passes(copy):
    # A deny inside an allowed folder holds (runtime docs, denyWrite).
    root, home, managed = copy
    st._user({"sandbox": {"filesystem": {"allowWrite": [str(root.parent)]}}})(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    assert "write-allow" not in failed_ids(report)


def test_user_scope_single_slash_edit_rule_anchors_at_the_user_claude_folder(copy):
    # Edit(/.claude/**) in user settings means ~/.claude/.claude/**, not the project's .claude.
    root, home, managed = copy
    st._user({"permissions": {"allow": ["Edit(/.claude/**)"]}})(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    assert "write-allow" not in failed_ids(report)


def test_absolute_edit_allow_into_the_project_fails(copy):
    root, home, managed = copy
    st._user({"permissions": {"allow": [f"Edit(/{root}/.git/hooks/**)"]}})(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    assert "write-allow" in failed_ids(report)


def test_additional_directory_inside_a_protected_folder_fails(copy):
    root, home, managed = copy
    st._local({"permissions": {"additionalDirectories": ["./data/inner"]}})(root, home, managed)
    report = verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    assert "write-allow" in failed_ids(report)


# --- reporting and the record ------------------------------------------------------------------


def test_runtime_allow_rules_in_the_local_file_pass(copy):
    root, home, managed = copy
    st._local({"permissions": {"allow": ["Bash(npm test)"]}})(root, home, managed)
    assert verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,)).ok


def test_unseen_sources_are_reported_as_information(copy):
    report = st.run_case(*copy, None)
    info = {c.id for c in report.checks if c.ok is None}
    assert {"unseen:--settings", "unseen:managed", "placement"} <= info


def test_verify_writes_nothing(copy):
    root, home, managed = copy
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    verify_static(root, claude_version=st.VERSION, home=home, managed=(managed,))
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert before == after


def test_record_verify_appends_to_both_halves_and_staleness_follows(copy):
    root, home, managed = copy
    report = verify_static(root, claude_version="2.1.310", home=home, managed=(managed,))
    record_verify(root, report, now="2026-11-08T00:00:00Z")
    entry = json.loads((root / COMMITTED_RECORD).read_text())["entries"][-1]
    assert entry["step"] == "verify" and entry["ok"] is True and entry["claude_code"] == "2.1.310"
    assert json.loads((root / LOCAL_RECORD).read_text())["entries"][-1]["step"] == "verify"
    assert verify_static(root, claude_version="2.1.310", home=home, managed=(managed,)).stale is None
    later = verify_static(root, claude_version="2.1.319", home=home, managed=(managed,))
    assert later.stale and "2.1.310" in later.stale and "2.1.319" in later.stale


def test_a_verify_entry_does_not_count_as_drift_for_generate(copy):
    from ratchethub.generate import generate

    root, home, managed = copy
    record_verify(root, verify_static(root, claude_version="v", home=home, managed=(managed,)), now="t")
    generate(root, claude_version="v", now="t2")  # must not raise
