"""Agent email front door — S3 ObjectCreated (in/) → invoke the same-account agent → SES reply.

SES drops the raw .eml in s3://<bucket>/in/<messageId> and this fires. We:
  1. parse the message,
  2. gate it — sender on the allowlist AND DMARC=pass (the allowlist is only a wall if it
     stands on DMARC; `From:` is a plain string), and no automatic message (an out-of-office
     answering the agent's reply would be answered, and answer back, forever),
  3. dedup on Message-ID (SES can redeliver / S3 can double-fire),
  4. invoke this gerp's own AgentCore runtime with a thread-stable session,
  5. SES-reply, threaded, From the agent's subdomain address.

Sandbox: the reply only reaches a *verified* recipient — which is exactly the allowlist, so a
spoofed/unknown sender is dropped before we'd ever try to answer it.
"""
import email
import hashlib
import json
import os
import re
import time
import uuid
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr

from aws import client as _aws_client, resource as _aws_resource, log


BUCKET = os.environ["EMAIL_BUCKET"]
SETTINGS_TABLE = os.environ.get("SETTINGS_TABLE", "")
GERP_ID = os.environ.get("GERP_ID", "")
MAILBOXES_KEY = "GERP#mailboxes"
AGENT_MAILBOX = "agent"     # the one that WAKES the agent; every other one is quiet
IN_PREFIX = "in/"           # mailboxes live here — NOT watched, or filing would re-trigger this
SPAM_MAILBOX = "spam"       # addressed to a local part this firm never declared
DEDUP_TABLE = os.environ["DEDUP_TABLE"]
AGENT_ADDRESS = os.environ["AGENT_ADDRESS"]
ALLOWLIST = {a.strip().lower() for a in os.environ.get("ALLOWLIST", "").split(",") if a.strip()}
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_MB", "5")) * 1024 * 1024
DEDUP_TTL_DAYS = 4  # past SES's redelivery window

# invoke_agent_runtime wants the RUNTIME arn + the endpoint name as `qualifier` (same split poke_agent uses)
_EP_ARN = os.environ["AGENT_RUNTIME_ENDPOINT_ARN"]
if "/runtime-endpoint/" in _EP_ARN:
    RUNTIME_ARN, QUALIFIER = _EP_ARN.split("/runtime-endpoint/", 1)
else:
    RUNTIME_ARN, QUALIFIER = _EP_ARN, "DEFAULT"

s3 = _aws_client("s3")
ses = _aws_client("ses")
ddb = _aws_client("dynamodb")
agentcore = _aws_client("bedrock-agentcore")


def handler(event, _context):
    key = event["Records"][0]["s3"]["object"]["key"]
    raw = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    msg = email.message_from_bytes(raw, policy=default_policy)

    sender = parseaddr(msg.get("From", ""))[1].lower()
    message_id = (msg.get("Message-ID") or key).strip()

    # 1. sort — which of the firm's mailboxes was this addressed to?
    #
    # Quiet is the default and quiet is the common case: forwarded customer replies, bounces,
    # vendor receipts. Waking a model turn for each would be constant and expensive. Only mail
    # deliberately sent to `agent@` goes past this point.
    mailbox = _mailbox_for(msg)
    filed = _file_under(key, raw, mailbox)
    if mailbox != AGENT_MAILBOX:
        print(json.dumps({"event": "mail_filed", "mailbox": mailbox, "key": filed,
                          "from": sender, "subject": (msg.get("Subject") or "")[:200]}))
        return

    # 2. gate — allowlist + DMARC (fail closed).
    #
    # Only on the agent path, and that matters: forwarded mail routinely fails DMARC because the
    # forwarding server is not authorised to send for the original sender's domain. Since a
    # forwarded reply lands in a quiet mailbox, it never reaches this and is not dropped for it.
    if sender not in ALLOWLIST:
        log.info("dropped: sender not on the allowlist", sender=sender)
        return
    if not _sender_authenticated(msg, sender):
        log.info("dropped: sender not authenticated (no DMARC pass, no aligned DKIM)", sender=sender)
        return
    why = automatic(msg, sender)
    if why:
        log.info("dropped: an automatic message", sender=sender, reason=why)
        return

    # 3. dedup — conditional put; if it's already there, this is a redelivery
    if _seen(message_id):
        log.info("dropped: duplicate delivery", message_id=message_id)
        return

    # 4. attachments — small ones ride along; over the cap, bounce to the upload screen
    attachments, total = _attachments(msg)
    if total > MAX_ATTACHMENT_BYTES:
        _reply(msg, sender, "That attachment is too big for email — please use the upload screen in the app.")
        return

    body = _body_text(msg) or (msg.get("Subject") or "").strip()  # subject-only emails: fall back to the subject
    note = ""
    if attachments:
        note = "\n\n[attachments: " + ", ".join(a["filename"] for a in attachments) + "]"

    # 5. invoke this gerp's agent on a thread-stable session
    session_id = _session_for(msg, message_id)
    payload = json.dumps({"prompt": body + note, "role": "owner", "source": "email"}).encode()
    resp = agentcore.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        qualifier=QUALIFIER,
        runtimeSessionId=session_id,
        payload=payload,
        contentType="application/json",
    )
    answer = _read_answer(resp) or "(the agent returned no text)"

    # 6. reply, threaded
    _reply(msg, sender, answer)


