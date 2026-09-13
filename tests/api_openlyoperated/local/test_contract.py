"""The api's contract keeps its shape: every path names a backend folder that exists, every backend
folder is named by a path, `llms.txt` names every path, and a streaming path's lambda is Node."""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
API = REPO / "prod" / "api_openlyoperated" / "api" / "v1"


def _spec():
    return json.loads((API / "openapi.json").read_text())


def _backends():
    return {d.name: d for d in API.iterdir() if d.is_dir() and any((d / f).exists() for f in ("handler.py", "handler.mjs", "Dockerfile"))}


def _ops():
    return [(p, m.upper(), op) for p, methods in _spec()["paths"].items() for m, op in methods.items()]


def test_every_path_has_a_backend_folder_and_every_folder_a_path():
    backends = _backends()
    named = set()
    for path, method, op in _ops():
        assert op.get("x-backend") in backends, f"{method} {path} names no backend folder"
        named.add(op["x-backend"])
    assert set(backends) == named, f"folders without a path: {set(backends) - named}"


def test_llms_txt_names_every_path():
    text = (API / "llms.txt").read_text()
    for path, method, _ in _ops():
        assert f"{method} {path}" in text, f"llms.txt does not name {method} {path}"


def test_a_streaming_path_is_a_node_lambda_or_a_service():
    backends = _backends()
    for path, method, op in _ops():
        if op.get("x-stream"):
            d = backends[op["x-backend"]]
            assert (d / "handler.mjs").exists() or (d / "Dockerfile").exists(), f"{path} streams but its backend is not Node or a service"


def test_the_stage_is_the_version():
    spec = _spec()
    assert spec["servers"][0]["url"].endswith("/v1")
    assert not any(p.startswith("/v1") for p in spec["paths"]), "paths are written without the stage"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print(f"ok {_name}")
    print("all contract tests passed")
