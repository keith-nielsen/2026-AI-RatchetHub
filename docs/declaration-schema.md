# Declaration schema: `ratchethub.toml`

> **Status:** draft, 2026-10-24, schema version 1. Implements charter C4 (extended by declaration)
> and C5 (only tightens).

## 1. What the declaration is

A project plugs into RatchetHub (RH) by placing a declaration at its root. The declaration states
**facts** about the project: what to protect, which commands are exempt, what network the agent may
use. It contains no code. RH's generator reads it and writes the runtime's settings files.

| File | Holds | Committed | Generated into |
| --- | --- | --- | --- |
| `ratchethub.toml` | facts true on every machine; all paths **relative to the project root** | yes | `.claude/settings.json` |
| `ratchethub.local.toml` | facts about one machine; **absolute** paths allowed only here | **no**, must be ignored by git | `.claude/settings.local.json` |

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
| self-protection | `.claude/`, `ratchethub.toml`, `ratchethub.local.toml` | C2: the agent can't edit its own leash |

Self-protection is written both ways: `sandbox.filesystem.denyWrite` stops Bash, and a
`permissions.deny` `Edit(...)` rule stops the file tools. The sandbox covers shell commands only;
the file tools follow permission rules instead.

## 3. Sections

### `[ratchethub]` (required)

| Field | Type | Rule |
| --- | --- | --- |
| `schema` | integer | must be `1`; the generator refuses any version it doesn't know |

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
- **Must exist** when generating, and must not pass through a symbolic link.

### `[[exempt]]`: commands that run outside the sandbox

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `command` | string, a command pattern | `sandbox.excludedCommands` | **removes the sandbox entirely** for a matching call |
| `reason` | string, required | RH's report | — |

An exempt command runs with the full access of the person running the session. The runtime can't
limit it to one folder. Matching is on the command's text: a call that adds `cd`, a redirect or a
command substitution stays sandboxed. Exemptions are allowed only in `ratchethub.toml`, where they
are reviewed with the project, never in the local file.

### `[network]`: hosts sandboxed commands may reach

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `allow` | array of host names | `sandbox.network.allowedDomains` | enforced by the sandbox proxy, by host name only |

Empty by default. The proxy doesn't inspect encrypted traffic, so an allowed host is trusted with
whatever the agent sends it. Allowed only in `ratchethub.toml`.

### `[[deny_command]]`: command forms the agent must not run

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `pattern` | string, a command pattern | `permissions.deny` `Bash(<pattern>)` | **tripwire only**: matches text |
| `reason` | string, required | RH's report | — |

A command written another way isn't matched (for example `git -C . push` against `git push *`).
RH reports each pattern as a tripwire, never as a barrier (C6).

### `[publish]`: where the project may send its history

| Field | Type | Generated into | Strength |
| --- | --- | --- | --- |
| `remotes_allowed` | boolean | RH's outbound hook, not a settings key | tripwire: hooks match command text |

The runtime has no setting for "this project has no remotes". RH enforces it with a hook. Hooks run
outside the sandbox.

## 4. What the local file may contain

`ratchethub.local.toml` may only **add** restrictions, because the runtime ranks
`settings.local.json` above `settings.json` and a loosening there would override the committed
controls. It may contain `[protect]` (absolute paths allowed) and `[[deny_command]]` only.
The generator refuses any other section in it.

## 5. Example

A project that protects its raw data and its CI workflows, and lets one loader script run outside
the sandbox:

```toml
# ratchethub.toml — declared once; RatchetHub generates .claude/settings.json from it.

[ratchethub]
schema = 1

[project]
name = "example-analytics"

[protect]
folders = ["data/raw", ".github/workflows"]

[[exempt]]
command = "python scripts/load_orders.py *"
reason = "opens a database connection the sandbox proxy can't carry"

[network]
allow = ["api.github.com", "github.com"]

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
   it checks the effective settings (charter §3, step 3).
3. **Sessions that start outside the project. Ruled: out of v1, a stated gap.** A project's
   settings apply only to sessions started in it. Protecting it from sessions started elsewhere
   would mean writing to the user-level settings file, which every project shares. A project that
   needs more lists the sibling folders in its local declaration.
4. **Format. Ruled: TOML.** JSON allows no comments, and a declaration needs to say why. YAML
   depends on indentation and guesses types (an unquoted `no` can read as `false`). TOML has
   neither problem and Python reads it without a package.
