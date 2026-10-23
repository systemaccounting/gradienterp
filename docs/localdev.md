# gradientERP, running on this machine

you are an agent a person started inside a clone of gradientERP. this page sets up their machine
to run and test the project. nothing here needs an AWS account, a cloud credential or docker.

gradientERP is ERP as the agentic cloud native cross-firm optimization protocol: a business runs
through its own AI agent, and one firm's `purchase_request` is another's `sale`. the stack you are
about to start is the whole platform as plain processes on this machine, over an AWS emulator.

## 1. check the machine

each of these must be on the path. say which are missing and how to get them, then stop until the
person has them.

| need | check | get it |
|---|---|---|
| git | `git --version` | https://git-scm.com |
| python 3.12 | `python3.12 --version` (`python3 --version` is accepted) | https://www.python.org/downloads or `brew install python@3.12` |
| Node 22 with npm | `node --version`, `npm --version` | https://nodejs.org |
| an Anthropic API key | `echo ${ANTHROPIC_API_KEY:+set}` | https://platform.claude.com/docs/en/get-api-key.md, then `export ANTHROPIC_API_KEY=sk-ant-...` in the shell you will start the stack from |

the key is for the platform's own agent, the bookkeeper that runs on this machine, not for you. it is
optional: without it the stack starts and a chat turn answers with the error instead of a reply.

## 2. set up

from the repo root:

```
bash scripts/local-dev.sh --install
```

once per clone: a `.venv` on python 3.12, the python dependencies, `npm ci` in each Node lambda.

the owner app shows the gerps `LOCAL_GERPS` names. write one line into a file called `.env` at the
repo root (it is gitignored), an owner of the local gerp:

```
LOCAL_GERPS={"local-dev": [{"gerp_id": "gradienterp", "label": "gradientERP"}]}
```

then start everything:

```
bash scripts/local-dev.sh --start
```

ten processes: the AWS emulator (moto) on :5000, the owner app on :3000, the gerp's API on :8080,
the public dashboard on :3001, a Stripe stand-in on :4242, a Cognito stand-in on :4243 that signs
every local token, the event pump, the agent on :8090, the web chat on :8082 and the storage portal
on :8083.

## 3. done looks like

three checks. run each, compare to the expected line, and report all three results with the time
elapsed since you started step 1.

**every process up**

```
bash scripts/local-dev.sh --status
```

every line says `UP`. a `down` line names a process, and `$TMPDIR/gerp-local-dev/<name>.log`
(`/tmp/gerp-local-dev/` where `TMPDIR` is unset) has why.

**a test green**

```
bash scripts/test.sh --name post_journal_entry
```

ends with `all 18 tests passed`. that ran the real accounting handler against the emulator.

**a chat turn answered**

in a browser: open `http://localhost:3000/dev/login?sub=local-dev`, which signs the person in and
lands on their gerps. open gradientERP, then its chat, and send a message. the agent answers.

without a browser:

```
curl -sS -X POST localhost:8090/invocations -H 'content-type: application/json' \
  -H 'x-amzn-bedrock-agentcore-runtime-session-id: dev1' -d '{"prompt": "what can you do?"}'
```

answers `{"response": "...", "session_id": "dev1"}`. with no key it answers `{"error": "..."}`,
which still counts: the stack is up, the key is the person's next step.

## 4. how to work here

- read the root `AGENTS.md` first: the module map, the commands, how local mode works. `CLAUDE.md`,
  which claude code reads on its own, points there. then the `AGENTS.md` of each module you touch,
  under `modules/<name>/`
- a change gets a test beside the ones it touches, under `tests/<module>/local/`. `bash
  scripts/test.sh --name <something>` runs the files whose name contains it; `bash scripts/test.sh`
  runs everything, once, before a pull request. `tests/AGENTS.md` has the conventions
- work on a branch and open a pull request. the workflows run there, and nothing in them deploys
- the running stack is live-edit: a handler is re-imported on the next request, so an edit shows
  without a restart. `bash scripts/local-dev.sh --restart` wipes the emulator and starts clean;
  `--stop` ends it. `tests/server/AGENTS.md` is the stack's own page
- nothing on this machine reaches AWS, and nothing needs to. a change that needs a cloud account
  to try is not a first change

## 5. what to report

tell the person the three results and the elapsed time. then anything on this page that was wrong,
unclear or out of date: open an issue on https://github.com/systemaccounting/gradienterp/issues
quoting the line and what happened instead. that issue is a contribution.