_AUTO_PRECEDENCE = {"bulk", "junk", "list", "auto_reply"}


def automatic(msg, sender: str) -> str:
    """Why `msg` is a message a machine sent — one the agent must not answer — or "" for a person's.
    RFC 3834's `Auto-Submitted`, the older `Precedence` and `X-Autoreply`/`X-Autorespond` headers that
    out-of-office responders still send, a mailing list's `List-Id`, a bounce's null return path, and
    this agent's own address."""
    auto = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto and auto != "no":
        return f"Auto-Submitted: {auto}"
    if (msg.get("Precedence") or "").strip().lower() in _AUTO_PRECEDENCE:
        return f"Precedence: {msg.get('Precedence').strip().lower()}"
    for header in ("X-Autoreply", "X-Autorespond"):
        if msg.get(header) is not None:
            return header
    if msg.get("List-Id") is not None:
        return "List-Id"
    if (msg.get("Return-Path") or "").strip() == "<>":
        return "a null return path"
    if sender == AGENT_ADDRESS.lower():
        return "the agent's own address"
    return ""


def _mailboxes() -> set:
    """The local parts this firm accepts, from settings. `agent` is always one of them — a firm
    that empties the list must not lose the way to reach its own agent."""
    if not SETTINGS_TABLE:
        return {AGENT_MAILBOX}
    try:
        item = ddb.get_item(
            TableName=SETTINGS_TABLE,
            Key={"gerp_id": {"S": GERP_ID}, "sk": {"S": MAILBOXES_KEY}},
        ).get("Item")
    except Exception as e:  # noqa: BLE001 — degraded: the one mailbox stands
        log.warning("mailbox list unreadable, using the default only", mailbox=AGENT_MAILBOX, error=str(e))
        return {AGENT_MAILBOX}
    names = [v.get("S", "") for v in (item or {}).get("value", {}).get("L", [])]
    return {n.strip().lower() for n in names if n.strip()} | {AGENT_MAILBOX}


def _mailbox_for(msg) -> str:
    """The local part this was addressed to, if the firm declared it. Anything else is spam —
    a guessed address, a scrape, a typo — parked rather than dropped so the owner can look."""
    allowed = _mailboxes()
    for header in ("X-Original-To", "Delivered-To", "To", "Cc"):
        for _, addr in [(None, parseaddr(a)[1]) for a in (msg.get_all(header) or [])]:
            local = (addr or "").split("@")[0].strip().lower()
            if local in allowed:
                return local
    return SPAM_MAILBOX


def _file_under(key: str, raw: bytes, mailbox: str) -> str:
    """Move the message out of the landing zone and into its mailbox.

    SES writes to `inbound/` and that is what triggers this; mailboxes are `in/<name>/`, a
    different top-level prefix. They have to be different: filing into the watched prefix would
    fire this lambda again on its own output, forever.

    The prefix IS the mailbox — what sits in `in/billing/` is what nobody has dealt with, and
    the portal lists it by prefix."""
    name = key.rsplit("/", 1)[-1]
    dest = f"{IN_PREFIX}{mailbox}/{name}"
    if dest == key:
        return dest
    s3.put_object(Bucket=BUCKET, Key=dest, Body=raw)
    s3.delete_object(Bucket=BUCKET, Key=key)
    return dest


