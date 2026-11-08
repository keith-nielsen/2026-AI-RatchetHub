"""Load and validate a project's declaration (docs/declaration-schema.md, schema version 1).

Validation is all-or-nothing: every error is collected and raised together, and nothing is
returned unless the whole declaration is valid. Paths are checked against the filesystem
(existence, symbolic links), so validation needs the project root.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
COMMITTED_NAME = "ratchethub.toml"
LOCAL_NAME = "ratchethub.local.toml"

COMMITTED_SECTIONS = {"ratchethub", "project", "protect", "network", "deny_command", "publish"}
LOCAL_SECTIONS = {"protect", "deny_command"}
# Runtime settings keys. Setting them by declaration would bypass the generator's floor (C5).
FLOOR_SECTIONS = {"sandbox", "permissions", "hooks"}
# Planned for later schema versions (docs/change-protocol.md §5, docs/v1-scope.md §5).
LATER_SECTIONS = {"exempt", "exception", "writable"}

SECTION_KEYS = {
    "ratchethub": {"schema"},
    "project": {"name"},
    "protect": {"folders"},
    "network": {"allow"},
    "deny_command": {"pattern", "reason"},
    "publish": {"remotes_allowed"},
}

WILDCARDS = set("*?[")
# Characters the runtime's rule syntax gives meaning to: a path holding one can't be written
# safely inside Edit(...). Stricter than the schema text, so it only ever refuses more.
RULE_SPECIAL = set("()\\")
LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class DeclarationError(Exception):
    def __init__(self, source: str, errors: list[str]):
        self.source = source
        self.errors = errors
        super().__init__(f"{source}: " + "; ".join(errors))


@dataclass(frozen=True)
class DenyCommand:
    pattern: str
    reason: str


@dataclass(frozen=True)
class Declaration:
    """A validated `ratchethub.toml`. `protect` holds normalized project-relative paths."""

    name: str
    protect: tuple[str, ...]
    network_allow: tuple[str, ...]
    deny_commands: tuple[DenyCommand, ...]
    remotes_allowed: bool | None


@dataclass(frozen=True)
class LocalDeclaration:
    """A validated `ratchethub.local.toml`. `protect` holds absolute or project-relative paths."""

    protect: tuple[str, ...]
    deny_commands: tuple[DenyCommand, ...]


def load_committed(root: Path) -> Declaration:
    path = Path(root) / COMMITTED_NAME
    if not path.is_file():
        raise DeclarationError(COMMITTED_NAME, [f"{COMMITTED_NAME} not found in {root}"])
    data, errors = _parse(path)
    if data is None:
        raise DeclarationError(COMMITTED_NAME, errors)

    _check_sections(data, COMMITTED_SECTIONS, "committed declaration", errors)

    header = data.get("ratchethub")
    if not isinstance(header, dict):
        errors.append("[ratchethub] is required")
    else:
        version = header.get("schema")
        if type(version) is not int or version != SCHEMA_VERSION:
            errors.append(f"[ratchethub] schema must be the integer {SCHEMA_VERSION}, got {version!r}")

    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name.strip():
        errors.append("[project] name is required and must be a non-empty string")

    protect = _folders(data, Path(root), allow_absolute=False, errors=errors)
    network = _network(data, errors)
    deny = _deny_commands(data, errors)

    remotes = None
    publish = data.get("publish")
    if isinstance(publish, dict) and "remotes_allowed" in publish:
        remotes = publish["remotes_allowed"]
        if not isinstance(remotes, bool):
            errors.append("[publish] remotes_allowed must be a boolean")

    if errors:
        raise DeclarationError(COMMITTED_NAME, errors)
    return Declaration(name, protect, network, deny, remotes)


def load_local(root: Path) -> LocalDeclaration:
    """A missing local file is an empty local declaration. Whether git ignores it is Generate's check."""
    path = Path(root) / LOCAL_NAME
    if not path.exists():
        return LocalDeclaration((), ())
    data, errors = _parse(path)
    if data is None:
        raise DeclarationError(LOCAL_NAME, errors)
    _check_sections(data, LOCAL_SECTIONS, "local declaration", errors)
    protect = _folders(data, Path(root), allow_absolute=True, errors=errors)
    deny = _deny_commands(data, errors)
    if errors:
        raise DeclarationError(LOCAL_NAME, errors)
    return LocalDeclaration(protect, deny)


# --- helpers -------------------------------------------------------------------------------------


def _parse(path: Path) -> tuple[dict | None, list[str]]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), []
    except tomllib.TOMLDecodeError as exc:
        return None, [f"{path.name} is not valid TOML: {exc}"]


