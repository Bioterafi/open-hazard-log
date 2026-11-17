"""M4 done-when: under raw `uvicorn --host 0.0.0.0`, a foreign Host header gets 400 and a
non-loopback client gets 403 (OHL-H9). Runs a real server in a subprocess."""

import http.client
import os
import socket
import subprocess
import sys
import time

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _lan_address() -> str | None:
    """This computer's own non-loopback address, if it has one (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: only picks the outgoing interface
            addr = s.getsockname()[0]
    except OSError:
        return None
    return None if addr.startswith("127.") or addr == "0.0.0.0" else addr


def _get(addr: str, port: int, host: str) -> tuple[int, str]:
    c = http.client.HTTPConnection(addr, port, timeout=5)
    try:
        c.request("GET", "/", headers={"Host": host})
        r = c.getresponse()
        return r.status, r.read().decode()
    finally:
        c.close()


@pytest.fixture
def server(tmp_path):
    port = _free_port()
    env = dict(os.environ, OHL_DB=str(tmp_path / "server.db"))
    env.pop("OHL_USER", None)
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "ohl.app:app", "--host", "0.0.0.0", "--port", str(port)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if proc.poll() is not None:
                pytest.fail("uvicorn exited")
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("uvicorn did not start")
    yield port
    proc.terminate()
    proc.wait(timeout=10)


def test_raw_uvicorn_on_all_interfaces_still_only_answers_this_computer(server):
    port = server
    status, body = _get("127.0.0.1", port, f"127.0.0.1:{port}")
    assert status == 503 and "Who is using this?" in body       # reached the app (no CSO yet)
    status, body = _get("127.0.0.1", port, f"rebind.evil.example:{port}")
    assert status == 400 and "localhost" in body
    lan = _lan_address()
    if lan is None:
        pytest.skip("this computer has no non-loopback address to test from")
    status, body = _get(lan, port, f"127.0.0.1:{port}")
    assert status == 403 and "this computer" in body
