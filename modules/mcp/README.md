# mcp — a vendor's own tools, installed for the firm

A firm runs on more than its books: Stripe or Square for payments, Linear or Notion for its
work, Xero for the accountant, Dropbox for its files. Each of those vendors now runs an MCP
server, and this module installs one for a gerp so its agent uses the vendor's tools directly
— reads the Stripe balance, lists the Linear issues, files the Xero invoice — with the grant
held in the firm's own account.

The owner does one thing: approves access at the vendor. No key is copied between screens.
Where a vendor hands out keys instead, the key path takes one through `collect_secret` and
the agent never sees it.

## why this shape

A vended account is the firm's isolation boundary, and the same account holds the firm's
OAuth clients and tokens in AgentCore Identity. A second gateway per gerp holds one target per
installed vendor; every caller of that gateway is the firm — one client-credentials app
client per gerp on the operator's Cognito pool — so a vendor grant is the firm's, consented
once by an owner, and an automated turn holds it as an owner's turn does. Every call the agent
makes at a vendor is in the gateway's log, beside the ledger those calls produced.

The vendors are sized for this: a per-firm client with a per-firm callback is how every MCP
client registers, and twelve of the fifteen in the catalog take that registration from a POST.
Where a vendor publishes no registration endpoint (Xero, GitHub, HubSpot), the owner makes
the app on the firm's own account there and pastes one url; the client is the firm's all the
same.

## the catalog is the supported list

A firm's need at a vendor in the catalog is the agent's, through that vendor's own tools; the
platform adds no tool of its own for a catalog vendor. `data/providers.json` names each vendor's endpoint, authorization server, registration method,
scopes, tool prefix and which of its tools write. A contributor adds a vendor by PR; the lint
in `tests/mcp/local/test_catalog.py` is the review. Every client is the firm's own on the firm's
own account at the vendor, so a vendor's app review is no concept of the platform's; the guide
says what is known to connect, per vendor.
