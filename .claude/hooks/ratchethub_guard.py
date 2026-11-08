#!/usr/bin/env python3
"""RatchetHub's hooks (v1 scope A3–A5). One standalone file, standard library only.

Generate copies this file to `.claude/hooks/ratchethub_guard.py`, under the floor's
self-protection, and wires it into `.claude/settings.json`:

    outbound [--no-remotes]   PreToolUse, Bash: refuses publishing forms (A3)
    gh                        PreToolUse, Bash: the `gh` invocation-form allowlist (A4)
    relay                     Stop: the driver's handoff line reaches the person unedited (A5)

**All three are tripwires (charter C6).** They read the command's text in command position:
the shell is tokenized here, so a quoted argument, a comment or a heredoc fed to a non-shell is
data, never a command. They do not catch a command they can't see:

- `curl`, `wget` or any HTTP client talking to GitHub's API (only `uploads.github.com` is named);
- the agent's web fetch tool, which is not Bash;
- a script file, a git alias, a Makefile target or any program that pushes on the agent's behalf;
- a command built at run time from variables (`$CMD`), which only the shell expands.

Fail closed: a command the tokenizer can't read is refused, and so is a payload the hook can't
read. A refusal names its reason so the agent can rewrite the command plainly.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

# --- the shell tokenizer ---------------------------------------------------------------------------


class ParseError(Exception):
    pass


SEPARATORS = {";", "&", "&&", "||", "|", "|&", "\n", "(", ")"}
REDIRECTS = {"<", ">", ">>", "<<", "<<-", "<<<", ">&", "<&", "&>", "&>>", ">|", "<>"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
RESERVED = {"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "time"}
# Prefix commands that run their arguments as a command. Value-taking flags are skipped with them.
WRAPPERS = {
    "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
    "sudo": {"-u", "--user", "-g", "--group", "-C", "-D", "--chdir", "-h", "--host", "-p", "--prompt", "-r", "-t"},
    "command": set(), "builtin": set(), "exec": {"-a"}, "nohup": set(), "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"}, "stdbuf": {"-i", "-o", "-e"}, "xargs": {
        "-a", "--arg-file", "-d", "--delimiter", "-E", "-e", "-I", "-i", "-L", "-l", "-n", "--max-args",
        "-P", "--max-procs", "-s", "--max-chars"},
    "doas": {"-u", "-C"}, "chronic": set(), "ionice": {"-c", "-n", "-p"}, "setsid": set(), "unbuffer": set(),
}


class _Lexer:
    def __init__(self, text: str):
        self.s, self.i = text, 0
        self.tokens: list[tuple[str, str]] = []  # (kind, value): word | op
        self.subs: list[str] = []  # command substitutions found anywhere, to parse in turn
        self.heredocs: list[tuple[int, str, bool]] = []  # (token index of the delimiter, word, strip tabs)
        self.bodies: dict[int, str] = {}

    def peek(self, n: int = 0) -> str:
        j = self.i + n
        return self.s[j] if j < len(self.s) else ""

    def run(self):
        while self.i < len(self.s):
            c = self.peek()
            if c in " \t":
                self.i += 1
            elif c == "\\" and self.peek(1) == "\n":
                self.i += 2
            elif c == "\n":
                self.tokens.append(("op", "\n"))
                self.i += 1
                self._read_heredoc_bodies()
            elif c == "#":
                while self.i < len(self.s) and self.peek() != "\n":
                    self.i += 1
            elif c in ";&|()<>":
                self._operator()
            else:
                self.tokens.append(("word", self._word()))
        if self.heredocs:
            raise ParseError("a heredoc is not terminated")
        return self

    def _operator(self):
        for op in ("&>>", "<<-", "<<<", "&&", "||", "|&", ";;", ">>", "<<", ">&", "<&", "&>", ">|", "<>"):
            if self.s.startswith(op, self.i):
                break
        else:
            op = self.peek()
        if op in ("<", ">") and self.peek(1) == "(":  # process substitution
            self.i += 1
            self.subs.append(self._balanced())
            self.tokens.append(("word", "<(...)"))
            return
        self.i += len(op)
        if op == ";;":
            op = ";"
        self.tokens.append(("op", op))
        if op in ("<<", "<<-"):
            while self.peek() in " \t":
                self.i += 1
            word = self._word()
            if not word:
                raise ParseError("a heredoc has no delimiter")
            self.tokens.append(("word", word))
            self.heredocs.append((len(self.tokens) - 1, word, op == "<<-"))

    def _read_heredoc_bodies(self):
        for index, word, strip in self.heredocs:
            lines = []
            while True:
                if self.i >= len(self.s):
                    raise ParseError(f"the heredoc {word!r} is not terminated")
                end = self.s.find("\n", self.i)
                end = len(self.s) if end == -1 else end
                line = self.s[self.i:end]
                self.i = end + 1
                if (line.lstrip("\t") if strip else line) == word:
                    break
                lines.append(line)
            self.bodies[index] = "\n".join(lines)
        self.heredocs = []

    def _balanced(self) -> str:
        """At an opening `(`: return the text up to its matching `)`, quotes respected."""
        assert self.peek() == "("
        depth, start = 0, self.i + 1
        while self.i < len(self.s):
            c = self.peek()
            if c == "\\":
                self.i += 2
                continue
            if c in "'\"":
                self._skip_quote(c)
                continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start:self.i - 1]
            self.i += 1
        raise ParseError("a parenthesis is not closed")

    def _skip_quote(self, q: str):
        self.i += 1
        while self.i < len(self.s) and self.peek() != q:
            self.i += 2 if (q == '"' and self.peek() == "\\") else 1
        if self.i >= len(self.s):
            raise ParseError(f"a {q} quote is not closed")
        self.i += 1

    def _backtick(self) -> str:
        self.i += 1
        start = self.i
        while self.i < len(self.s) and self.peek() != "`":
            self.i += 2 if self.peek() == "\\" else 1
        if self.i >= len(self.s):
            raise ParseError("a backtick is not closed")
        body = self.s[start:self.i]
        self.i += 1
        return body.replace("\\`", "`")

    def _word(self) -> str:
        out = []
        while self.i < len(self.s):
            c = self.peek()
            if c in " \t\n;&|()<>":
                break
            if c == "\\":
                out.append(self.peek(1))
                self.i += 2
            elif c == "'":
                end = self.s.find("'", self.i + 1)
                if end == -1:
                    raise ParseError("a ' quote is not closed")
                out.append(self.s[self.i + 1:end])
                self.i = end + 1
            elif c == '"':
                self.i += 1
                while True:
                    if self.i >= len(self.s):
                        raise ParseError('a " quote is not closed')
                    d = self.peek()
                    if d == '"':
                        self.i += 1
                        break
                    if d == "\\" and self.peek(1) in '"\\$`\n':
                        out.append(self.peek(1))
                        self.i += 2
                    elif d == "$" and self.peek(1) == "(":
                        self.i += 1
                        self.subs.append(self._balanced())
                        out.append("$(...)")
                    elif d == "`":
                        self.subs.append(self._backtick())
                        out.append("$(...)")
                    else:
                        out.append(d)
                        self.i += 1
            elif c == "$" and self.peek(1) == "(":
                self.i += 1
                self.subs.append(self._balanced())
                out.append("$(...)")
            elif c == "`":
                self.subs.append(self._backtick())
                out.append("$(...)")
            else:
                out.append(c)
                self.i += 1
        return "".join(out)


def commands(text: str, depth: int = 0) -> list[list[str]]:
    """Every simple command in `text` as its argv in command position, including those inside
    command substitutions, `sh -c '...'`, `eval ...` and a heredoc fed to a shell."""
    if depth > 8:
        raise ParseError("commands nest too deeply to read")
    lex = _Lexer(text).run()
    out: list[list[str]] = []
    current: list[str] = []
    feed: list[str] = []  # heredoc bodies attached to the current command
    skip_next = False
    for index, (kind, value) in enumerate(lex.tokens):
        if kind == "op" and value in SEPARATORS:
            out += _resolve(current, feed, depth)
            current, feed = [], []
            continue
        if kind == "op":  # a redirect: its target is not an argument
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            if index in lex.bodies:
                feed.append(lex.bodies[index])
            continue
        current.append(value)
    out += _resolve(current, feed, depth)
    for sub in lex.subs:
        out += commands(sub, depth + 1)
    return out


def _is_assignment(word: str) -> bool:
    name, eq, _ = word.partition("=")
    return bool(eq) and bool(name) and (name[0].isalpha() or name[0] == "_") and all(
        c.isalnum() or c == "_" for c in name)


def _resolve(argv: list[str], feed: list[str], depth: int) -> list[list[str]]:
    args = list(argv)
    while args:
        head = args[0]
        base = head.rsplit("/", 1)[-1]
        if head in RESERVED or _is_assignment(head):
            args.pop(0)
        elif base in WRAPPERS:
            args.pop(0)
            takes = WRAPPERS[base]
            if base == "timeout":
                while args and args[0].startswith("-"):
                    flag = args.pop(0)
                    if flag in takes and args:
                        args.pop(0)
                if args:
                    args.pop(0)  # the duration
                continue
            while args and (args[0].startswith("-") or (base == "env" and _is_assignment(args[0]))):
                flag = args.pop(0)
                if flag == "--":
                    break
                if flag in takes and args:
                    args.pop(0)
        elif base in SHELLS:
            out = []
            rest = args[1:]
            for k, word in enumerate(rest):
                if word.startswith("-") and not word.startswith("--") and "c" in word[1:] and k + 1 < len(rest):
                    out += commands(rest[k + 1], depth + 1)
                    break
            for body in feed:
                out += commands(body, depth + 1)
            return out + [args]
        elif base == "eval":
            return commands(" ".join(args[1:]), depth + 1)
        else:
            break
    return [args] if args else []


def git_subcommand(argv: list[str]) -> tuple[str, list[str]] | None:
    """For a git invocation: (subcommand, its arguments), past git's own options."""
    if not argv or argv[0].rsplit("/", 1)[-1] != "git":
        return None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return a, argv[i + 1:]
    return None


