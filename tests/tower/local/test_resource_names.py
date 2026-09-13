"""Every resource in a gerp's account is named `gerp-<module>-<gerp_id>-<thing>`, and each AWS
resource type caps its name. The gerp_id is capped at GERP_ID_MAX (the BFF's `_new_gerp_id`);
this holds the other side: rendered with an id that long, every function, bucket and queue
name the modules declare fits its limit — so a new long function name fails here, not on the
27th module of a vend."""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "prod" / "gradienterp_cloud" / "bff"))
sys.path.insert(0, str(REPO / "modules" / "aws"))
STACK_PREFIX = "gerp"
SKIP = {"__pycache__", "vendor", "node_modules"}

LAMBDA_NAME_MAX = 64
IAM_ROLE_MAX = 64
S3_BUCKET_MAX = 63
SQS_QUEUE_MAX = 80
EVENT_RULE_MAX = 64
ACCOUNT_ID = "123456789012"


def gerp_id_max():
    import main as bff  # the BFF, which makes the id
    return bff.GERP_ID_MAX


def longest_id():
    return "x" * gerp_id_max()


def test_the_longest_gerp_id_is_a_slug_plus_six_hex():
    import main as bff
    label = "A Very Long Business Name Indeed Spanning Many Words"
    gid = bff._new_gerp_id(label)
    assert bff.GERP_ID_MAX - 1 <= len(gid) <= bff.GERP_ID_MAX, gid   # a trailing dash on the cut is dropped
    assert re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{6}", gid) and "--" not in gid, gid


def test_every_function_name_fits_with_the_longest_gerp_id():
    """`gerp-<module>-<id>-<function dir>` is the convention; the agent module names its
    functions `agentcore-<id>-<x>` (shorter) and labor's close handler `gerp-labor-<id>-close-handler`."""
    gid = longest_id()
    over = []
    for d in sorted((REPO / "modules").glob("*/lambdas/*/")):
        if d.name.startswith("_") or d.name in SKIP:
            continue
        name = f"{STACK_PREFIX}-{d.parent.parent.name}-{gid}-{d.name}"
        if len(name) > LAMBDA_NAME_MAX:
            over.append(f"{name} ({len(name)})")
    assert not over, f"function names past {LAMBDA_NAME_MAX} with a {gerp_id_max()}-character gerp_id:\n  " + "\n  ".join(over)


def test_every_declared_bucket_queue_and_rule_name_fits():
    """The literal name templates in modules/*/infra: `${local.prefix}-<suffix>` and the
    `${var.stack_prefix}-<module>-${gerp_id}-<suffix>-${account}` buckets, rendered with the
    longest id and a 12-digit account."""
    gid = longest_id()
    over = []
    for tf in sorted((REPO / "modules").glob("*/infra/*.tf")):
        module = tf.parent.parent.name
        text = tf.read_text()
        for m in re.finditer(r'\n\s*(bucket|name|queue_name)\s*=\s*"([^"\n]*\$\{(?:local\.prefix|var\.stack_prefix|local\.mail_prefix)[^"\n]*)"', text):
            attr, tmpl = m.group(1), m.group(2)
            rendered = (tmpl.replace("${local.prefix}", f"{STACK_PREFIX}-{module}-{gid}")
                        .replace("${local.mail_prefix}", f"{STACK_PREFIX}-mail-{gid}")
                        .replace("${var.stack_prefix}", STACK_PREFIX)
                        .replace('${replace(var.gerp_id, "_", "-")}', gid)
                        .replace("${var.gerp_id}", gid)
                        .replace("${data.aws_caller_identity.current.account_id}", ACCOUNT_ID)
                        .replace("${each.key}", "x" * 22))
            if "${" in rendered:
                continue   # an expression the render does not know; the function test covers dirs
            limit = S3_BUCKET_MAX if attr == "bucket" else SQS_QUEUE_MAX if rendered.endswith(("-dlq", "-failed", "-watch")) else 255
            if attr == "name" and "aws_cloudwatch_event_rule" in text[max(0, m.start() - 400):m.start()]:
                limit = EVENT_RULE_MAX
            if len(rendered) > limit:
                over.append(f"{tf.relative_to(REPO)}: {rendered} ({len(rendered)} > {limit})")
    assert not over, "names past their limit with the longest gerp_id:\n  " + "\n  ".join(over)


def test_stream_mapping_queue_names_fit():
    """`modules/terraform/stream` names its queue `<name>-failed`; the callers pass
    `${local.prefix}-<mapping>`."""
    gid = longest_id()
    over = []
    for tf in sorted((REPO / "modules").glob("*/infra/*.tf")):
        module = tf.parent.parent.name
        for m in re.finditer(r'terraform/stream"[^}]*?\n\s*name\s*=\s*"([^"\n]+)"', tf.read_text(), re.S):
            q = m.group(1).replace("${local.prefix}", f"{STACK_PREFIX}-{module}-{gid}") + "-failed"
            if len(q) > SQS_QUEUE_MAX:
                over.append(f"{q} ({len(q)})")
    assert not over, "\n".join(over)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all resource-name tests passed")