def _sender_authenticated(msg, sender: str) -> bool:
    """Cryptographic proof the From domain is genuine, read from the Authentication-Results header
    SES wrote: DMARC pass for the From domain, OR DKIM pass *aligned* to it. DMARC pass already
    implies an aligned SPF/DKIM; aligned DKIM also counts when the domain simply hasn't published a
    DMARC policy (dmarc=none — common, e.g. Google Workspace without _dmarc — and still spoof-proof:
    a forged From can't produce a DKIM signature that aligns to that domain).

    Only SES's header counts. SES prepends it above every header the sender wrote, so it is the
    first one, and its authserv-id is `amazonses.com`. A sender can write any number of
    `Authentication-Results` headers of their own below it, and those are the sender's text."""
    from_domain = sender.rsplit("@", 1)[-1].lower()
    results = msg.get_all("Authentication-Results", [])
    if not results:
        return False
    authserv, _, rest = str(results[0]).partition(";")
    if authserv.strip().lower() != "amazonses.com":
        return False
    for clause in (c.strip() for c in rest.lower().split(";")):
        if re.match(r"dmarc=pass\b", clause):
            m = re.search(r"header\.from=([a-z0-9.\-]+)", clause)
            if m and m.group(1).strip(".") == from_domain:
                return True
        if re.match(r"dkim=pass\b", clause):
            for d in re.findall(r"header\.[di]=@?([a-z0-9.\-]+)", clause):  # signing domain (header.d / header.i)
                d = d.strip(".")
                if d == from_domain or from_domain.endswith("." + d):  # exact or organizational-domain match
                    return True
    return False


def _seen(message_id: str) -> bool:
    try:
        ddb.put_item(
            TableName=DEDUP_TABLE,
            Item={"message_id": {"S": message_id}, "ttl": {"N": str(int(time.time()) + DEDUP_TTL_DAYS * 86400)}},
            ConditionExpression="attribute_not_exists(message_id)",
        )
        return False
    except ddb.exceptions.ConditionalCheckFailedException:
        return True


def _attachments(msg):
    out, total = [], 0
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        total += len(payload)
        out.append({"filename": part.get_filename() or "attachment", "size": len(payload)})
    return out, total


def _body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    return content.strip()


def _session_for(msg, message_id: str) -> str:
    # Thread root = References[0] if present, else In-Reply-To, else this message — so a reply
    # three deep lands back in the same session. Derived (not stored): deterministic 33-char id.
    refs = (msg.get("References") or "").split()
    root = (refs[0] if refs else (msg.get("In-Reply-To") or message_id)).strip()
    return hashlib.sha256(root.encode()).hexdigest()[:32] + "a"  # runtimeSessionId must be >=33 chars


def _read_answer(resp) -> str:
    raw = resp["response"].read()
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    # container streams SSE (data: {type:"text", text:"..."}); accumulate the text chunks.
    chunks = []
    saw_sse = False
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        saw_sse = True
        try:
            ev = json.loads(line[5:].strip())
        except json.JSONDecodeError:
            continue
        if ev.get("type") in (None, "text") and ev.get("text"):
            chunks.append(ev["text"])
    if saw_sse:
        return "".join(chunks).strip()
    # buffered fallback — a bare JSON {text|output|...} or plain text
    try:
        obj = json.loads(text)
        return (obj.get("text") or obj.get("output") or obj.get("response") or text).strip()
    except json.JSONDecodeError:
        return text.strip()


def _reply(orig, to_addr: str, answer: str):
    reply = EmailMessage()
    reply["From"] = AGENT_ADDRESS
    reply["To"] = to_addr
    # the agent's reply is automatic (RFC 3834): a compliant out-of-office stays quiet instead of answering it
    reply["Auto-Submitted"] = "auto-replied"
    subject = orig.get("Subject", "") or "your agent"
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    mid = orig.get("Message-ID")
    if mid:
        reply["In-Reply-To"] = mid
        reply["References"] = (orig.get("References", "") + " " + mid).strip()
    reply.set_content(answer)
    ses.send_raw_email(Source=AGENT_ADDRESS, Destinations=[to_addr], RawMessage={"Data": reply.as_bytes()})
