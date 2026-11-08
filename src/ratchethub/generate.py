"""The Generate step (docs/change-protocol.md §3–§4).

Refuses unless every precondition holds, and writes nothing when it refuses. Order of writes:
the settings files, then the records, each by write-then-rename. A crash between writes leaves a
file whose hash differs from the record, which the next run reports as drift: it fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .declaration import COMMITTED_NAME, LOCAL_NAME, SCHEMA_VERSION, DeclarationError, load_committed, load_local
from .render import RenderError, Row, render_committed, render_local

SETTINGS = ".claude/settings.json"
SETTINGS_LOCAL = ".claude/settings.local.json"
COMMITTED_RECORD = ".claude/ratchethub-record.json"
LOCAL_RECORD = ".claude/ratchethub-record.local.json"
MUST_BE_IGNORED = (LOCAL_NAME, SETTINGS_LOCAL, LOCAL_RECORD)

# Keys whose loss loosens the controls. Adoption refuses to drop an entry under any of them.
RESTRICTION_KEYS = (
    "permissions.deny",
    "permissions.ask",
    "sandbox.filesystem.denyWrite",
    "sandbox.filesystem.denyRead",
    "sandbox.network.deniedDomains",
    "sandbox.credentials",
    "hooks",
)
LOCAL_RH_KEYS = ("permissions.deny", "sandbox.filesystem.denyWrite")


class GenerateError(Exception):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass
class GenerateResult:
    hashes: dict[str, str]
    rows: list[Row]
    notes: list[str] = field(default_factory=list)


def generate(root: Path, *, claude_version: str, now: str, adopt: bool = False) -> GenerateResult:
    root = Path(root).resolve()
    if not claude_version:
        raise GenerateError(["no Claude Code version given; the record must name the runtime it was generated for"])

    _check_git(root)

    try:
        decl = load_committed(root)
        local = load_local(root)
    except DeclarationError as exc:
        raise GenerateError(exc.errors) from None
    try:
        committed = render_committed(decl)
        local_out = render_local(local)
    except RenderError as exc:
        raise GenerateError([str(exc)]) from None

    settings_bytes = (json.dumps(committed.settings, indent=2) + "\n").encode("utf-8")
    new_local_keys = {
        "permissions.deny": local_out.settings["permissions"]["deny"],
        "sandbox.filesystem.denyWrite": local_out.settings["sandbox"]["filesystem"]["denyWrite"],
    }

    records = _read_records(root)
    notes: list[str] = []
    if records is None:
        notes += _first_run_checks(root, committed.settings, new_local_keys, adopt)
    else:
        _drift_checks(root, *records)

    # --- write -------------------------------------------------------------------------------------
    (root / ".claude").mkdir(exist_ok=True)
    _atomic_write(root / SETTINGS, settings_bytes)
    _write_local_keys(root / SETTINGS_LOCAL, new_local_keys)

    hashes = {SETTINGS: _sha(settings_bytes), SETTINGS_LOCAL: _keys_hash(rh_local_keys(root / SETTINGS_LOCAL))}
    local_decl = root / LOCAL_NAME
    common = {"step": "generate", "at": now, "ratchethub": __version__, "schema": SCHEMA_VERSION,
              "claude_code": claude_version}
    committed_entry = {**common, "declaration": _sha((root / COMMITTED_NAME).read_bytes()),
                       "files": {SETTINGS: hashes[SETTINGS]}, "adopted": records is None and adopt}
    local_entry = {**common, "declaration": _sha(local_decl.read_bytes()) if local_decl.exists() else None,
                   "files": {SETTINGS_LOCAL: hashes[SETTINGS_LOCAL]}}
    old_committed, old_local = records if records is not None else ({"entries": []}, {"entries": []})
    _write_record(root / COMMITTED_RECORD, old_committed, committed_entry)
    _write_record(root / LOCAL_RECORD, old_local, local_entry)

    return GenerateResult(hashes, committed.rows + local_out.rows, notes)


def rh_local_keys(path: Path) -> dict[str, list]:
    """RH's keys of `.claude/settings.local.json`; an absent file or key reads as empty."""
    data = _read_json(path) if path.exists() else {}
    if data is None:
        raise GenerateError([f"{path.name} is not valid JSON"])
    return {key: list(_get(data, key) or []) for key in LOCAL_RH_KEYS}


# --- preconditions -------------------------------------------------------------------------------


def _check_git(root: Path) -> None:
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        raise GenerateError([f"{root} is not a git repository; RH needs git to confirm what is ignored"])
    if Path(top.stdout.strip()).resolve() != root:
        raise GenerateError([f"{root} is not the root of its git repository; the runtime loads "
                             f"{SETTINGS_LOCAL} from the repository root"])
    reasons = []
    for name in MUST_BE_IGNORED:
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", name], capture_output=True)
        if ignored.returncode == 1:
            reasons.append(f"{name} is not ignored by git (or is tracked); it must never be committed")
        elif ignored.returncode != 0:
            reasons.append(f"git check-ignore failed for {name}: {ignored.stderr.decode().strip()}")
    if reasons:
        raise GenerateError(reasons)


