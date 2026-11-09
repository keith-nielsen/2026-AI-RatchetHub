"""Declared platform state and its reconciler (v1 scope G6). Read-only and anonymous: it compares
content, never timestamps, and reports drift. It never fixes.

A ruleset change is a person's (the `gh` guard refuses ruleset writes to the agent). The one write
helper here prepares it the only safe way: fetch the whole ruleset, change one field, send the
whole thing back, because a ruleset write replaces the entire rule list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..declaration import GitHub
from .read import Reader, ReadError

OK, DRIFT, UNMEASURED = "OK", "DRIFT", "UNMEASURED"
# Fields GitHub adds or changes on its own; comparing them would report drift that isn't.
VOLATILE = {"id", "node_id", "created_at", "updated_at", "_links", "source", "source_type",
            "current_user_can_bypass", "url", "html_url"}


@dataclass
class Row:
    item: str
    result: str
    detail: str
    channel: str = ""


# Fields GitHub leaves out of a read that lacks the right access. Absent from the read, they are
# neither a match nor drift: REST "Get a repository ruleset" returns bypass_actors only to a caller
# with write access to the ruleset, and RH reads anonymously.
HIDDEN = {"bypass_actors": "GitHub returns bypass_actors only to a caller with write access to the "
                           "ruleset (REST: Get a repository ruleset); this read can't see it"}


def ruleset_content(obj: dict) -> dict:
    """`content`, with the rule list in a fixed order: the API description promises no order."""
    c = content(obj)
    if isinstance(c.get("rules"), list):
        c["rules"] = sorted(c["rules"], key=lambda r: json.dumps(r, sort_keys=True))
    return c


def content(obj):
    if isinstance(obj, dict):
        return {k: content(v) for k, v in sorted(obj.items()) if k not in VOLATILE}
    if isinstance(obj, list):
        return [content(v) for v in obj]
    return obj


def reconcile(reader: Reader, slug: str, gh: GitHub, root: Path) -> list[Row]:
    rows: list[Row] = []
    rows += _rulesets(reader, slug, gh, Path(root))
    rows += _required_checks(reader, slug, gh)
    rows += _labels(reader, slug, gh)
    rows += _security(reader, slug, gh)
    rows += _stale_branches(reader, slug, gh)
    rows += _tags_and_releases(reader, slug)
    return rows


def _rulesets(reader, slug, gh, root) -> list[Row]:
    try:
        listing, ch = reader.get(f"/repos/{slug}/rulesets?includes_parents=false")
    except ReadError as exc:
        return [Row("rulesets", UNMEASURED, str(exc)[:160])]
    live = {}
    for item in listing if isinstance(listing, list) else []:
        try:
            full, _ = reader.get(f"/repos/{slug}/rulesets/{item['id']}")
            live[full.get("name")] = full
        except (ReadError, KeyError) as exc:
            return [Row("rulesets", UNMEASURED, f"ruleset {item.get('name')}: {exc}"[:160], ch)]
    rows = []
    declared_names = set()
    for rel in gh.rulesets:
        want = json.loads((root / rel).read_text())
        name = want.get("name")
        declared_names.add(name)
        have = live.get(name)
        if have is None:
            rows.append(Row(f"ruleset {name}", DRIFT, f"declared in {rel}, absent on GitHub", ch))
        else:
            h, w = ruleset_content(have), ruleset_content(want)
            unseen = [k for k in HIDDEN if k in w and k not in h]
            for k in unseen:
                del w[k]
            diff = sorted(k for k in set(h) | set(w) if h.get(k) != w.get(k))
            if diff:
                rows.append(Row(f"ruleset {name}", DRIFT, f"differs from {rel} in: {', '.join(diff)}", ch))
            else:
                rows.append(Row(f"ruleset {name}", OK, f"matches {rel}"
                                + (f" (not seen: {', '.join(unseen)})" if unseen else ""), ch))
            rows += [Row(f"ruleset {name} {k}", UNMEASURED, HIDDEN[k], ch) for k in unseen]
    for name in sorted(set(live) - declared_names):
        rows.append(Row(f"ruleset {name}", DRIFT, "on GitHub, not declared", ch))
    return rows


def _required_checks(reader, slug, gh) -> list[Row]:
    try:
        rules, ch = reader.get(f"/repos/{slug}/rules/branches/{gh.default_branch}")
    except ReadError as exc:
        return [Row("required checks", UNMEASURED, str(exc)[:160])]
    live = set()
    for rule in rules if isinstance(rules, list) else []:
        if rule.get("type") == "required_status_checks":
            live |= {c.get("context") for c in rule.get("parameters", {}).get("required_status_checks", [])}
    want = set(gh.required_checks)
    if live == want:
        return [Row("required checks", OK, ", ".join(sorted(want)) or "none", ch)]
    return [Row("required checks", DRIFT, f"missing: {sorted(want - live)}; undeclared: {sorted(live - want)}", ch)]


def _labels(reader, slug, gh) -> list[Row]:
    try:
        live, ch = reader.get(f"/repos/{slug}/labels?per_page=100")
    except ReadError as exc:
        return [Row("labels", UNMEASURED, str(exc)[:160])]
    have = {lab.get("name"): lab for lab in live if isinstance(lab, dict)}
    rows = []
    for lab in gh.labels:
        got = have.get(lab.name)
        if got is None:
            rows.append(Row(f"label {lab.name}", DRIFT, "declared, absent on GitHub", ch))
        elif (got.get("color", "").lower(), got.get("description") or "") != (lab.color, lab.description):
            rows.append(Row(f"label {lab.name}", DRIFT, f"GitHub has color {got.get('color')}, "
                            f"description {got.get('description')!r}", ch))
        else:
            rows.append(Row(f"label {lab.name}", OK, "matches", ch))
    return rows


def _security(reader, slug, gh) -> list[Row]:
    wanted = {"secret_scanning": gh.secret_scanning, "push_protection": gh.push_protection}
    if all(v is None for v in wanted.values()):
        return []
    try:
        repo, ch = reader.get(f"/repos/{slug}")
    except ReadError as exc:
        return [Row("security", UNMEASURED, str(exc)[:160])]
    sa = repo.get("security_and_analysis") if isinstance(repo, dict) else None
    rows = []
    for key, field_ in (("secret_scanning", "secret_scanning"), ("push_protection", "secret_scanning_push_protection")):
        if wanted[key] is None:
            continue
        status = ((sa or {}).get(field_) or {}).get("status")
        if status is None:
            rows.append(Row(key, UNMEASURED, "GitHub shows this only to an admin; an anonymous read can't see "
                            "it, and coverage depends on repository type and plan", ch))
        elif (status == "enabled") == wanted[key]:
            rows.append(Row(key, OK, status, ch))
        else:
            rows.append(Row(key, DRIFT, f"GitHub has {status}, declared {'enabled' if wanted[key] else 'disabled'}", ch))
    return rows


def _stale_branches(reader, slug, gh) -> list[Row]:
    try:
        branches, ch = reader.branches(slug)
    except ReadError as exc:
        return [Row("stale branches", UNMEASURED, str(exc)[:160])]
    stale = []
    for b in branches if isinstance(branches, list) else []:
        name = b.get("name")
        if name == gh.default_branch:
            continue
        try:
            pulls, _ = reader.pulls_for_branch(slug, name)
        except ReadError:
            continue
        if any(p.get("merged_at") for p in pulls) and not any(p.get("state") == "open" for p in pulls):
            stale.append(name)
    return [Row("stale merged branches", DRIFT if stale else OK, ", ".join(stale) or "none", ch)]


def _tags_and_releases(reader, slug) -> list[Row]:
    try:
        tags, ch = reader.get(f"/repos/{slug}/tags?per_page=100")
        releases, _ = reader.get(f"/repos/{slug}/releases?per_page=100")
    except ReadError as exc:
        return [Row("tags and releases", UNMEASURED, str(exc)[:160])]
    tag_names = {t.get("name") for t in tags}
    rel_tags = {r.get("tag_name") for r in releases if not r.get("draft")}
    missing_release, missing_tag = sorted(tag_names - rel_tags), sorted(rel_tags - tag_names)
    if not missing_release and not missing_tag:
        return [Row("tags and releases", OK, f"{len(tag_names)} tags, each with a release", ch)]
    return [Row("tags and releases", DRIFT, f"tags without a release: {missing_release}; "
                f"releases without a tag: {missing_tag}", ch)]


def ruleset_change(reader: Reader, slug: str, ruleset_id: int, field_path: str, value) -> dict:
    """The whole ruleset with one field changed, ready for a person's `gh api -X PUT --input`.
    Refuses a path that doesn't exist, so a typo can't add a field the API then ignores."""
    full, _ = reader.get(f"/repos/{slug}/rulesets/{ruleset_id}")
    payload = {k: v for k, v in full.items() if k not in VOLATILE}
    node = payload
    *parents, last = field_path.split(".")
    for part in parents:
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"{field_path}: {part} is not in the ruleset")
        node = node[part]
    if not isinstance(node, dict) or last not in node:
        raise KeyError(f"{field_path}: {last} is not in the ruleset")
    node[last] = value
    return payload
