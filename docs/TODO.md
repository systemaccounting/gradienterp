# docs — open work

- [ ] **say client error and server error.** the repo calls a 4xx a refusal and a 5xx a failure, in the
      root `AGENTS.md` § module conventions, in CLAUDE.md § requirement docs, in
      `tests/tower/local/test_handler_exits.py`, and across 51 docs (137 lines) and 193 code files
      (499 lines). HTTP already names both classes, client error and server error, and the rule
      attached to them — a client error logs at info and never alarms, a server error is one ERROR
      line — reads the same with HTTP's words. one sweep, docs and the lint's names together
