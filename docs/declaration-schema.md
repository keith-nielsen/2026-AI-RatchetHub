# Declaration schema: `ratchethub.toml`

> **Status:** schema version 1 signed off by the operator, 2026-10-24. **Schema version 2 drafted
> 2026-11-08, awaiting the operator's sign-off** (v1 scope §5, decision 1: `[writable]` only). Version 2
> also brings this text level with the generator, which already refused more than version 1 said
> (§7). Implements charter C4 (extended by declaration) and C5 (only tightens).

## 1. What the declaration is

A project plugs into RatchetHub (RH) by placing a declaration at its root. The declaration states
**facts** about the project: what to protect, which command forms to refuse, what network the agent
may use. It contains no code. RH's generator reads it and writes the runtime's settings files.

| File | Holds | Committed | Generated into |
| --- | --- | --- | --- |
| `ratchethub.toml` | facts true on every machine; all paths **relative to the project root** | yes | `.claude/settings.json` |
| `ratchethub.local.toml` | facts about one machine; **absolute** paths allowed only here | **no**, must be ignored by git; the generator refuses to run unless `git check-ignore` confirms it, and the same for `.claude/settings.local.json` and `.claude/ratchethub-record.local.json` | `.claude/settings.local.json` |

The format is [TOML](https://toml.io/en/v1.0.0). Python reads it with the standard library
(`tomllib`, Python 3.11 and later), so reading a declaration needs no installed package.

## 2. The floor (not declarable)

The generator always writes these, whatever the declaration says. A declaration that sets any of
them is refused (C5).

| Generated setting | Value | Why |
| --- | --- | --- |
| `sandbox.enabled` | `true` | Bash runs inside the operating system sandbox |
| `sandbox.failIfUnavailable` | `true` | the runtime refuses to start rather than run unsandboxed |
| `sandbox.allowUnsandboxedCommands` | `false` | the agent can't retry a blocked command outside the sandbox |
| `permissions.disableBypassPermissionsMode` | `"disable"` | no one can switch permission checks off. It is a lock: the runtime applies the strictest value any settings file sets |
| self-protection | `.claude/`, `ratchethub.toml`, `ratchethub.local.toml` | C2: the agent can't edit its own leash |
| code-run paths | `.mcp.json`, `.git/hooks`, `.git/config` | each makes the runtime or git run code outside the sandbox |
| `disableAllHooks` | `false` | RH's hooks run. The runtime ranks the project file above the user file for this key, so a user-file `true` doesn't switch them off; a local-file `true` does, and Verify reports it |
| hooks | the outbound guard, the `gh` guard, the relay guard (§3 `[publish]`, §8) | tripwires the runtime can't express as settings. The guard file is copied to `.claude/hooks/ratchethub_guard.py`, under self-protection, and its hash is in the record |

Self-protection is written both ways: `sandbox.filesystem.denyWrite` stops Bash, and a
`permissions.deny` `Edit(...)` rule stops the file tools. The sandbox covers shell commands only;
the file tools follow permission rules instead.

Code-run paths get an `Edit(...)` deny only. The sandbox already denies Bash writes to them, but
for the file tools the runtime only prompts for them, or leaves them to the classifier in auto
mode, so RH denies them outright.

## 3. Sections

### `[ratchethub]` (required)

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | integer | `1` or `2`; the generator refuses any version it doesn't know. `[writable]` needs `2` |

### `[project]` (required)

| Field | Type | Rule |
| --- | --- | --- |
| `name` | string | a label for reports; RH attaches no meaning to it |

The project root is the directory that holds `ratchethub.toml`. It is never declared.

### `[protect]`: folders the agent must not write

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `folders` | array of paths | `sandbox.filesystem.denyWrite` **and** `permissions.deny` `Edit(/<path>/**)` | Bash: enforced by the operating system. File tools: enforced by permission rule |

Path rules, all enforced by the generator:

- **Relative to the project root** in `ratchethub.toml`. Absolute paths are allowed only in
  `ratchethub.local.toml`. A committed declaration must work on every machine.
- **No wildcards** (`*`, `?`, `[`). On Linux the runtime silently skips a write rule that contains
  one, so a wildcard would look like protection and be none.
- **Must exist** when generating, must be a folder, and must not pass through a symbolic link.
- **Not** `~`, the project root itself, the filesystem root, or a path that leaves the project.
- **No character the rule syntax treats specially:** `(`, `)`, `\`, or a control character. Inside
  `Edit(...)` one of these would change what the rule matches.

### `[writable]`: folders sandboxed commands may also write (schema 2)

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `folders` | array of paths, project-relative or starting `~/` | `sandbox.filesystem.allowWrite` | **a loosening**, enforced by the operating system: **any** code the agent runs may write here, not only the tool it was added for |

The remedy for a build or package tool that needs one more folder (protocol §5, "Widen, don't
except"). npm, for example, needs both `~/.npm` here and its registry host in `[network]`. Rules,
all enforced by the generator:

- **Project-relative, or starting `~/`.** No other absolute path, and not the home folder itself.
- **Must not overlap a protected path,** in either direction: `[protect]` (committed or local), or
  the floor's `.claude/`, `ratchethub.toml`, `ratchethub.local.toml`, `.mcp.json` and `.git`.
- **Must not reach a folder whose contents run outside the sandbox** or hold credentials, in either
  direction: `~/.config`, `~/.local/bin`, `~/.claude`, `~/.ssh`, `~/.gnupg`, shell startup files,
  `~/.gitconfig`, `~/.npmrc`, `~/.pypirc`, and any folder on `PATH` when Generate runs. The list can't
  be complete, so the report states every writable folder as "writable by any code the agent runs".
- No wildcards, must exist, must be a folder, no symbolic link, as for `[protect]`.
- Allowed only in `ratchethub.toml`: the local file may only add restrictions (§4).

### Exemptions: not before schema 3

> **Known gap, narrowed by `[writable]`.** A tool that only needs a folder or a host is now served
> by widening. A tool that can't run sandboxed at all (such as docker) still can't, so a project
> that needs one can't adopt RH yet. The generator refuses `[[exempt]]` and `[[exception]]`. The
> standing-exception design is ruled (protocol §5) and ships as schema 3, when a project needs it
> (v1 scope §3). It meets three facts:
>
> - The runtime's `sandbox.excludedCommands` **removes the sandbox entirely** for a matching call.
>   The command runs with the full access of the person running the session, and the runtime can't
>   limit it to one folder.
> - The agent could rewrite whatever an exempt command runs (a script, a config file it reads), then
>   run it with that access. Everything it runs must be protected.
> - Matching is on the command's text. A call that adds `cd`, a redirect or a command substitution
>   stays sandboxed, but a trailing `*` passes any arguments the agent chooses.

### `[network]`: hosts sandboxed commands may reach

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `allow` | array of host names | `sandbox.network.allowedDomains` | listed hosts: enforced by the sandbox proxy, by host name only. Unlisted hosts: decided by permission mode |

Each entry must be a plain, fully qualified host name: at least two labels, no scheme, port, path
or wildcard, and a last label that isn't all digits. The generator lowercases it.

Empty by default, and **empty doesn't mean no network.** A host outside the list is asked about in
the default mode, denied in `dontAsk` mode, checked per command in auto mode, and allowed in
`bypassPermissions` mode (which the floor disables). Denying unlisted hosts outright needs
`sandbox.network.strictAllowlist`, which only user or managed settings can set; a repository can't.

The proxy doesn't inspect encrypted traffic, so an allowed host is trusted with whatever the agent
sends it. Don't list `localhost` or an IP address: the proxy refuses names that resolve to local
addresses unless an IP address is listed, and a `localhost` entry exposes every local port to
commands that use the proxy. Allowed only in `ratchethub.toml`.

### `[[deny_command]]`: command forms the agent must not run

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `pattern` | string, a command pattern | `permissions.deny` `Bash(<pattern>)` | **tripwire only**: matches text |
| `reason` | string, required | RH's report | — |

A command written another way isn't matched (for example `git -C . push` against `git push *`).
RH reports each pattern as a tripwire, never as a barrier (C6). A pattern may not contain `)` or a
control character, which would end or break the generated `Bash(...)` rule.

### `[publish]`: where the project may send its history

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `remotes_allowed` | boolean | RH's outbound hook (`--no-remotes` when `false`), not a settings key | tripwire: the hook reads the command in command position |

The runtime has no setting for "this project has no remotes", so RH's outbound hook carries it.
Hooks run outside the sandbox. **The hook is generated whether or not `[publish]` is declared:**
it always refuses the irreversible forms (a tag, mirror or all-refs push, adding or changing a
remote, creating, forking or releasing a repository, a package publish, and their REST
equivalents through `gh api`). `remotes_allowed = false` also refuses a branch push; otherwise a
branch push asks. **It doesn't catch** `curl` or `wget` to GitHub's API, the agent's web fetch tool,
a script, a git alias, or a command assembled from variables at run time.

## 4. What the local file may contain

`ratchethub.local.toml` may only **add** restrictions, because the runtime ranks
`settings.local.json` above `settings.json` and a loosening there would override the committed
controls. It may contain `[protect]` (absolute paths allowed) and `[[deny_command]]` only.
The generator refuses any other section in it.

## 5. Example

A project that protects its raw data and its CI workflows:

```toml
# ratchethub.toml — declared once; RatchetHub generates .claude/settings.json from it.

