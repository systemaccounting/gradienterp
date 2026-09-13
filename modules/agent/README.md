# agent

Each customer gets one agent, running in their own AWS sub-account. It is the whole interface to the business: onboarding, bookkeeping, inventory, purchasing, contacts, invoicing — everything the owner needs to run the shop, through chat. No dashboards to learn, no forms to fill in a fixed order; the owner and their people (employees, customers, vendors) just talk to it, on the web or by email.

One brain, many front doors. Web chat, inbound email, and the dev harness all reach the same agent over the same runtime, so the answer is the same whichever way you ask. When a step needs sensitive input — an API key, a worker's SSN — the agent can open a structured form in the chat and collect it straight into storage without ever seeing the value.

## why operator-hosted

The agent is not something a customer installs and runs themselves. The operator runs one AWS Organization and provisions a sub-account per customer. That is what makes the payoff possible: because every customer's agent lives under one roof, one customer's agent can transact with another's. A cafe's agent can request a quote from a coffee vendor's agent; acceptance posts a balanced journal entry on both sides. That cross-customer commerce — agents doing business with agents — is the reason the platform is hosted rather than shipped, and it is what the shared event bus in `TODO.md` builds toward.

See `AGENTS.md` for how the module works and how to operate it, and `TODO.md` for what's still open.
