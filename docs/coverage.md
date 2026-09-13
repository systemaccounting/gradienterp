# erp coverage

modules provide lambdas + terraform infra and emit spec-compliant events; agents and other modules subscribe. internal lambda interfaces are documented by each lambda's `main.py` + `schema.json` + the module's `AGENTS.md`, which the agent reads directly. the tables below are feature-first: every row is live on the operator's own tenant; an empty cell is open work, tracked in [`modules/TODO.md`](../modules/TODO.md) + module TODOs — the books are open here too.

## financial management

| feature                                   | modules/                                                                                               |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| general ledger                            | [accounting](../modules/accounting/)                                                                   |
| accounts payable / accounts receivable    | [purchasing](../modules/purchasing/) · [invoicing](../modules/invoicing/)                              |
| AP payment execution (bill payment runs)  | [purchasing](../modules/purchasing/)                                                                   |
| invoice & receipt capture (AP automation) | [storage](../modules/storage/)                                                                         |
| intercompany transactions                 | [purchasing](../modules/purchasing/) · [invoicing](../modules/invoicing/) · [inbox](../modules/inbox/) |
| fixed asset management                    | [assets](../modules/assets/)                                                                           |
| cash & treasury management                | [treasury](../modules/treasury/)                                                                       |
| budgeting, planning & forecasting         | [rules](../modules/rules/) · [calendar](../modules/calendar/) · [agent](../modules/agent/)             |
| financial consolidation & close           |                                                                                                        |
| multi-currency & multi-entity accounting  |                                                                                                        |
| tax management & compliance               | [rules](../modules/rules/) · [agent](../modules/agent/)                                                |
| expense management                        |                                                                                                        |
| billing & invoicing                       | [invoicing](../modules/invoicing/)                                                                     |
| recurring / subscription billing          | [invoicing](../modules/invoicing/) · [calendar](../modules/calendar/)                                  |
| revenue recognition                       |                                                                                                        |
| financial reporting & statements          | [accounting](../modules/accounting/)                                                                   |
| bank reconciliation                       | [accounting](../modules/accounting/)                                                                   |
| credit & collections management           |                                                                                                        |

## supply chain & procurement

| feature                                                    | modules/                                                                |
| ---------------------------------------------------------- | ----------------------------------------------------------------------- |
| purchase order management                                  | [purchasing](../modules/purchasing/)                                    |
| supplier/vendor management                                 | [contacts](../modules/contacts/)                                        |
| sourcing & RFQ management                                  | [purchasing](../modules/purchasing/)                                    |
| contract management                                        | [agreements](../modules/agreements/)                                    |
| goods receiving & three-way match (PO ⇄ receipt ⇄ invoice) | [purchasing](../modules/purchasing/) · [shipping](../modules/shipping/) |
| requisition & approval workflows                           | [agent](../modules/agent/)                                              |
| supplier portals                                           | [agent](../modules/agent/) · [inbox](../modules/inbox/)                 |
| landed cost tracking                                       |                                                                         |
| demand planning & forecasting                              |                                                                         |
| supply chain analytics                                     | [server](../modules/server/) · [events](../modules/events/)             |

## inventory & warehouse management

