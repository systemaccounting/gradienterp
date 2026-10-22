"""The Cognito stand-in as a subprocess for a test: `start()` runs `tests/server/cognito/server.py`
under uvicorn on a free port, with `LOCAL_COGNITO_URL` set to that address so the tokens it signs
name it as their issuer, and waits until it answers."""

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start() -> tuple[subprocess.Popen, str]:
    """The process and its url. The caller terminates the process."""
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--app-dir", str(HERE),
                             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
                            env={**os.environ, "LOCAL_COGNITO_URL": url, "PYTHONPATH": str(HERE.parent)},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(200):
        try:
            urllib.request.urlopen(url + "/", timeout=1)
            return proc, url
        except urllib.error.HTTPError:
            return proc, url
        except (urllib.error.URLError, OSError):
            if proc.poll() is not None:
                raise RuntimeError(f"the Cognito stand-in exited: {proc.stdout.read()}")
            time.sleep(0.05)
    proc.terminate()
    raise RuntimeError("the Cognito stand-in did not come up")
