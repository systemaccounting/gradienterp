"""The bus's lambda targets, delivered locally.

An emit is only half a path. `put_events` lands on the bus, a RULE decides who hears it, and the
target runs — and a pattern that does not match is a live failure mode, so the matching is the part
worth keeping real. moto does that part: it evaluates the patterns and delivers to SQS targets.
What it cannot do is invoke a lambda TARGET, because our functions are source on disk rather than
uploaded zips in its Lambda service.

So this fills exactly that gap and nothing else:

    put_events → moto matches the rule → an SQS queue per rule → this → the real handler

One queue per rule, because the queue is then the answer to "which rule matched" — no transformer,
no envelope to parse. Rules, patterns and targets all come from `platform/image.json`, taken off
the running bus by `snapshot.py`.

What this makes runnable locally, none of which was before:

  · `gerp-addressed-events` → the event dispatcher, i.e. the cross-firm routing substrate
  · `gerp-counters` → `openlyoperated_biz`'s counter, from an event `per_customer` emitted
  · `gerp-platform-reports` → the issue collector

Non-lambda targets are left alone: an SQS target is moto's job already, and the firehose archive
has no local form. Both are reported at startup so the omission is visible.

    bash scripts/local-dev.sh --start      # runs this alongside the stacks
"""

import json
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "modules" / "aws"))
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(HERE))

POLL_SECONDS = 1

# A rule that matches EVERYTHING, whose only job is to show the emit happened. Without it an event
# that matches no rule is invisible — and "nobody heard it" is the failure a pattern change causes,
# so it is the one you most need to see. `{"version": ["0"]}` is true of every EventBridge event.
TAP = {"name": "pump-tap", "pattern": {"version": ["0"]}}


def _load():
    """Every bus the images carry (the hub's, the operator's, the gerp's own), plus every
    stack's functions — a target can live in any stack.

    `src_dir` comes from the `gerp:src-dir` tag via the snapshot. Resolving a target by its NAME
    instead would take the last segment and glob for it, which finds the wrong module when a
    name repeats.
    """
    buses, env, fns = [], {}, {}
    for image in sorted(HERE.glob("*/image.json")):
        d = json.loads(image.read_text())
        if d.get("bus"):
            buses.append(d["bus"])
        for name, cfg in d.get("functions", {}).items():
            env.update(cfg.get("env") or {})
            fns.setdefault(name, cfg)
    return buses, env, fns


