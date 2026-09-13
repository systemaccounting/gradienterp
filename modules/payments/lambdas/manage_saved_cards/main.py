"""manage_saved_cards — list, select, delete and forget the cards a payer saved.

A card is a pair, `(cus_…, pm_…)`: a payment method can only be charged against the customer it is
attached to, so both halves travel together or neither works. That shapes everything here.

**The set is Stripe's; the selection is ours.** One Stripe customer holds every card its payer saved,
and listing them is a read against Stripe rather than a table of our own — brand, last4 and expiry
are Stripe's to change and a copy of them goes stale. What we store is which pair gets charged: the
contact's `stripe_customer_id` + `stripe_payment_method_id`, the same two fields
`save_payment_method` writes.

    {op: "list",   contact_id}
        → {customer, own_customer, selected,
           methods: [{id, type, label, brand, last4, exp, fingerprint, customer, origin, selected}]}
    {op: "select", contact_id, payment_method_id, from_customers?, name?, email?, legal_name?}
        → {contact_id, stripe_customer_id, stripe_payment_method_id, fingerprint}
    {op: "delete", contact_id, payment_method_id, used_by: [contact_id, …]}
        → {deleted, cleared_selection}
    {op: "forget", contact_id}
        → {forgotten: customer_id | null}

`fingerprint` is Stripe's `card.fingerprint`: the same value for the same card number under any
customer on this account, and the identifier the gerp-cloud BFF keeps when an account is deleted
(gerp-priors) and checks before a card vends a gerp.

`select` accepts a method attached to a DIFFERENT customer than the contact currently names, and
writes both halves. That is how a gerp uses the account's card: the gerp's contact stores the
account's customer alongside that method, and `charge_saved_method` reads the pair it always reads.
The caller says which customers the subject may draw from — it is the only component that knows
which gerps an account owns — and this refuses anything outside that set.

`delete` detaches at Stripe, which is TERMINAL: a detached payment method cannot be used for a
payment or re-attached to a customer. So it refuses while any contact in `used_by` selects the
method — those are the gerps whose invoices would stop being payable. The subject's own selection is
not a bar: deleting the card you had selected clears the selection, which is the only way to remove
a last card at all.

**`delete` and `forget` are deliberately absent from `schema.json`.** The gateway validates a call
against the schema, so an agent or an automation script asking for either gets a 400 and this
handler never has to know who called. Detaching is terminal — a detached method cannot be
re-attached to a customer — so removing one stays with its owner, through the gerp-cloud BFF, which
invokes this directly and is not bound by the gateway's schema. `list` and `select` are offered
because a script walking a payer's methods after a decline needs both.

`forget` deletes the payer's OWN Stripe customer (`stripe_own_customer_id`) — the card, the billing
address, every method attached — and clears the three ids off the contact. It is the account
deletion's step for the seller's copy of the payer, and nothing else calls it.

Design: modules/payments/AGENTS.md § connecting a processor.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import stripe_api

from aws import client as _aws, log as alog, Failure
from _kinds import STRIPE_CALL_FAILED, INVOKE_FAILED, CONTACT_READ_FAILED, CONTACT_WRITE_FAILED

log = logging.getLogger()
log.setLevel(logging.INFO)

CUSTOMER_ID = os.environ.get("CUSTOMER_ID", "local")
SECRET_PARAM_PREFIX = os.environ.get(
    "SECRET_PARAM_PREFIX", f"/gradienterp/customers/{CUSTOMER_ID}/secrets"
)
STRIPE_API_BASE = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")
STRIPE_KEY_SECRET = os.environ.get("STRIPE_KEY_SECRET", "stripe_billing")
CONTACTS_GET_FN = os.environ.get("CONTACTS_GET_FN", "")
CONTACTS_UPDATE_FN = os.environ.get("CONTACTS_UPDATE_FN", "")
# `select` on a payer nobody has met yet creates the contact, the way save_payment_method does on
# first sight. One contacts lambda answers every op, so the get name covers the put.
CONTACTS_PUT_FN = os.environ.get("CONTACTS_PUT_FN") or CONTACTS_GET_FN

CUSTOMER_FIELD = "stripe_customer_id"          # the SELECTED pair's customer
OWN_CUSTOMER_FIELD = "stripe_own_customer_id"  # this payer's own
METHOD_FIELD = "stripe_payment_method_id"


def _read_secret(name):
    from botocore.exceptions import ClientError
    try:
        return _aws("ssm").get_parameter(
            Name=f"{SECRET_PARAM_PREFIX}/{name}", WithDecryption=True
        )["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            return None
        raise


def _stripe(api_key, path, body=None, method=None):
    """GET when there is no body, POST when there is, or the `method` named. Stripe puts the reason
    in the response BODY, which urllib throws away unless it is read here."""
    data = urllib.parse.urlencode(body or [], doseq=True).encode() if body is not None else None
    req = urllib.request.Request(f"{STRIPE_API_BASE}{path}", data=data,
                                 method=method or ("POST" if data is not None else "GET"))
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Stripe-Version", stripe_api.VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = (json.loads(e.read() or b"{}").get("error") or {})
        raise Failure(STRIPE_CALL_FAILED, path=path, status=e.code, error=err.get("message", ""),
                      code=err.get("code", "")) from None


def _invoke(fn, payload):
    resp = _aws("lambda").invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
    raw = resp["Payload"].read()
    if resp.get("FunctionError"):
        raise Failure(INVOKE_FAILED, fn=fn, error=raw[:400].decode(errors="replace"))
    return json.loads(raw or b"{}")


def _contact(contact_id):
    """The contact row, or `{}` when nothing knows this payer yet. A 404 is the ordinary
    first-time case; any other failure raises rather than being read as an empty row."""
    if not CONTACTS_GET_FN:
        return {}
    out = _invoke(CONTACTS_GET_FN, {"op": "get", "contact_id": contact_id})
    if out.get("statusCode") == 404:
        return {}
    if out.get("statusCode") != 200:
        raise Failure(CONTACT_READ_FAILED, contact_id=contact_id, status=out.get("statusCode"), error=out.get("body"))
    return json.loads(out["body"]).get("contact") or {}


def _create_contact(contact_id, name, email, legal_name="", legal=None):
    """First sight of this payer through `select`: a gerp created against a card the account
    already holds never passes through save_payment_method, which is where the contact was born
    before. Same row, same fields, named by the business and carrying the owner's legal name."""
    fields = {"op": "put", "contact_id": contact_id, "entity_type": "organization",
              "is_customer": True, "name": name or contact_id}
    if email:
        fields["email"] = email
    if legal_name:
        fields["legal_name"] = legal_name
    fields.update(_contact_fields(legal))
    out = _invoke(CONTACTS_PUT_FN, fields)
    if out.get("statusCode") not in (200, 201, 202):
        raise Failure(CONTACT_WRITE_FAILED, contact_id=contact_id, op="put", status=out.get("statusCode"), error=out.get("body"))


