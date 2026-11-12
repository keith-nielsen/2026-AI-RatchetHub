"""Static Verify (docs/change-protocol.md §4): reads the effective settings and reports.

`verify_static` writes nothing, so the agent may run it. `record_verify` appends the verify entry
to the record, under `.claude/`, which the floor denies to the agent: only a person can record.

The effective settings are every file the runtime merges on this machine. Precedence, highest
first (runtime docs fetched 2026-10-24): managed, `--settings`, local, project, user. Lists
combine across files; for a scalar the highest file that sets it wins. Static Verify can't see
`--settings`, MDM profiles or server-managed settings, and says so in its report.

Before any check is trusted it must reject its broken inputs (`selftest`, charter §3).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import __version__
from . import generate as _generate
from .declaration import COMMITTED_NAME, LOCAL_NAME, DeclarationError, load_committed, load_local
from .generate import (
    COMMITTED_RECORD,
    LOCAL_RECORD,
    SETTINGS,
    SETTINGS_LOCAL,
    GenerateError,
    drift_reasons,
    get,
    last_generate,
    read_json,
    read_records,
    sha,
    write_record,
)
from .render import (
    CODE_RUN_DIRS,
    CODE_RUN_FILES,
    GUARD_PATH,
    SELF_PROTECTED_DIRS,
    SELF_PROTECTED_FILES,
    render_committed,
    render_local,
)

# The runtime write-protects these itself (seen in its sandbox configuration, 2026-11-08).
MANAGED_PATHS = (Path("/etc/claude-code/managed-settings.json"), Path("/etc/claude-code/managed-settings.d"))
FLOOR_SCALARS = (
    ("sandbox.enabled", True),
    ("sandbox.failIfUnavailable", True),
    ("sandbox.allowUnsandboxedCommands", False),
)
WILDCARDS = set("*?[")


@dataclass(frozen=True)
class Check:
    id: str
    ok: bool | None  # None: information, not a pass or a fail
    detail: str


@dataclass
class Report:
    checks: list[Check]
    sources: list[str]
    claude_code: str
    stale: str | None = None
    hashes: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.ok is False]


@dataclass(frozen=True)
class Source:
    scope: str  # managed | local | project | user
    path: Path
    data: dict


def verify_static(root: Path, *, claude_version: str, home: Path | None = None,
                  managed: tuple[Path, ...] = MANAGED_PATHS, platform: str | None = None) -> Report:
    root = Path(root).resolve()
    home = Path(home) if home is not None else Path.home()
    checks: list[Check] = []

    sources, unreadable = _sources(root, home, managed)
    for path in unreadable:
        checks.append(Check("settings-readable", False, f"{path} is not valid JSON; the runtime's view is unknown"))

    records = _check_record(root, checks)
    _check_floor(sources, platform or _platform(), checks)
    _check_hooks(root, sources, checks)
    _check_loosening(sources, checks)
    _check_write_allows(root, home, sources, checks)
    _check_rows_present(root, sources, checks)
    checks += [
        Check("unseen:--settings", None, "a session started with --settings can override these files; "
              "static Verify can't see it"),
        Check("unseen:managed", None, "MDM profiles and server-managed settings aren't visible here; "
              "only managed files are read"),
        Check("placement", None, "the floor lives in the project file, so a change in the local file is "
              "caught only when Verify runs (protocol §6: detective)"),
    ]

    stale = None
    if records is not None:
        verifies = [e for e in records[0]["entries"] if isinstance(e, dict) and e.get("step") == "verify"]
        if verifies and verifies[-1].get("claude_code") != claude_version:
            stale = f"last verified against Claude Code {verifies[-1].get('claude_code')}, now {claude_version}"

    hashes = {}
    for name in (SETTINGS, SETTINGS_LOCAL, GUARD_PATH, COMMITTED_NAME, LOCAL_NAME):
        path = root / name
        hashes[name] = sha(path.read_bytes()) if path.exists() else None
    return Report(checks, [f"{s.scope}: {s.path}" for s in sources], claude_version, stale, hashes)


def record_verify(root: Path, report: Report, *, now: str) -> None:
    """Append the verify entry to both halves of the record. A person runs this, never the agent."""
    root = Path(root).resolve()
    records = read_records(root)
    if records is None:
        raise GenerateError(["there is no record to add a verify entry to; run Generate first"])
    entry = {
        "step": "verify", "kind": "static", "at": now, "ratchethub": __version__,
        "claude_code": report.claude_code, "ok": report.ok, "files": report.hashes,
        "checks": [{"id": c.id, "ok": c.ok, "detail": c.detail} for c in report.checks],
    }
    write_record(root / COMMITTED_RECORD, records[0], entry)
    write_record(root / LOCAL_RECORD, records[1], {**entry, "checks": None, "see": COMMITTED_RECORD})


# --- sources -------------------------------------------------------------------------------------


def _sources(root: Path, home: Path, managed: tuple[Path, ...]) -> tuple[list[Source], list[Path]]:
    """Every settings file, highest precedence first."""
    candidates: list[tuple[str, Path]] = []
    for path in managed:
        if path.is_dir():
            candidates += [("managed", p) for p in sorted(path.glob("*.json"))]
        else:
            candidates.append(("managed", path))
    candidates += [("local", root / SETTINGS_LOCAL), ("project", root / SETTINGS),
                   ("user", home / ".claude" / "settings.json")]
    sources, unreadable = [], []
    for scope, path in candidates:
        if not path.is_file():
            continue
        data = read_json(path)
        if isinstance(data, dict):
            sources.append(Source(scope, path, data))
        else:
            unreadable.append(path)
    return sources, unreadable


def _effective(sources: list[Source], key: str):
    for source in sources:
        value = get(source.data, key)
        if value is not None:
            return value, source
    return None, None


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        try:
            if "microsoft" in Path("/proc/version").read_text().lower():
                return "wsl"
        except OSError:
            pass
        return "linux"
    return sys.platform


# --- checks --------------------------------------------------------------------------------------


def _check_record(root: Path, checks: list[Check]):
    try:
        records = read_records(root)
    except GenerateError as exc:
        checks.append(Check("record", False, "; ".join(exc.reasons)))
        return None
    if records is None:
        checks.append(Check("record", False, "no record: Generate has not run here"))
        return None
    committed, local = records
    reasons = []
    decl_path, local_decl = root / COMMITTED_NAME, root / LOCAL_NAME
    current = sha(decl_path.read_bytes()) if decl_path.exists() else None
    if last_generate(committed).get("declaration") != current:
        reasons.append(f"{COMMITTED_NAME} changed since the last Generate")
    current_local = sha(local_decl.read_bytes()) if local_decl.exists() else None
    if last_generate(local).get("declaration") != current_local:
        reasons.append(f"{LOCAL_NAME} changed since the last Generate")
    checks.append(Check("record", not reasons, "; ".join(reasons) or "the record's generate entry matches "
                        "both declarations"))
    try:
        drift = drift_reasons(root, committed, local)
    except GenerateError as exc:
        drift = exc.reasons
    checks.append(Check("drift", not drift, "; ".join(drift) or "the generated files match the record"))
    return records


def _check_floor(sources: list[Source], platform: str, checks: list[Check]) -> None:
    for key, want in FLOOR_SCALARS:
        value, source = _effective(sources, key)
        where = f"{source.scope} ({source.path})" if source else "no file"
        ok = value is want
        detail = f"effective {key} = {value!r}, set by {where}"
        overridden = [s for s in sources if get(s.data, key) is not None and get(s.data, key) != want and s is not source]
        if ok and overridden:
            detail += "; overridden lower down: " + ", ".join(f"{s.scope} sets {get(s.data, key)!r}" for s in overridden)
        checks.append(Check(f"floor:{key}", ok, detail))

    setters = [s.scope for s in sources if get(s.data, "permissions.disableBypassPermissionsMode") == "disable"]
    checks.append(Check("floor:permissions.disableBypassPermissionsMode", bool(setters),
                        f"set to \"disable\" by {', '.join(setters)}" if setters else "no file sets it to \"disable\""))

    disabled = [s.scope for s in sources if get(s.data, "sandbox.filesystem.disabled") is True]
    checks.append(Check("floor:sandbox.filesystem.disabled", not disabled,
                        f"filesystem isolation switched off by {', '.join(disabled)}" if disabled
                        else "filesystem isolation is not switched off"))

    platforms, source = _effective(sources, "sandbox.enabledPlatforms")
    ok = platforms is None or platform in platforms
    checks.append(Check("floor:sandbox.enabledPlatforms", ok,
                        "not set" if platforms is None else
                        f"{source.scope} limits the sandbox to {platforms}; this platform is {platform}"))


def _hook_commands(data: dict) -> set[str]:
    out = set()
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for matchers in hooks.values():
            for m in matchers if isinstance(matchers, list) else []:
                for h in (m.get("hooks") or []) if isinstance(m, dict) else []:
                    if isinstance(h, dict) and isinstance(h.get("command"), str):
                        out.add(h["command"])
    return out


def _check_hooks(root: Path, sources: list[Source], checks: list[Check]) -> None:
    """RH's hooks are tripwires, but a tripwire switched off must still be reported."""
    value, source = _effective(sources, "disableAllHooks")
    checks.append(Check("floor:disableAllHooks", value is not True,
                        f"hooks switched off by {source.scope} ({source.path})" if value is True
                        else "hooks are not switched off"))
    managed_only = [str(s.path) for s in sources if s.scope == "managed" and s.data.get("allowManagedHooksOnly") is True]
    checks.append(Check("floor:allowManagedHooksOnly", not managed_only,
                        f"only managed hooks run ({', '.join(managed_only)}), so RH's hooks don't"
                        if managed_only else "project hooks may run"))
    present = set().union(*(_hook_commands(s.data) for s in sources)) if sources else set()
    try:
        wanted = _hook_commands(render_committed(load_committed(root)).settings)
    except Exception:  # reported by the record and rows checks
        wanted = set()
    missing = sorted(wanted - present)
    guard = root / GUARD_PATH
    if not guard.is_file():
        missing.append(f"the guard file {GUARD_PATH}")
    checks.append(Check("hooks", not missing, "missing: " + "; ".join(missing) if missing
                        else "RH's three hooks are wired and the guard file is present"))
    if guard.is_file():
        # The drift check compares the guard with RH's last output. This one compares it with the
        # guard this RatchetHub would write now: a guard change that hasn't been through Generate
        # leaves the hook that actually runs on the old code, and nothing else would say so.
        current = guard.read_bytes() == _generate.GUARD_SOURCE.read_bytes()
        checks.append(Check("guard-current", current,
                            "the installed guard is this RatchetHub's guard" if current else
                            f"{GUARD_PATH} is not this RatchetHub's guard source: a guard change hasn't "
                            "been through Generate, so the hook that runs is the old one. The whole cycle "
                            "is owed: Generate, static and live Verify, Approve"))


