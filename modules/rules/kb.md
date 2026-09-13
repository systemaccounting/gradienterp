# attaching an automation — the mechanics

A rule is a function the platform ships. A **rule instance** is a row saying "run that one, here,
with these settings". The row is the whole act of turning an automation on; there is no other switch,
no deploy, and nothing seeded. A business that owes no sales tax simply has no tax row.

    rule_params op=get catalog=true          what is on offer
    rule_params op=get catalog=true callsite=…   what is on offer HERE
    manage_rules op=list                     what this firm already has on
    manage_rules op=add                      turn one on
    manage_rules op=delete                   turn it off

## the key says when it fires

`matches` is `<CALLSITE>#<subject>`, and the callsite half names the moment. So the key reads as the
answer to "when should this happen":

    PAY_RUN#<contact_id>          when this worker's pay is run
    CLOSE_SHIFT#<contact_id>      when their shift closes
    INVOICE_LINE#<catalog_key>    when this is added to an invoice — a tax, a tip, a fee
    INVOICE_LINE#*                ...on everything sold
    INVOICE_STATUS#issued         when an invoice reaches a status
    INVOICE_TAG#disputed          when a tag goes on   (…#removed when it comes off)
    STOCK_SOLD#<catalog_key>      when stock moves on a sale — the backflush
    REORDER#<catalog_key>         when the reorder loop reads a par level
    STOCK_ADJUSTED#*              when a count adjustment lands
    ITEM_CREATED#*                when an inventory item is created
    INVOICE_TEMPLATE#<name>       when a template expands into its items
    DISTRIBUTION#<instrument_id>  when an instrument's period runs

**One catalog item appears three times, and they are three different automations.** A tax on beans,
a backflush that burns beans, and a par level for beans are three moments, so three keys. Attaching
a tax to `STOCK_SOLD#` would be a row that never fires.

`manage_rules` op=add refuses a rule that cannot run where you are attaching it, and the refusal names where
that key IS read — so a wrong guess costs a turn, not a silent dead row. `rule_params op=get
catalog=true callsite=<name>` lists what fits a given moment.

`ITEM_TRANSITION#<what an item credits>#<state>` is the exception: what collecting cash means is not
a firm's config, so those answer from code and there is nothing to attach.

## the shape of a row

    manage_rules op=add matches="INVOICE_LINE#*" rule="multiply_item_value" name="ca_sales_tax" n=300
             param={"factor": 0.0725, "creditor": "SALES_TAX_PAYABLE", "name": "CA sales tax"}

- **`rule`** — from the catalog. Never invent one.
- **`name`** — what the FIRM calls this use of it. `ca_sales_tax`, not `multiply_item_value`. It is
  how the owner will refer to it and how you will find it again.
- **`n`** — the order it runs in when several match the same key. A city tax after a state tax. Leave
  it at the default unless order matters.
- **`param`** — exactly what the catalog's spec lists. A param with no default is required; the spec
  is read off the function, so it cannot be out of date.

Writing the same `(matches, n, name)` again replaces the row. That is how you edit one.

## before you offer

**Check `manage_rules` op=list first.** It shows what is attached, including built-in defaults, so you never
offer something already on. Offer in the owner's terms — *"want me to deduct beans automatically
every time a doppio sells?"* — and write the row only on their yes.

**Read the domain guide for the specific rule.** This is the mechanism; each rule has its own setup
in the module's own guide (sales tax and reorder points in inventory's, count-variance valuation in
accounting's, collection in invoicing's). Search for it rather than improvising the params.

## it is reversible, and that is worth saying out loud

`manage_rules` op=delete with the same `matches` and `name` turns it off. On a key with a built-in default the
default resumes. Nothing already posted changes — a period that ran is frozen in the ledger, and the
config store keeps no history because it does not need one.

Owners hesitate over automation because it feels permanent. It is a row.