| feature                                                                                 | modules/                                                              |
| --------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| inventory control & tracking                                                            | [inventory](../modules/inventory/)                                    |
| warehouse management (WMS)                                                              |                                                                       |
| barcode/RFID scanning                                                                   |                                                                       |
| lot & serial number tracking                                                            |                                                                       |
| bin/location management                                                                 |                                                                       |
| cycle counting & physical inventory                                                     | [inventory](../modules/inventory/) · [rules](../modules/rules/)       |
| reorder point & safety stock management                                                 | [inventory](../modules/inventory/)                                    |
| multi-warehouse support                                                                 | [settings](../modules/settings/) · [inventory](../modules/inventory/) |
| pick, pack & ship workflows                                                             | [shipping](../modules/shipping/)                                      |
| kitting & bundling                                                                      | [inventory](../modules/inventory/)                                    |
| reservations & booking (rooms, chairs, bays, a person's shifts — capacity as inventory) | [inventory](../modules/inventory/)                                    |

## manufacturing / production

| feature                                              | modules/                                                                  |
| ---------------------------------------------------- | ------------------------------------------------------------------------- |
| bill of materials (BOM)                              | [inventory](../modules/inventory/)                                        |
| material requirements planning (MRP)                 |                                                                           |
| master production scheduling (MPS)                   |                                                                           |
| work orders & job tracking                           | [tasks](../modules/tasks/)                                                |
| shop floor control                                   |                                                                           |
| capacity planning                                    |                                                                           |
| quality management (QMS)                             |                                                                           |
| product lifecycle management (PLM)                   |                                                                           |
| engineering change management                        |                                                                           |
| maintenance management (CMMS/EAM)                    | [assets](../modules/assets/) · [tasks](../modules/tasks/)                 |
| costing (standard, actual, job costing)              | [accounting](../modules/accounting/) · [inventory](../modules/inventory/) |
| discrete, process & mixed-mode manufacturing support |                                                                           |

## sales & order management

| feature                                      | modules/                                                                  |
| -------------------------------------------- | ------------------------------------------------------------------------- |
| quote/estimate management (CPQ)              | [agent](../modules/agent/) · [invoicing](../modules/invoicing/)           |
| sales order processing                       | [purchasing](../modules/purchasing/) · [invoicing](../modules/invoicing/) |
| pricing & discount management                | [rules](../modules/rules/)                                                |
| returns management (RMA)                     | [payments](../modules/payments/)                                          |
| commission tracking                          |                                                                           |
| backorder management                         |                                                                           |
| order promising / ATP (available to promise) | [inventory](../modules/inventory/)                                        |

## customer relationship management (CRM)

| feature                            | modules/                                                                                           |
| ---------------------------------- | -------------------------------------------------------------------------------------------------- |
| lead & opportunity management      | [agent](../modules/agent/) · [contacts](../modules/contacts/) · [notes](../modules/notes/)         |
| contact & account management       | [contacts](../modules/contacts/)                                                                   |
| sales pipeline tracking            | [agent](../modules/agent/) · [contacts](../modules/contacts/) · [tasks](../modules/tasks/)         |
| marketing automation               |                                                                                                    |
| customer service & case management | [agent](../modules/agent/) · [tasks](../modules/tasks/)                                            |
| field service management           | [tasks](../modules/tasks/) · [inventory](../modules/inventory/) · [calendar](../modules/calendar/) |

## human resources / HCM

| feature                               | modules/                                                        |
| ------------------------------------- | --------------------------------------------------------------- |
| employee records & HR administration  | [labor](../modules/labor/)                                      |
| payroll                               | [labor](../modules/labor/) · [rules](../modules/rules/)         |
| time & attendance                     | [labor](../modules/labor/)                                      |
| benefits administration               |                                                                 |
| recruiting & applicant tracking (ATS) |                                                                 |
| onboarding/offboarding                | [labor](../modules/labor/) · [agent](../modules/agent/)         |
| performance management                |                                                                 |
| learning management (LMS)             |                                                                 |
| succession planning                   |                                                                 |
| workforce scheduling                  | [labor](../modules/labor/) · [inventory](../modules/inventory/) |
| compensation management               | [labor](../modules/labor/)                                      |

## project management / PSA

| feature                        | modules/                                                                  |
| ------------------------------ | ------------------------------------------------------------------------- |
| project planning & scheduling  | [tasks](../modules/tasks/) · [calendar](../modules/calendar/)             |
| resource allocation            | [inventory](../modules/inventory/) · [labor](../modules/labor/)           |
| project costing & billing      | [accounting](../modules/accounting/) · [invoicing](../modules/invoicing/) |
| time & expense tracking        | [labor](../modules/labor/)                                                |
| milestone tracking             |                                                                           |
| gantt charts & task management | [tasks](../modules/tasks/) · [calendar](../modules/calendar/)             |

## e-commerce & retail

| feature                       | modules/                                                                                           |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| point of sale (POS)           |                                                                                                    |
| online storefront integration | [payments](../modules/payments/)                                                                   |
| product catalog management    | [inventory](../modules/inventory/)                                                                 |
| omnichannel order management  | [agent](../modules/agent/) · [payments](../modules/payments/) · [invoicing](../modules/invoicing/) |

## analytics & reporting

| feature                    | modules/                                                  |
| -------------------------- | --------------------------------------------------------- |
| dashboards & KPIs          | [server](../modules/server/)                              |
| business intelligence (BI) | [agent](../modules/agent/) · [server](../modules/server/) |
| ad hoc reporting           | [agent](../modules/agent/)                                |
| predictive analytics       |                                                           |
| embedded AI/ML insights    | [agent](../modules/agent/)                                |

## governance, risk & compliance

| feature                                 | modules/                                                            |
| --------------------------------------- | ------------------------------------------------------------------- |
| audit trails                            | [accounting](../modules/accounting/) · [events](../modules/events/) |
| regulatory compliance (SOX, GDPR, etc.) |                                                                     |
| document management                     | [storage](../modules/storage/)                                      |
| workflow approvals & internal controls  |                                                                     |
| risk management                         |                                                                     |

## platform / technical features

| feature                                   | modules/                                                                                                                                             |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| role-based access control & security      | [agent](../modules/agent/)                                                                                                                           |
| workflow automation                       | [rules](../modules/rules/) · [automation](../modules/automation/)                                                                                    |
| API & integration tools (EDI, middleware) | [server](../modules/server/) · [events](../modules/events/) · [cmd](../modules/cmd/) · [automation](../modules/automation/) · [mcp](../modules/mcp/) |
| mobile access                             | [agent](../modules/agent/)                                                                                                                           |
| multi-language & localization             |                                                                                                                                                      |
| customization & low-code tools            | [schemas](../modules/schemas/) · [rules](../modules/rules/)                                                                                          |
| notifications & alerts                    | [agent](../modules/agent/) · [calendar](../modules/calendar/)                                                                                        |
| data import/export tools                  | [export](../modules/export/) · [accounting](../modules/accounting/)                                                                                  |

## agent-native & open-books (no generic-list equivalent)

| feature                                                                                                                                                                           | modules/                                                                                                                                                                   |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| conversational interface — chat + email front doors (the owner today; per-role tools are open work)                                                                                  | [agent](../modules/agent/)                                                                                                                                                 |
| headless browser for counterparties with no API (recorded sessions; persistent logins; secrets filled in-tool)                                                                    | [agent](../modules/agent/) · [secrets](../modules/secrets/)                                                                                                                |
| PII & credential intake that bypasses the model — credentials via an in-chat secure form, everything else via portal forms                                                        | [agent](../modules/agent/) · [secrets](../modules/secrets/) · [storage](../modules/storage/)                                                                               |
| durable per-person memory across conversations                                                                                                                                    | [agent](../modules/agent/)                                                                                                                                                 |
| guides retrieved on demand — setup walkthroughs AND the mechanics of a workflow the agent is starting                                                                             | [playbooks](../modules/playbooks/)                                                                                                                                         |
| open books — accounting AND operational data published: dashboards, per-business pages (financials, inventory, utilization), the live economy counters (revenue, expense, margin) | [server](../modules/server/) + per-module `oob` reads                                                                                                                      |
| cross-firm agent commerce (the PO ⇄ invoice exchange; hub-and-spoke a2a) on one agree-and-settle substrate                                                                        | [agreements](../modules/agreements/) · [purchasing](../modules/purchasing/) · [invoicing](../modules/invoicing/) · [inbox](../modules/inbox/) · [agent](../modules/agent/) |
| capital structure as rules (capped distributions — the event stream is the prospectus)                                                                                            | [treasury](../modules/treasury/) · [rules](../modules/rules/) · [agreements](../modules/agreements/)                                                                       |
| self-serve web surfaces the agent authors on the fly — pages it publishes and forms whose submissions land as structured intake (no CMS, no page builder)                         | [storage](../modules/storage/)                                                                                                                                             |
| a shell with internet whose dependencies the agent builds itself — writes a script, hits a missing command, builds the layer, retries                                             | [cmd](../modules/cmd/)                                                                                                                                                     |
| a shared standards corpus — what a jurisdiction requires or a convention defines, contributed by tenants and curated once for the network                                         | [agent](../modules/agent/) + the operator corpus                                                                                                                           |
| spec-compliant event stream (EDI-shaped schemas; every transaction an event)                                                                                                      | [events](../modules/events/)                                                                                                                                               |