LOOSENING_LISTS = ("sandbox.excludedCommands", "sandbox.network.allowUnixSockets")
LOOSENING_TRUE = ("sandbox.network.allowAllUnixSockets", "sandbox.enableWeakerNestedSandbox",
                  "sandbox.enableWeakerNetworkIsolation")


def _check_loosening(sources: list[Source], checks: list[Check]) -> None:
    found = []
    for s in sources:
        for key in LOOSENING_LISTS:
            if get(s.data, key):
                found.append(f"{s.scope} sets {key} = {get(s.data, key)!r}")
        for key in LOOSENING_TRUE:
            if get(s.data, key) is True:
                found.append(f"{s.scope} sets {key} = true")
        if get(s.data, "sandbox.ignoreViolations"):
            found.append(f"{s.scope} sets sandbox.ignoreViolations")
    checks.append(Check("loosening", not found, "; ".join(found) or "no file sets a key that loosens the sandbox"))


def _protected(root: Path) -> list[PurePosixPath]:
    paths = [*SELF_PROTECTED_DIRS, *SELF_PROTECTED_FILES, *CODE_RUN_DIRS, *CODE_RUN_FILES]
    out = [PurePosixPath(root) / p for p in paths]
    for loader in (load_committed, load_local):
        try:
            for p in loader(root).protect:
                out.append(PurePosixPath(p) if p.startswith("/") else PurePosixPath(root) / p)
        except DeclarationError:
            pass  # reported by the record check: the declaration changed since Generate
    return out


