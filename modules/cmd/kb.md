# the shell — running scripts, growing your own tooling

`cmd` runs a shell script you write, on linux with outbound internet. It's the reach for
anything no purpose-built tool covers: an API nobody wrapped, a bulk fetch, a one-off
transform. You draft the WHOLE script and pass it; there's no interactive session and no
state between runs.

## before you draft: look for a saved one

Reusable scripts live in storage under `scripts/`. Search there first — a past session may
have solved this, and the caption says what the script does and which env vars it needs.
Read it, pass its content to `cmd`. When a script you wrote proves reusable, save it back
the same way, captioned: what it does, what env vars it needs, and which layer (if any) it
depends on. That caption is how a session that never met you finds it.

## credentials are already in the environment

The owner's secrets are env vars by the time your script runs, so a script says `$GH_TOKEN` and
never a value.

**If the credential you need isn't there, ask for it — that's a normal step, not a blocker.**
Call `collect_secret(name="GH_TOKEN", label="GitHub personal access token (repo scope)",
scope="automation_env")`. The `scope="automation_env"` is what makes it an env var your scripts can read;
without it the secret lands in the vault where only a named tool can reach it, and your script
still sees nothing. Name it exactly as the env var (`GH_TOKEN`, not `github_token`), tell the
owner what scope or permissions the credential needs, and the next run has it.

Never write a secret's value into a script, and never print one. The value goes owner → vault →
env; it never passes through you.

## missing a command? build it

If the script needs a binary that isn't there (`gh`, a CLI, a converter), write a buildspec
into storage under `buildspecs/` and call `build_layer` with its key. Contract: install into
`layer/`, artifact `layer.zip`; anything in `bin/` lands on PATH at `/opt/bin`. Then poll
with the `build_id` you got back. A failed build returns its log tail — read the error, fix
the spec, build again; that loop is yours, not the owner's. A successful build attaches the
layer to `cmd` permanently, so the next session just calls the command. Five layers max per
function, 250MB unzipped.

## the shape of a good script

- `sh -e` semantics: it stops at the first failing command, so check what matters and let
  the rest fail loudly. A non-zero exit comes back as a tool failure with the output.
- 15 minutes is the ceiling. A longer job needs to checkpoint into S3 and resume.
- `/tmp` is scratch and vanishes with the invocation.
- Big output belongs in S3 with the key reported — output over ~200k chars spills there
  automatically and you get the key plus a tail. Never paste bulk output into chat.
- Print what you learned, not everything you saw: the script's stdout is what you read back.

## when NOT to use it

A domain tool beats a script every time — post entries with `post_journal_entry`, move
inventory with the inventory tools, file documents with `manage_storage`. Shelling out to
touch the books goes around the rules, the registry, and the ledger's own validation. Use
`cmd` for what's genuinely outside the ERP.