def _read_records(root: Path) -> tuple[dict, dict] | None:
    committed_path, local_path = root / COMMITTED_RECORD, root / LOCAL_RECORD
    if not committed_path.exists() and not local_path.exists():
        return None
    reasons = []
    if not committed_path.exists():
        reasons.append(f"{LOCAL_RECORD} exists without {COMMITTED_RECORD}; restore the record from git")
    if not local_path.exists():
        reasons.append(f"{COMMITTED_RECORD} exists without {LOCAL_RECORD}; this machine's half of the record "
                       "is missing")
    if reasons:
        raise GenerateError(reasons)
    out = []
    for path in (committed_path, local_path):
        data = _read_json(path)
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise GenerateError([f"{path.name}: the record is not readable"])
        if not any(isinstance(e, dict) and e.get("step") == "generate" for e in data["entries"]):
            raise GenerateError([f"{path.name}: the record has no generate entry"])
        out.append(data)
    return out[0], out[1]


def _first_run_checks(root: Path, new_settings: dict, new_local_keys: dict, adopt: bool) -> list[str]:
    reasons, notes = [], []
    settings_path = root / SETTINGS
    if settings_path.exists():
        existing = _read_json(settings_path)
        if not isinstance(existing, dict):
            raise GenerateError([f"{SETTINGS} exists and is not valid JSON"])
        if not adopt:
            raise GenerateError([f"{SETTINGS} exists and RH has no record of writing it; pass adopt to take it over"])
        for key in RESTRICTION_KEYS:
            lost = _lost(_get(existing, key), _get(new_settings, key))
            if lost:
                reasons.append(f"adopting {SETTINGS} would drop {key} entries {lost}; declare them first")
        for key in sorted(_leaf_keys(existing) - _leaf_keys(new_settings)):
            if not any(key == r or key.startswith(r + ".") for r in RESTRICTION_KEYS):
                notes.append(f"adoption drops {key} from {SETTINGS}")

    local_path = root / SETTINGS_LOCAL
    if local_path.exists():
        existing_local = rh_local_keys(local_path)
        for key in LOCAL_RH_KEYS:
            lost = _lost(existing_local[key], new_local_keys[key])
            if lost:
                reasons.append(f"generating would replace {key} in {SETTINGS_LOCAL} and drop {lost}; "
                               f"declare them in {LOCAL_NAME} first")
    if reasons:
        raise GenerateError(reasons)
    return notes


def _drift_checks(root: Path, committed_record: dict, local_record: dict) -> None:
    reasons = []
    expected = _last_generate(committed_record)["files"].get(SETTINGS)
    path = root / SETTINGS
    actual = _sha(path.read_bytes()) if path.exists() else None
    if actual != expected:
        what = "is missing" if actual is None else "differs from RH's last output"
        reasons.append(f"drift: {SETTINGS} {what}. Restore it from git, or move the change into "
                       f"{COMMITTED_NAME} and restore, then run Generate again")
    expected_local = _last_generate(local_record)["files"].get(SETTINGS_LOCAL)
    if _keys_hash(rh_local_keys(root / SETTINGS_LOCAL)) != expected_local:
        reasons.append(f"drift: RH's keys in {SETTINGS_LOCAL} ({', '.join(LOCAL_RH_KEYS)}) differ from RH's last "
                       f"output. Move the change into {LOCAL_NAME}, undo it in the file, then run Generate again")
    if reasons:
        raise GenerateError(reasons)


# --- helpers -------------------------------------------------------------------------------------


def _write_local_keys(path: Path, keys: dict[str, list]) -> None:
    data = _read_json(path) if path.exists() else {}
    if data is None:
        raise GenerateError([f"{SETTINGS_LOCAL} is not valid JSON"])
    for key, value in keys.items():
        _set(data, key, value)
    _prune(data)
    if data or path.exists():
        _atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"))


def _write_record(path: Path, record: dict, entry: dict) -> None:
    record = {**record, "entries": [*record["entries"], entry]}
    _atomic_write(path, (json.dumps(record, indent=2) + "\n").encode("utf-8"))


def _last_generate(record: dict) -> dict:
    return [e for e in record["entries"] if isinstance(e, dict) and e.get("step") == "generate"][-1]


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".rh-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _keys_hash(keys: dict) -> str:
    return _sha(json.dumps(keys, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _get(data: dict, dotted: str):
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _set(data: dict, dotted: str, value: list) -> None:
    *parents, last = dotted.split(".")
    for part in parents:
        data = data.setdefault(part, {})
    if value:
        data[last] = value
    else:
        data.pop(last, None)


def _prune(data: dict) -> None:
    for key in list(data):
        if isinstance(data[key], dict):
            _prune(data[key])
            if not data[key]:
                del data[key]


def _lost(old, new) -> list:
    if not old:
        return []
    if isinstance(old, list):
        new_list = new if isinstance(new, list) else []
        return [x for x in old if x not in new_list]
    return [] if old == new else [old]


def _leaf_keys(data: dict, prefix: str = "") -> set[str]:
    out = set()
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out |= _leaf_keys(value, dotted + ".")
        else:
            out.add(dotted)
    return out