def _contact_fields(legal):
    """The contact's email, phone and business address off the legal business profile the create
    screen collected. The form takes one street line; the contact splits number and name."""
    legal = legal or {}
    out = {}
    if legal.get("email"):
        out["email"] = legal["email"]
    if legal.get("phone"):
        out["phone"] = legal["phone"]
    if any(legal.get(k) for k in ("street", "city", "state", "zip", "country")):
        street = (legal.get("street") or "").strip()
        m = re.match(r"^(\S*\d\S*)\s+(.+)$", street)
        addr = {"address_type": "business",
                "street_number": m.group(1) if m else "", "street_name": m.group(2) if m else street,
                "unit": legal.get("unit") or "", "city": legal.get("city") or "",
                "state": legal.get("state") or "", "postal_code": legal.get("zip") or "",
                "country": legal.get("country") or ""}
        out["addresses"] = [{k: v for k, v in addr.items() if v}]
    return out


def _update_contact(contact_id, updates):
    out = _invoke(CONTACTS_UPDATE_FN, {"op": "update", "contact_id": contact_id, "updates": updates})
    if out.get("statusCode") not in (200, 201, 202):
        raise Failure(CONTACT_WRITE_FAILED, contact_id=contact_id, op="update", status=out.get("statusCode"), error=out.get("body"))


def _methods(api_key, customer_id):
    """Every saved method on the customer, of every TYPE.

    Not `?type=card`. Stripe Checkout offers Link alongside the card form and a payer who takes it
    saves a `type=link` method — chargeable off-session exactly like a card, and invisible to a
    card-filtered list. Filtering here would show a payer an empty wallet holding a method, and
    would let the delete guard read a customer as empty when it is not.

    Stripe paginates at 100 and a payer with more than that is not a case worth carrying code for.
    """
    if not customer_id:
        return []
    got = _stripe(api_key, f"/v1/customers/{customer_id}/payment_methods?limit=100")
    return got.get("data") or []


def _display(pm):
    """What a payer needs to recognise their own method. `label` is built HERE rather than in the
    caller because only a card has a brand and a last4 — Link has an email, and a type we have not
    met yet has neither. The card fields stay for a caller that wants them."""
    kind = pm.get("type", "")
    card = pm.get("card") or {}
    exp = (f"{card['exp_month']:02d}/{str(card.get('exp_year', ''))[-2:]}"
           if card.get("exp_month") else "")
    if card:
        label = f"{card.get('brand', 'card')} ····{card.get('last4', '')}"
    elif kind == "link":
        label = f"Link · {(pm.get('link') or {}).get('email', '')}".rstrip(" ·")
    else:
        label = kind or "saved method"
    return {"id": pm.get("id"), "type": kind, "label": label,
            "brand": card.get("brand", ""), "last4": card.get("last4", ""), "exp": exp,
            "fingerprint": card.get("fingerprint", "")}


def _ok(body, code=200):
    return {"statusCode": code, "body": json.dumps(body)}


