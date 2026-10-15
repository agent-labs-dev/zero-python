"""The Python end of `harness/runner.ts`, which runs the published @rocicorp/zero package."""

import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "harness"


class HarnessError(Exception):
    """The TypeScript side threw. Its stack is the message."""


class Harness:
    def __init__(self) -> None:
        self._process = subprocess.Popen(
            ["node", "runner.ts"],
            cwd=HARNESS,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def call(self, op: str, **args: Any) -> Any:
        stdin, stdout = self._process.stdin, self._process.stdout
        assert stdin is not None
        assert stdout is not None
        stdin.write(json.dumps({"op": op, **args}) + "\n")
        stdin.flush()
        response = json.loads(stdout.readline())
        if not response["ok"]:
            raise HarnessError(response["error"])
        return response["value"]

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._process.wait(timeout=10)