def _check_write_allows(root: Path, home: Path, sources: list[Source], checks: list[Check]) -> None:
    """An allow at or inside a protected path is a loosening. A deny inside an allowed folder holds
    (runtime docs, denyWrite), so an allow of a parent folder is not; the overlap of the two is
    undocumented, so RH fails it."""
    protected = _protected(root)
    found = []
    for s in sources:
        allows = [(e, "allowWrite") for e in (get(s.data, "sandbox.filesystem.allowWrite") or [])]
        allows += [(e, "additionalDirectories") for e in (get(s.data, "permissions.additionalDirectories") or [])]
        rules = get(s.data, "permissions.allow") or []
        for rule in rules:
            if not isinstance(rule, str):
                continue
            if rule in ("Edit", "Write") or rule.startswith(("Edit(", "Write(")):
                allows.append((rule, "permissions.allow"))
        for entry, key in allows:
            hit = _allow_hits(entry, key, s.scope, root, home, protected)
            if hit:
                found.append(f"{s.scope} {key} {entry!r} reaches {hit}")
    checks.append(Check("write-allow", not found, "; ".join(found) or "no write allow reaches a protected path"))


def _allow_hits(entry: str, key: str, scope: str, root: Path, home: Path, protected) -> str | None:
    if not isinstance(entry, str):
        return None
    if key == "permissions.allow":
        if entry in ("Edit", "Write") or entry in ("Edit()", "Write()"):
            return "every path"
        inner = entry[entry.index("(") + 1:-1] if entry.endswith(")") else entry
        base = home / ".claude" if scope == "user" else root
        if inner.startswith("//"):
            path = inner[1:]
        elif inner.startswith("~/"):
            path = str(home / inner[2:])
        elif inner.startswith("/"):
            path = str(base / inner[1:])
        else:
            path = str(root / inner)
    else:
        base = home / ".claude" if scope == "user" and key == "allowWrite" else root
        if entry.startswith("/"):
            path = "/" + entry.lstrip("/")
        elif entry.startswith("~/"):
            path = str(home / entry[2:])
        else:
            path = str(base / entry)
    wild = any(WILDCARDS & set(part) for part in PurePosixPath(path).parts)
    parts = []
    for part in PurePosixPath(path).parts:
        if WILDCARDS & set(part):
            break
        parts.append(part)
    allowed = PurePosixPath(*parts) if parts else PurePosixPath("/")
    for p in protected:
        if allowed == p or p in allowed.parents:
            return str(p)
        if wild and allowed in p.parents:
            return f"{p} (a wildcard below {allowed} can match it)"
    return None


def _check_rows_present(root: Path, sources: list[Source], checks: list[Check]) -> None:
    try:
        expected = render_committed(load_committed(root)).settings
        expected_local = render_local(load_local(root)).settings
    except Exception as exc:  # reported by the record check when the declaration is the cause
        checks.append(Check("rows", False, f"can't render the declaration: {exc}"))
        return
    merged = {key: [x for s in sources for x in (get(s.data, key) or [])]
              for key in ("permissions.deny", "sandbox.filesystem.denyWrite", "sandbox.network.allowedDomains",
                          "sandbox.filesystem.allowWrite")}
    missing = []
    for settings in (expected, expected_local):
        for key, values in merged.items():
            for v in get(settings, key) or []:
                if v not in values:
                    missing.append(f"{key} {v!r}")
    checks.append(Check("rows", not missing, "missing from the effective settings: " + ", ".join(missing)
                        if missing else "every declared row is present in the effective settings"))
