"""End-to-end tests against a real sandbox API.

Skipped unless ``SANDBOX_API_URL`` is set. These are the tests that prove the
payment handshake, the sealed envelope and the provider's command path against
the real service; everything else runs offline.

    kubectl -n sandbox-system port-forward pod/<api-pod> 18081:3000
    SANDBOX_API_URL=http://127.0.0.1:18081 pytest tests/integration -v
"""

from __future__ import annotations

import os
import socket

import pytest

from conftest import requires_hermes

API_URL = os.environ.get("SANDBOX_API_URL", "")

pytestmark = [
    pytest.mark.skipif(not API_URL, reason="set SANDBOX_API_URL to run live tests"),
    requires_hermes,
]


@pytest.fixture()
def client():
    from hermes_sandbox.client import SandboxClient

    with SandboxClient(API_URL, timeout_seconds=120) as c:
        yield c


def test_lists_the_pinned_image(client):
    images = client.list_images()
    assert images, "the deployment advertised no images"
    assert any(image.get("id") for image in images)


def test_create_exec_destroy(client):
    session = client.create_session(tier="standard")
    try:
        client.wait_for_ready(session, timeout_seconds=120)
        result = client.exec(session, ["bash", "-c", "echo hello-from-guest"])
        assert result.ok
        assert result.exit_code == 0
        assert "hello-from-guest" in result.stdout
    finally:
        client.destroy(session)


def test_execution_is_in_the_guest_not_the_host(client):
    """The command must see the sandbox, not this machine."""
    session = client.create_session(tier="standard")
    try:
        client.wait_for_ready(session, timeout_seconds=120)
        result = client.exec(session, ["bash", "-c", "hostname; uname -s"])
        assert result.ok
        assert result.stdout.strip().splitlines()[0] != socket.gethostname()
        assert session.session_id.replace("sbx_", "") in result.stdout
    finally:
        client.destroy(session)


def test_nonzero_exit_code_and_streams(client):
    session = client.create_session(tier="standard")
    try:
        client.wait_for_ready(session, timeout_seconds=120)
        result = client.exec(session, ["bash", "-c", "echo out; echo err >&2; exit 42"])
        assert result.ok is True
        assert result.exit_code == 42
        assert "out" in result.stdout
        assert "err" in result.stderr
    finally:
        client.destroy(session)


def test_file_round_trip_through_sealed_envelopes(client):
    session = client.create_session(tier="standard")
    try:
        client.wait_for_ready(session, timeout_seconds=120)
        payload = b"sealed bytes\n"
        client.put_file(session, "/workspace/round-trip.txt", payload)
        assert client.get_file(session, "/workspace/round-trip.txt") == payload
        entries = client.list_files(session, "/workspace")
        assert any(entry.get("path", "").endswith("round-trip.txt") for entry in entries)
    finally:
        client.destroy(session)


def test_cwd_is_confined_to_the_workspace(client):
    session = client.create_session(tier="standard")
    try:
        client.wait_for_ready(session, timeout_seconds=120)
        result = client.exec(session, ["bash", "-c", "pwd"], cwd="/workspace")
        assert result.ok
        assert "/workspace" in result.stdout
        outside = client.exec(session, ["bash", "-c", "pwd"], cwd="/etc")
        assert outside.ok is False
        # The guest distinguishes an unusable cwd from a path escaping the
        # workspace; either is a refusal, and neither may look like success.
        assert outside.error_code in ("invalid_cwd", "path_outside_workspace")
    finally:
        client.destroy(session)


def test_provider_runs_a_command_in_a_microvm():
    from hermes_sandbox.config import SandboxConfig
    from hermes_sandbox.environment import SandboxEnvironment

    config = SandboxConfig.from_env({"SANDBOX_API_URL": API_URL})
    env = SandboxEnvironment(config=config, cwd="/workspace", timeout=60, task_id="integration")
    try:
        result = env.execute("python3 -c \"import socket;print(socket.gethostname())\"")
        assert result["returncode"] == 0
        assert socket.gethostname() not in result["output"]
        assert env.session_id is not None
        # Exit codes survive the trip through the provider.
        assert env.execute("exit 7")["returncode"] == 7
    finally:
        env.cleanup()


def test_cleanup_releases_the_lease(client):
    session = client.create_session(tier="standard")
    client.wait_for_ready(session, timeout_seconds=120)
    client.destroy(session)
    with pytest.raises(Exception):
        # Destroyed sessions must not accept work.
        client.exec(session, ["bash", "-c", "true"])
