# gradientERP, for your agent

this file teaches an agent how to use gradientERP: what it does, module by module, and how a person asks
for it. someone using gradientERP, or deciding whether to, hands their agent this url. an agent working on
gradientERP's own code wants the root `AGENTS.md` instead

## what gradientERP is

gradientERP runs a business through the business's own AI agent. the owner talks to that agent in plain
language, and it keeps the books, sends invoices, takes payments, orders stock, schedules and pays people,
files documents, and trades with other businesses on gradientERP. the screens are sign-up and the agent's
chat

- **openly operated by default** — a business's books publish to openlyoperated.biz, where capital and
  engineers can see where it earns and where it leaks. the owner can turn that off. the business's economic
  activity counts toward the public indicators either way, with no business named, and people's
  information never publishes (see [openly operated](#openly-operated))
- **price** — the AWS cost of running the business's own instance, times 1.2, metered per business. an
  owner getting started may pay about $13–31 a month. the agent's model use is most of it, so the bill
  follows how much the agent does
- **where it runs** — each business gets its own AWS account in the region for its country: US East
  (Virginia), Europe (Ireland, Frankfurt, London), Asia Pacific (Singapore, Tokyo, Sydney, Mumbai)

## getting started

the person does these, not their agent:

1. sign up at https://gradienterp.cloud and confirm the email
2. fill in the account record (name, contact, address)
3. create a gerp — one business's instance: its name, legal details, country (which picks the region),
   whether it's openly operated, and which public details show
4. put a card on file for it
5. wait about 25 minutes; an email says when it's ready

## how you use it

open the gerp from gradienterp.cloud and talk to its agent in the chat, or email it at
`agent@<gerp_id>.agents.gradienterp.cloud` from the owner's address. ask for outcomes, the way you'd ask a
bookkeeper, an office manager or an analyst:

- "record the $240 check from Ken's Cafe against invoice 1042"
- "what did we spend on supplies last month, by vendor"
- "reorder oat milk when we're down to two cases"
- "pay the team for last week's hours"
- "make a form my staff can use to log their hours"

the agent asks when it needs a decision, a credential or a document. the chat answers the owner today;
employees, customers and vendors talking to it are open work

## modules and features

one entry per module every gerp is built with. each says what it does for a business, a few things to
ask, and what in it is still open work

### accounting — `modules/accounting`

keeps the books and refuses any entry that doesnt balance. produces the balance sheet, income statement,
cash flow and trial balance on request or on a schedule, matches bank deposits against the books, and
brings in books from a previous system

- ask: "income statement for last quarter" · "connect my bank account" · "move my books over from QuickBooks as of january 1"
- not yet: connecting a real bank waits on Plaid's production approval

### the agent — `modules/agent`

the business's own AI agent, reached by web chat or by email from approved senders. it remembers what each
person tells it, searches the web, works websites with no API in a recorded browser, looks up shared
standards like GAAP, and emails from its own address

- ask: "remember that we close on mondays" · "download our invoices from the supplier's portal" · "email this statement to our accountant"
- not yet: web chat answers the owner only, not staff, customers or vendors

### agreements — `modules/agreements`

negotiates purchase orders and capital offers with other gradientERP businesses: send, accept, counter or
decline. rules you set can answer incoming proposals for you, like accepting under a dollar limit or when
the stock is on hand. an agreed deal settles itself

- ask: "send Acme Roasters a PO for 20 bags at $12" · "decline the offer from Northside" · "auto-accept purchase orders we have stock for"

### assets — `modules/assets`

a register of equipment the business owns or services: make, model, serial, location, warranty, status.
adding a purchase with its cost books it in the ledger, and repairs attach to the asset as tasks

- ask: "add the new espresso machine, $8,400 cash" · "list equipment at the second location" · "retire the old van"
- not yet: depreciation, and a gain or loss entry on retirement

### automation — `modules/automation`

scripts the agent writes that run on their own: on a schedule, as a timed sequence, or when another system
calls a private url. a separate review approves each script before it runs, and a failure opens an
incident and tells you

- ask: "remind overdue customers every three days for two weeks" · "give me a url our form service can call" · "what automations are running"
- not yet: starting an automation when something happens in the business

### calendar — `modules/calendar`

holds dated commitments like appointments and inspections, read back by date range, and sets timers that
wake the agent at a set time or on a repeating schedule

- ask: "put the health inspection on my calendar for august 14 at 10am" · "whats on my calendar next week" · "remind me every friday at 4pm to count the drawer"
- not yet: repeating calendar entries, like a weekly staff meeting

### command shell — `modules/cmd`

a linux shell with internet where the agent runs scripts and installs the command line tools it needs,
kept for next time. scripts use credentials you enter through the secure form. saved scripts and long
outputs land in the filing cabinet

- ask: "open a GitHub issue for this bug" · "pull last month's orders from our vendor's API" · "check when our SSL certificate expires"
- not yet: a run stops at 15 minutes

### contacts — `modules/contacts`

the customers, vendors and employees the business deals with: contact details, tax id, vendor payment
terms, customer credit limits, employee hire date and rate. invoicing, purchasing and payroll use these
records. a contact is named in published data only when linked to their own public gradientERP profile

- ask: "add Blue Bottle Wholesale as a vendor, net 30" · "update Maria Lopez's phone to 555-0142" · "list our vendors"
- not yet: deleting a contact

### events — `modules/events`

how the modules and the businesses tell each other what happened. a posted journal entry, an accepted
order or a paid distribution goes out as an event: to this business's own modules, to the other
gradientERP business it's addressed to, and, while the business is openly operated, to its live stream on
openlyoperated.biz with the fields that name people removed. theres nothing here to ask for directly; it's
what brings another business's order to the inbox and the business's activity to its public page

- not yet: events for stock moves, price changes, clock-ins and issued invoices

### export — `modules/export`

copies the business's records out: the books (the ledger also as CSV), invoices, purchase orders, contacts,
inventory, labor, settings, rules and documents. the owner gets a link to a download script that needs no
setup; the link lasts an hour and is reissued on request for the 30 days the export is kept

- ask: "export all our data" · "export just our books and contacts" · "send me a fresh download link"
- not yet: a narrowed export still copies every stored document

### inbox — `modules/inbox`

where messages from other businesses' agents land: purchase orders, quote requests, shipment notices,
payments. a proposal the owner's standing rules answer (accept what is in stock) needs no agent turn.
anything else wakes the agent, which brings it to the owner and sends back the owner's answer

- ask: "any new orders from other businesses?" · "what proposals are waiting on me?" · "what did Westwood send this week?"
- not yet: choosing which kinds of message wake the agent

### inventory — `modules/inventory`

the catalog of what the business sells and uses. stock is received, sold and counted, and a reorder rule
raises a purchase order below par. recipes build goods from components, even at the moment of sale.
bookable units (a room, a bay) take one-off or recurring bookings

- ask: "we counted 14 lb of beans at close" · "each doppio uses 18 g of beans" · "book room 101 June 3 to 6"
- not yet: a pool of identical units (parking spaces) as one item

### invoicing — `modules/invoicing`

bills customers and keeps receivables in the books: draft, issue, record payment. taxes and fees come from
rules on catalog items, templates expand a booking into lines (2 nights → room-nights plus cleans), and
revenue counts once paid. an order accepted from another gradientERP business becomes a draft invoice

- ask: "invoice Maria Lopez $450 for June catering, due in 30 days" · "mark invoice 1042 paid" · "which invoices are unpaid?"
- not yet: voids, write-offs and emailed invoice PDFs

### labor — `modules/labor`

workers, pay rates and clock-ins; each clock-out books wages. a pay run for a period withholds federal
income tax, Social Security and Medicare and books the employer's Social Security, Medicare and FUTA, plus
for California workers CA income tax and SDI withheld and CA UI and ETT booked. W-4, DE-4 and I-9
documents are stored with SSNs masked

- ask: "add Alice Chen as a W-2 barista at $20/hr" · "clock Alice out" · "run Alice's June payroll"
- not yet: state payroll taxes outside California, W-2, 941 and 1099 forms, overtime and meal/rest premiums, salaried pay

### vendor connections — `modules/mcp`

connects the business's own accounts at other software vendors so the agent works in them with each
vendor's tools: Stripe, Square, PayPal, Xero, Notion, Linear, Jira, Confluence, GitHub, HubSpot, Gong,
Zapier, Canva, Figma, Dropbox, Sentry. the owner approves access on the vendor's screen, read-only or write

- ask: "connect Linear, read only" · "what's our Stripe balance?" · "disconnect Dropbox"

### notes — `modules/notes`

notes on a contact, invoice, purchase order or journal entry, found by what they are about. every edit is
kept as a new version, and the names and amounts inside a note stay out of anything published

- ask: "note that Dana wants invoices on the 1st" · "any notes on invoice 1042?" · "update the landlord note"
- not yet: viewing a note's earlier versions

### payments — `modules/payments`

connects Stripe, Square or PayPal so sales, refunds and payouts post to the books on their own (PayPal:
payments and refunds). on Stripe the agent also sends invoice payment links and card-saving links, and
charges a saved card, by request or when an invoice is issued

- ask: "connect our Stripe" · "send Acme a payment link for invoice 1042" · "charge Acme's saved card for it"
- not yet: payment links and saved cards on Square and PayPal; moving the Stripe balance to the bank

### setup guides — `modules/playbooks`

how-to guides the agent looks up and follows when a task needs one: connecting payment processors and
vendors, onboarding, moving existing books in, stock recipes and counts, booking capacity, shipping and
receiving, equipment, the owner portal, locations and job tags, invoice tags, capital agreements,
automations, browsing websites

- ask: "walk me through moving my books from QuickBooks" · "how do I connect Square?" · "help me set up job tags"

### purchasing — `modules/purchasing`

purchase orders, the bill when goods arrive (stocked items go onto the shelf count), and the payment. with
a vendor also on gradientERP, the agent requests a quote and sends a PO the vendor accepts or counters

- ask: "PO to Main St Dairy for 40 gallons of milk" · "the milk arrived" · "get a quote from Northside Roasters"
- not yet: a spending limit that holds large orders for the owner's approval

### rules — `modules/rules`

settings the business attaches to how things happen, with no code: sales tax, tips or fees on items sold;
payroll taxes; ingredients deducted per sale; automatic reorders; auto-charging saved cards on invoices;
accepting incoming orders under a price cap; starting its own automations

- ask: "add 7.25% sales tax to everything" · "deduct 18 g of beans per doppio sold" · "turn off the delivery fee"
- not yet: rules that flag a problem, like an expiring license

### custom accounts and fields — `modules/schemas`

adds accounts to the business's chart of accounts and its own fields to contacts, items, workers, notes,
tasks, assets and shipments, like a part's fitment or bin. it also adopts new entries from the platform's
standard list

- ask: "add a Tips Revenue account" · "track fitment and bin on our parts" · "anything new in the standard accounts?"
- not yet: new standard entries arrive when you ask; the weekly check is off

### secrets — `modules/secrets`

stores the business's API keys, tokens and logins. the agent opens a secure field in the chat, the value
goes into the business's own vault, and the agent only ever holds the name it uses to put that credential
to work

- ask: "add my stripe secret key" · "what credentials do you have stored?" · "replace my paypal client secret" · "delete the old square token"
- not yet: a one-time setup key stays stored after use until you ask the agent to delete it

### business web address — `modules/server`

the business's own web address. Stripe, Square and PayPal send sales, refunds and payouts to it so they
book themselves, the agent gives a vendor a private hook address that runs one of the business's scripts,
and while the business is openly operated anyone reads its financials, metrics and inventory there

- ask: "what's my webhook address for square?" · "check that my stripe webhook is live" · "what does my business publish right now?"
- not yet: webhooks from POS, payroll, spend-card or accounting-software providers beyond Stripe, Square and PayPal

### settings — `modules/settings`

the business's standing configuration: its timezone (which decides the month a sale lands in and when
schedules run), its locations, and standing instructions the agent follows on every turn. openly operated
and the owner's notification email are set on the settings screen

- ask: "set my timezone to America/Chicago" · "we opened a second location on elm street" · "from now on, always put rush orders first" · "what locations do we have?"
- not yet: staff accounts with their own settings; only the owner has one

### shipping and receiving — `modules/shipping`

one record per shipment for the receiving dock, the office mailroom and outgoing orders: who signed, what
condition it arrived in, carrier and tracking, and the freight cost booked to the ledger. tracking links
are generated for FedEx, UPS, USPS and DHL. when a supplier on gradientERP ships, the expected delivery
appears on its own

- ask: "3 pallets from sysco arrived, dana signed, one box crushed" · "a package came for jordan, hold it at the desk" · "shipped invoice 1042 by ups, tracking 1Z999, $18 freight" · "what's in transit or waiting for pickup?"
- not yet: buying labels, rate shopping, and live tracking updates from carriers

### filing cabinet and portal — `modules/storage`

files receipts, contracts and photos, finds them later, and hands back a download link. it reads receipts
(vendor, total, tax, date, line items) so expenses book from the printed numbers. the agent also publishes
pages that show live data, and forms with their own link for staff or customers; submissions and uploads
come back to the agent

- ask: "make a receipt upload form and book what comes in" · "pull up the W-9 from acme" · "make a page of my open tasks" · "make a form for staff to log hours and give me the link to share"
- not yet: reading documents other than receipts, multi-page scans, form uploads over about 4.5MB, and handling submissions without being asked

### tasks — `modules/tasks`

the business's work queue: tasks with due dates, a forecast of when each will be done, subtasks (a parent
cant close while a subtask is open), tags, and a history of every change. assigning a task to the agent
hands it over. the agent also files bug and feature reports to gradientERP with the business's details
kept private

