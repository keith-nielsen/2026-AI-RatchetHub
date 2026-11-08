"""The Approve step (docs/change-protocol.md §4). A person runs it; the agent can't (it writes under
`.claude/`, which the floor denies to the agent).

Approve refuses unless the record's latest static verify entry passed, isn't stale, and matches the
files on disk now. A live verify entry for the same files is required too, unless the person
approves with `static_only`: the approval then says so, and the report says no row was shown to
hold (charter C6).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import __version__
from .declaration import COMMITTED_NAME, LOCAL_NAME
from .generate import COMMITTED_RECORD, LOCAL_RECORD, SETTINGS, SETTINGS_LOCAL, GenerateError, read_records, sha, write_record
from .render import GUARD_PATH

HASHED = (SETTINGS, SETTINGS_LOCAL, GUARD_PATH, COMMITTED_NAME, LOCAL_NAME)


class ApproveError(GenerateError):
    pass


def current_hashes(root: Path) -> dict[str, str | None]:
    return {n: (sha((root / n).read_bytes()) if (root / n).exists() else None) for n in HASHED}


def entry_hash(entry: dict) -> str:
    return sha(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _latest(entries: list, kind: str) -> dict | None:
    found = [e for e in entries if isinstance(e, dict) and e.get("step") == "verify" and e.get("kind") == kind]
    return found[-1] if found else None


def approve(root: Path, *, who: str, now: str, claude_version: str, static_only: bool = False) -> dict:
    root = Path(root).resolve()
    if not who or not who.strip():
        raise ApproveError(["Approve must name who approves"])
    records = read_records(root)
    if records is None:
        raise ApproveError(["there is no record: run Generate, then Verify"])
    committed, local = records
    entries = committed["entries"]
    reasons = []
    disk = current_hashes(root)

    static = _latest(entries, "static")
    last_generate = max(i for i, e in enumerate(entries) if isinstance(e, dict) and e.get("step") == "generate")
    if static is None or entries.index(static) < last_generate:
        reasons.append("there is no static verify entry since the last Generate")
    else:
        if not static.get("ok"):
            reasons.append("the latest static verify entry did not pass")
        if static.get("claude_code") != claude_version:
            reasons.append(f"the static verify entry is stale: verified against Claude Code "
                           f"{static.get('claude_code')}, now {claude_version}")
        changed = [n for n in HASHED if static.get("files", {}).get(n) != disk[n]]
        if changed:
            reasons.append("changed since the static verify entry: " + ", ".join(changed))

    live = _latest(entries, "live")
    if not static_only:
        if live is None or entries.index(live) < last_generate:
            reasons.append("there is no live verify entry since the last Generate (a person runs it, outside "
                           "the sandbox), or approve static-only and say so")
        else:
            if not live.get("ok"):
                reasons.append("the latest live verify entry did not pass")
            if live.get("claude_code") != claude_version:
                reasons.append(f"the live verify entry is stale: verified against Claude Code "
                               f"{live.get('claude_code')}, now {claude_version}")
            changed = [n for n in HASHED if live.get("files", {}).get(n) != disk[n]]
            if changed:
                reasons.append("changed since the live verify entry: " + ", ".join(changed))
    if reasons:
        raise ApproveError(reasons)

    entry = {"step": "approve", "at": now, "who": who.strip(), "ratchethub": __version__,
             "claude_code": claude_version, "files": disk, "static_verify": entry_hash(static),
             "live_verify": None if static_only else entry_hash(live), "static_only": static_only}
    write_record(root / COMMITTED_RECORD, committed, entry)
    write_record(root / LOCAL_RECORD, local, {**entry, "see": COMMITTED_RECORD})
    return entry
