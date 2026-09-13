# mcp — open work

- [ ] **automate, live** — a rule naming a vendor tool through `automate` is unit-tested and not
      yet run against gradienterp's vendor gateway; Linear is installed there for it.
- [ ] **GitHub by manifest** — the owner's-app install pastes a callback and copies two values.
      GitHub's app manifest flow makes the app from a link (name, permissions, callback carried
      in it) and returns a code the landing exchanges for the client id and secret: nothing
      pasted. A landing route on top of the paste path.
- [ ] **Xero and HubSpot, live** — GitHub is connected on gradienterp through the owner's
      app; Xero and HubSpot wait on a firm with an account there. Xero's `scopes` row is its
      MCP server's granular list for a new app, unconfirmed by an approval.
- [ ] **the policy engine** — the write bound is the container's tool filter off the row. The
      vendor gateway's Cedar policy engine would enforce it at the gateway too, in the gateway's
      log; policies would be `manage_mcp`'s, made per target at install from the row's `write`.
- [ ] **the second consent's link in the same reply** — today the target's link comes from
      `install`, and the firm's from the first tool call after the target syncs. Two replies.
      A `status` that asks the gateway for the firm's token once the target is READY would put
      both links in one.
- [ ] **westwood** — the module lands on a gerp at its next per_customer apply; westwood has not
      had one since. Its container image is behind as well.
- [ ] **`write_tools` per vendor** — only Stripe's and Linear's are recorded; a row without them
      gates every tool on `write`. Each vendor's list is a PR after its first install, and a
      vendor that adds write tools later (Linear did) leaves a read-only row letting them
      through until the row is updated: a check of the live list against the row is worth a
      line in `status`.
- [ ] **`key_header` per vendor** — only Stripe, GitHub and Zapier carry one; whether the other
      twelve accept a personal token as bearer on their MCP server is a probe per vendor, and a
      row gains the field when it does.
