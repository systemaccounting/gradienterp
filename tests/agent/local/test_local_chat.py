"""The web chat door, locally: `modules/agent/lambdas/chat/index.mjs` run by `tests/server/node_lambda.mjs`
with its SDK clients pointed by env at moto and at an agent process, its issuer the Cognito stand-in.

The contracts this pins, each one a thing a side assumes of the other:
  - the chat lambda verifies a bearer against the issuer `COGNITO_ISSUER` names, so the stand-in's
    signed token gets in and an unsigned one is refused
  - `AWS_ENDPOINT_URL_BEDROCK_AGENTCORE` lands `InvokeAgentRuntime` on `POST /runtimes/<arn>/invocations`
    with the session id in its header and the turn's payload as its body — the path the agent process
    answers — and a buffered `{"response"}` comes back to the browser as one text chunk
  - the chats row the lambda keeps is written to the table on moto that `CHATS_TABLE` names

No model: the agent is a stub answering a fixed reply, so the test costs nothing and runs anywhere."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests"))
sys.path.insert(0, str(REPO / "modules" / "aws"))
os.environ.pop("AWS_LAMBDA_FUNCTION_NAME", None)
from helpers.localaws import make_table  # noqa: E402

OWNER = "owner-sub-0001"
ARN = "arn:aws:bedrock-agentcore:us-east-1:000000000000:runtime/r1/runtime-endpoint/ep1"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url, proc=None):
    for _ in range(200):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except (urllib.error.URLError, OSError):
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(f"process exited: {proc.stdout.read()}")
            time.sleep(0.05)
    raise RuntimeError(f"{url} did not come up")


def _http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class _AgentStub(BaseHTTPRequestHandler):
    """What the agent process answers: a buffered turn. Records what it was sent."""
    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        _AgentStub.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(body or b"{}")})
        out = json.dumps({"response": "hi from the stub", "session_id": "x"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):  # quiet
        pass


def test_the_chat_door_runs_locally_against_the_stand_in_and_an_agent_process():
    endpoint = os.environ.get("LOCAL_AWS_ENDPOINT", "http://localhost:5000")
    chats = make_table("agent-chats")
    from aws import client
    client("ssm").put_parameter(Name="/local-chat-test/owner_sub", Value=OWNER, Type="String", Overwrite=True)

    stub = HTTPServer(("127.0.0.1", 0), _AgentStub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    agent_url = f"http://127.0.0.1:{stub.server_port}"

    py = sys.executable
    cog_port = _free_port()
    cog_url = f"http://127.0.0.1:{cog_port}"
    cognito = subprocess.Popen([py, "-m", "uvicorn", "server:app", "--app-dir", str(REPO / "tests/server/cognito"),
                                "--host", "127.0.0.1", "--port", str(cog_port), "--log-level", "warning"],
                               env={**os.environ, "LOCAL_COGNITO_URL": cog_url, "PYTHONPATH": str(REPO / "tests/server")},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    chat_port = _free_port()
    chat_url = f"http://127.0.0.1:{chat_port}"
    node_env = {
        "AWS_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "local", "AWS_SECRET_ACCESS_KEY": "local",
        "AWS_ENDPOINT_URL": endpoint, "AWS_ENDPOINT_URL_BEDROCK_AGENTCORE": agent_url,
        "COGNITO_ISSUER": cog_url, "COGNITO_CLIENT_ID": "local", "COGNITO_DOMAIN_PREFIX": "",
        "MEMORY_ID": "", "SESSIONS_BUCKET": "", "CONTACTS_TABLE": "",
        "CHATS_TABLE": chats, "OWNER_SUB_PARAM": "/local-chat-test/owner_sub",
        "AGENT_RUNTIME_ENDPOINT_ARN": ARN, "BUSINESS_NAME": "Test Co",
    }
    node = subprocess.Popen(["node", str(REPO / "tests/server/node_lambda.mjs"), "--port", str(chat_port),
                             "--front", "modules/agent/lambdas/chat"],
                            env={**os.environ, **node_env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait(cog_url + "/", cognito)
        _wait(chat_url + "/", node)
        token = lambda sub: json.loads(_http("GET", f"{cog_url}/token?sub={sub}&email={sub}@localhost")[1])["IdToken"]  # noqa: E731

        # the page, and the door's refusals: no token, an unsigned token, a signed token for a non-member
        status, page = _http("GET", chat_url + "/")
        assert status == 200 and "<html" in page.lower()
        assert _http("GET", chat_url + "/api/chats") == (401, '{"error":"missing bearer token"}')
        unsigned = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJvd25lci1zdWItMDAwMSJ9.local"
        status, body = _http("GET", chat_url + "/api/chats", headers={"authorization": f"Bearer {unsigned}"})
        assert (status, json.loads(body)["error"]) == (401, "auth failed: unexpected alg")
        status, body = _http("GET", chat_url + "/api/chats", headers={"authorization": f"Bearer {token('someone-else')}"})
        assert (status, json.loads(body)["error"]) == (403, "not a member of this workspace")

        # the owner: in, nothing yet
        auth = {"authorization": f"Bearer {token(OWNER)}"}
        assert _http("GET", chat_url + "/api/chats", headers=auth) == (200, '{"chats":[]}')

        # a turn: the lambda invokes the agent process over the SDK, at the path the SDK sends
        status, body = _http("POST", chat_url + "/api/chat", {"message": "hello there"}, headers=auth)
        assert status == 200
        lines = [json.loads(line) for line in body.splitlines()]
        assert lines[0]["type"] == "session" and lines[0]["role"] == "owner"
        sid = lines[0]["session_id"]
        assert lines[1] == {"type": "text", "text": "hi from the stub"}, "a buffered {response} is one text chunk"
        assert lines[2] == {"type": "done"}
        assert len(_AgentStub.seen) == 1
        call = _AgentStub.seen[0]
        assert call["path"].startswith("/runtimes/") and call["path"].endswith("/invocations?qualifier=ep1"), call["path"]
        assert "runtime/r1" in urllib.request.unquote(call["path"]) and "runtime-endpoint" not in urllib.request.unquote(call["path"])
        assert call["headers"]["x-amzn-bedrock-agentcore-runtime-session-id"] == sid
        assert call["body"] == {"prompt": "hello there", "role": "owner", "account_id": OWNER, "source": "chat", "stream": True}

        # the chats row landed on moto, and the index answers it
        status, body = _http("GET", chat_url + "/api/chats", headers=auth)
        assert status == 200 and [c["session_id"] for c in json.loads(body)["chats"]] == [sid]
    finally:
        node.terminate()
        cognito.terminate()
        stub.shutdown()
        node.wait(timeout=5)
        cognito.wait(timeout=5)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all local chat tests passed")
