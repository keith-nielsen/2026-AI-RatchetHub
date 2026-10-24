# The change protocol

> **Status:** draft, awaiting operator review. Details charter §3 (declare → generate → verify →
> approve), adds standing exceptions (closing the gap in charter §4), and records where the floor
> lives. Runtime facts come from the Claude Code documentation fetched 2026-10-24, read against
> Claude Code v2.1.310. A fact marked **unprobed** is documented but not yet shown by a live probe,
> so RatchetHub (RH) can't report it as enforced (charter C6).

## 1. What the protocol governs

Every change to a project's controls takes one route, and there is no other. The route governs
these files:

| File | Written by | Committed |
| --- | --- | --- |
| `ratchethub.toml` | a person (Declare) | yes |
| `ratchethub.local.toml` | a person (Declare) | no |
| `.claude/settings.json` | RH (Generate), whole file | yes |
| `.claude/settings.local.json` | RH (Generate), its own keys only (schema §6, ruling 2) | no |
| `.claude/ratchethub-record.json` | RH (each step), for the committed half | yes |
| `.claude/ratchethub-record.local.json` | RH (each step), for the local half | no |

All six sit under the floor's self-protection (schema §2), so the agent can write none of them,
by Bash or by the file tools.

## 2. Who runs each step

| Step | Run by | Writes | Refuses when |
| --- | --- | --- | --- |
| Declare | a person | the declaration | (no tool; Generate validates) |
| Generate | a person | the generated files, the record | the declaration fails validation; a generated file differs from the record (drift); `ratchethub.local.toml` isn't ignored by git |
| Verify | a person or the agent (static part); a person (live part) | the record's verify entry | the generated files differ from the record; the record has no generate entry for the current declaration |
| Approve | a person | the record's approval entry | the record has no passing verify entry for the files now on disk |

**The agent can't run Generate or Approve, by construction:** both write under `.claude/`, which
the floor denies to the agent in both channels. The agent's part in a change is to **propose** it:
it writes the declaration change as text (a diff in its reply or in an unprotected file), and a
person applies it. RH provides no route for the agent to apply a proposal. (Probed for the record
file in both channels; see §8.)

The live part of Verify is person-run because it launches headless sessions and writes probe files
in each protected location, which can lie outside the project (local declarations allow absolute
paths).

## 3. The record

Each step reads the record, checks it against the files on disk, refuses on any mismatch, and
appends its own entry. The record holds:

- the schema version, RH's version, and the Claude Code version the step ran against;
- Generate: the SHA-256 of the declaration it read and of each file it wrote. For
  `.claude/settings.local.json` the hash covers only RH's keys (`permissions.deny`,
  `sandbox.filesystem.denyWrite`), because the runtime itself writes the rest of that file;
- Verify: those same hashes, each check's result, and for live checks the baseline it was compared
  against;
- Approve: who approved, when, and the hash of the verify entry approved.

**The record protects against the agent and against mistakes, not against a person who means to
forge it.** It is a plain file that a person can edit, and that is within charter C2's scope ("casual
mistakes"). A signed record is out of v1.

The committed half lives in git, so the approval for a change is committed with the change.

## 4. The steps in detail

### Declare

A person edits `ratchethub.toml` or `ratchethub.local.toml`. Nothing is checked yet.

### Generate

1. Refuses unless `git check-ignore` confirms `ratchethub.local.toml` is ignored (schema §1).
2. Validates the declaration against the schema. Refuses on any error, and refuses any attempt to
   set the floor (C5).
3. Compares each generated file on disk with the hash in the record. **Any difference is drift and
   Generate stops.** It never merges and never overwrites. The person resolves the drift: either the
   hand edit was wanted, so they move it into the declaration and restore the file from git, or it
   wasn't, so they just restore the file. Then they run Generate again. RH never discards a change on
   its own (C3).
4. Writes the generated files and the record's generate entry.

A first run has no record. Generate then refuses to overwrite an existing `.claude/settings.json`
unless the person names it as adopted (an explicit flag recorded in the record). RH never silently
takes over a file it didn't write.

### Verify

**Static checks** read the **effective** settings: every file the runtime merges (user, project,
local, and managed if present), not only RH's own. They confirm the floor holds in the merged result,
each declared row is present, nothing outside RH's keys loosens a row, and the generated files match
the record. Static checks write nothing but the verify entry, so the agent may run them.

**Live checks** run one headless session with the generated settings and one with no settings (the
baseline), attempt each protected write in both channels (Bash and the file tools), and judge by the
filesystem afterwards. A row is reported **enforced** only if it holds in the governed session and
the same write succeeds in the baseline. A write that fails in both proves nothing about RH. Every
probe file has a unique name and is deleted by that exact name afterwards.

