"""A Postgres for the tests: `ZERO_SERVER_PG` if set (as in CI), otherwise a throwaway Docker container."""

import os
import subprocess
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager

IMAGE = "postgres:17"


@contextmanager
def postgres_server() -> Generator[str]:
    """Yields a base URL, `postgresql://user:password@host:port`, with no database on the end."""
    if url := os.environ.get("ZERO_SERVER_PG"):
        yield url.rstrip("/")
        return
    name = f"zero-server-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        port = (
            subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True)
            .stdout.split(":")[-1]
            .strip()
        )
        for _ in range(120):
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"], capture_output=True
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        yield f"postgresql://postgres:postgres@127.0.0.1:{port}"
    finally:
        # -v: the image declares a data volume, which would otherwise outlive the container.
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
