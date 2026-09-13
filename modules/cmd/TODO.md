# cmd — open work

## the line against modules/automation

`modules/cmd` runs the OUTWARD half of custom code — scripts reaching the internet, the cabinet and
their own SSM env path, with a role holding no `lambda:InvokeFunction` so they cannot touch a module
tool. `modules/automation` reaches the ERP surface, and it carries the review gate.

They stay separate modules. Tool dependence is the boundary and IAM already enforces it.

cmd's INLINE scripts are ungated because they run while the owner is in the conversation waiting for
the output — the review gate is for code that runs when nobody is looking. Which is why a scheduled
cmd takes a `script_key` under `automations/approved/external/` instead: same prefix, same runner, but
what runs unattended has passed review. See `AGENTS.md` § current features.

What's live is in [`AGENTS.md`](AGENTS.md) (`## current features`). Built and proven end to
end 2026-08-03: the agent authored a buildspec, read the `log_tail` off a failed build, fixed
it, the layer attached, and it filed GitHub issues with `gh` off `/opt/bin` using a credential
it asked the owner for through the in-chat form. Open:

- [ ] **stale layer VERSIONS** — `build_layer` attaches the new version and leaves the old one
      published (gh:1 sits behind gh:2). Lambda has no TTL for these, so a lifecycle rule can't
      reach them: it's a scheduled janitor calling DeleteLayerVersion on anything not attached.
      (The build ZIPS are handled — `layers/` in the cabinet expires at 7 days, and Lambda
      copies content at publish, so the zip is scrap the moment the version exists.)
- [ ] **the 5-attached cap** — a function holds at most 5 layers, and that isn't retention at
      all: something has to be dropped for a 6th capability, and which one is least useful is a
      judgment about the work in front of the agent. Either the tool refuses with the current
      list and lets the agent choose, or it drops least-recently-used and says so.
- [ ] **check before building** — a fresh session doesn't know what's already installed. It
      doesn't reflexively rebuild (verified: a plain task used the attached layer and never
      called `build_layer`), but an explicit mention of layers made it rebuild a working one.
      Cheapest fix is a guide line: test with `command -v <tool>` in a throwaway script and
      only build on a miss.
- [ ] **the script library in practice** — three gh scripts are saved under `scripts/` with
      captions, and a cold session found one by caption. Still unproven: a session USING a
      saved script rather than redrafting the same thing.
- [ ] **timeout as a script concern** — 15 min is the ceiling; a longer job needs the script to
      background + checkpoint, or `continue_later` to poll it. No machinery yet.