def _check_sections(data: dict, allowed: set[str], kind: str, errors: list[str]) -> None:
    for section, value in data.items():
        if section in FLOOR_SECTIONS:
            errors.append(f"[{section}] sets runtime settings directly; the floor is not declarable (C5)")
        elif section in LATER_SECTIONS:
            errors.append(f"[{section}] is not in schema version {SCHEMA_VERSION}")
        elif section not in COMMITTED_SECTIONS:
            errors.append(f"unknown section [{section}]")
        elif section not in allowed:
            errors.append(f"section [{section}] is not allowed in a {kind} (schema §4)")
        else:
            tables = value if section == "deny_command" and isinstance(value, list) else [value]
            for table in tables:
                if isinstance(table, dict):
                    for key in table:
                        if key not in SECTION_KEYS[section]:
                            errors.append(f"unknown key {key!r} in [{section}]")


def _folders(data: dict, root: Path, allow_absolute: bool, errors: list[str]) -> tuple[str, ...]:
    section = data.get("protect")
    if not isinstance(section, dict) or "folders" not in section:
        return ()
    folders = section["folders"]
    if not isinstance(folders, list):
        errors.append("[protect] folders must be an array of paths")
        return ()
    out: list[str] = []
    for raw in folders:
        path = _folder(raw, root, allow_absolute, errors)
        if path is not None and path not in out:
            out.append(path)
    return tuple(out)


def _folder(raw: object, root: Path, allow_absolute: bool, errors: list[str]) -> str | None:
    where = f"[protect] folder {raw!r}"
    if not isinstance(raw, str) or not raw:
        errors.append(f"{where} must be a string")
        return None
    if WILDCARDS & set(raw):
        errors.append(f"{where} contains a wildcard; on Linux the runtime silently skips such a rule")
        return None
    if RULE_SPECIAL & set(raw) or any(ord(c) < 32 for c in raw):
        errors.append(f"{where} contains a character the rule syntax treats specially")
        return None

    absolute = raw.startswith("/")
    if raw.startswith("~") or (absolute and not allow_absolute):
        need = "relative to the project root, or absolute" if allow_absolute else "relative to the project root"
        errors.append(f"{where} must be {need}; absolute paths belong in {LOCAL_NAME}")
        return None

    norm = posixpath.normpath(raw)
    if absolute:
        if norm in ("/", "//"):
            errors.append(f"{where} is the filesystem root")
            return None
        norm = "/" + norm.lstrip("/")
        start, parts = Path("/"), norm.strip("/").split("/")
    else:
        if norm == ".":
            errors.append(f"{where} is the project root itself")
            return None
        if norm == ".." or norm.startswith("../"):
            errors.append(f"{where} is outside the project")
            return None
        start, parts = root, norm.split("/")

    current = start
    for part in parts:
        current = current / part
        if current.is_symlink():
            errors.append(f"{where} passes through a symbolic link at {current}")
            return None
        if not current.exists():
            errors.append(f"{where} does not exist")
            return None
    if not current.is_dir():
        errors.append(f"{where} is not a folder")
        return None
    return norm


def _network(data: dict, errors: list[str]) -> tuple[str, ...]:
    section = data.get("network")
    if not isinstance(section, dict) or "allow" not in section:
        return ()
    hosts = section["allow"]
    if not isinstance(hosts, list):
        errors.append("[network] allow must be an array of host names")
        return ()
    out: list[str] = []
    for raw in hosts:
        host = raw.lower() if isinstance(raw, str) else None
        problem = _host_problem(host)
        if problem:
            errors.append(f"[network] allow entry {raw!r} {problem}")
        elif host not in out:
            out.append(host)
    return tuple(out)


def _host_problem(host: str | None) -> str | None:
    if not host:
        return "must be a non-empty host name"
    try:
        ipaddress.ip_address(host.strip("[]"))
        return "is an IP address; list host names only"
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost"):
        return "is a local address; it would expose every local port"
    labels = host.split(".")
    if len(labels) < 2 or not all(LABEL.match(label) for label in labels):
        return "is not a plain, fully qualified host name (no scheme, port, path or wildcard)"
    if labels[-1].isdigit():
        return "looks like an IP address; list host names only"
    return None


def _deny_commands(data: dict, errors: list[str]) -> tuple[DenyCommand, ...]:
    if "deny_command" not in data:
        return ()
    entries = data["deny_command"]
    if not isinstance(entries, list):
        errors.append("deny_command must be written as [[deny_command]] entries")
        return ()
    out: list[DenyCommand] = []
    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            errors.append(f"[[deny_command]] #{i} must be a table")
            continue
        pattern, reason = entry.get("pattern"), entry.get("reason")
        ok = True
        if not isinstance(pattern, str) or not pattern.strip():
            errors.append(f"[[deny_command]] #{i} pattern is required and must be a non-empty string")
            ok = False
        elif ")" in pattern or any(ord(c) < 32 for c in pattern):
            errors.append(f"[[deny_command]] #{i} pattern contains ')' or a control character")
            ok = False
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"[[deny_command]] #{i} reason is required and must be a non-empty string")
            ok = False
        if ok and pattern not in [d.pattern for d in out]:
            out.append(DenyCommand(pattern, reason))
    return tuple(out)
