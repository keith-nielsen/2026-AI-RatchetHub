"""Declaration validation (docs/declaration-schema.md, schema version 1).

Adversarial cases come first: each is a declaration the validator must refuse. The confirming
cases at the end show that a valid declaration passes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ratchethub.declaration import DeclarationError, load_committed, load_local


def write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


HEADER = '[ratchethub]\nschema = 1\n\n[project]\nname = "p"\n'


def refuse_committed(root: Path, body: str, needle: str) -> None:
    write(root, "ratchethub.toml", body)
    with pytest.raises(DeclarationError) as info:
        load_committed(root)
    assert any(needle in e for e in info.value.errors), info.value.errors


def refuse_local(root: Path, body: str, needle: str) -> None:
    write(root, "ratchethub.local.toml", body)
    with pytest.raises(DeclarationError) as info:
        load_local(root)
    assert any(needle in e for e in info.value.errors), info.value.errors


# --- header -------------------------------------------------------------------------------------


def test_refuses_missing_file(tmp_path):
    with pytest.raises(DeclarationError) as info:
        load_committed(tmp_path)
    assert any("not found" in e for e in info.value.errors)


def test_refuses_invalid_toml(tmp_path):
    refuse_committed(tmp_path, "[ratchethub\nschema = 1\n", "not valid TOML")


def test_refuses_missing_ratchethub_section(tmp_path):
    refuse_committed(tmp_path, '[project]\nname = "p"\n', "[ratchethub] is required")


@pytest.mark.parametrize("value", ["3", "0", '"1"', "true", "1.0"])
def test_refuses_unknown_or_mistyped_schema(tmp_path, value):
    refuse_committed(tmp_path, f'[ratchethub]\nschema = {value}\n[project]\nname = "p"\n', "schema")


def test_refuses_missing_project_name(tmp_path):
    refuse_committed(tmp_path, "[ratchethub]\nschema = 1\n[project]\n", "[project] name")


def test_refuses_empty_project_name(tmp_path):
    refuse_committed(tmp_path, '[ratchethub]\nschema = 1\n[project]\nname = ""\n', "[project] name")


# --- the floor and unknown sections (C5) ---------------------------------------------------------


@pytest.mark.parametrize("section", ["sandbox", "permissions", "hooks"])
def test_refuses_an_attempt_to_set_the_floor(tmp_path, section):
    refuse_committed(tmp_path, HEADER + f"[{section}]\nenabled = false\n", "floor")


@pytest.mark.parametrize("section", ["[[exempt]]", "[[exception]]", "[writable]"])
def test_refuses_sections_not_in_schema_1(tmp_path, section):
    refuse_committed(tmp_path, HEADER + f'{section}\nx = "y"\n', "not in schema version 1")


@pytest.mark.parametrize("section", ["[[exempt]]", "[[exception]]"])
def test_refuses_sections_not_in_schema_2(tmp_path, section):
    refuse_committed(tmp_path, HEADER.replace("schema = 1", "schema = 2") + f'{section}\nx = "y"\n',
                     "not in schema version 2")


def test_refuses_unknown_section(tmp_path):
    refuse_committed(tmp_path, HEADER + '[extras]\nx = "y"\n', "unknown section")


def test_refuses_unknown_key_inside_a_section(tmp_path):
    refuse_committed(tmp_path, HEADER + '[protect]\nfolders = []\nfiles = ["a"]\n', "unknown key")


# --- [protect] paths ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["data/*", "data/?", "data/[ab]", "data/**"])
def test_refuses_wildcards(tmp_path, path):
    refuse_committed(tmp_path, HEADER + f'[protect]\nfolders = ["{path}"]\n', "wildcard")


@pytest.mark.parametrize("name", ["a(b", "a)b", "a\\\\b"])
def test_refuses_characters_the_rule_syntax_treats_specially(tmp_path, name):
    (tmp_path / name.replace("\\\\", "\\")).mkdir()
    refuse_committed(tmp_path, HEADER + f'[protect]\nfolders = ["{name}"]\n', "treats specially")


def test_refuses_absolute_path_in_committed_file(tmp_path):
    refuse_committed(tmp_path, HEADER + f'[protect]\nfolders = ["{tmp_path}"]\n', "relative")


def test_refuses_home_path_in_committed_file(tmp_path):
    refuse_committed(tmp_path, HEADER + '[protect]\nfolders = ["~/data"]\n', "relative")


@pytest.mark.parametrize("path", ["..", "../sibling", "data/../../x"])
def test_refuses_paths_that_leave_the_project(tmp_path, path):
    refuse_committed(tmp_path, HEADER + f'[protect]\nfolders = ["{path}"]\n', "outside the project")


@pytest.mark.parametrize("path", [".", "./", "data/.."])
def test_refuses_the_project_root_itself(tmp_path, path):
    (tmp_path / "data").mkdir()
    refuse_committed(tmp_path, HEADER + f'[protect]\nfolders = ["{path}"]\n', "project root")


def test_refuses_a_missing_folder(tmp_path):
    refuse_committed(tmp_path, HEADER + '[protect]\nfolders = ["nope"]\n', "does not exist")


def test_refuses_a_file_where_a_folder_is_declared(tmp_path):
    write(tmp_path, "afile", "x")
    refuse_committed(tmp_path, HEADER + '[protect]\nfolders = ["afile"]\n', "not a folder")


def test_refuses_a_path_through_a_symbolic_link(tmp_path):
    (tmp_path / "real" / "inner").mkdir(parents=True)
    os.symlink(tmp_path / "real", tmp_path / "link")
    refuse_committed(tmp_path, HEADER + '[protect]\nfolders = ["link/inner"]\n', "symbolic link")


def test_refuses_a_folder_that_is_a_symbolic_link(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")
    refuse_committed(tmp_path, HEADER + '[protect]\nfolders = ["link"]\n', "symbolic link")


def test_refuses_non_string_folder(tmp_path):
    refuse_committed(tmp_path, HEADER + "[protect]\nfolders = [1]\n", "must be a string")


def test_refuses_folders_not_an_array(tmp_path):
    refuse_committed(tmp_path, HEADER + '[protect]\nfolders = "data"\n', "array")


# --- [network] ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "app.localhost", "127.0.0.1", "10.0.0.1", "::1", "[::1]", "*.github.com", "github.com:443",
     "https://github.com", "github.com/path", "", "-bad.com", "a..b"],
)
def test_refuses_hosts_that_are_not_plain_host_names(tmp_path, host):
    refuse_committed(tmp_path, HEADER + f'[network]\nallow = ["{host}"]\n', "[network] allow")


# --- [[deny_command]] -----------------------------------------------------------------------------


def test_refuses_deny_command_without_reason(tmp_path):
    refuse_committed(tmp_path, HEADER + '[[deny_command]]\npattern = "git push *"\n', "reason")


def test_refuses_deny_command_with_empty_pattern(tmp_path):
    refuse_committed(tmp_path, HEADER + '[[deny_command]]\npattern = ""\nreason = "r"\n', "pattern")


@pytest.mark.parametrize("pattern", ["git push)", "git\\npush"])
def test_refuses_pattern_that_breaks_the_rule_syntax(tmp_path, pattern):
    refuse_committed(
        tmp_path, HEADER + f'[[deny_command]]\npattern = "{pattern}"\nreason = "r"\n', "pattern"
    )


def test_refuses_deny_command_as_a_table_not_an_array(tmp_path):
    refuse_committed(tmp_path, HEADER + '[deny_command]\npattern = "x"\nreason = "r"\n', "[[deny_command]]")


# --- [publish] ------------------------------------------------------------------------------------


def test_refuses_non_boolean_remotes_allowed(tmp_path):
    refuse_committed(tmp_path, HEADER + '[publish]\nremotes_allowed = "no"\n', "boolean")


# --- the local file -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    ["[ratchethub]\nschema = 1\n", '[project]\nname = "p"\n', '[network]\nallow = ["github.com"]\n',
     "[publish]\nremotes_allowed = true\n"],
)
def test_local_file_refuses_anything_but_protect_and_deny_command(tmp_path, body):
    refuse_local(tmp_path, body, "local declaration")


def test_local_file_refuses_wildcards_too(tmp_path):
    refuse_local(tmp_path, '[protect]\nfolders = ["/srv/*"]\n', "wildcard")


def test_local_file_refuses_a_missing_absolute_folder(tmp_path):
    refuse_local(tmp_path, f'[protect]\nfolders = ["{tmp_path}/nope"]\n', "does not exist")


def test_local_file_refuses_an_absolute_path_through_a_symbolic_link(tmp_path):
    (tmp_path / "real").mkdir()
    os.symlink(tmp_path / "real", tmp_path / "link")
    refuse_local(tmp_path, f'[protect]\nfolders = ["{tmp_path}/link"]\n', "symbolic link")


def test_all_errors_are_reported_together(tmp_path):
    write(tmp_path, "ratchethub.toml", HEADER + '[protect]\nfolders = ["a/*", "nope"]\n[extras]\n')
    with pytest.raises(DeclarationError) as info:
        load_committed(tmp_path)
    assert len(info.value.errors) == 3, info.value.errors


# --- confirming cases -----------------------------------------------------------------------------


def test_accepts_the_schema_example(tmp_path):
    (tmp_path / "data" / "raw").mkdir(parents=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    write(tmp_path, "ratchethub.toml", EXAMPLE)
    decl = load_committed(tmp_path)
    assert decl.name == "example-analytics"
    assert decl.protect == ("data/raw", ".github/workflows")
    assert decl.network_allow == ("api.github.com", "github.com")
    assert decl.deny_commands[0].pattern == "gh api graphql *"
    assert decl.remotes_allowed is True


def test_minimal_declaration_has_empty_defaults(tmp_path):
    write(tmp_path, "ratchethub.toml", HEADER)
    decl = load_committed(tmp_path)
    assert decl.protect == () and decl.network_allow == () and decl.deny_commands == ()
    assert decl.remotes_allowed is None


def test_normalizes_trailing_slash_and_dot_prefix(tmp_path):
    (tmp_path / "data").mkdir()
    write(tmp_path, "ratchethub.toml", HEADER + '[protect]\nfolders = ["./data/"]\n')
    assert load_committed(tmp_path).protect == ("data",)


def test_missing_local_file_is_an_empty_local_declaration(tmp_path):
    local = load_local(tmp_path)
    assert local.protect == () and local.deny_commands == ()


def test_local_file_accepts_absolute_and_relative_folders(tmp_path):
    (tmp_path / "data").mkdir()
    shared = tmp_path / "shared"
    shared.mkdir()
    write(tmp_path, "ratchethub.local.toml", f'[protect]\nfolders = ["{shared}", "data"]\n')
    assert load_local(tmp_path).protect == (str(shared), "data")


EXAMPLE = """\
# ratchethub.toml — declared once; RatchetHub generates .claude/settings.json from it.