def handler(event, context):
    body = event if isinstance(event, dict) and "op" in event else json.loads(
        event.get("body") or "{}"
    )
    op = (body.get("op") or "").strip()
    contact_id = (body.get("contact_id") or "").strip()
    if op not in ("list", "select", "delete", "forget"):
        return _ok({"error": "op must be list, select, delete or forget"}, 400)
    if not contact_id:
        return _ok({"error": "contact_id required"}, 400)

    try:
        api_key = _read_secret(STRIPE_KEY_SECRET)
        if not api_key:
            return _ok({"error": f"no '{STRIPE_KEY_SECRET}' secret; needs Payment Methods: read, and "
                                 "write to detach"}, 400)

        contact = _contact(contact_id)
        selected = contact.get(METHOD_FIELD) or ""

        if op == "list":
            # EVERY customer this payer can be charged against, not just the selected one. Their own
            # (`stripe_own_customer_id`) plus whoever the selection points at — a gerp billed to a card
            # its account owner saved names that owner's customer here. A caller falling back after a
            # decline needs the union: with one customer, "try the next card" silently means "the next
            # card on whichever set was chosen last".
            own = contact.get(OWN_CUSTOMER_FIELD) or ""
            sel_customer = contact.get(CUSTOMER_FIELD) or ""
            methods, seen = [], set()
            for cid, origin in ((own, "own"), (sel_customer, "selected")):
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                methods += [{**_display(pm), "customer": cid, "origin": origin,
                             "selected": pm.get("id") == selected}
                            for pm in _methods(api_key, cid)]
            # `selected` rides on its own as well as per row: it can name a method on a customer this
            # payer no longer draws from, or one the issuer removed at the processor's end.
            return _ok({"customer": sel_customer, "own_customer": own,
                        "selected": selected, "methods": methods})

        if op == "forget":
            # The payer's own customer, never the selected one: a gerp billed to its owner's card names
            # the owner's customer in `stripe_customer_id`, and forgetting the gerp must not take the
            # owner's card with it.
            own = contact.get(OWN_CUSTOMER_FIELD) or ""
            if not own and not contact.get(CUSTOMER_FIELD):
                return _ok({"forgotten": None})
            if not own:
                # a contact written before the split holds one field, and it is its own
                own = contact.get(CUSTOMER_FIELD)
            try:
                _stripe(api_key, f"/v1/customers/{own}", method="DELETE")
            except Failure as e:
                if e.fields.get("status") != 404:
                    raise
            _update_contact(contact_id, {CUSTOMER_FIELD: "", OWN_CUSTOMER_FIELD: "", METHOD_FIELD: ""})
            log.info(f"forgot customer {own} for contact {contact_id}")
            return _ok({"forgotten": own})

        method_id = (body.get("payment_method_id") or "").strip()
        if not method_id:
            return _ok({"error": "payment_method_id required"}, 400)

        if op == "select":
            # Which customers this contact may draw from. Its own is always allowed; anything else has
            # to be named by the caller, which is what stops a gerp being pointed at a card it does not
            # own. A method is only chargeable against the customer it hangs off, so both are written.
            allowed = [c for c in ([contact.get(CUSTOMER_FIELD)] + list(body.get("from_customers") or []))
                       if c]
            owner, chosen = next(((c, pm) for c in allowed for pm in _methods(api_key, c)
                                  if pm.get("id") == method_id), (None, None))
            if not owner:
                return _ok({"error": f"{method_id} is not a saved card this contact can charge"}, 404)
            if not contact:
                _create_contact(contact_id, body.get("name"), body.get("email"), body.get("legal_name") or "",
                                body.get("legal"))
            _update_contact(contact_id, {CUSTOMER_FIELD: owner, METHOD_FIELD: method_id})
            log.info(f"contact {contact_id} selected {method_id} on {owner}")
            return _ok({"contact_id": contact_id, CUSTOMER_FIELD: owner, METHOD_FIELD: method_id,
                        "fingerprint": (chosen.get("card") or {}).get("fingerprint", "")})

        # delete. The guard reads OUR rows, which is what makes it exact: the selection is the thing we
        # store rather than a thing inferred from Stripe.
        holders = [c for c in (body.get("used_by") or []) if c and c != contact_id]
        for other in holders:
            if _contact(other).get(METHOD_FIELD) == method_id:
                return _ok({"error": f"{other} is billed to this card — select another for it first",
                            "used_by": other}, 409)

        _stripe(api_key, f"/v1/payment_methods/{method_id}/detach", [])
        cleared = selected == method_id
        if cleared:
            # Its own selection is not a bar, or a last card could never be removed. Cleared rather
            # than repointed: which card replaces it is the payer's choice, not a guess made here.
            _update_contact(contact_id, {METHOD_FIELD: ""})
        log.info(f"detached {method_id} for contact {contact_id}"
                 f"{' and cleared its selection' if cleared else ''}")
        return _ok({"deleted": method_id, "cleared_selection": cleared})
    except Exception as e:  # noqa: BLE001 — a raise here is a FunctionError the BFF forwards verbatim
        alog.exception("payment method op failed", op=op, contact_id=contact_id)
        return _ok({"error": str(e), **getattr(e, "fields", {})}, 502)
