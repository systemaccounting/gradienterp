"""`node_lambda.mjs` runs a Node lambda locally the way `_image.py` runs a Python one: the
snapshot's env applied (the shell's winning), the handler imported from source and re-imported when
the file changes, the event the deployed function receives. Two doors: the --front function as a
Function URL, streaming through; `/invoke/<fn>` answering what the handler returned or wrote."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
REPO = HERE.parents[1]
RUNNER = HERE / "node_lambda.mjs"
sys.path.insert(0, str(HERE))

ECHO = """
export const handler = async (event, ctx) => ({
  statusCode: 200, headers: { "x-fn": process.env.WHO },
  body: JSON.stringify({ event, who: process.env.WHO, shell: process.env.SHELL_WINS, request: ctx.awsRequestId }),
});
"""
STREAM = """
async function handlerImpl(event, responseStream) {
  const s = awslambda.HttpResponseStream.from(responseStream, { statusCode: 201, headers: { "content-type": "application/x-ndjson", "x-fn": process.env.WHO } });
  s.write(JSON.stringify({ type: "first" }) + "\\n");
  s.write(JSON.stringify({ path: event.rawPath, method: event.requestContext.http.method, q: event.queryStringParameters,
                           body: event.body, host: event.headers.host, raw: event.rawQueryString }) + "\\n");
  s.end();
}
export const handler = (globalThis.awslambda?.streamifyResponse || ((fn) => fn))(handlerImpl);
"""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body.encode() if isinstance(body, str) else body)
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


class _Runner:
    def __init__(self, image: Path, front: str, env: dict):
        self.port = _free_port()
        self.proc = subprocess.Popen(["node", str(RUNNER), "--image", str(image), "--port", str(self.port), "--front", front],
                                     env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base + "/invoke/none", data=b"{}", timeout=1)
            except urllib.error.HTTPError:
                return                      # 404 for an unknown function: the process answers
            except (urllib.error.URLError, ConnectionError, OSError):
                if self.proc.poll() is not None:
                    raise RuntimeError(f"node_lambda.mjs exited: {self.proc.stdout.read()}")
                time.sleep(0.05)
        raise RuntimeError("node_lambda.mjs did not come up")

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


def _fixture(tmp: Path) -> Path:
    (tmp / "fns/echo").mkdir(parents=True)
    (tmp / "fns/stream").mkdir(parents=True)
    (tmp / "fns/echo/index.mjs").write_text(ECHO)
    (tmp / "fns/stream/index.mjs").write_text(STREAM)
    image = {"stack": "t", "gerp": "g", "routes": [], "functions": {
        "t-echo": {"src_dir": str(tmp / "fns/echo"), "env": {"WHO": "echo", "SHELL_WINS": "snapshot"}},
        "t-stream": {"src_dir": str(tmp / "fns/stream"), "env": {"WHO": "stream"}},
        "t-python": {"src_dir": "modules/accounting/lambdas/post_journal_entry", "env": {}},
    }}
    (tmp / "image.json").write_text(json.dumps(image))
    return tmp / "image.json"


def test_invoke_runs_the_handler_under_its_env_and_the_shell_wins():
    with tempfile.TemporaryDirectory() as d:
        r = _Runner(_fixture(Path(d)), "t-stream", {"SHELL_WINS": "shell"})
        try:
            status, _, text = _http("POST", r.base + "/invoke/t-echo", {"x": 1})
            assert status == 200
            out = json.loads(text)
            assert out["statusCode"] == 200 and out["headers"] == {"x-fn": "echo"}
            body = json.loads(out["body"])
            assert body["event"] == {"x": 1} and body["who"] == "echo" and body["request"]
            assert body["shell"] == "shell", "a value the shell set wins over the snapshot's"
            # the src dir's last segment names the function too — a tool is known by its dir
            assert json.loads(json.loads(_http("POST", r.base + "/invoke/echo", {})[2])["body"])["who"] == "echo"
            status, _, text = _http("POST", r.base + "/invoke/t-nope", {})
            assert status == 404 and json.loads(text)["known"] == ["t-echo", "t-stream"], "a Python function is not a Node one"
            # a streaming handler through /invoke: what it wrote, as the lambda's result
            out = json.loads(_http("POST", r.base + "/invoke/t-stream", {"rawPath": "/p", "requestContext": {"http": {"method": "GET"}}, "headers": {}})[2])
            assert out["statusCode"] == 201 and out["headers"]["x-fn"] == "stream"
            assert [json.loads(line) for line in out["body"].splitlines()][0] == {"type": "first"}
        finally:
            r.stop()


def test_the_front_is_the_function_url_streamed_through():
    with tempfile.TemporaryDirectory() as d:
        r = _Runner(_fixture(Path(d)), "t-stream", {})
        try:
            status, headers, text = _http("POST", r.base + "/api/thing?y=1&z=two", "hello")
            assert status == 201 and headers["content-type"] == "application/x-ndjson" and headers["x-fn"] == "stream"
            first, second = [json.loads(line) for line in text.splitlines()]
            assert first == {"type": "first"}
            assert second == {"path": "/api/thing", "method": "POST", "q": {"y": "1", "z": "two"}, "body": "hello",
                              "host": f"127.0.0.1:{r.port}", "raw": "y=1&z=two"}
        finally:
            r.stop()


def test_an_edited_handler_runs_on_the_next_request():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        r = _Runner(_fixture(tmp), "t-stream", {})
        try:
            assert json.loads(json.loads(_http("POST", r.base + "/invoke/t-echo", {})[2])["body"])["who"] == "echo"
            f = tmp / "fns/echo/index.mjs"
            f.write_text(ECHO.replace("who: process.env.WHO", 'who: "edited"'))
            st = f.stat()
            os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))   # a later mtime whatever the clock did
            assert json.loads(json.loads(_http("POST", r.base + "/invoke/t-echo", {})[2])["body"])["who"] == "edited"
        finally:
            r.stop()


def test_the_image_forwards_a_node_function_to_the_runner():
    import _image
    with tempfile.TemporaryDirectory() as d:
        r = _Runner(_fixture(Path(d)), "t-stream", {})
        try:
            os.environ["LOCAL_NODE_URL"] = r.base
            out = _image._node_handler("t-echo")({"rawPath": "/"}, None)
            assert out["statusCode"] == 200 and json.loads(out["body"])["event"] == {"rawPath": "/"}
        finally:
            r.stop()
            os.environ.pop("LOCAL_NODE_URL", None)
    # with no runner up, a lambda-shaped 502 names the address, so the route answers rather than raising
    os.environ["LOCAL_NODE_URL"] = "http://127.0.0.1:1"
    out = _image._node_handler("t-echo")({}, None)
    assert out["statusCode"] == 502 and "127.0.0.1:1" in json.loads(out["body"])["error"]
    os.environ.pop("LOCAL_NODE_URL", None)


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all node_lambda tests passed")
