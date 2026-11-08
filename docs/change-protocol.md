# The change protocol

> **Status:** signed off by the operator, 2026-11-08; its three decisions ruled (§9, 2026-10-25).
> **Amendment drafted 2026-11-08, awaiting the operator's sign-off (§10):** who records a verify
> entry, what Approve requires, the guard file, the command for each step, and how live Verify
> runs (from RH's first live Verify of itself). Details charter §3 (declare → generate → verify →
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
| `.claude/hooks/ratchethub_guard.py` | RH (Generate), a copy of its hook file | yes |
| `.claude/ratchethub-record.json` | RH (each step), for the committed half | yes |
| `.claude/ratchethub-record.local.json` | RH (each step), for the local half | no |

All seven sit under the floor's self-protection (schema §2), so the agent can write none of them,
by Bash or by the file tools.

## 2. Who runs each step

| Step | Run by | Writes | Refuses when |
| --- | --- | --- | --- |
| Declare | a person | the declaration | (no tool; Generate validates) |
| Generate | a person | the generated files, the record | the declaration fails validation; a generated file differs from the record (drift); `ratchethub.local.toml` isn't ignored by git |
| Verify | static checks: a person or the agent (`ratchethub verify`, writes nothing); recording them: a person (`ratchethub verify --record`); live checks: a person (`ratchethub live`) | the record's verify entry (recording only) | the generated files differ from the record; the record has no generate entry for the current declaration; a check fails its broken inputs (the self-test) |
| Approve | a person (`ratchethub approve --who`) | the record's approval entry | the record has no passing, unstale static **and** live verify entry since the last Generate for the files now on disk (static only if the person says so, recorded as such) |

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
the record. Static checks write nothing, so the agent may run them. Recording their result appends
to the record under `.claude/`, which the floor denies to the agent, so a person records it.

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
entries it approves. It needs both a static and a live verify entry recorded since the last
Generate. A person may approve on the static entry alone; the approval then records
`static_only`, and RH reports no row as shown to hold (charter C6).

### When the route runs

- A declaration changes.
- RH is upgraded.
- Claude Code is upgraded (Verify goes stale).
- Verify reports drift. The runtime's `/sandbox` panel, for example, writes its choices to
  `.claude/settings.local.json` and can switch the floor off there. Verify catches that, but only
  when it runs. **This is a detective control** (schema §6, ruling 2).

## 5. Standing exceptions

Some tools can't run under the plain floor. There are two remedies, and RH prefers the first:

1. **Widen the sandbox** (`[network]` and a new `[writable]` row). The tool stays sandboxed, with
   one more host or one more folder. This is the remedy for build and package tools such as `make`
   and `npm`.
2. **A standing exception** (`[[exception]]`). One fixed wrapper script runs outside the sandbox,
   and a person approves each use. This is only for a tool that can't run sandboxed at all, such as
   `docker`.

Both loosen the floor, so both are changes like any other: declared in `ratchethub.toml` only (the
local file may only add restrictions, schema §4), generated, verified, approved. The live probes
behind this section (2026-10-24 and 2026-10-25, Claude Code 2.1.310, Linux, auto mode, headless)
are summarised at the end of each part, with each claim's count against a no-floor baseline.

### Widen, don't except

```toml
[network]
allow = ["registry.npmjs.org"]

[writable]
folders = ["~/.npm"]    # project-relative, or starting `~/`; no other absolute paths
```

| Field | Generated into | Strength |
| --- | --- | --- |
| `[network] allow` | `sandbox.network.allowedDomains` | as schema `[network]` |
| `[writable] folders` | `sandbox.filesystem.allowWrite` | enforced by the operating system: sandboxed commands may also write here |

**What widening keeps:** the tool, and everything it runs, stays sandboxed. After widening, `npm
install` succeeded and ran the project's `postinstall` script, and both that script and a `make`
target were still refused at `.claude/`, `.git/hooks/` and a folder outside the project. Without
the floor the same code wrote all of them.

**What widening costs:** a writable folder is writable by **any** code the agent can get run, not
only by the tool it was added for. Both the `make` target and the `postinstall` script wrote into
the cache folder. So a writable folder must hold nothing that runs later outside the sandbox.
The runtime's own documentation gives the same warning.

Rules the generator enforces:

- **A writable folder must not overlap a protected one,** neither `[protect]` nor the floor's own
  paths (`.claude/`, `.git/hooks/`, `.git/config`, `.mcp.json`).
- **No folder that runs outside the sandbox.** RH refuses the home folder itself, shell startup
  files, `~/.config`, `~/.local/bin`, `~/.claude`, and any folder on `PATH`. The list can't be
  complete, so the report states each writable folder as "writable by any code the agent runs".
- **No wildcards, must exist, no symbolic link,** as for `[protect]`.
- npm needs both rows. With the registry host listed but no writable cache, npm got past the
  network and then failed to create its cache. The sandbox's refusal surfaced as `ENOENT`, not
  as a permission error, so RH's report names the missing row instead of passing on the tool's
  message.

Evidence: confinement after widening held in 4 of 4 sessions, and the no-floor baseline wrote every
target in 2 of 2. The probes put the cache in a scratch folder, so `~/.npm` itself is **unprobed**.

### Exceptions only for a fixed wrapper

```toml
[[exception]]
wrapper = "scripts/compose-up.sh"    # a committed script in the project; takes no arguments
runs = ["compose.yaml", "docker/"]   # everything the wrapper reads, executes or builds from
reason = "the compose plugin can't run inside bubblewrap"
expires = 2027-01-24                  # at most 90 days after approval
```

| Generated setting | From | Strength |
| --- | --- | --- |
| `sandbox.excludedCommands` `./scripts/compose-up.sh`, exact | `wrapper` | removes the operating system sandbox for that exact call only |
| `permissions.ask` `Bash(./scripts/compose-up.sh)` | `wrapper` | permission rule: a person approves each use (probed headless as a deny, §8; the prompt itself **unprobed**) |
| `sandbox.filesystem.denyWrite` and `Edit(...)` deny for the wrapper and each `runs` path | `wrapper`, `runs` | as `[protect]`: the agent can't rewrite what the exception runs |
| the wrapper's hash in the record | `wrapper` | **detective**: Verify reports a changed wrapper |
| expiry | `expires` | **detective**: Generate refuses an expired exception and Verify reports it; the runtime has no expiry |

**Why a wrapper and not a command.** The runtime matches excluded commands by the call's text. A
pattern for the tool itself (`docker compose *`) passes whatever arguments the agent chooses. An
exact pattern naming a wrapper passes nothing: a call that adds an argument or an environment
prefix doesn't match and stays sandboxed, which fails closed. Everything the tool needs lives in
the protected wrapper and its `runs` files, never in the call.

**The wrapper must shut out every input the agent can still write.** Protecting the wrapper isn't
enough. A plain wrapper running `docker compose` read the project's `.env`, which the agent had
written, and the image name it chose reached the excluded run. The hardened wrapper passes
`--env-file /dev/null` and starts the tool under `env -i` with a fixed `PATH` and `HOME`:

```sh
#!/bin/sh
cd "$(dirname "$0")/.." || exit 1
exec env -i PATH=/usr/bin:/bin HOME="$HOME" \
    docker compose --env-file /dev/null -f compose.yaml up -d
```

The same reasoning extends to inputs the probes didn't exercise (**unprobed**): a compose build
context and every file a container executes from a bind mount must be inside `runs`, so a build
context of the project root can't qualify. Images should be pinned by digest, because a tag can
change between approvals.

Rules the generator enforces:

- **`wrapper` is a committed file in the project, and `runs` is required and must exist.** RH
  protects them all, and refuses an exception whose wrapper is untracked by git or whose `runs`
  list is empty.
- **No pattern.** RH generates the exact call. ` *`, an argument, and an interpreter
  (`bash`, `sh`, `python`, `node`, and similar) as the command are all refused.
- **Renewal is a new change.** Moving `expires` goes through the whole route.

Verify adds a **live check per exception**. In a throwaway copy of the project it plants a `.env`
and a caller variable that each try to steer the wrapper, runs the wrapper as excluded, and reports
the exception **steerable** if either reached the run. Approve refuses a steerable exception. RH
can't read a shell script and prove it closed, so the live check is the test. The check must not
start the real thing: for compose the probes ran `config` with `DOCKER_HOST` pointing at a socket
that doesn't exist. A dry form for other tools is designed per tool, and none is designed yet.

The ask rule is the barrier the person sees. They read the call at each prompt, and the call is
always the same text, so what they approve is the wrapper as protected and hashed. In `dontAsk`
mode and in headless runs an ask becomes a deny, so an exception only works in an interactive
session.

Evidence: the wrapper and its compose file were left unchanged against both channels in 9 of 9
attempts. The plain wrapper was steered by `.env` in 5 of 5 runs and the hardened one in 0 of 4.
An environment prefix and an extra argument stayed sandboxed in every run. `env -i` was confirmed
against inherited variables only outside a probe session, because a prefixed call never matched the
exact pattern.

### Docker

The sandbox blocks the docker daemon's socket, so under the floor `docker` fails at the connection
and never reaches the daemon (probed). A person in the `docker` group controls the daemon, and the
daemon runs as root, so **an excluded call that reaches the daemon has root's reach** on the
machine. RH therefore allows docker driven by the agent only through a fixed wrapper as above, and
reports each such exception as "root-equivalent per approved use". No widening may reach the
daemon: RH refuses `network.allowUnixSockets` and `network.allowAllUnixSockets` (the runtime's own
documentation names `docker.sock` as a sandbox escape), and Verify reports either if any merged
settings file sets it.

### The auto mode classifier is not part of the floor

In auto mode, a model reviews some calls before they run, and it refused several probe calls. RH
counts none of those refusals:

- **It varies from run to run.** The same Bash append to a protected wrapper was refused by the
  sandbox 4 times and by the classifier 5 times.
- **It fails closed on error.** During one run the classifier service returned no verdict on 10
  calls, and each was denied even though an allow rule matched. That is safe, but it means nothing
  about the floor.
- **It isn't in every mode.** `default` and `dontAsk` don't run it, and a person can switch mode.

A live check reports a row **enforced** only where the sandbox or a permission rule refused, and
reports a call the classifier refused as **inconclusive**, to be re-run.

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
| A widened tool stays confined outside its writable folders | operating system (probed, §5); a writable folder is writable by any code the agent runs |
| An exception runs only its fixed, protected wrapper | text match on an exact call (probed, §5); the wrapper's hash is **detective** |
| An exception's wrapper can't be steered by `.env` or the caller's environment | Verify's live check, when it runs (probed for compose, §5) |
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

**All three ruled** (2026-10-24 and 2026-10-25). Each is kept with the recommendation it ruled on.

1. **The approval record. Ruled 2026-10-24: as recommended.** RH keeps its own record (§3), independent of any
   change-ceremony tool such as OpenSpec. Which ceremony tool RH adopts is still undecided, and a
   ceremony's approval gate governs a branch's change, not a project's controls. The two can be
   joined later.
2. **Widening and standing exceptions** (§5, redrafted 2026-10-25 from the probes). **Ruled
   2026-10-25: as drafted.** The recommendation:
   widen first (`[network]` plus a new `[writable]` row) for tools that only need a host or a
   folder; an `[[exception]]` only for a fixed, committed wrapper: exact call, an ask rule, wrapper
   and `runs` protected and hashed, ambient inputs shut out and checked live, committed only, 90
   days at most. Docker driven by the agent only through such a wrapper, reported as
   root-equivalent. The auto mode classifier counts for nothing in a verdict. Adding `[writable]`
   and `[[exception]]` is a schema change, so it ships as schema version 2 through this protocol,
   and the schema's "Exemptions: not in v1" note is rewritten then.
3. **Where the floor lives** (§6). **Ruled 2026-10-25: as recommended.** The recommendation: the project file for v1, reported as detective
   against local changes. The managed placement is recorded as considered and declined for v1, and
   the `--settings` launcher as the first candidate after v1.

## 10. Amendment (drafted 2026-11-08, awaiting sign-off)

Found while building the steps. Each brings this text level with what the code does; none loosens
anything.

1. **Recording a static verify is a person's step.** §2 said the agent may run static Verify, which
   writes the verify entry. The record is under `.claude/`, which the floor denies to the agent, so
   the two are split: `ratchethub verify` checks and writes nothing; `ratchethub verify --record`
   appends the entry, run by a person.
2. **Approve needs a live verify entry too,** or an explicit static-only approval that says so. The
   charter (C6) reports a row as enforced only after a live probe, so an approval that rests on
   static checks alone must say it does.
3. **The guard file is a governed file** (§1): `.claude/hooks/ratchethub_guard.py`, written by
   Generate, hashed in the record, and drift when edited.
4. **Verify refuses to report when its self-test fails:** each check runs against its broken inputs
   first (§4, "A check is trusted only after it has failed on a broken input").

The following were found on RH's first live Verify of its own repository (2026-11-08, Claude Code
2.1.319, Linux, auto mode). They define how live Verify runs; none loosens the floor.

