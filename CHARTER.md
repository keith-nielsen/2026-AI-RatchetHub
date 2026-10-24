# RatchetHub charter

> **Status:** draft, 2026-10-24. Awaiting operator sign-off (the Phase 1 exit). Once signed, a change
> to this charter goes through the same protocol as any other RatchetHub change (§3).

## 1. What RatchetHub is

RatchetHub (RH) locks down a project for an AI coding agent. A project tells RH what to protect, and
RH turns that into the agent's actual controls: the sandbox, permission rules and hooks. Like a
ratchet, the controls move one way. A project can tighten them, and nothing a project declares can
loosen RH's floor.

v1 supports one agent runtime (Claude Code) and one forge (GitHub). Nothing else is in scope.

## 2. The six properties

Each property is a requirement, and each has to be shown by a test, not asserted.

1. **C1. Stands alone.** RH contains no knowledge of any particular project: no project's paths,
   names or rules. A project that has never heard of any other product can use it.
   *Test:* RH's full test suite passes on a machine where no consumer project exists.
2. **C2. Protects itself from casual mistakes.** The agent RH leashes cannot edit RH's generated
   settings, a project's declaration, or RH's own installation. A person's mistake (a hand edit, a
   toggled setting) is caught by verification, never silently accepted.
   *Test:* a live probe shows the agent's writes to those paths refused, and a hand-edited copy is
   reported as drift.
3. **C3. Changes only through its own protocol.** Every change to a project's controls follows
   one route: **declare → generate → verify → approve** (§3). Generated files are never edited by
   hand. Drift is reported and refused, never auto-fixed.
   *Test:* each step refuses when the step before it hasn't completed, and a generated file changed
   outside the route (by a person or by the agent) is reported as drift by verify and refused by
   generate.
4. **C4. Extended by declaration.** A project plugs itself in by shipping a declaration file
   (`ratchethub.toml`, specified in `docs/declaration-schema.md`). Adding a project never requires
   changing RH's code.
   *Test:* two unrelated example projects are governed by the same RH build.
5. **C5. Only tightens.** A declaration can add restrictions. It cannot lower RH's floor: the
   sandbox stays on, refuses to start if it can't run, and allows no unsandboxed retries.
   *Test:* a declaration that tries to loosen the floor is refused by the generator.
6. **C6. Honest about strength.** Every control RH generates is reported at its real strength:
   enforced by the operating system, by a permission rule, or only by a text-matching tripwire.
   Nothing is described as stronger than it is.
   *Test:* every generated rule carries its strength in RH's report. A rule reported as enforced (by
   the operating system or by a permission rule) is shown to hold by a live probe compared against a
   baseline with no settings; the runtime's documentation alone is not enough.

## 3. The change protocol (outline)

The detailed protocol is its own document. Its shape:

1. **Declare.** A person edits the project's declaration. The agent can't, by C2. It may only
   propose a change for a person to apply.
2. **Generate.** RH renders the declaration into the runtime's settings files. It refuses a
   declaration that fails validation, and it refuses to overwrite a generated file that was changed
   by hand.
3. **Verify.** RH checks the **effective** settings, meaning every settings file the runtime merges,
   not only the files RH wrote. It checks statically, against the schema and the runtime's documented
   rules, and live, with a probe that attempts each protected write and compares the result against
   a baseline with no settings. A check is only trusted after it has been shown to fail on a broken
   input.
4. **Approve.** A person accepts the verified result, and the approval is recorded with what was
   verified.

## 4. Out of scope for v1

- A second agent runtime, or a forge other than GitHub.
- Protecting a project from sessions that start **outside** it (see the schema's open decisions).
- Read protection for secrets. It is left to a dedicated secrets tool; RH can add it by declaration
  later.
- Emergency break-glass (a fast bypass of the protocol, alarmed and reviewed after use). Deferred
  until a need appears.
- **Exemptions (commands run outside the sandbox). A known gap that must be closed:** until it is,
  a project with a tool that can't run sandboxed can't adopt RH. They return as standing
  exceptions: planned, approved, expiring, a variant of the change protocol (§3). See the schema's
  "Exemptions: not in v1".
