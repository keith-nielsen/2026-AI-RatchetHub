"""The residual conduct rules (v1 scope A7): what no control RH generates can reach.

They are residue, never controls (charter C6): nothing enforces them, and RH's report prints them
under that heading. `docs/residual-rules.md` explains each; a test keeps the two equal.
"""

RULES = (
    ("A refusal is handed over, never routed around",
     "When the sandbox, a permission rule or a hook refuses, report it and stop. Trying another "
     "spelling, tool or channel after a refusal is evasion, however sure the agent is it was wrong."),
    ("Propose a change to the controls; never apply one",
     "The agent's part in a change is a proposal a person applies (protocol §2). It writes the "
     "declaration change as text, never by any route around the floor."),
    ("Never hand a person a form the agent may not run",
     "A deny rule or hook stops the agent running a command, not printing it. A refused form is "
     "refused for the person's terminal too; the drivers' REST paths are the route."),
    ("Relay a driver's handoff block exactly",
     "Copy the block as printed, step tag included. The relay guard blocks a changed line once per "
     "emission and then stands aside, so the rule carries the rest."),
    ("No route the guards can't see",
     "Don't reach GitHub's write API with curl, wget, a web fetch or a script, and don't assemble a "
     "command from variables. The hooks read the command line; these forms pass them unread."),
    ("Measure a capability before stating it",
     "Run `ratchethub probe`, then speak. One failed call is not a limit: retry it and try an "
     "independent channel. A row the probe only inspected was not exercised."),
    ("State every control at its real strength",
     "In prose as in the report: a tripwire is never a barrier, a detective control never a "
     "preventive one, and a row not yet probed live is not enforced."),
)
