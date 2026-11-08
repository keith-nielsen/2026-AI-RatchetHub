"""The capability probe (v1 scope A6): what this session can reach, measured now, never recalled.

The subject is the project: the folder holding `ratchethub.toml`, never the working directory.
Each row names its channel, a state from a closed vocabulary, and its evidence: `inspected`
(a precondition was read) or `attempted:<channel>` (the channel was exercised). The agent may run
it; it writes nothing, prints no secret, and exits 0 whatever it finds (a probe that fails teaches
its caller to stop probing).

Axis trap: AUTHENTICATED and UNAUTHENTICATED describe a credential. ABSENT describes the tool or
the credential's source, and leaves the credential state unknown. It is not UNAUTHENTICATED.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

AUTHENTICATED, UNAUTHENTICATED, ABSENT = "AUTHENTICATED", "UNAUTHENTICATED", "ABSENT"
REACHABLE, UNREACHABLE, PRESENT, UNMEASURED = "REACHABLE", "UNREACHABLE", "PRESENT", "UNMEASURED"
STATES = frozenset({AUTHENTICATED, UNAUTHENTICATED, ABSENT, REACHABLE, UNREACHABLE, PRESENT, UNMEASURED})
INSPECTED = "inspected"
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
API = "https://api.github.com"


def attempted(channel: str) -> str:
    return f"attempted:{channel}"


@dataclass(frozen=True)
class ProbeRow:
    channel: str
    state: str
    evidence: str
    note: str = ""

    def __post_init__(self):
        if self.state not in STATES:
            raise AssertionError(f"undeclared probe state: {self.state!r}")


def _run(argv: list[str], cwd: Path, env: dict | None = None, timeout: int = 20) -> subprocess.CompletedProcess:
    full = {**(os.environ if env is None else env), "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=full)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _http_status(url: str, token: str | None, opener=urllib.request.urlopen) -> int | None:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "ratchethub-probe"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with opener(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def credential_helpers(root: Path, env: dict | None = None) -> list[tuple[str, str]]:
    """Every configured credential helper, as (config key, helper), in git's own order. Includes
    helpers set through the environment (GIT_CONFIG_COUNT), which a sandbox may inject."""
    r = _run(["git", "-C", str(root), "config", "--get-regexp", r"^credential\..*helper$"], root, env)
    out = []
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        out.append((key, value))
    return out


def probe(root: Path, *, opener=urllib.request.urlopen, env: dict | None = None) -> list[ProbeRow]:
    root = Path(root).resolve()
    env = dict(os.environ if env is None else env)
    rows: list[ProbeRow] = []

    status = _http_status(f"{API}/zen", None, opener)
    rows.append(ProbeRow("github api (anonymous)", REACHABLE if status and status < 500 else UNREACHABLE,
                         attempted("rest-anonymous"), f"HTTP {status}" if status else "no response"))

    for var in TOKEN_VARS:
        token = env.get(var)
        if not token:
            rows.append(ProbeRow(f"env {var}", ABSENT, INSPECTED))
            continue
        code = _http_status(f"{API}/user", token, opener)
        state = AUTHENTICATED if code == 200 else UNAUTHENTICATED if code in (401, 403) else UNMEASURED
        rows.append(ProbeRow(f"env {var}", state, attempted("rest-token"), f"HTTP {code}" if code else "no response"))

    if shutil.which("gh", path=env.get("PATH")):
        r = _run(["gh", "auth", "status"], root, env)
        ok = r.returncode == 0 and "Logged in" in r.stdout + r.stderr
        rows.append(ProbeRow("gh credential", AUTHENTICATED if ok else UNAUTHENTICATED, attempted("gh-auth-status"),
                             "" if ok else "this process only; not a statement about the person's account"))
    else:
        rows.append(ProbeRow("gh credential", ABSENT, INSPECTED, "gh not on PATH: credential state unknown"))

    helpers = credential_helpers(root, env)
    if not helpers:
        rows.append(ProbeRow("git credential.helper", ABSENT, INSPECTED, "no helper configured"))
    for key, helper in helpers:
        rows.append(ProbeRow(f"git {key}", PRESENT, INSPECTED, helper))
    if helpers:
        rows.append(_fill(root, env))

    sock = env.get("SSH_AUTH_SOCK")
    if not sock:
        rows.append(ProbeRow("ssh agent", ABSENT, INSPECTED, "SSH_AUTH_SOCK not set"))
    else:
        r = _run(["ssh-add", "-l"], root, env, timeout=10)
        rows.append(ProbeRow("ssh agent", AUTHENTICATED if r.returncode == 0 else UNAUTHENTICATED if r.returncode == 1
                             else UNREACHABLE, attempted("ssh-add"),
                             f"{len(r.stdout.splitlines())} key(s)" if r.returncode == 0 else r.stderr.strip()[:80]))

    home = Path(env.get("HOME") or Path.home())
    keys = sorted(p.name for p in (home / ".ssh").glob("id_*") if not p.name.endswith(".pub")) \
        if (home / ".ssh").is_dir() else []
    rows.append(ProbeRow("ssh key files", PRESENT if keys else ABSENT, INSPECTED, ", ".join(keys)))

    remotes = _run(["git", "-C", str(root), "remote"], root, env).stdout.split()
    if not remotes:
        rows.append(ProbeRow("git remotes", ABSENT, INSPECTED, "no remote configured"))
    for name in remotes:
        r = _run(["git", "-C", str(root), "ls-remote", "--heads", name], root, env, timeout=30)
        rows.append(ProbeRow(f"git ls-remote {name}", REACHABLE if r.returncode == 0 else UNREACHABLE,
                             attempted("git-subprocess"), _cause(r.stderr)))
    return rows


def _fill(root: Path, env: dict) -> ProbeRow:
    """Ask the helpers for a github.com credential, without a prompt, and keep nothing but the
    answer's shape. The password never leaves this function."""
    full = {**env, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}
    try:
        r = subprocess.run(["git", "-C", str(root), "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                           capture_output=True, text=True, timeout=20, env=full)
    except subprocess.TimeoutExpired:
        return ProbeRow("git credential fill github.com", UNMEASURED, attempted("git-credential"), "timed out")
    except OSError as exc:
        return ProbeRow("git credential fill github.com", UNMEASURED, attempted("git-credential"), str(exc)[:80])
    got = any(line.startswith("password=") and len(line) > len("password=") for line in r.stdout.splitlines())
    return ProbeRow("git credential fill github.com", AUTHENTICATED if got and r.returncode == 0 else UNAUTHENTICATED,
                    attempted("git-credential"), "a stored credential was returned (not checked against GitHub)"
                    if got else "no credential returned")


def _cause(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    for ln in lines:
        if any(k in ln.lower() for k in ("fatal:", "error:", "denied", "not found", "could not", "authentication")):
            return ln[:100]
    return lines[-1][:100] if lines else ""


def render(rows: list[ProbeRow], root: Path, as_json: bool = False) -> str:
    if as_json:
        return json.dumps({"subject": str(root), "rows": [asdict(r) for r in rows]}, indent=2)
    width = max(len(r.channel) for r in rows)
    lines = [f"CAPABILITY PROBE (measured now) for {root}"]
    lines += [f"  {r.channel:<{width}}  {r.state:<16} {r.evidence:<24} {r.note}".rstrip() for r in rows]
    return "\n".join(lines)
