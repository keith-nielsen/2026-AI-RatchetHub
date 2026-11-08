# RatchetHub v1 scope

> **Status:** draft, 2026-11-08; its two decisions ruled (§5, 2026-11-08), the whole not yet signed
> off. Lists what RatchetHub (RH) v1 contains, what it
> leaves out, and when it counts as done. The properties come from `CHARTER.md`, the declaration from
> `docs/declaration-schema.md` and the route from `docs/change-protocol.md`; this list cites them
> and doesn't restate them.

## 1. The rule for what's in

**v1 is what its first consumer project uses today, and nothing speculative.** A component is in
only if a project uses it now, and each requirement ships with the tests that carry its lessons.
Checks that belong to one project (its naming rules, its own invariants, its own test suites) stay
with that project. RH runs none of them (charter C1).

## 2. In v1

### The agent half: the leash

| # | Component | Requirement |
| --- | --- | --- |
| A1 | Declaration and generator | Schema v1 as signed off, plus the `[writable]` row (§5, decision 1), rendered into `.claude/settings.json` and RH's keys of `.claude/settings.local.json` |
| A2 | The change protocol | Generate, Verify (static and live), Approve and the record, exactly as `docs/change-protocol.md` |
| A3 | Outbound guard (hook) | Enforces `[publish]`. Matches command position only: tokenized, never inside quotes, comments or heredoc bodies. Covers `gh api`. Reported as a tripwire that names the forms it doesn't catch, `curl`, `wget` and web fetches to GitHub among them |
| A4 | `gh` invocation guard (hook) | An allowlist of `gh` command forms. A denied form is never printed for a person to run instead; the driver uses REST paths |
| A5 | Relay guard (hook) | A driver's handoff line reaches the person unedited, with its step tag |
| A6 | Capability probe | Reports what it found and in which channel: `AUTHENTICATED`, `UNAUTHENTICATED` or `ABSENT`, with an `evidence` field (`inspected` or `attempted:<channel>`) and `--json` output. Its roots come from the declaration. It enumerates `credential.helper` |
| A7 | Residual conduct rules | The short list of rules no control can reach, declared as residue, never presented as controls (C6) |

### The GitHub half: the agent's reach

| # | Component | Requirement |
| --- | --- | --- |
| G1 | Pull request (PR) lifecycle driver | **Level-triggered.** Each run derives the route state from GitHub, including "merged, branch still present". A mutation's own response outranks a lagging read. An empty or failed read is no answer. A queued merge is not merged. Verify reads retry on the measured ladder (5, 8, 13 seconds) and hand over to a monitor when it runs out. Suppression covers only the mutation still unconfirmed. The lag log's outcomes partition. Pending checks are cross-checked against the job-level view. **Every behaviour is tested through the real call path**, with the emit site's own command shape |
| G2 | Handoff file (`next.sh`) | Pins branch, step id and PR number for both the mutation and its verification. Refuses, or is removed, once its step verifies. Used only for person-run steps (ruled: agent-run steps re-run the driver instead) |
| G3 | Release driver | Same handoff format as G1, with prose and the executable line labelled separately |
| G4 | Read channel | Every GitHub read degrades to anonymous REST and names the channel that answered. Mutations use REST paths, never `gh pr edit` |
| G5 | Preflight and PR state | The checks run before every push, and PR state reported per layer |
| G6 | Platform state, declared and reconciled | The project declares its rulesets, required status checks and labels, and whether GitHub's secret scanning and push protection are on. A read-only, anonymous reconciler compares content (never timestamps) and reports drift. It never fixes. Stale merged branches and tag-to-Release parity are rows. Any ruleset write fetches the whole ruleset, changes one field, and sends it back |
| G7 | Reusable CI (continuous integration) workflows | The scope review (lists both sides of a rename), a generic protected-element gate (the project declares the elements, by hash) and the Markdown content check. A project calls them at a pinned commit, so each gate exists once |
| G8 | Supply-chain pins | Every action and dependency RH uses is pinned, and a test checks it |

**Rules for every gate (G7, and any check a driver waits on):** a gate either can fail or is named
advisory. Only required gates that can fail hold the lifecycle. A gate's name is frozen once it's
required. A gate ships blocking once its ported tests pass. A gate with no completed burn-in ships
advisory, under a name that says so. The protected-element gate is such a gate.

## 3. Out of v1

Everything in charter §4 stays out, except that the exemptions gap is narrowed by `[writable]` (§5).
Also out:

- **Standing exceptions (`[[exception]]`).** The design is ruled (protocol §5). Building it waits for
  a project that needs it (§5, decision 1).
- **A change-ceremony approval gate**, keyed to a branch's own change (§5, decision 2).
- **Merge queue and strict status checks** (ruled 2026-10-24: not in current use). Required status
  checks stay in (G6).
- **A signed record** (protocol §3), and **moving the floor** to managed settings or a `--settings`
  launcher (protocol §6, ruled; the launcher is the first candidate after v1).
- **Copies kept equal by mirroring.** Under RH, a project depends on a pinned RH version and holds no
  copy of RH's code, so no mirror, parity check or sync manifest is built.
- **General Markdown linting and link checking.** These are each repository's own housekeeping,
  not part of the leash. (The content check in G7 is different: it guards what the agent may write.)

## 4. Done when (observed, not built)

1. Each charter property's *Test:* line passes (C1–C6).
2. RH's full test suite passes on a machine with no consumer project present (C1).
3. Two unrelated projects are governed by the same RH build (C4). One of them is the first consumer.
4. Each check has been seen to fail on its broken input before it passes on the real one
   (protocol §4), and each GitHub-half behaviour is reached through its real call path (G1).
5. The probes still owed by protocol §8 have been run: a repeat run, and the interactive prompt
   for the ask rule.

Still open, not blocking this list:

- **Distribution:** a Python package with the generator, or a Claude Code plugin for the hooks with
  the settings generated separately. Measure what a plugin can carry before choosing, at the start
  of the build.
- **Where G6's declaration lives:** a `[github]` section of `ratchethub.toml` (C4 points there) or a
  file of its own. This is a schema change, so its design comes with the build.
- **Which repository types and plans GitHub's secret scanning and push protection cover.** It has
  to be checked before G6 reports them as a control.

## 5. Decisions

**Both ruled by the operator on 2026-11-08, as recommended.** Each is kept with the recommendation
it ruled on.

1. **Schema v2: `[writable]` now, `[[exception]]` later. Ruled: as recommended.** Recommendation: v2 adds `[writable]` only.
   Without it, a project can't run its package installer under the floor (it needs a cache folder
   and a registry host), and a second, unrelated project (§4, item 3) will probably need one. The
   `[[exception]]` row also needs a per-tool dry run for its live check, and none is designed. No
   current project needs one, so §1 keeps it out. **Risk:** until then, a project whose tool can't
   run sandboxed at all (such as docker) still can't adopt RH. Protocol ruling 2 said both rows ship
   as schema v2, so this splits them: `[[exception]]` becomes v3. Alternatives: both in v1, or
   neither (the gap stays as charter §4 states it).
2. **The change-ceremony approval gate stays out of v1. Ruled: as recommended.** Recommendation: out. RH's own record
   already approves every change to a project's controls (protocol §9, ruling 1). A ceremony gate
   governs a branch's change, which is each repository's own process. Keeping it in ties v1 to a
   ceremony tool that hasn't been chosen. **Risk:** a project that relies on a ceremony gate keeps its
   own until a later RH version. Alternative: keep it in, and v1 waits on choosing the ceremony tool.
