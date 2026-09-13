"""The failure kinds this module's lambdas raise, declared once (aws.Kind): the identifier a
metric counts, a task keys on and an investigator filters by; the message for a person; the
category; the ids a line of the kind carries."""

from aws import Kind

UNKNOWN_MONEY_STEP = Kind("unknown_money_step", "the kind's config names a money step the code lacks",
                          "config", ids=("thread", "terms_hash"))
EFFECT_FAILED = Kind("effect_failed", "a settle effect failed", "dependency", ids=("thread", "terms_hash"))
