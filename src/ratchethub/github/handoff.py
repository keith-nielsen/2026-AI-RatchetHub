"""The handoff file (v1 scope G2): a person-run step as a short, self-guarding `next.sh`.

When a driver's next step is a person's (a merge, a release, a tag push), it writes the full
command to `.git/ratchethub/next.sh` and prints one short line to relay. The paste channel has
corrupted long commands, so only the short line is typed. The file pins:

- the **branch**, **step id** and **PR number**, for the mutation and for its verification;
- an expiry (24 hours: an approval doesn't outlive the state it was given for);
- a refusal once its step has verified: the driver writes `.git/ratchethub/verified/<step>-<key>`
  (the key is the PR number, else the branch or tag), and the file refuses to run when that marker
  exists. Marking a step verified also removes the file and the relay sidecar.

Agent-run steps never get a file (ruled: the agent re-runs the driver). The relay line is written to
the sidecar the relay guard compares against, and the emission record the outbound guard reads.
Paths are `guard.py`'s own, so the three can't drift apart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path

from ..guard import EMISSION, RELAY_DIR, RELAY_LINE, RELAY_MARK

TTL = 24 * 60 * 60
MAX_WIDTH = 80
RUN_PREFIX = "! "  # how the operator runs a relayed line inside Claude Code; it counts toward the width
STEP = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


def plan_path(root) -> Path:
    return Path(root) / RELAY_DIR / "next.sh"


def verified_marker(root, step: str, key: int | str | None) -> Path:
    """The marker one plan refuses on. `key` names what the step acted on (a PR number, else the
    branch or tag), so a step done once for one change can't spend the same step for another."""
    if key is None or str(key) == "":
        raise ValueError(f"step {step!r} needs a key (a PR number, branch or tag) for its marker")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(key))
    if safe != str(key):  # `a/b` and `a_b` must not share a marker
        safe += "." + hashlib.sha256(str(key).encode()).hexdigest()[:10]
    return Path(root) / RELAY_DIR / "verified" / f"{step}-{safe}"


def write_emission(root, step: str, command: str, branch: str, *, now: float | None = None) -> Path | None:
    """The command the driver printed, for the outbound guard to match byte for byte. Never fatal:
    without it the guard asks, which is the safe direction."""
    try:
        (Path(root) / RELAY_DIR).mkdir(parents=True, exist_ok=True)
        path = Path(root) / EMISSION
        path.write_text(json.dumps({"command": command, "step": step, "branch": branch,
                                    "expires": int((now or time.time()) + TTL)}, indent=2) + "\n")
        return path
    except OSError:
        return None


def write_plan(root, *, step: str, command: str, branch: str, pr: int | None, verify_lines: list[str],
               assert_lines: list[str] | None = None, foreign: bool = False, now: float | None = None,
               key: int | str | None = None) -> Path:
    """Write the person's step. Refuses a plan it can't guard: no branch, a bad step id, or a
    foreign branch (no local copy, so no checkout to compare) without a state assertion."""
    if not branch:
        raise ValueError("a plan must name the branch it was written for")
    if not STEP.match(step):
        raise ValueError(f"step id {step!r} must be lower-case letters, digits and hyphens")
    if foreign and not assert_lines:
        raise ValueError("a foreign-branch plan needs a state assertion: it is that plan's only guard")
    if not verify_lines:
        raise ValueError("a plan must verify its own step after the mutation")
    root = Path(root).resolve()
    marker = verified_marker(root, step, key if key is not None else pr)
    expiry = int((now or time.time()) + TTL)
    body = [
        "#!/usr/bin/env bash",
        f"# ratchethub handoff: step {step}, branch {branch}, PR {pr if pr is not None else '(none)'}",
        "set -euo pipefail",
        f"_step={shlex.quote(step)} _branch={shlex.quote(branch)} _pr={shlex.quote(str(pr or ''))}",
        f'if [ "$(date +%s)" -gt {expiry} ]; then',
        '  echo "this plan has expired; run the driver again for a current one" >&2; exit 1',
        "fi",
        f"if [ -e {shlex.quote(str(marker))} ]; then",
        '  echo "step $_step has already verified; this plan is spent. Run the driver again." >&2; exit 1',
        "fi",
    ]
    if foreign:
        body.append(f"# branch {branch} has no local copy; the assertion below pins the PR and its head")
    else:
        body += [
            f'_have="$(git -C {shlex.quote(str(root))} branch --show-current)"',
            'if [ "$_have" != "$_branch" ]; then',
            '  echo "this plan is for branch $_branch (step $_step), but you are on $_have" >&2; exit 1',
            "fi",
        ]
    body.append(f"cd {shlex.quote(str(root))}")
    body += list(assert_lines or [])
    body += ["", "# the mutation: the step you are approving", command, "", "# verification of the same step:",
             *verify_lines]
    path = plan_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body) + "\n")
    path.chmod(0o755)
    return path


def mark_verified(root, step: str, key: int | str) -> None:
    """The step verified: write the marker the plan refuses on, and remove the plan, the emission
    record and the relay sidecar (with its block mark), so a spent command isn't left where a later
    one can match it, and the relay guard doesn't hold a later copy block to a spent line."""
    marker = verified_marker(root, step, key)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
    for p in (plan_path(root), Path(root) / EMISSION, Path(root) / RELAY_LINE, Path(root) / RELAY_MARK):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def relay_line(root, step: str, pr: int | None, mnemonic: str = "change") -> str:
    """The one line a person runs: flush left, its path shortened to `~/` under the home folder,
    and a short `#` tag naming the change and the step, so identical lines in history can be told
    apart. The PR number joins the tag only while the line still fits the terminal; the plan pins
    the PR either way."""
    path = str(plan_path(Path(root).resolve()))
    home = os.path.expanduser("~")
    if path.startswith(home + "/"):
        path = "~" + path[len(home):]
    line = f"bash {path}  # {mnemonic} {step}"
    with_pr = f"{line} #{pr}" if pr is not None else line
    return with_pr if len(RUN_PREFIX + with_pr) <= MAX_WIDTH else line


def emit(root, step: str, pr: int | None, *, mnemonic: str = "change", has_assertion: bool, out=print) -> str:
    """Print the copy block and write the sidecar the relay guard checks against."""
    line = relay_line(root, step, pr, mnemonic)
    try:
        (Path(root) / RELAY_DIR).mkdir(parents=True, exist_ok=True)
        (Path(root) / RELAY_LINE).write_text(line + "\n")
    except OSError:
        out("(could not write the relay sidecar, so the relay guard can't check this line)")
    out("Relay this block exactly, copied whole:")
    out("START COPY")
    out("```bash")
    out(line)
    out("```")
    out("END COPY")
    width = len(RUN_PREFIX) + len(line)
    if width > MAX_WIDTH:
        out(f"(the line is {width} columns with the `{RUN_PREFIX.strip()}` prefix; widen the terminal so it "
            "doesn't wrap before copying)")
    out("It re-checks the state you were shown and stops without changing anything if GitHub has moved; "
        "it expires in 24 hours." if has_assertion else
        "It expires in 24 hours. This step has no PR state to re-check.")
    return line
