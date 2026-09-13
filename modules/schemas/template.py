"""template — free-form text that is public by construction.

Every module eventually grows a field where a person types a sentence, and a sentence is the one
shape the registry cannot classify: `"called Dana about the Henderson invoice"` and `"restock
Tuesdays, the Friday truck runs late"` are the same field holding entirely different things. The
usual answer is to classify the field `secret` and serve nothing, which is safe and useless — it
publishes nothing where something real was available.

So the text splits in two, the way a shell command does:

    text    "reserve 409ed past a retry booking $1 for $2"      publishes
    values  ["a tuesday window table", "the Henderson wedding"] never served

The writer answers an easy question — WHICH SPANS ARE MINE? — instead of a hard one — is this prose
safe to publish? And the artifact is safe by construction rather than by a scrubbing step someone
has to remember: there is no filter to pass, because a firm-specific value was never in the text.

Two consequences fall out that the "just hide it" answer cannot give:

  - DEDUP becomes a string match. The firm-specific nouns WERE the wording variance, so two firms
    reporting one defect stop being a judgment call and start being `==`.
  - the owner loses nothing. `expand()` puts the values back for whoever holds both halves, so the
    private view reads exactly as it was written.

Shared, not per-module: `scripts/deploy.py` resolves `modules/*/<name>.py` across modules, so any
lambda that imports this bundles it with no terraform.
"""

import re

PLACEHOLDER = re.compile(r"\$(\d+)")
VALUES_CAP = 40   # placeholders in one text; past this it is prose, not a template


def placeholders(text: str) -> list[int]:
    """The `$n` indexes the text uses, ascending and deduped."""
    return sorted({int(n) for n in PLACEHOLDER.findall(text or "")})


def check(text: str, values) -> str | None:
    """None when a template and its values correspond, else the reason they don't.

    Both directions are errors, and both are refusals rather than warnings:
      - a dangling `$2` publishes a text nobody can read
      - a value with no `$n` means the writer meant to hide something and didn't — the text
        publishes still carrying it, which is the exact failure the split exists to prevent
    """
    values = values or []
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        return "private values must be a list of strings — what $1, $2, … stand for, in order"
    if len(values) > VALUES_CAP:
        return f"private values exceed {VALUES_CAP}"

    used = placeholders(text)
    if used and (used[0] != 1 or used[-1] != len(used)):
        return f"placeholders must run $1..$n with no gaps; found {['$%d' % n for n in used]}"
    if len(used) != len(values):
        return (f"text uses {len(used)} placeholder(s) but {len(values)} private value(s) were given "
                "— each $n needs its value, and a value with no $n would publish in the text instead")
    return None


def expand(text: str, values) -> str:
    """The template with its values put back — the private view, for a caller holding both halves.

    Out-of-range indexes are left standing rather than raising: a reader that legitimately holds
    only the public half still gets readable text, with the placeholders showing exactly where the
    firm-specific spans were.
    """
    values = values or []

    def sub(match):
        i = int(match.group(1))
        return values[i - 1] if 1 <= i <= len(values) else match.group(0)

    return PLACEHOLDER.sub(sub, text or "")