[ratchethub]
schema = 1

[project]
name = "example-analytics"

[protect]
folders = ["data/raw", ".github/workflows"]

[network]
allow = ["api.github.com", "github.com"]

[[deny_command]]
pattern = "gh api graphql *"
reason = "GraphQL needs credentials this agent must not hold"

[publish]
remotes_allowed = true
"""


# --- [writable] (schema 2) ----------------------------------------------------------------------------

V2 = HEADER.replace("schema = 1", "schema = 2")


def writable(root: Path, home: Path, folders: str, path_env: str = "/usr/bin:/bin"):
    write(root, "ratchethub.toml", V2 + f"[writable]\nfolders = {folders}\n")
    return load_committed(root, home=home, path_env=path_env)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    for d in (".npm", ".config/x", ".local/bin", ".claude", ".ssh", "bin", "cache"):
        (h / d).mkdir(parents=True)
    return h


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    for d in ("build/cache", "data", ".claude", ".git/hooks"):
        (p / d).mkdir(parents=True)
    return p


@pytest.mark.parametrize("folders, needle", [
    ('["~"]', "must be relative"), ('["~/"]', "home folder itself"), ('["~/."]', "home folder itself"),
    ('["~/../x"]', "outside it"), ('["~bob/x"]', "must be relative"), ('["/opt/x"]', "no other absolute"),
    ('["~/.config"]', "~/.config"), ('["~/.config/x"]', "~/.config"), ('["~/.local"]', "~/.local/bin"),
    ('["~/.local/bin"]', "~/.local/bin"), ('["~/.claude"]', "~/.claude"), ('["~/.ssh"]', "~/.ssh"),
    ('["."]', "project root itself"), ('["../x"]', "outside the project"), ('[".claude"]', "overlaps"),
    ('[".git"]', "overlaps"), ('[".git/hooks"]', "overlaps"), ('["ratchethub.toml"]', "overlaps"),
    ('["~/.npm/*"]', "wildcard"), ('["~/a(b"]', "specially"), ('["~/missing"]', "does not exist"),
    ('"~/.npm"', "array"), ("[3]", "must be a string"),
])
def test_writable_refusals(proj, home, folders, needle):
    with pytest.raises(DeclarationError) as info:
        writable(proj, home, folders)
    assert any(needle in e for e in info.value.errors), info.value.errors


def test_writable_refuses_a_folder_on_path(proj, home):
    with pytest.raises(DeclarationError, match="on PATH"):
        writable(proj, home, '["~/bin"]', path_env=f"/usr/bin:{home}/bin")


def test_writable_refuses_an_ancestor_of_a_folder_on_path(proj, home):
    with pytest.raises(DeclarationError, match="on PATH"):
        writable(proj, home, '["build"]', path_env=f"/usr/bin:{proj}/build/cache")


def test_writable_refuses_overlap_with_protect(proj, home):
    write(proj, "ratchethub.toml", V2 + '[protect]\nfolders = ["data"]\n[writable]\nfolders = ["data"]\n')
    with pytest.raises(DeclarationError, match="overlaps the protected path data"):
        load_committed(proj, home=home, path_env="")


def test_writable_refuses_a_symlink(proj, home, tmp_path):
    (home / "link").symlink_to(home / "cache")
    with pytest.raises(DeclarationError, match="symbolic link"):
        writable(proj, home, '["~/link"]')


def test_writable_is_not_in_the_local_file(proj):
    refuse_local(proj, '[writable]\nfolders = ["build"]\n', "not allowed in a local declaration")


def test_writable_accepts_home_and_project_folders(proj, home):
    d = writable(proj, home, '["~/.npm", "build/cache", "~/.npm/", "./build/cache"]')
    assert d.writable == ("~/.npm", "build/cache") and d.schema == 2


def test_schema_1_still_loads(proj):
    write(proj, "ratchethub.toml", HEADER)
    assert load_committed(proj).schema == 1
