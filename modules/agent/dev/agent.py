"""Local dev harness for the gradientERP bookkeeper agent.

Runs a stdin/stdout conversation loop backed by Claude via the Anthropic SDK.
Each tool call dispatches to a real accounting lambda handler via importlib —
same data stores, same validation, no AWS. Use this to iterate on the prompt,
tool schemas, and conversation flow before deploying to Bedrock AgentCore.

Prerequisites:
    export ANTHROPIC_API_KEY=sk-ant-...        # the default
    # or: AGENT_PROVIDER=bedrock, which uses your AWS creds and production's model id

Usage:
    bash modules/agent/dev/run.sh
    (handles the .venv bootstrap; then type; Ctrl-D or Ctrl-C to exit)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools import TOOLS, invoke  # noqa: E402


# anthropic by default: an API key is one env var, where Bedrock wants creds, a region, and model
# access granted on the account. `AGENT_PROVIDER=bedrock` makes the model call identical to
# production's (same service, same inference profile, same throttling) — worth it when the question
# is about the MODEL rather than about the prompt or the tools.
PROVIDER = os.environ.get("AGENT_PROVIDER", "anthropic")
DEFAULT_MODEL = {"anthropic": "claude-sonnet-4-6",
                 "bedrock": "us.anthropic.claude-sonnet-4-6"}
MODEL = os.environ.get("AGENT_MODEL") or DEFAULT_MODEL.get(PROVIDER, DEFAULT_MODEL["anthropic"])
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
AGENT_MODE = os.environ.get("AGENT_MODE", "bookkeeper")  # bookkeeper | onboarding
BUSINESS_NAME = os.environ.get("BUSINESS_NAME", "the cafe")


def load_system_prompt():
    path = PROMPTS_DIR / f"{AGENT_MODE}.md"
    if not path.exists():
        raise SystemExit(f"unknown AGENT_MODE={AGENT_MODE!r} — no prompt at {path}")
    return path.read_text().replace("{{ business_name }}", BUSINESS_NAME)


def run_turn(client, system, messages):
    """Drive one user-turn: call Claude, execute any tool_use blocks, loop until text-only reply."""
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # append the full assistant turn to history
        messages.append({"role": "assistant", "content": response.content})

        tool_uses = [c for c in response.content if c.type == "tool_use"]
        if not tool_uses:
            text_blocks = [c.text for c in response.content if c.type == "text"]
            return "\n".join(text_blocks).strip()

        # resolve every tool use in this turn, then continue the loop
        tool_results = []
        for tu in tool_uses:
            sys.stderr.write(f"[tool] {tu.name}({json.dumps(tu.input)})\n")
            try:
                result = invoke(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result),
                })
            except Exception as e:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "is_error": True,
                    "content": f"{type(e).__name__}: {e}",
                })
        messages.append({"role": "user", "content": tool_results})


def make_client():
    """The model transport. Both speak the same messages/tool_use shape, so the turn loop is
    identical either way — only the credentials and the model id differ."""
    if PROVIDER == "bedrock":
        from anthropic import AnthropicBedrock
        return AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-east-1"))
    if PROVIDER != "anthropic":
        raise SystemExit(f"unknown AGENT_PROVIDER={PROVIDER!r} — expected anthropic | bedrock")
    from anthropic import Anthropic
    return Anthropic()


def main():
    try:
        client = make_client()
    except ImportError:
        sys.stderr.write("requires: pip3 install anthropic (or python3 -m pip install anthropic)\n")
        sys.exit(1)
    system = load_system_prompt()
    messages = []

    sys.stderr.write(f"agent ready — {PROVIDER}/{MODEL}, {len(TOOLS)} tools, business={BUSINESS_NAME}\n")
    sys.stderr.write("type a message, Ctrl-D to exit\n\n")

    while True:
        try:
            user_input = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not user_input.strip():
            continue

        messages.append({"role": "user", "content": user_input})
        reply = run_turn(client, system, messages)
        print(reply)
        print()


if __name__ == "__main__":
    main()