**A check is trusted only after it has failed on a broken input.** Each check ships with a broken
copy of the settings it must reject (the floor missing, a row removed, a loosening in the local
file), and RH's test suite runs those first.

**Verify goes stale.** Capability is environment state. Verify reports **stale** when the Claude
Code version on the machine differs from the one in the last verify entry, and Approve refuses a
stale verify entry. A runtime upgrade therefore needs a new verify and approval, even when the
declaration hasn't changed.

### Approve

A person reads RH's report (each row with its strength, and each live result against its baseline)
and approves. Approve records the approval and refuses if anything has changed since the verify
entry it approves.

### When the route runs

- A declaration changes.
- RH is upgraded.
- Claude Code is upgraded (Verify goes stale).
- Verify reports drift. The runtime's `/sandbox` panel, for example, writes its choices to
  `.claude/settings.local.json` and can switch the floor off there. Verify catches that, but only
  when it runs. **This is a detective control** (schema §6, ruling 2).

## 5. Standing exceptions

A standing exception lets one command run outside the sandbox, for a tool that can't run sandboxed.
It is a change like any other: declared, generated, verified, approved. It also **expires**, and a
person approves every single use.

### What the runtime offers, and its limits

- `sandbox.excludedCommands` removes the sandbox entirely for a matching call: no filesystem
  restriction and no network proxy. The command runs with the full access of the person running the
  session.
- Matching is on the call's text. Every command in a chained call must match. A redirect, a `cd`, or
  a command substitution keeps the whole call sandboxed, and so does a script that calls the tool
  internally. A pattern ending ` *` passes any arguments.
- An excluded command still goes through the permission flow. **An `ask` rule that matches it
  prompts a person in every mode, auto mode included, with no time limit**, and outranks any allow
  rule, including a "Yes, and don't ask again" saved to the local file. In `dontAsk` mode and in
  headless runs an ask becomes a deny, so an exception only works in an interactive session.

### What RH generates for one

```toml
[[exception]]
command = "docker compose up -d"     # the exact call; ` *` allowed but reported as "any arguments"
runs = ["compose.yaml", "docker/"]   # everything the command reads or executes
reason = "the compose plugin can't run inside bubblewrap"
expires = 2027-01-24                  # at most 90 days after approval
```

| Generated setting | From | Strength |
| --- | --- | --- |
| `sandbox.excludedCommands` entry | `command` | removes the operating system sandbox for matching calls |
| `permissions.ask` `Bash(<command>)` | `command` | permission rule: a person approves each use (probed headless as a deny, §8) |
| `sandbox.filesystem.denyWrite` and `Edit(...)` deny for each `runs` path | `runs` | as `[protect]`: the agent can't rewrite what the exception runs |
| expiry | `expires` | **detective**: Generate refuses an expired exception and Verify reports it, but the runtime has no expiry, so an expired entry stays live until one of them runs |

Rules the generator enforces:

- **Committed declaration only.** An exception loosens, and the local file may only add
  restrictions (schema §4).
- **`runs` is required and must exist.** The runtime warns that an exception covering a file the
  agent can write lets the agent rewrite it and then run it outside the sandbox. RH protects each
  path, and refuses an exception whose `runs` list is empty.
- **No interpreter as the command** (`bash`, `sh`, `python`, `node`, and similar). An excluded
  interpreter runs whatever it is given.
- **Renewal is a new change.** Moving `expires` goes through the whole route.

The ask rule is the barrier the person sees: they read the full command at each prompt. It matches
text, so a call written differently isn't excluded either. It stays sandboxed, which fails closed.

## 6. Where the floor lives

The floor is written into `.claude/settings.json` today. The runtime ranks
`.claude/settings.local.json` above it, and a person (or the `/sandbox` panel) can loosen the floor
there. Verify catches that, but only after the fact. Three placements would make the floor
preventive:

| Placement | Needs | Effect on the floor | Cost |
| --- | --- | --- | --- |
| **Project file (today)** | nothing | detective against a person's local change; `disableBypassPermissionsMode` holds anyway, because the strictest value applies | none |
| **Managed settings** (`managed-settings.json` in a root-owned system folder) | root to install and to change | preventive: nothing below managed overrides it; `strictAllowlist` becomes available | a machine-wide file for **every** project; the sandbox becomes **admin-required**, so the runtime **ignores the repository's `network.allowedDomains` and `excludedCommands`**. Every project's network list and exceptions would have to move into that one shared file |
| **A launcher passing `--settings`** | no root; every session must start through RH's launcher | preventive for sessions it launches; per project | the same admin-required effect (a repository's allowlist and exceptions are ignored, so the launcher's file must carry them); a session started without the launcher falls back to the project file, and Verify can't tell how a session was started |

