"""Render a validated declaration into the runtime's settings (docs/declaration-schema.md §2–§3).

Pure: no filesystem access. Every generated entry is paired with a report row stating its
strength (charter C6). A strength here is the class the entry belongs to; only Verify's live
check may report an entry as shown to hold.

Path anchors (runtime docs fetched 2026-10-24): in `Edit(...)` rules `/x` is project-relative and
`//x` absolute; in `sandbox.filesystem` lists `./x` is project-relative and `/x` absolute.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .declaration import Declaration, DenyCommand, LocalDeclaration

OS = "operating system sandbox (Bash only)"
RULE = "permission rule (file tools)"
TRIPWIRE = "tripwire: matches command text only; another spelling of the command is not caught"
PROXY = "sandbox proxy, by host name only; unlisted hosts are decided by permission mode"
FLOOR = "floor setting, written whatever the declaration says"

SELF_PROTECTED_DIRS = (".claude",)
SELF_PROTECTED_FILES = ("ratchethub.toml", "ratchethub.local.toml")
CODE_RUN_DIRS = (".git/hooks",)
CODE_RUN_FILES = (".mcp.json", ".git/config")

FLOOR_EDIT_DENY = (
    *(f"Edit(/{d}/**)" for d in SELF_PROTECTED_DIRS),
    *(f"Edit(/{f})" for f in SELF_PROTECTED_FILES),
    "Edit(/.mcp.json)",
    *(f"Edit(/{d}/**)" for d in CODE_RUN_DIRS),
    "Edit(/.git/config)",
)
FLOOR_DENY_WRITE = tuple(f"./{p}" for p in (*SELF_PROTECTED_DIRS, *SELF_PROTECTED_FILES))


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class Row:
    source: str
    generated: str
    strength: str
    reason: str | None = None
    note: str | None = None


@dataclass
class Rendered:
    settings: dict
    rows: list[Row] = field(default_factory=list)


def render_committed(decl: Declaration) -> Rendered:
    if decl.remotes_allowed is False:
        raise RenderError(
            "[publish] remotes_allowed = false needs RH's outbound hook, which this build doesn't "
            "generate yet; RH won't report a control it doesn't generate (C6)"
        )
    rows: list[Row] = []
    deny: list[str] = []
    deny_write: list[str] = []

    for key, value in (
        ("sandbox.enabled", "true"),
        ("sandbox.failIfUnavailable", "true"),
        ("sandbox.allowUnsandboxedCommands", "false"),
        ("permissions.disableBypassPermissionsMode", '"disable"'),
    ):
        rows.append(Row("floor", f"{key} = {value}", FLOOR))

    for d in SELF_PROTECTED_DIRS:
        _add(deny, rows, f"Edit(/{d}/**)", "floor: self-protection", RULE)
        _add(deny_write, rows, f"./{d}", "floor: self-protection", OS, prefix="denyWrite ")
    for f in SELF_PROTECTED_FILES:
        _add(deny, rows, f"Edit(/{f})", "floor: self-protection", RULE)
        _add(deny_write, rows, f"./{f}", "floor: self-protection", OS, prefix="denyWrite ",
             note="unprobed while the file doesn't exist")
    _add(deny, rows, "Edit(/.mcp.json)", "floor: code-run path", RULE)
    for d in CODE_RUN_DIRS:
        _add(deny, rows, f"Edit(/{d}/**)", "floor: code-run path", RULE)
    _add(deny, rows, "Edit(/.git/config)", "floor: code-run path", RULE)

    _protect(decl.protect, deny, deny_write, rows, "[protect]")
    _deny_commands(decl.deny_commands, deny, rows, "[[deny_command]]")

    sandbox: dict = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False}
    if decl.network_allow:
        domains: list[str] = []
        for host in decl.network_allow:
            _add(domains, rows, host, "[network] allow", PROXY, prefix="allowedDomains ")
        sandbox["network"] = {"allowedDomains": domains}
    sandbox["filesystem"] = {"denyWrite": deny_write}

    settings = {"permissions": {"deny": deny, "disableBypassPermissionsMode": "disable"}, "sandbox": sandbox}
    return Rendered(settings, rows)


def render_local(local: LocalDeclaration) -> Rendered:
    """Only RH's keys of `.claude/settings.local.json` (schema §6, ruling 2)."""
    rows: list[Row] = []
    deny: list[str] = []
    deny_write: list[str] = []
    _protect(local.protect, deny, deny_write, rows, "local [protect]")
    _deny_commands(local.deny_commands, deny, rows, "local [[deny_command]]")
    return Rendered({"permissions": {"deny": deny}, "sandbox": {"filesystem": {"denyWrite": deny_write}}}, rows)


def _protect(paths: tuple[str, ...], deny: list, deny_write: list, rows: list, source: str) -> None:
    for path in paths:
        if path.startswith("/"):
            edit, sandbox_path = f"Edit(/{path}/**)", path  # Edit(//abs/**) and /abs
        else:
            edit, sandbox_path = f"Edit(/{path}/**)", f"./{path}"
        _add(deny, rows, edit, source, RULE)
        _add(deny_write, rows, sandbox_path, source, OS, prefix="denyWrite ")


def _deny_commands(entries: tuple[DenyCommand, ...], deny: list, rows: list, source: str) -> None:
    for entry in entries:
        _add(deny, rows, f"Bash({entry.pattern})", source, TRIPWIRE, reason=entry.reason)


def _add(target: list, rows: list, value: str, source: str, strength: str, prefix: str = "",
         reason: str | None = None, note: str | None = None) -> None:
    if value in target:
        return
    target.append(value)
    rows.append(Row(source, prefix + value, strength, reason, note))
