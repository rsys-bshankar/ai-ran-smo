"""PR-ST-6: a stopping service finishes the requests it already accepted, then exits.

This runs the Dockerfile's own `CMD` (parsed out of it, so the test cannot drift from the image) against a
small app with a slow route, sends SIGTERM (what `docker stop` sends) while a request is in flight, and checks:
the in-flight request completes with its real answer, nothing new is served from that moment (a single worker
refuses the connection; with several, the kernel may accept it and the worker resets it), and the process exits on
its own, by the signal it was sent, rather than being killed. A second test shows the drain is bounded: a request that outlasts
UVICORN_GRACEFUL_SHUTDOWN_SECONDS is cut and the process still exits.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import httpx
import pytest

SMO_ROOT = Path(__file__).resolve().parents[1]

APP = textwrap.dedent('''
    import time
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/slow")
    def slow(seconds: float):
        time.sleep(seconds)
        return {"slept": seconds}
''')


def dockerfile_command() -> str:
    """The shell command string of the Dockerfile's `CMD ["sh", "-c", "..."]`."""
    line = next(l for l in (SMO_ROOT / "Dockerfile").read_text().splitlines() if l.startswith("CMD "))
    argv = json.loads(line[len("CMD "):])
    assert argv[:2] == ["sh", "-c"], f"the CMD must run through sh so the settings are read from the environment: {argv}"
    return argv[2]


def free_port() -> int:
    """A TCP port on 127.0.0.1 that was free a moment ago."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def service(tmp_path):
    """A factory fixture: `start(workers, grace_seconds)` runs the Dockerfile's own CMD (loopback, a free port) against a small app with a slow
    route, waits until it answers and returns (process, base URL); every process is stopped after the test.
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text(APP)
    processes = []

    def start(workers: int, grace_seconds: int):
        port = free_port()
        command = dockerfile_command().replace("--port 8000", f"--port {port}").replace("0.0.0.0", "127.0.0.1")
        env = {**os.environ, "PYTHONPATH": f"{tmp_path}{os.pathsep}{SMO_ROOT / 'shared'}", "UVICORN_WORKERS": str(workers),
               "UVICORN_GRACEFUL_SHUTDOWN_SECONDS": str(grace_seconds)}
        process = subprocess.Popen(["sh", "-c", command], cwd=tmp_path, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(f"{base}/health", timeout=1.0).status_code == 200:
                    return process, base
            except httpx.HTTPError:
                time.sleep(0.1)
        raise AssertionError("the service did not start")

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()


def request_in_background(url: str):
    """Starts a GET in a thread and returns (thread, outcome) where the outcome dict receives `response` or `error` when it ends."""
    outcome = {}

    def run():
        try:
            outcome["response"] = httpx.get(url, timeout=30.0)
        except httpx.HTTPError as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("workers", [1, 2])
def test_sigterm_lets_the_inflight_request_finish_refuses_new_ones_and_exits_cleanly(service, workers):
    """On SIGTERM the request in flight completes with its real answer, new requests stop being served within 2 seconds, and the process exits
    cleanly, with one worker and with several.
    """
    process, base = service(workers=workers, grace_seconds=15)
    thread, outcome = request_in_background(f"{base}/slow?seconds=3")
    time.sleep(0.7)                                           # the request is now running on the server
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)

    # nothing new is served: refused, or accepted by the kernel and reset. Not at a fixed instant: with several workers uvicorn's
    # supervisor looks at the stop flag every half second before it signals them, so a probe at exactly 0.5 s could still be answered
    # (the flake of this test on a loaded runner). The request in flight keeps the drain open for about 2 s after the signal.
    deadline = time.monotonic() + 2.0
    while True:
        try:
            httpx.get(f"{base}/health", timeout=1.0)
        except httpx.TransportError:
            break
        assert time.monotonic() < deadline, "the service was still answering new requests 2 s after SIGTERM"
        time.sleep(0.1)

    thread.join(timeout=20)
    assert "error" not in outcome, outcome.get("error")
    assert outcome["response"].status_code == 200 and outcome["response"].json() == {"slept": 3.0}
    # exited on its own, having finished the request: uvicorn re-raises the stop signal once it has drained, so the
    # status is "terminated by SIGTERM" (143 in a container), the normal result of `docker stop`; a kill would be -9
    assert process.wait(timeout=20) in (0, -signal.SIGTERM)
    assert time.monotonic() - started >= 1.5                  # it waited for the request rather than exiting at once


def test_the_drain_is_bounded_by_the_graceful_shutdown_setting(service):
    """A request longer than the graceful-shutdown setting is cut and the process exits long before it would have finished, so a stuck request
    cannot hold a stopping service.
    """
    process, base = service(workers=1, grace_seconds=1)
    thread, outcome = request_in_background(f"{base}/slow?seconds=12")
    time.sleep(0.7)
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)

    process.wait(timeout=10)                                  # gone long before the 12 s request would have finished
    assert time.monotonic() - started < 8
    thread.join(timeout=20)
    assert "response" not in outcome or outcome["response"].status_code >= 500   # the straggler was cut, not answered


def test_the_dockerfile_command_reads_its_settings_from_the_environment_and_execs_uvicorn():
    """The Dockerfile command takes workers and the drain time from the environment, runs uvicorn as PID 1 (only the mTLS option step comes before
    it) and every compose `stop_grace_period` is longer than the drain, so docker's SIGKILL never lands first.
    """
    command = dockerfile_command()
    # PR-SEC-2: the only thing ahead of `exec uvicorn` is the mTLS options (and an exit when they cannot be made); uvicorn must still be PID 1
    assert command.startswith("tls=$(python -m smo_shared.mtls uvicorn-args) || exit 1; exec uvicorn app.main:app") and command.count("exec ") == 1
    assert "--workers ${UVICORN_WORKERS}" in command and "--timeout-graceful-shutdown ${UVICORN_GRACEFUL_SHUTDOWN_SECONDS}" in command
    text = (SMO_ROOT / "Dockerfile").read_text()
    assert re.search(r"UVICORN_WORKERS=1", text) and re.search(r"UVICORN_GRACEFUL_SHUTDOWN_SECONDS=\d+", text)
    # compose gives every service of this image longer than the drain, so docker's SIGKILL never lands first
    grace = int(re.search(r"UVICORN_GRACEFUL_SHUTDOWN_SECONDS=(\d+)", text).group(1))
    compose = (SMO_ROOT / "docker-compose.yml").read_text()
    periods = [int(m) for m in re.findall(r"stop_grace_period: (\d+)s", compose)]
    services = len(re.findall(r"^    build: \{ context: \., (?:additional_contexts: \{ specs: \.\./specs/5G_APIs \}, )?args: \{ <<: \*build_info, MODULE:", compose, flags=re.M))
    assert len(periods) == services and min(periods) > grace
