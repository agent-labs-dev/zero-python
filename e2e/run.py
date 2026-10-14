"""The end-to-end tier: an unmodified Zero client syncing through zero-cache, once per app server.

Starts Postgres and two zero-cache instances (docker compose), the TypeScript reference app server and
the Python one on the host, runs `client.ts` through each, and diffs what the client ended up with and
what each upstream database holds, and checks the reference run against `expected.json`. Exits non-zero
on any difference.

    uv run --group e2e python e2e/run.py [--keep]
"""

import argparse
import difflib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
PG = "postgres:postgres@localhost:55432"
TABLES = {
    "users": "SELECT * FROM users ORDER BY id",
    "project": "SELECT * FROM project ORDER BY id",
    "task": "SELECT id, project_id, title, done, priority, tags::text, due::text, created_at::text "
    "FROM task ORDER BY id",
    "label": "SELECT * FROM label ORDER BY id",
    "task_label": "SELECT * FROM task_label ORDER BY task_id, label_id",
    "clients": 'SELECT "lastMutationID" FROM {app}_0.clients',
    "mutations": 'SELECT "mutationID", result::text FROM {app}_0.mutations ORDER BY 1',
}


def step(message: str) -> None:
    print(f"==> {message}", flush=True)


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=HERE, check=True)


def wait_for(url: str, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except OSError as error:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{url} not ready") from error
            time.sleep(0.5)


def wait_for_post(url: str, timeout: float = 60) -> None:
    """An app server answers POSTs only; any HTTP response, even an error, means it is up."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=b"null", method="POST"), timeout=2)
            return
        except urllib.error.HTTPError:
            return
        except OSError as error:
            if time.monotonic() > deadline:
                raise TimeoutError(f"{url} not ready") from error
            time.sleep(0.5)


def server(stack: ExitStack, name: str, command: list[str], env: dict[str, str]) -> None:
    log = stack.enter_context((BUILD / f"{name}.log").open("w"))
    process = subprocess.Popen(command, cwd=HERE, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT)
    stack.callback(process.wait, 10)
    stack.callback(process.terminate)


def client(port: int) -> dict[str, Any]:
    out = subprocess.run(
        ["node", "client.ts", f"http://localhost:{port}"], cwd=HERE, capture_output=True, text=True, timeout=180
    )
    (BUILD / f"client-{port}.log").write_text(out.stderr)
    if out.returncode != 0:
        raise RuntimeError(f"client against {port} failed:\n{out.stderr}")
    return json.loads(out.stdout)


def dump(database: str, app: str) -> dict[str, Any]:
    tables = {}
    for name, template in TABLES.items():
        sql = template.format(app=app)
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", database, "-At", "-c", sql],
            cwd=HERE,
            check=True,
            capture_output=True,
            text=True,
        )
        tables[name] = out.stdout.splitlines()
    return tables


def diff(label: str, ts: Any, py: Any) -> bool:
    if ts == py:
        print(f"{label}: identical")
        return True
    print(f"{label}: DIFFERENT")
    ts_lines = json.dumps(ts, indent=2, sort_keys=True).splitlines()
    py_lines = json.dumps(py, indent=2, sort_keys=True).splitlines()
    sys.stdout.writelines(line + "\n" for line in difflib.unified_diff(ts_lines, py_lines, "typescript", "python"))
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the containers running")
    keep = parser.parse_args().keep

    BUILD.mkdir(exist_ok=True)
    schema_json = BUILD / "schema.json"
    schema_json.write_text(
        subprocess.run(["node", "emit_schema.ts"], cwd=HERE, check=True, capture_output=True, text=True).stdout
    )

    step("starting postgres")
    compose("down", "-v", "--remove-orphans")
    compose("up", "-d", "--wait", "postgres")
    with ExitStack() as stack:
        if not keep:
            stack.callback(compose, "down", "-v")
        server(stack, "ts-server", ["node", "ts_server.ts"], {"PORT": "3001", "UPSTREAM_DB": f"postgres://{PG}/e2e_ts"})
        server(
            stack,
            "py-server",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "py_server:app",
                "--host",
                "0.0.0.0",
                "--port",
                "3002",
                "--log-level",
                "warning",
            ],
            {"SCHEMA_JSON": str(schema_json), "UPSTREAM_DB": f"postgresql+asyncpg://{PG}/e2e_py"},
        )
        step("waiting for the app servers")
        wait_for_post("http://localhost:3001/query")
        wait_for_post("http://localhost:3002/query")
        step("starting zero-cache")
        compose("up", "-d", "zero-ts", "zero-py")
        try:
            wait_for("http://localhost:4848/")
            wait_for("http://localhost:4849/")
            step("running the client through the TypeScript app server")
            ts = client(4848)
            step("running the client through the Python app server")
            py = client(4849)
        finally:
            for name in ("zero-ts", "zero-py"):
                with (BUILD / f"{name}.log").open("w") as log:
                    subprocess.run(["docker", "compose", "logs", name], cwd=HERE, stdout=log, stderr=subprocess.STDOUT)
        (BUILD / "client-ts.json").write_text(json.dumps(ts, indent=2))
        (BUILD / "client-py.json").write_text(json.dumps(py, indent=2))
        # Identical is not enough: both sides could be wrong the same way. The reference run must also
        # produce the outcome the script was written for. After changing the app or the script, review
        # build/client-ts.json and copy it over expected.json.
        same = diff("reference against expected.json", json.loads((HERE / "expected.json").read_text()), ts)
        same = diff("client", ts, py) and same
        same = diff("upstream rows", dump("e2e_ts", "zts"), dump("e2e_py", "zpy")) and same
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
