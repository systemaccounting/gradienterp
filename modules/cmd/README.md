# cmd

A shell with internet, whose toolbox the agent grows itself.

The agent's other execution vessel (`analyze`) computes over the firm's data but reaches
only S3 — deliberate, that's the bookkeeping sandbox. cmd is the outward-facing one: the
agent drafts a whole script, cmd runs it on a real linux runtime with outbound internet,
the owner's own credentials in env, and whatever dependencies the agent has built into it.
Hit a missing tool mid-task? Write the build recipe, build it, retry — the toolbox is an
artifact the agent maintains, not a list the platform ships.

Everything about it is owner-auditable in their own filing cabinet: the reusable scripts,
the build recipes that produced the deps, the oversized outputs. The credentials come from
the owner's own vault path and never pass through the agent. What the scripts can DO is
bounded by the cmd role, never the agent's.
