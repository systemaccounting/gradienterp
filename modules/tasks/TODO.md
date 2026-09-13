# tasks — open work

What's live is in [`AGENTS.md`](AGENTS.md) (`## current features`). Open work below.

The self-heal loop has four legs left:

- **the gh-issue leg** — the triaged public half files as a GitHub issue. Open choice when it lands:
  issues on the platform repo, or a dedicated issues repo. Gated on the repo going public;
  escalations accumulate as operator tasks meanwhile and nothing publishes without triage.
- **Claude-on-Actions triage** — board hygiene on `issues.opened`, rides the same repo.
  Done when an `escalate` call ends as a public gh issue with its placeholders intact.
- **the log-side producer** — `[ERROR]` metric filters into the same collector with `source: logs`.
  Covers raised exceptions.
- **retry-before-file hardening** — persona-level today ("errored past a retry"), not enforced.

## hardening / future

- [ ] **allocation over the queue** — quoting and allocation are one pure read: rank the
  open queue by lapse (oldest first, dues pulling items up) × available hours (inventory
  capacity items) × a geometric split (80/20 down the rank — the hedge keeps the second
  item moving) ⇒ each task's `quote` falls out; the only write is `reserve` committing the
  plan (the demo-03 scheduler move). For the platform-as-vendor the "labor" is fix cycles.

- [ ] **FK GSIs are due_date-sparse** — a contact/PO/invoice/journal task WITHOUT a
  due_date is invisible to its FK query (the open index had the same flaw; fixed by
  re-ranging on created_at 2026-07-31). Re-range the four FK GSIs the same way when
  pulled — same one-line tf change + backfill wait each.
- [ ] **Cedar policies on Gateway targets** — not in POC scope.

## known limitation

- **`due_date` agent semantics** — agent computes "by friday" → ISO date from its
  understanding of today; date drift is prompt-level (inject today at runtime), owned by
  `modules/agent/`, not tasks.