5. **The prompt goes on stdin.** `--add-dir <directories...>` takes every argument after it, so a
   prompt placed last on the command line is read as one more folder, and the session exits before
   it starts (first run: both sessions, nothing measured).
6. **A session that never starts measured nothing.** Live Verify names it with its error and does
   not record the run.
7. **The classifier is kept out of the measurement, not just discounted** (§5, "The auto mode
   classifier is not part of the floor"). On the second run it refused every governed Bash probe
   before the sandbox saw it, so those rows were inconclusive. The documentation gives two reasons
   (permission-modes, "How the classifier evaluates actions" and "Protected paths"): with
   server-side review, sandboxed commands wait for the classifier; and a write the runtime
   recognises in one of its own protected directories (`.claude`, `.git` and others) goes to the
   classifier even when an allow rule matches. Both sessions therefore run with:
   - `CLAUDE_CODE_AUTO_MODE_SERVER=0`, which turns server-side review off;
   - an exact allow rule (`--allowedTools`) for each Bash probe command, never a wildcard;
   - inside the runtime's protected directories, a probe that writes through an interpreter
     (`python3 -c`), which the runtime doesn't trace as a write. That is the case the sandbox
     exists for: code the agent runs.

   None of these can let a governed write through. Deny rules and hooks outrank an allow rule, and
   the floor forbids the unsandboxed retry that an allow rule would otherwise approve. Both
   sessions get the same flags, rules and folders, so the baseline still shows the write lands
   without the floor.
8. **The committed record never names the machine.** Every entry bound for it passes through one
   function that writes each absolute local protected folder as `<local folder N>`, the repository
   as `.`, the home folder as `~` and the live run's scratch folder as `<scratch>`. The local record
   keeps the real paths. The committed record is published with the repository (charter C1).

**Result of the third run** (2026-11-08T16:09:16Z), recorded and then approved:

| Row | Governed session | Baseline |
| --- | --- | --- |
| project root, Bash and file tools (controls) | landed | landed |
| `.claude/`, Bash | refused by the sandbox (`Read-only file system`) | landed |
| `.claude/`, file tools | refused by permission rule | landed |
| `.git/hooks/`, file tools | refused by permission rule | landed |
| two local protected folders, Bash | refused by the sandbox (`Read-only file system`) | landed |
| two local protected folders, file tools | refused by permission rule | landed |
| outbound guard (`git push --tags`) | refused by the hook | ran, failed on no remote |
| `gh` guard (`gh pr`) | refused by the hook | ran, failed on no remote |

No probe file was left behind. The two earlier runs stay in the record as failed entries.
