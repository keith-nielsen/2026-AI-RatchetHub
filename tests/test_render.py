"""Rendering a validated declaration into the runtime's settings (schema §2 and §3).

Path anchors differ between the two lists the runtime reads (runtime docs fetched 2026-10-24):
in `Edit(...)` rules `/x` is project-relative and `//x` absolute; in `sandbox.filesystem` lists
`./x` is project-relative and `/x` absolute. Each test pins the form RH must emit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ratchethub.declaration import Declaration, DenyCommand, LocalDeclaration
from ratchethub.render import FLOOR_DENY_WRITE, FLOOR_EDIT_DENY, RenderError, render_committed, render_local


def decl(**kw) -> Declaration:
    base = dict(name="p", protect=(), network_allow=(), deny_commands=(), remotes_allowed=None)
    base.update(kw)
    return Declaration(**base)


# --- adversarial: the floor can't be lost, a control can't be claimed without being generated ----


def test_floor_is_present_in_a_minimal_declaration():
    s = render_committed(decl()).settings
    assert s["sandbox"]["enabled"] is True
    assert s["sandbox"]["failIfUnavailable"] is True
    assert s["sandbox"]["allowUnsandboxedCommands"] is False
    assert s["permissions"]["disableBypassPermissionsMode"] == "disable"
    for rule in FLOOR_EDIT_DENY:
        assert rule in s["permissions"]["deny"]
    for path in FLOOR_DENY_WRITE:
        assert path in s["sandbox"]["filesystem"]["denyWrite"]


def test_floor_self_protection_is_written_in_both_channels():
    s = render_committed(decl()).settings
    deny, deny_write = s["permissions"]["deny"], s["sandbox"]["filesystem"]["denyWrite"]
    for edit_rule, sandbox_path in [
        ("Edit(/.claude/**)", "./.claude"),
        ("Edit(/ratchethub.toml)", "./ratchethub.toml"),
        ("Edit(/ratchethub.local.toml)", "./ratchethub.local.toml"),
    ]:
        assert edit_rule in deny and sandbox_path in deny_write


def test_code_run_paths_get_an_edit_deny_only():
    s = render_committed(decl()).settings
    for edit_rule in ("Edit(/.mcp.json)", "Edit(/.git/hooks/**)", "Edit(/.git/config)"):
        assert edit_rule in s["permissions"]["deny"]
    for sandbox_path in ("./.mcp.json", "./.git/hooks", "./.git/config"):
        assert sandbox_path not in s["sandbox"]["filesystem"]["denyWrite"]


def test_no_project_relative_path_is_emitted_with_a_single_slash_in_the_sandbox_list():
    # A single leading slash in denyWrite is absolute: "/data" would protect the machine's /data.
    s = render_committed(decl(protect=("data",))).settings
    assert all(p.startswith("./") for p in s["sandbox"]["filesystem"]["denyWrite"])


def test_no_project_relative_edit_rule_uses_a_double_slash():
    s = render_committed(decl(protect=("data",))).settings
    edits = [r for r in s["permissions"]["deny"] if r.startswith("Edit(")]
    assert all(r.startswith("Edit(/") and not r.startswith("Edit(//") for r in edits)


def test_refuses_to_render_a_no_remotes_declaration_until_the_outbound_hook_exists():
    # C6: RH never reports a control it doesn't generate.
    with pytest.raises(RenderError, match="outbound"):
        render_committed(decl(remotes_allowed=False))


def test_empty_network_list_writes_no_allowed_domains_key():
    assert "network" not in render_committed(decl()).settings["sandbox"]


def test_no_allow_or_ask_rules_are_generated():
    s = render_committed(decl(protect=("data",), deny_commands=(DenyCommand("git push *", "r"),))).settings
    assert set(s["permissions"]) == {"deny", "disableBypassPermissionsMode"}


def test_every_generated_rule_carries_a_strength():
    result = render_committed(
        decl(protect=("data",), network_allow=("github.com",), deny_commands=(DenyCommand("x *", "r"),))
    )
    generated = set(result.settings["permissions"]["deny"]) | {
        f"denyWrite {p}" for p in result.settings["sandbox"]["filesystem"]["denyWrite"]
    } | {f"allowedDomains {h}" for h in result.settings["sandbox"]["network"]["allowedDomains"]}
    reported = {row.generated for row in result.rows}
    assert generated <= reported, generated - reported
    assert all(row.strength for row in result.rows)


def test_deny_command_is_reported_as_a_tripwire():
    rows = render_committed(decl(deny_commands=(DenyCommand("git push *", "no pushes"),))).rows
    row = next(r for r in rows if r.generated == "Bash(git push *)")
    assert "tripwire" in row.strength and row.reason == "no pushes"


def test_local_render_holds_only_rh_keys():
    s = render_local(LocalDeclaration(protect=("/srv/shared",), deny_commands=())).settings
    assert set(s) == {"permissions", "sandbox"}
    assert set(s["permissions"]) == {"deny"}
    assert set(s["sandbox"]) == {"filesystem"} and set(s["sandbox"]["filesystem"]) == {"denyWrite"}


def test_local_absolute_folder_uses_each_lists_absolute_form():
    s = render_local(LocalDeclaration(protect=("/srv/shared",), deny_commands=())).settings
    assert s["permissions"]["deny"] == ["Edit(//srv/shared/**)"]
    assert s["sandbox"]["filesystem"]["denyWrite"] == ["/srv/shared"]


def test_local_relative_folder_uses_each_lists_relative_form():
    s = render_local(LocalDeclaration(protect=("data",), deny_commands=())).settings
    assert s["permissions"]["deny"] == ["Edit(/data/**)"]
    assert s["sandbox"]["filesystem"]["denyWrite"] == ["./data"]


# --- confirming ------------------------------------------------------------------------------------


def test_renders_the_schema_example_exactly():
    result = render_committed(
        decl(
            name="example-analytics",
            protect=("data/raw", ".github/workflows"),
            network_allow=("api.github.com", "github.com"),
            deny_commands=(DenyCommand("gh api graphql *", "GraphQL needs credentials"),),
            remotes_allowed=True,
        )
    )
    assert result.settings == {
        "permissions": {
            "deny": [
                "Edit(/.claude/**)",
                "Edit(/ratchethub.toml)",
                "Edit(/ratchethub.local.toml)",
                "Edit(/.mcp.json)",
                "Edit(/.git/hooks/**)",
                "Edit(/.git/config)",
                "Edit(/data/raw/**)",
                "Edit(/.github/workflows/**)",
                "Bash(gh api graphql *)",
            ],
            "disableBypassPermissionsMode": "disable",
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": ["api.github.com", "github.com"]},
            "filesystem": {
                "denyWrite": [
                    "./.claude",
                    "./ratchethub.toml",
                    "./ratchethub.local.toml",
                    "./data/raw",
                    "./.github/workflows",
                ]
            },
        },
    }


def test_declared_floor_path_is_not_duplicated():
    s = render_committed(decl(protect=(".claude",))).settings
    assert s["permissions"]["deny"].count("Edit(/.claude/**)") == 1
    assert s["sandbox"]["filesystem"]["denyWrite"].count("./.claude") == 1


def test_output_is_deterministic():
    d = decl(protect=("b", "a"), network_allow=("z.com", "a.com"))
    assert json.dumps(render_committed(d).settings) == json.dumps(render_committed(d).settings)


SCHEMA = os.environ.get("RH_SETTINGS_SCHEMA")


@pytest.mark.skipif(not SCHEMA, reason="set RH_SETTINGS_SCHEMA to the runtime's settings JSON schema")
def test_rendered_files_validate_against_the_runtime_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(Path(SCHEMA).read_text(encoding="utf-8"))
    full = decl(protect=("data",), network_allow=("github.com",), deny_commands=(DenyCommand("x *", "r"),))
    jsonschema.validate(render_committed(full).settings, schema)
    jsonschema.validate(render_local(LocalDeclaration(("/srv/x",), (DenyCommand("y", "r"),))).settings, schema)
