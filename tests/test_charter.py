"""Charter properties with a test of their own. C4: two unrelated projects, one build, no code change."""

from __future__ import annotations

import subprocess

from ratchethub.generate import generate
from ratchethub.verify import verify_static

IGNORE = "ratchethub.local.toml\n.claude/settings.local.json\n.claude/ratchethub-record.local.json\n"


def _project(base, name, toml, folders=()):
    root = base / name
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for f in folders:
        (root / f).mkdir(parents=True)
    (root / ".gitignore").write_text(IGNORE)
    (root / "ratchethub.toml").write_text(toml)
    return root


def test_c4_two_unrelated_projects_one_build(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".npm").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    analytics = _project(tmp_path, "analytics", '[ratchethub]\nschema = 1\n[project]\nname = "example-analytics"\n'
                         '[protect]\nfolders = ["data/raw", ".github/workflows"]\n[network]\n'
                         'allow = ["api.github.com", "github.com"]\n[[deny_command]]\npattern = "gh api graphql *"\n'
                         'reason = "GraphQL needs credentials"\n[publish]\nremotes_allowed = true\n',
                         ("data/raw", ".github/workflows"))
    webapp = _project(tmp_path, "webapp", '[ratchethub]\nschema = 2\n[project]\nname = "a node web app"\n'
                      '[protect]\nfolders = ["migrations"]\n[network]\nallow = ["registry.npmjs.org"]\n'
                      '[writable]\nfolders = ["~/.npm"]\n[publish]\nremotes_allowed = false\n'
                      '[github]\ndefault_branch = "trunk"\nrequired_checks = ["build"]\n', ("migrations",))
    for root in (analytics, webapp):
        generate(root, claude_version="v", now="t")
        report = verify_static(root, claude_version="v", home=home, managed=())
        assert report.ok, (root.name, report.failures)
    a = (analytics / ".claude" / "settings.json").read_text()
    w = (webapp / ".claude" / "settings.json").read_text()
    assert "data/raw" in a and "migrations" not in a and "--no-remotes" not in a
    assert "migrations" in w and "~/.npm" in w and "--no-remotes" in w