# --- gh api ------------------------------------------------------------------------------------------

GH_VALUE_FLAGS = {
    "-X", "--method", "-f", "--field", "-F", "--raw-field", "-H", "--header", "-q", "--jq", "-t",
    "--template", "--input", "--hostname", "-p", "--preview", "--cache", "-R", "--repo",
}
PARAM_FLAGS = ("-f", "--field", "-F", "--raw-field", "--input")
READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def gh_positional(args: list[str]) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = "=" not in a and a in GH_VALUE_FLAGS
        else:
            out.append(a)
    return out


def gh_api_call(args: list[str]) -> tuple[str, str | None]:
    """(METHOD, endpoint) for the arguments after `gh api`. A parameter flag without -X makes
    gh send a POST, so it counts as a write (gh manual: "the default HTTP request method is GET
    normally and POST if any parameters were added")."""
    method, params, i = None, False, 0
    while i < len(args):
        a = args[i]
        if a in ("-X", "--method"):
            method = args[i + 1] if i + 1 < len(args) else ""
            i += 2
            continue
        if a.startswith("--method="):
            method = a.split("=", 1)[1]
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:]
        if a.split("=", 1)[0] in PARAM_FLAGS or (a[:2] in ("-f", "-F") and len(a) > 2 and a[1] != "-"):
            params = True
        i += 2 if a in GH_VALUE_FLAGS else 1
    positional = gh_positional(args)
    endpoint = positional[0] if positional else None
    return (method or ("POST" if params else "GET")).upper(), endpoint