def _wire(buses):
    """Create every bus and its rules, one capture queue per rule with a lambda or a bus target,
    and one tap on each bus."""
    from aws import client
    ev, sqs = client("events"), client("sqs")
    wired, skipped, taps = {}, [], {}
    for bus in buses:
        try:
            ev.create_event_bus(Name=bus["name"])
        except ev.exceptions.ResourceAlreadyExistsException:
            pass
        qurl = sqs.create_queue(QueueName=f"pump-tap-{bus['name']}"[:80])["QueueUrl"]
        qarn = sqs.get_queue_attributes(QueueUrl=qurl, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        ev.put_rule(Name=TAP["name"], EventBusName=bus["name"], EventPattern=json.dumps(TAP["pattern"]))
        ev.put_targets(Rule=TAP["name"], EventBusName=bus["name"], Targets=[{"Id": "tap", "Arn": qarn}])
        taps[qurl] = bus["name"]
        for rule in bus["rules"]:
            targets = [t["function"] or t["bus"] for t in rule["targets"] if t.get("function") or t.get("bus")]
            if not targets:
                skipped.append((rule["name"], [t["arn"].split(":")[2] for t in rule["targets"]]))
                continue
            qname = f"pump-{rule['name']}"[:80]
            qurl = sqs.create_queue(QueueName=qname)["QueueUrl"]
            qarn = sqs.get_queue_attributes(QueueUrl=qurl, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
            ev.put_rule(Name=rule["name"], EventBusName=bus["name"],
                        EventPattern=json.dumps(rule["pattern"] or {}))
            ev.put_targets(Rule=rule["name"], EventBusName=bus["name"],
                           Targets=[{"Id": "pump", "Arn": qarn}])
            wired[qurl] = (bus["name"], rule["name"], targets)
    return wired, skipped, taps


def _handler(name, catalog):
    """The target's real handler, found by its snapshotted source dir."""
    import importlib.util
    from aws import _bundle_dirs, _local_handler
    src = (catalog.get(name) or {}).get("src_dir")
    if not src:
        return _local_handler(name)
    main = REPO / src / "main.py"
    for d in _bundle_dirs(main):
        if d not in sys.path:
            sys.path.insert(0, d)
    spec = importlib.util.spec_from_file_location(f"_pump_{name.replace('-', '_')}", main)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.handler


def _deliver(targets, event, catalog, buses):
    """Hand the matched event to each target, as EventBridge would: a function's handler, or a
    put onto the bus a bus target names (the hub's spoke edge onto the gerp's own bus, the
    forward onto the operator's) — a new event there, the way a bus-to-bus target lands."""
    from aws import client
    for target in targets:
        try:
            if target in buses:
                client("events").put_events(Entries=[{
                    "EventBusName": target, "Source": event.get("source", ""),
                    "DetailType": event.get("detail-type", ""), "Detail": json.dumps(event.get("detail") or {})}])
                print(f"  → bus {target}: put", flush=True)
                continue
            _handler(target, catalog)(event, None)
            print(f"  → {target}: ok", flush=True)
        except Exception as e:  # noqa: BLE001 — a target that throws must not stop the pump
            print(f"  → {target}: {type(e).__name__}: {e}", flush=True)


def main():
    buses, env, catalog = _load()
    if not buses:
        sys.exit("no bus in any image.json — run: python3 tests/server/snapshot.py hub (and platform, per_customer)")
    import os
    from _image import RUNTIME_ENV       # what the Lambda runtime provides, not the snapshot
    os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
    os.environ.update({**RUNTIME_ENV, **env})

    wired, skipped, taps = _wire(buses)
    names = {b["name"] for b in buses}
    print(f"[pump] {', '.join(sorted(names))}: {len(wired)} rule(s) delivering", flush=True)
    for bus, name, targets in wired.values():
        print(f"  {bus}: {name} → {', '.join(targets)}", flush=True)
    for name, kinds in skipped:
        print(f"  {name} → {', '.join(kinds)} (not delivered locally)", flush=True)
    print("  every emit is logged below; a line with no → underneath matched no rule", flush=True)

    from aws import client
    sqs = client("sqs")
    while True:
        # the taps first, so the emit prints above whatever it triggered
        for tap, bus in taps.items():
            for m in sqs.receive_message(QueueUrl=tap, MaxNumberOfMessages=10,
                                         WaitTimeSeconds=0).get("Messages", []):
                e = json.loads(m["Body"])
                d = e.get("detail") or {}
                addr = f" to={d['to']}" if d.get("to") else ""
                body = json.dumps(d, default=str)
                print(f"[emit] {bus} · {e.get('source')} · {e.get('detail-type')}{addr}  "
                      f"{body[:160]}{'…' if len(body) > 160 else ''}", flush=True)
                sqs.delete_message(QueueUrl=tap, ReceiptHandle=m["ReceiptHandle"])

        for qurl, (bus, rule, targets) in wired.items():
            r = sqs.receive_message(QueueUrl=qurl, MaxNumberOfMessages=10, WaitTimeSeconds=0)
            for m in r.get("Messages", []):
                event = json.loads(m["Body"])
                print(f"[pump] {bus}: matched {rule}", flush=True)
                _deliver(targets, event, catalog, names)
                sqs.delete_message(QueueUrl=qurl, ReceiptHandle=m["ReceiptHandle"])
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
