"""`ratchethub <step>`: the change protocol's steps, plus the probe and the report.

    generate [--adopt]                 a person: write the settings, the guard and the record
    verify [--record] [--json]         anyone: static Verify; --record (a person) appends the entry
    live [--yes] [--record] [--mode M] a person, outside any agent sandbox: live Verify
    approve --who NAME [--static-only] a person: approve what was verified
    report                             anyone: every generated row with its strength
    probe [--json]                     anyone: what this session can reach
    selftest                           anyone: every check against its broken inputs
    pr / release                       the drivers: agent steps run, person steps handed off (G1, G3)
    preflight                          the checks run before every push (G5)
    platform                           GitHub compared with the declared state, read-only (G6)

Exit codes: 0 done and passing; 1 refused or a check failed; 2 a usage error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .approve import ApproveError, approve
from .declaration import DeclarationError, load_committed, load_local
from .generate import GenerateError, generate
from .render import render_committed, render_local


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def claude_version(given: str | None) -> str:
    if given:
        return given
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    if not out:
        raise SystemExit("ratchethub: can't read `claude --version`; pass --claude-version")
    return out.split()[0]


def project_root(given: str | None) -> Path:
    if given:
        return Path(given).resolve()
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else Path.cwd().resolve()


def _refused(reasons: list[str]) -> int:
    print("REFUSED")
    for r in reasons:
        print(f"  - {r}")
    return 1


def cmd_generate(a) -> int:
    try:
        result = generate(a.root, claude_version=claude_version(a.claude_version), now=_now(), adopt=a.adopt)
    except GenerateError as exc:
        return _refused(exc.reasons)
    for note in result.notes:
        print(f"note: {note}")
    print(f"generated {len(result.rows)} rows; next: ratchethub verify, then live, then approve")
    return 0


def cmd_verify(a) -> int:
    from .selftest import selftest
    from .verify import record_verify, verify_static

    problems = selftest()
    if problems:
        print("SELFTEST FAILED: a check did not reject its broken input, so no result is reported")
        for p in problems:
            print(f"  - {p}")
        return 1
    report = verify_static(a.root, claude_version=claude_version(a.claude_version))
    if a.json:
        print(json.dumps({"ok": report.ok, "stale": report.stale, "sources": report.sources,
                          "checks": [vars(c) for c in report.checks]}, indent=2))
    else:
        print("sources: " + "; ".join(report.sources))
        for c in report.checks:
            mark = {True: "PASS", False: "FAIL", None: "INFO"}[c.ok]
            print(f"  {mark}  {c.id}: {c.detail}")
        if report.stale:
            print(f"  STALE  {report.stale}")
        print("OK" if report.ok else "FAILED")
    if a.record:
        try:
            record_verify(a.root, report, now=_now())
        except (GenerateError, OSError) as exc:
            return _refused(getattr(exc, "reasons", [str(exc)]))
        print("recorded the static verify entry")
    return 0 if report.ok else 1


def cmd_live(a) -> int:
    from .live import plan, record_live, run_live

    scratch = Path(a.scratch) if a.scratch else Path(tempfile.mkdtemp(prefix="rh-live-")) / "run"
    tag = "plan"
    p = plan(a.root, scratch, tag)
    print(f"live Verify builds two copies under {p['scratch']} and runs two headless sessions "
          f"(mode {a.mode}). Targets: " + "; ".join(p["targets"]))
    if p["outside_scratch"]:
        print("It writes probe files outside the scratch folder, each deleted by exact name afterwards:")
        for path in p["outside_scratch"]:
            print(f"  {path.replace('-plan-', '-<tag>-')}")
    if not a.yes:
        print("Re-run with --yes to start. It must run outside any agent sandbox.")
        return 2
    result = run_live(a.root, scratch, claude_version=claude_version(a.claude_version), mode=a.mode)
    for v in result.verdicts:
        print(f"  {v.verdict:<13} {v.target}\n      governed: {v.governed[:140]}\n      baseline: {v.baseline[:140]}")
    for s in result.sessions:
        print(f"  session {s['label']}: started in {s['started_mode']}, exit {s['exit_code']}")
        if s["started_mode"] is None:
            print(f"      did not start: {(s.get('stderr') or '(no stderr)').splitlines()[0][:200]}")
    if result.residue:
        print("RESIDUE (could not delete): " + ", ".join(result.residue))
    print(f"evidence: {scratch}")
    print("OK" if result.ok else "FAILED")
    if a.record and result.not_started:
        print("not recorded: a session never started, so nothing was measured")
    elif a.record:
        record_live(a.root, result, now=_now())
        print("recorded the live verify entry")
    return 0 if result.ok else 1


def cmd_approve(a) -> int:
    try:
        entry = approve(a.root, who=a.who, now=_now(), claude_version=claude_version(a.claude_version),
                        static_only=a.static_only)
    except ApproveError as exc:
        return _refused(exc.reasons)
    print(f"approved by {entry['who']} at {entry['at']}" + (" (static only: no row shown to hold live)"
                                                           if entry["static_only"] else ""))
    return 0


def cmd_report(a) -> int:
    try:
        rows = render_committed(load_committed(a.root)).rows + render_local(load_local(a.root)).rows
    except DeclarationError as exc:
        return _refused(exc.errors)
    for r in rows:
        print(f"{r.source:<24} {r.generated}\n{'':<24}   strength: {r.strength}"
              + (f"\n{'':<24}   reason: {r.reason}" if r.reason else "")
              + (f"\n{'':<24}   note: {r.note}" if r.note else ""))
    print("A strength is the class a row belongs to. Only a passing live verify entry shows it holds.")
    from .residue import RULES

    print("\nRESIDUE: rules no control here reaches (not controls; nothing enforces them)")
    for i, (title, _) in enumerate(RULES, 1):
        print(f"  {i}. {title}")
    return 0


def cmd_probe(a) -> int:
    from .probe import probe, render

    print(render(probe(a.root), a.root, a.json))
    return 0


def cmd_selftest(a) -> int:
    from .selftest import CASES, selftest

    problems = selftest()
    for p in problems:
        print(f"  - {p}")
    print(f"{len(CASES)} broken inputs: " + ("all rejected" if not problems else f"{len(problems)} not rejected"))
    return 0 if not problems else 1


def _github(a):
    from .github.read import Reader, slug_from_remote

    slug = slug_from_remote(str(a.root))
    if not slug:
        raise SystemExit("ratchethub: no GitHub remote named origin; the slug comes from the remote, never the folder")
    gh = load_committed(a.root).github
    return slug, Reader(), gh


def cmd_pr(a) -> int:
    from .github.driver import Driver
    from .github.state import git

    slug, reader, gh = _github(a)
    base = a.base or (gh.default_branch if gh else "main")
    branch = a.branch or git(a.root, "branch", "--show-current").stdout.strip()
    d = Driver(a.root, slug, reader)
    if a.assert_head or a.assert_pr:
        ok = d.assert_state(branch, base, head=a.assert_head or "", pr=a.assert_pr or 0)
        print("state unchanged: proceeding" if ok else "GitHub has moved since this plan was written; stopping "
              "without changing anything. Run the driver again.")
        return 0 if ok else 1
    return d.run(branch, base, title=a.title or "", body_file=a.body_file or "", mnemonic=a.tag or "change").code


def cmd_release(a) -> int:
    from .github.driver import release

    slug, reader, gh = _github(a)
    return release(a.root, slug, reader, tag=a.tag, notes_file=a.notes, default_branch=gh.default_branch if gh else "main")


def cmd_preflight(a) -> int:
    from .github.preflight import FINDING, preflight

    items = preflight(a.root, base_ref=a.base)
    for i in items:
        print(f"  {i.result:<10} {i.name}: {i.detail}")
    return 1 if any(i.result == FINDING for i in items) else 0


def cmd_platform(a) -> int:
    from .github.platform import DRIFT, reconcile

    slug, reader, gh = _github(a)
    if gh is None:
        return _refused(["no [github] section declares the platform state to compare"])
    rows = reconcile(reader, slug, gh, a.root)
    for r in rows:
        print(f"  {r.result:<10} {r.item}: {r.detail}" + (f" [{r.channel}]" if r.channel else ""))
    print("read-only: drift is reported, never fixed")
    return 1 if any(r.result == DRIFT for r in rows) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ratchethub", description=__doc__.split("\n")[0])
    ap.add_argument("--version", action="version", version=f"ratchethub {__version__}")
    ap.add_argument("--root", help="the project root (default: the git top level of the working directory)")
    ap.add_argument("--claude-version", help="the Claude Code version (default: `claude --version`)")
    sub = ap.add_subparsers(dest="step", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--adopt", action="store_true", help="take over an existing settings file RH didn't write")
    v = sub.add_parser("verify")
    v.add_argument("--record", action="store_true")
    v.add_argument("--json", action="store_true")
    li = sub.add_parser("live")
    li.add_argument("--yes", action="store_true")
    li.add_argument("--record", action="store_true")
    li.add_argument("--mode", default="auto")
    li.add_argument("--scratch")
    ap_ = sub.add_parser("approve")
    ap_.add_argument("--who", required=True)
    ap_.add_argument("--static-only", action="store_true")
    sub.add_parser("report")
    p = sub.add_parser("probe")
    p.add_argument("--json", action="store_true")
    sub.add_parser("selftest")
    pr = sub.add_parser("pr", help="the PR lifecycle driver (level-triggered)")
    pr.add_argument("--branch")
    pr.add_argument("--base")
    pr.add_argument("--title")
    pr.add_argument("--body-file")
    pr.add_argument("--tag", help="a short name for the change, used in the handoff line's tag")
    pr.add_argument("--assert-head")
    pr.add_argument("--assert-pr", type=int)
    pr.add_argument("--after", help="the step the plan just ran; the driver re-derives the state")
    rel = sub.add_parser("release", help="the release driver")
    rel.add_argument("--tag", required=True)
    rel.add_argument("--notes", required=True)
    pf = sub.add_parser("preflight", help="the checks run before every push")
    pf.add_argument("--base")
    sub.add_parser("platform", help="compare GitHub with the declared platform state (read-only)")
    a = ap.parse_args(argv)
    a.root = project_root(a.root)
    return {"generate": cmd_generate, "verify": cmd_verify, "live": cmd_live, "approve": cmd_approve,
            "report": cmd_report, "probe": cmd_probe, "selftest": cmd_selftest, "pr": cmd_pr,
            "release": cmd_release, "preflight": cmd_preflight, "platform": cmd_platform}[a.step](a)


if __name__ == "__main__":
    sys.exit(main())