def endpoint_path(endpoint: str) -> list[str]:
    path = endpoint.split("?", 1)[0].split("#", 1)[0]
    if "://" in path:
        rest = path.split("://", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
    return [p for p in path.split("/") if p]


def matches(parts: list[str], template: str) -> bool:
    """`{slug}` is owner/repo: two segments, or one where a placeholder stands for both."""
    tparts = [p for p in template.split("/") if p]
    i = j = 0
    while j < len(tparts):
        t = tparts[j]
        if t == "{slug}":
            if i >= len(parts):
                return False
            two = i + 1 < len(parts) and len(parts) - (i + 2) == len(tparts) - (j + 1)
            i += 2 if two else 1
        elif t.startswith("{"):
            if i >= len(parts):
                return False
            i += 1
        else:
            if i >= len(parts) or parts[i] != t:
                return False
            i += 1
        j += 1
    return i == len(parts)


# --- A3: outbound ----------------------------------------------------------------------------------

# Writes that send history or a build out of the repository by REST. Irreversible: a release, a
# tag, a commit made through the contents API, a new repository or a fork.
OUTBOUND_REST = (
    ("POST", "/repos/{slug}/releases"), ("PATCH", "/repos/{slug}/releases/{id}"),
    ("DELETE", "/repos/{slug}/releases/{id}"), ("POST", "/repos/{slug}/git/refs"),
    ("PATCH", "/repos/{slug}/git/refs/{a}/{b}"), ("POST", "/repos/{slug}/git/tags"),
    ("PUT", "/repos/{slug}/contents/{path}"), ("DELETE", "/repos/{slug}/contents/{path}"),
    ("POST", "/user/repos"), ("POST", "/orgs/{org}/repos"), ("POST", "/repos/{slug}/forks"),
    ("POST", "/repos/{slug}/generate"), ("POST", "/gists"),
)
PUBLISHERS = {
    ("npm", "publish"), ("yarn", "publish"), ("pnpm", "publish"), ("twine", "upload"), ("docker", "push"),
    ("podman", "push"), ("cargo", "publish"), ("gem", "push"), ("uv", "publish"), ("poetry", "publish"),
    ("hatch", "publish"), ("flit", "publish"),
}
GH_IRREVERSIBLE = {("repo", "create"), ("repo", "fork"), ("repo", "delete"), ("repo", "rename"),
                   ("repo", "archive"), ("release", "create"), ("release", "edit"), ("release", "upload"),
                   ("release", "delete"), ("gist", "create"), ("gist", "edit")}
EMISSION = os.path.join(".git", "ratchethub", "emitted.json")
PLAN_SUFFIX = ".git/ratchethub/next.sh"


def _contents_match(parts: list[str]) -> bool:
    # repos/o/r/contents/<any path>
    return len(parts) >= 5 and parts[0] == "repos" and parts[3] == "contents"


def outbound_verdict(argv: list[str], no_remotes: bool) -> tuple[str, str] | None:
    """('deny' | 'ask', reason) for one command, or None."""
    base = argv[0].rsplit("/", 1)[-1] if argv else ""
    if any(a.replace("\\", "/").endswith(PLAN_SUFFIX) for a in argv):
        return "deny", ("the handoff file holds a person's step (a merge, a tag, a release); the agent "
                        "relays its line and never runs it")
    git = git_subcommand(argv)
    if git:
        sub, rest = git
        if sub == "push":
            if no_remotes:
                return "deny", "this project declares no remotes ([publish] remotes_allowed = false)"
            if any(a in ("--tags", "--follow-tags", "--mirror", "--all") or "refs/tags/" in a for a in rest):
                return "deny", ("a tag, mirror or all-refs push is irreversible: a published tag is frozen "
                                "by the server and is cached elsewhere at once")
            return "ask", "a branch push sends history off this machine"
        if sub == "remote" and rest and rest[0] in ("add", "set-url", "rename"):
            return "deny", "adding or changing a remote decides where history goes; that is a person's change"
        return None
    if base == "gh":
        pos = gh_positional(argv[1:])
        if len(pos) >= 2 and (pos[0], pos[1]) in GH_IRREVERSIBLE:
            return "deny", f"`gh {pos[0]} {pos[1]}` publishes or creates something that can't be rolled back"
        if pos[:1] == ["repo"] and len(pos) >= 2 and pos[1] == "edit" and any("visibility" in a for a in argv):
            return "deny", "changing a repository's visibility is irreversible once it is public"
        if pos[:1] == ["api"]:
            method, endpoint = gh_api_call(argv[2:] if argv[1:2] == ["api"] else argv[1:])
            if endpoint and method not in READ_METHODS:
                parts = endpoint_path(endpoint)
                if "uploads.github.com" in endpoint or _contents_match(parts) or any(
                        m == method and matches(parts, t) for m, t in OUTBOUND_REST):
                    return "deny", f"`gh api -X {method} {endpoint}` publishes by REST, the same as the subcommand"
        return None
    if any("uploads.github.com" in a for a in argv):
        return "deny", "uploads.github.com receives release assets"
    words = [a for a in argv[1:] if not a.startswith("-")]
    if words and (base, words[0]) in PUBLISHERS:
        return "deny", f"`{base} {words[0]}` publishes a package; that is a person's release step"
    if base in ("python", "python3") and "-m" in argv:
        k = argv.index("-m")
        if argv[k + 1:k + 3] == ["twine", "upload"]:
            return "deny", "`twine upload` publishes a package; that is a person's release step"
    return None


def _current_branch(repo: str) -> str:
    try:
        with open(os.path.join(repo, ".git", "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
    except OSError:
        return ""
    return head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else ""


def emission(project: str) -> dict:
    """The driver's live emission for this project, or {}. Every failure reads as no emission, so
    it can only turn an ask into an allow, never cause a refusal. The agent can write it, so a
    match means "byte-identical to what the driver printed", never an authorisation."""
    try:
        with open(os.path.join(project, EMISSION), encoding="utf-8") as fh:
            rec = json.load(fh)
        if not isinstance(rec, dict) or float(rec.get("expires", 0)) <= time.time():
            return {}
        if rec.get("branch") and rec.get("branch") != _current_branch(project):
            return {}
        return rec
    except (OSError, ValueError, TypeError):
        return {}


def outbound(command: str, project: str, no_remotes: bool) -> tuple[str, str] | None:
    try:
        argvs = commands(command)
    except ParseError as exc:
        return "deny", f"RatchetHub's outbound guard can't read this command ({exc}); write it plainly"
    worst = None
    for argv in argvs:
        v = outbound_verdict(argv, no_remotes)
        if v and (worst is None or v[0] == "deny"):
            worst = v
    if worst and worst[0] == "ask":
        rec = emission(project)
        if rec and rec.get("command") == command:
            return "allow", (f"matched the driver's printed command for step {rec.get('step', '?')!r}; "
                             "a match, not an authorisation")
        if rec:
            return "ask", (f"{worst[1]}. A driver command is live and this is not it.\n"
                           f"  printed: {rec.get('command', '')}\n  you ran: {command}")
    return worst


# --- A4: gh invocation forms -------------------------------------------------------------------------

# Writes the drivers need (G1, G3). GET is unconstrained except GraphQL.
SANCTIONED_WRITES = (
    ("POST", "/repos/{slug}/pulls"), ("PATCH", "/repos/{slug}/pulls/{n}"),
    ("PUT", "/repos/{slug}/pulls/{n}/merge"), ("POST", "/repos/{slug}/releases"),
    ("DELETE", "/repos/{slug}/releases/{id}"), ("POST", "/repos/{slug}/labels"),
    ("POST", "/repos/{slug}/issues"), ("POST", "/repos/{slug}/issues/{n}/comments"),
    ("DELETE", "/repos/{slug}/git/refs/heads/{branch}"),
)
EXCLUDED_WRITES = (  # excluded by decision, so the refusal can say why
    ("PUT", "/repos/{slug}/rulesets/{id}"), ("PATCH", "/repos/{slug}/rulesets/{id}"),
    ("DELETE", "/repos/{slug}/rulesets/{id}"), ("POST", "/repos/{slug}/rulesets"),
)
NO_RELAY = ("Don't hand this command to a person to run instead: the driver uses REST paths, and a "
            "refused form is refused for a person's terminal too.")
REST_HINT = "Use `gh api <REST path>`, e.g. `gh api repos/{owner}/{repo}/pulls`. " + NO_RELAY


def gh_verdict(argv: list[str]) -> str | None:
    if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
        return None
    pos = gh_positional(argv[1:])
    sub = pos[0] if pos else ""
    if sub == "auth" and pos[1:2] == ["status"]:
        return None
    if sub == "api":
        method, endpoint = gh_api_call(argv[2:] if argv[1:2] == ["api"] else argv[1:])
        if endpoint is None:
            return "`gh api` with no endpoint path can't be checked. " + REST_HINT
        parts = endpoint_path(endpoint)
        if parts[:1] == ["graphql"]:
            return ("`gh api graphql` is refused: GraphQL needs authentication for every call and has "
                    "failed silently as a no-op. " + REST_HINT)
        if method in READ_METHODS:
            return None
        if any(m == method and matches(parts, t) for m, t in EXCLUDED_WRITES):
            return (f"`gh api -X {method} {endpoint}` is excluded by decision: a ruleset write replaces "
                    "the whole rule list and can silently drop a protection. Ruleset changes are a "
                    "person's, made from the declared platform state (G6). " + NO_RELAY)
        if any(m == method and matches(parts, t) for m, t in SANCTIONED_WRITES):
            return None
        allowed = "; ".join(f"{m} {t}" for m, t in SANCTIONED_WRITES)
        return f"`gh api -X {method} {endpoint}` is not a sanctioned write. Sanctioned: {allowed}. " + NO_RELAY
    named = f"`gh {sub}`" if sub else "this `gh` form"
    return (f"{named} is refused: only `gh api <REST path>` and `gh auth status` are permitted, and "
            "every other form is refused by default. " + REST_HINT)


def gh(command: str) -> str | None:
    try:
        argvs = commands(command)
    except ParseError as exc:
        return f"RatchetHub's gh guard can't read this command ({exc}); write it plainly"
    for argv in argvs:
        reason = gh_verdict(argv)
        if reason:
            return reason
    return None


# --- A5: relay ----------------------------------------------------------------------------------------

RELAY_DIR = os.path.join(".git", "ratchethub")
RELAY_LINE = os.path.join(RELAY_DIR, "relay-line.txt")
RELAY_MARK = os.path.join(RELAY_DIR, "relay-blocked")
START, END = "START COPY", "END COPY"


def relayed_lines(message: str) -> list[str]:
    """The lines inside START COPY / END COPY blocks, leading whitespace kept (an indented paste
    is mangled, so it must read as a difference)."""
    out, inside = [], False
    for line in message.split("\n"):
        stripped = line.strip()
        if stripped.startswith(START):
            inside = True
        elif stripped.startswith(END):
            inside = False
        elif inside and stripped and not stripped.startswith("```"):
            out.append(line.rstrip())
    return out


def relay(message: str, project: str) -> str | None:
    """A reason to block the agent's stop once, or None. The canonical line is the one the driver
    wrote; the check applies only when a copy block is present in the message."""
    try:
        with open(os.path.join(project, RELAY_LINE), encoding="utf-8") as fh:
            canonical = fh.read().strip("\n")
    except OSError:
        return None
    if not canonical or START not in message:
        return None
    lines = relayed_lines(message)
    mark = os.path.join(project, RELAY_MARK)
    if canonical in lines:
        try:
            os.remove(mark)
        except OSError:
            pass
        return None
    want = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        with open(mark, encoding="utf-8") as fh:
            if fh.read().strip() == want:
                return None  # blocked once for this line already: never loop
    except OSError:
        pass
    try:
        with open(mark, "w", encoding="utf-8") as fh:
            fh.write(want)
    except OSError:
        return None  # can't record the block, so don't risk a loop
    shown = "\n".join(f"  you relayed: {line}" for line in lines) or "  you relayed: (no line)"
    return ("the handoff line you relayed is not the one the driver printed. Copy the driver's "
            "START COPY / END COPY block exactly: don't retype it, drop its step tag or indent it.\n"
            f"  driver printed: {canonical}\n{shown}")


# --- entry point ---------------------------------------------------------------------------------------


def _deny(reason: str, decision: str = "deny") -> None:
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                      "permissionDecisionReason": "RatchetHub: " + reason}}, sys.stdout)
    sys.stdout.write("\n")


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        sys.stderr.write("RatchetHub: the hook payload is not readable JSON; refusing (fail closed)\n")
        return 2
    project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or "."
    if mode in ("outbound", "gh"):
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            sys.stderr.write("RatchetHub: the Bash command is not readable; refusing (fail closed)\n")
            return 2
        if mode == "outbound":
            v = outbound(command, project, "--no-remotes" in argv)
            if v:
                _deny(v[1] + f"\n  command: {command}", v[0])
        else:
            reason = gh(command)
            if reason:
                _deny(reason)
        return 0
    if mode == "relay":
        if payload.get("stop_hook_active"):
            return 0
        message = payload.get("last_assistant_message")
        reason = relay(message, project) if isinstance(message, str) else None
        if reason:
            sys.stderr.write("RatchetHub relay guard: " + reason + "\n")
            return 2
        return 0
    sys.stderr.write(f"RatchetHub: unknown hook mode {mode!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
