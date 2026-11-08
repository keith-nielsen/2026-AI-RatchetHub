"""Broken inputs each static check must reject (charter §3: a check is trusted only after it has
been shown to fail on a broken input).

`selftest()` builds a throwaway project, generates it, confirms the clean copy passes, then applies
each broken input to a fresh copy and confirms the named check fails. Verify refuses to report a
result when the self-test doesn't pass. The suite runs these same cases (tests/test_verify.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .generate import SETTINGS, SETTINGS_LOCAL, generate
from .verify import verify_static

DECLARATION = '[ratchethub]\nschema = 1\n\n[project]\nname = "selftest"\n\n[protect]\nfolders = ["data"]\n'
IGNORE = "ratchethub.local.toml\n.claude/settings.local.json\n.claude/ratchethub-record.local.json\n"
VERSION = "selftest"


@dataclass(frozen=True)
class Case:
    name: str
    expect: str  # the check id that must fail
    apply: Callable[[Path, Path, Path], None]  # (project root, home, managed file)


def _merge_json(path: Path, patch: dict) -> None:
    data = json.loads(path.read_text()) if path.exists() else {}

    def deep(a: dict, b: dict) -> None:
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                deep(a[k], v)
            else:
                a[k] = v

    deep(data, patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _local(patch: dict):
    return lambda root, home, managed: _merge_json(root / SETTINGS_LOCAL, patch)


def _user(patch: dict):
    return lambda root, home, managed: _merge_json(home / ".claude" / "settings.json", patch)


def _managed(patch: dict):
    return lambda root, home, managed: _merge_json(managed, patch)


def _edit_project(root: Path, home: Path, managed: Path) -> None:
    path = root / SETTINGS
    data = json.loads(path.read_text())
    data["sandbox"]["filesystem"]["denyWrite"].remove("./data")
    path.write_text(json.dumps(data, indent=2) + "\n")


def _drop_bypass_lock(root: Path, home: Path, managed: Path) -> None:
    path = root / SETTINGS
    data = json.loads(path.read_text())
    del data["permissions"]["disableBypassPermissionsMode"]
    path.write_text(json.dumps(data, indent=2) + "\n")


def _drop_hooks(root: Path, home: Path, managed: Path) -> None:
    path = root / SETTINGS
    data = json.loads(path.read_text())
    del data["hooks"]
    path.write_text(json.dumps(data, indent=2) + "\n")


def _delete_guard(root: Path, home: Path, managed: Path) -> None:
    (root / ".claude" / "hooks" / "ratchethub_guard.py").unlink()


def _edit_guard(root: Path, home: Path, managed: Path) -> None:
    (root / ".claude" / "hooks" / "ratchethub_guard.py").write_text("import sys\nsys.exit(0)\n")


def _change_local_declaration(root: Path, home: Path, managed: Path) -> None:
    (root / "ratchethub.local.toml").write_text('[protect]\nfolders = ["data"]\n')


def _change_declaration(root: Path, home: Path, managed: Path) -> None:
    (root / "ratchethub.toml").write_text(DECLARATION + "\n# changed\n")


def _delete_record(root: Path, home: Path, managed: Path) -> None:
    (root / ".claude" / "ratchethub-record.json").unlink()
    (root / ".claude" / "ratchethub-record.local.json").unlink()


CASES = (
    Case("floor: sandbox off in the local file", "floor:sandbox.enabled", _local({"sandbox": {"enabled": False}})),
    Case("floor: unsandboxed retry allowed in the local file", "floor:sandbox.allowUnsandboxedCommands",
         _local({"sandbox": {"allowUnsandboxedCommands": True}})),
    Case("floor: failIfUnavailable off in a managed file", "floor:sandbox.failIfUnavailable",
         _managed({"sandbox": {"failIfUnavailable": False}})),
    Case("floor: filesystem isolation off in the user file", "floor:sandbox.filesystem.disabled",
         _user({"sandbox": {"filesystem": {"disabled": True}}})),
    Case("floor: sandbox limited to another platform", "floor:sandbox.enabledPlatforms",
         _local({"sandbox": {"enabledPlatforms": ["windows"]}})),
    Case("loosening: an excluded command in the user file", "loosening",
         _user({"sandbox": {"excludedCommands": ["make *"]}})),
    Case("loosening: every Unix socket allowed", "loosening",
         _local({"sandbox": {"network": {"allowAllUnixSockets": True}}})),
    Case("loosening: violations ignored", "loosening", _local({"sandbox": {"ignoreViolations": {"*": ["/"]}}})),
    Case("write allow: allowWrite into .claude", "write-allow",
         _local({"sandbox": {"filesystem": {"allowWrite": ["./.claude"]}}})),
    Case("write allow: Edit allow on a declared folder", "write-allow",
         _local({"permissions": {"allow": ["Edit(/data/**)"]}})),
    Case("write allow: wildcard Edit allow over the project", "write-allow",
         _local({"permissions": {"allow": ["Edit(/**)"]}})),
    Case("write allow: bare Edit allow", "write-allow", _user({"permissions": {"allow": ["Edit"]}})),
    Case("floor: the bypass lock removed from the project file", "floor:permissions.disableBypassPermissionsMode",
         _drop_bypass_lock),
    Case("drift: a row removed from the project file", "drift", _edit_project),
    Case("hooks: switched off in the local file", "floor:disableAllHooks", _local({"disableAllHooks": True})),
    Case("hooks: managed hooks only", "floor:allowManagedHooksOnly", _managed({"allowManagedHooksOnly": True})),
    Case("hooks: removed from the project file", "hooks", _drop_hooks),
    Case("hooks: the guard file deleted", "hooks", _delete_guard),
    Case("drift: the guard file edited", "drift", _edit_guard),
    Case("rows: a declared row missing from the effective settings", "rows", _edit_project),
    Case("record: the declaration changed since Generate", "record", _change_declaration),
    Case("record: only the local declaration changed since Generate", "record", _change_local_declaration),
    Case("record: no record", "record", _delete_record),
    Case("settings: an unreadable local file", "settings-readable",
         lambda root, home, managed: (root / SETTINGS_LOCAL).write_text("{not json")),
)


def build(base: Path) -> tuple[Path, Path, Path]:
    root, home, managed = base / "project", base / "home", base / "managed-settings.json"
    (root / "data").mkdir(parents=True)
    home.mkdir()
    subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"], check=True, capture_output=True)
    (root / ".gitignore").write_text(IGNORE)
    (root / "ratchethub.toml").write_text(DECLARATION)
    generate(root, claude_version=VERSION, now="1970-01-01T00:00:00Z")
    return root, home, managed


def run_case(root: Path, home: Path, managed: Path, case: Case | None):
    if case is not None:
        case.apply(root, home, managed)
    return verify_static(root, claude_version=VERSION, home=home, managed=(managed,))


def selftest(cases: tuple[Case, ...] = CASES) -> list[str]:
    """Return a list of problems; empty means every check rejected its broken input."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="rh-selftest-") as tmp:
        template = Path(tmp) / "template"
        template.mkdir()
        build(template)
        clean = Path(tmp) / "clean"
        shutil.copytree(template, clean, symlinks=True)
        report = run_case(clean / "project", clean / "home", clean / "managed-settings.json", None)
        if not report.ok:
            problems.append("the clean copy failed: " + "; ".join(f"{c.id}: {c.detail}" for c in report.failures))
        for i, case in enumerate(cases):
            copy = Path(tmp) / f"case{i}"
            shutil.copytree(template, copy, symlinks=True)
            report = run_case(copy / "project", copy / "home", copy / "managed-settings.json", case)
            if case.expect not in {c.id for c in report.failures}:
                problems.append(f"{case.name}: check {case.expect!r} did not fail")
    return problems