Placing `allowUnsandboxedCommands: false` in the user file holds against a project `true` without
making the sandbox admin-required. But the user file is shared by every project, which ruling 3
chose not to write.

**Recommendation for v1: keep the project file**, reported as detective against local changes.
Managed settings turn RH's per-project declarations into one machine-wide file and need root, which
cuts against charter C4. The `--settings` launcher is the first upgrade worth probing after v1.

## 7. What the protocol guarantees, at its real strength

| Guarantee | Strength |
| --- | --- |
| The agent can't edit a declaration, a generated file or the record | operating system (Bash) and permission rule (file tools); probed for the record file (§8) |
| Generate refuses drift, an invalid declaration, or a floor change | RH's code, when a person runs it |
| A loosening in `.claude/settings.local.json` is caught | **detective**: when Verify runs |
| An exception's each use is approved by a person | permission rule (probed headless as a deny; the prompt itself not yet observed, §8) |
| An exception expires | **detective**: when Generate or Verify runs |
| An approval matches what was verified | record hashes; protects against mistakes, not forgery |

## 8. Live probes (run 2026-10-24T14:07:36Z, Claude Code 2.1.310, Linux)

Each claim below was added from documentation (commit `1cb5bf1` or this document) and then probed
live. Method: headless `claude -p` sessions in throwaway projects, one session per mode with the
floor and one without settings **in the same mode**. Results were judged by the filesystem afterwards.
A row is reported enforced only where the governed session held **and** the no-settings session's
write landed. Where both held, the mode itself refuses the write, and that mode can't test the rule.

| # | Claim | Result |
| --- | --- | --- |
| 1 | `disableBypassPermissionsMode = "disable"` stops bypass | **Holds, silently.** `--permission-mode bypassPermissions` and `--dangerously-skip-permissions` both started the session in `auto` (the user's default mode) with no error and no warning. A person who asks for bypass gets no signal that it was refused. |
| 2 | file tools can't write `.mcp.json`, `.git/hooks/`, `.git/config` | **Enforced by permission rule** in `auto` and in the downgraded bypass sessions. Without settings, `auto` let the agent write all three. `default` and `acceptEdits` refuse them headless even without settings (held by mode). |
| 3 | the agent can't write the record file, by either channel | **Enforced:** file tools by permission rule, Bash by the operating system sandbox (`Read-only file system`), same mode pattern as row 2 |
| 4 | an `ask` rule on an excluded command makes a person approve each use | **Holds as a deny in headless runs** (`default`, `auto`), even with an allow rule matching. A headless run can't show a prompt, so the prompt itself wasn't observed. "Still prompts after 'Yes, and don't ask again'" **wasn't tested**: it needs a trusted workspace (see below). |
| 5 | an excluded command runs unsandboxed; after `cd` it stays sandboxed | **Holds** in `default` and `auto`. The excluded call landed in a folder the sandbox denies. The `cd .` form and a non-excluded control were refused by the sandbox. |

Facts the probes surfaced, not in the claims above:

- **Untrusted workspace:** the runtime ignores a project's `permissions.allow` entries until the
  workspace is trusted, and says so on stderr. RH generates no allow rules, so its rows are
  unaffected. The probes used `--allowedTools` in their place.
- **Working-directory check:** a command writing outside the session's working directories needs
  approval even when an allow rule matches. The probes kept their targets inside the project.
- **Each claim rests on one run per mode.** A repeat run, and the interactive prompt for row 4,
  are still owed before a formal report.

## 9. Decisions

**Open: awaiting the operator's ruling.** Each is stated with a recommendation.

1. **The approval record.** **Recommend:** RH keeps its own record (§3), independent of any
   change-ceremony tool such as OpenSpec. Which ceremony tool RH adopts is still undecided, and a
   ceremony's approval gate governs a branch's change, not a project's controls. The two can be
   joined later.
2. **Standing exceptions** (§5). **Recommend:** adopt the shape as drafted: excluded command plus
   an ask rule, `runs` protected, committed only, no interpreters, 90 days at most. Adding
   `[[exception]]` is a schema change, so it ships as schema version 2 through this protocol.
3. **Where the floor lives** (§6). **Recommend:** the project file for v1, reported as detective
   against local changes. The managed placement is recorded as considered and declined for v1, and
   the `--settings` launcher as the first candidate after v1.