- ask: "add a task to fix the walk-in cooler door by friday" · "what's still open, oldest first?" · "break the smith remodel into subtasks" · "report that invoice pdfs are cutting off the last line"
- not yet: a task shows under its customer, invoice or PO only when it has a due date

### treasury — `modules/treasury`

raises capital with no shares or votes. an investor pays in and holds a distribution rule: a percentage of
net income, optionally capped at a lifetime total. offers are proposed and accepted, and become active once
the cash is on the books. at each period close the payouts owed to every holder are booked

- ask: "offer 10% of net income until $750k is returned, for $250k" · "record the $50k that came in from dana for her offer" · "show my open and accepted offers" · "who holds distribution rules on the business?"
- not yet: scheduled interest-style payments, buying out a holder, and listing offers publicly for investors

## connecting what you already use

- **payment processors** — Stripe, Square and PayPal connect through the agent; the payments entry says
  what each does
- **your bank** — not yet: reconciling the books against a bank feed through Plaid is built, and
  connecting a real bank waits on Plaid's production approval
- **other software you have an account with** — the agent connects to vendors' MCP servers (vendor
  connections above) and works in them as the business, after you approve the connection on the vendor's
  own screen
- **software that needs to call the business** — the agent publishes a url with its own token for another
  system to post to (automation above)

## openly operated

- **always public** — the business's economic activity, summed with every other business's into the
  indicators on openlyoperated.biz (revenue, expense, margin), with no business named
- **public when openly operated** — the business page on openlyoperated.biz: its published name and place,
  statements, metrics, inventory, and its events as they happen, each read from the business's own
  instance when someone looks
- **never public** — people's information: employees', contractors' and customers' names and contact
  details, legal and tax records, and every secret and credential

an agent reading the economy, as opposed to running a business, starts at
https://api.openlyoperated.biz/v1/llms.txt: the directory of businesses, each one's published reads, the
economy's counters, and the live stream

## what your agent cant do for you

these are the person's:

- **creating the account** — sign-up and the emailed code happen at gradienterp.cloud
- **the card** — it's entered on Stripe's own page
- **approvals on other sites** — connecting a vendor or a bank, or approving a Stripe change, happens on that
  company's screen; the agent sends the link
- **secrets** — API keys and logins go through the secure form the agent opens, so the value stays out of
  the conversation