[ratchethub]
schema = 2

[project]
name = "example-analytics"

[protect]
folders = ["data/raw", ".github/workflows"]

[network]
allow = ["api.github.com", "github.com", "registry.npmjs.org"]

[writable]
folders = ["~/.npm"]      # with registry.npmjs.org in [network], so npm can install

[[deny_command]]
pattern = "gh api graphql *"
reason = "GraphQL needs credentials this agent must not hold"

[publish]
remotes_allowed = true
```

The same person's machine-only additions, never committed:

```toml
# ratchethub.local.toml — this machine only.

[protect]
folders = ["/srv/shared-datasets"]
```

## 6. Decisions

**All four ruled by the operator on 2026-10-24**, each as recommended.

1. **Who owns `.claude/settings.json`. Ruled: RH owns the whole file** and regenerates it. If the
   file on disk differs from RH's last output, the generator stops and reports drift. It never
   merges by hand and never overwrites silently. Anything a project wants in that file is declared.
2. **Who owns `.claude/settings.local.json`. Ruled: RH owns only the keys it generates**
   (`permissions.deny` and `sandbox.filesystem.denyWrite`) and leaves the rest. The runtime also
   writes this file: a "don't ask again" approval saves an allow rule there, and the `/sandbox`
   panel saves its choices there. Verification catches the panel switching the floor off, because
   it checks the effective settings (charter §3, step 3). This is a **detective** control: it
   catches the change when verification runs, and between runs the change goes unnoticed.
3. **Sessions that start outside the project. Ruled: out of v1, a stated gap.** A project's
   settings apply only to sessions started in it. Protecting it from sessions started elsewhere
   would mean writing to the user-level settings file, which every project shares. A project that
   needs more lists the sibling folders in its local declaration.
4. **Format. Ruled: TOML.** JSON allows no comments, and a declaration needs to say why. YAML
   depends on indentation and guesses types (an unquoted `no` can read as `false`). TOML has
   neither problem and Python reads it without a package.

## 7. What schema version 2 changes

**Drafted 2026-11-08, awaiting the operator's sign-off.** One addition and a catch-up:

1. **Added: `[writable]`** (§3), as ruled in v1 scope §5, decision 1. `[[exception]]` moves to
   schema 3.
2. **Stated: the hooks and `disableAllHooks` are part of the floor** (§2), and `[publish]` is now
   enforced (the generator refused `remotes_allowed = false` until the hook existed).
3. **Stated: refusals the generator already made** but version 1 didn't list: the rule-special
   path characters, `~` and the project root as `[protect]` folders, non-FQDN hosts, `)` in a
   command pattern, and the two further local files that must be ignored by git (§1). Each only
   refuses more, so no version 1 declaration that was valid becomes invalid.

A version 1 declaration still loads, unchanged.
