# connecting a vendor's own tools (Stripe, Square, Linear, Notion, Xero and more)

When the owner names a system the firm runs on, you can install that vendor's own tools and use
them yourself — read the Stripe balance, list the Linear issues, file the Xero invoice. The
owner does one thing: approves access at the vendor. No key is copied between screens.

`manage_mcp {op: list}` shows what is installed and every vendor the catalog offers, each with
a line saying what it is for and whether it takes a key. The names: stripe, square, paypal,
xero, notion, linear, atlassian, github, hubspot, gong, zapier, canva, figma, dropbox, sentry.

## what is known per vendor

Every client is the firm's own, on the firm's own account at the vendor. The vendor decides
what it grants at its approval screen; nothing here is reviewed or vouched for by the platform.
What has been seen to work:

| vendor | seen |
|---|---|
| Stripe | connected on gradienterp: both approvals, the balance and account reads, the webhook made through its tools. The approval screen picks an environment (live, sandbox, test) and a permission level per resource |
| Linear | connected on gradienterp: both approvals, workspace and issues read. Linear does not delete its clients on uninstall |
| Square, PayPal, Notion, Atlassian, Gong, Zapier, Canva, Figma, Dropbox, Sentry | the registration and the approval endpoints answer as the catalog says; no firm has connected one yet. Tell the owner it is the first time and read them whatever the vendor's screen says |
| GitHub | connected on gradienterp through the owner's own OAuth app: both approvals, a repo and its commits read. GitHub's app form also wants a Homepage URL; its redirect field is "Redirect URI"; leave "Expire user access tokens" on. To delete the app after an uninstall: the app's page (github.com/settings/developers → OAuth Apps → the app) → **Advanced** → Delete application; General only deletes the client secret |
| Xero, HubSpot | the owner makes an app at the vendor first (no registration endpoint): the section below. No firm has connected one yet |

## the install, step by step

1. Ask the owner one question before installing: **may the agent write to this vendor, or read
   only?** That is `write` on the install. Read-only keeps the vendor's write tools off your
   list; the owner can change it by uninstalling and installing again.
2. `manage_mcp {op: install, provider, write}`. The answer carries `consent_url`. **Put that link
   in your reply** and ask the owner to open it and approve. It is good for about fifteen
   minutes. Say what the vendor will show: its own sign-in and an approval screen naming
   "gradientERP <firm>".
3. The owner approves and lands back on gradienterp.cloud, which finishes the connection. You
   can check with `manage_mcp {op: status, provider}`: `target_status: READY` means the vendor's
   tools are yours from the next turn, as `<vendor>___<tool>` (`linear___get_issue`). When the
   firm has many vendor tools installed they come as two tools instead: `search_vendor_tools`
   (say the task, get the matching names) and `call_vendor_tool` (run one by name).
4. **Your first call to a newly connected vendor answers with a second link**, worded "needs
   the owner's approval before this works". This is expected, once per vendor: the first link
   let the platform read the vendor's tool list, the second is the firm's own grant. Put the
   link in your reply the same way, wait for the owner, and call again. After that every call
   works, in your turns and in the firm's automations alike.

If a link lapses (the owner took longer than fifteen minutes), `manage_mcp {op: status}`
reissues the first kind; calling the tool again reissues the second kind.

## when the owner makes the app (Xero, GitHub, HubSpot)

These three vendors hand out no client on request; the owner makes an app in the vendor's own
dashboard, on the firm's own account there, and the install runs in two halves.

1. `manage_mcp {op: install, provider, write}` — the first half. The answer carries
   `callback_url`, `new_app_url`, `callback_field` and `client_secret_name`. No consent link yet.
2. Tell the owner, in one message: open `new_app_url` signed in to the firm's own account
   there, create a new app named "gradientERP <firm>", paste `callback_url` into the field the
   vendor calls `callback_field`, save, and copy the app's client id and client secret. For
   Xero the app type is **Web app**; the scopes are asked for at approval, not on the app. GitHub
   also asks for a Homepage URL: `https://gradienterp.cloud`.
3. The secret goes through `collect_secret` under the name `client_secret_name` from step 1
   (`xero_client_secret`). The client id is not a secret: the owner tells you it in the chat.
4. `manage_mcp {op: install, provider, client_id, client_secret_name}` — the second half. From
   here it is the install above from its step 2: the answer carries `consent_url`, then the
   second link on the first call.

Running the first half again before the second answers the same `callback_url`; nothing is
made twice. On uninstall the app stays at the vendor; the owner deletes it there if they
want it gone.

## the key path

Stripe, GitHub and Zapier also take a key instead of the approval flow. The owner submits it
through `collect_secret` under a name you agree on, then `manage_mcp {op: install, provider,
secret_name}`. No consent, the tools are yours once `status` says READY. A vendor with no key
path refuses `secret_name` and says so.

## what the answers mean

- **install answers 409 "already installed"** — it is; `status` says where it stands.
- **install answers 404 "install {provider} first"** — the second half ran before the first;
  run `install {provider}` and hand the owner the callback.
- **install answers 400 "registers its own client"** — `client_id` was sent for a vendor that
  registers on its own; install without it.
- **the vendor refuses the approval with "invalid redirect uri" or "invalid client"** — the
  callback pasted into the owner's app differs from `callback_url`, or the client id was
  mistyped. The owner checks the app; `uninstall` and both halves again.
- **install answers 502 "refused the client registration"** — the vendor said no to the
  registration; the message carries what it said. Report the vendor's words. Try once more
  later; file a task only if it holds.
- **status shows FAILED with "authorization timed out"** — the link lapsed. `status` has
  already reissued it; send the new `consent_url`.
- **a tool answers "needs the owner's approval"** — step 4 above. This is not an error and not
  a bug: send the link.
- **a tool answers with a vendor's error** — the vendor's message is in the text; read it to the
  owner as it is. A 401 from the vendor means the grant was revoked at the vendor: `uninstall`
  and `install` again.

An install or a call that fails twice in a row with the same platform error (not a vendor's
refusal, not a consent) is worth one task. Say what the error text was. Do not tell the owner
"nothing you can do" — the two links above are always theirs to click.

## removing a vendor

`manage_mcp {op: uninstall, provider}` removes the tools, the credential and the grant here.
The owner can also revoke the connection in the vendor's own settings; if they do, the next
call answers 401 and you uninstall.
