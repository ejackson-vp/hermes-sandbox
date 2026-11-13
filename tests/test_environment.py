"""Environment behaviour through the real BaseEnvironment.

These run the actual Hermes ``execute()`` path (snapshot bootstrap, drain thread,
``_ThreadedProcessHandle``) against a fake API client, so they test the seam that
matters rather than a mock of it.
"""

from __future__ import annotations

import subprocess

import pytest

from conftest import requires_hermes
from fake_client import FakeClient

pytestmark = requires_hermes


def make_env(client: FakeClient, **overrides):
    from hermes_sandbox.config import SandboxConfig
    from hermes_sandbox.environment import SandboxEnvironment

    config = SandboxConfig.from_env({"SANDBOX_CARRY_WORKSPACE": "false", **overrides})
    env = SandboxEnvironment(
        config=config,
        cwd="/workspace",
        timeout=30,
        task_id="test",
        client=client,
    )
    # Most tests are about command semantics, not the login-shell snapshot.
    # Pretend the snapshot already exists so one command is one round trip;
    # test_bootstrap_runs_once_per_environment covers the real path.
    env._bootstrapped = True
    env._snapshot_ready = True
    return env


def test_execute_runs_in_the_sandbox_and_returns_a_returncode():
    from hermes_sandbox.client import ExecResult

    client = FakeClient(results=[ExecResult(ok=True, exit_code=0, stdout="hello\n")])
    env = make_env(client)
    try:
        result = env.execute("echo hello")
    finally:
        env.cleanup()
    # The contract core reads is `returncode`, not `exit_code`.
    assert result["returncode"] == 0
    assert "hello" in result["output"]
    assert client.execs, "no command reached the sandbox client"


def test_nonzero_exit_code_is_preserved():
    from hermes_sandbox.client import ExecResult

    client = FakeClient(results=[ExecResult(ok=True, exit_code=42, stdout="bad\n")])
    env = make_env(client)
    try:
        result = env.execute("exit 42")
    finally:
        env.cleanup()
    assert result["returncode"] == 42
    assert "bad" in result["output"]


def test_timeout_reports_a_timeout_and_a_nonzero_code():
    from hermes_sandbox.client import ExecResult

    client = FakeClient(results=[ExecResult(ok=True, exit_code=0, timed_out=True, duration_ms=1000)])
    env = make_env(client)
    try:
        result = env.execute("sleep 999")
    finally:
        env.cleanup()
    assert result["returncode"] == 124
    assert "timed out" in result["output"]


def test_guest_error_is_not_reported_as_success():
    from hermes_sandbox.client import ExecResult

    client = FakeClient(
        results=[ExecResult(ok=False, exit_code=None, error_code="path_outside_workspace", error_message="nope")]
    )
    env = make_env(client)
    try:
        result = env.execute("cat /etc/shadow")
    finally:
        env.cleanup()
    assert result["returncode"] != 0
    assert "path_outside_workspace" in result["output"]


def test_unreachable_api_fails_and_never_falls_back_to_the_host(monkeypatch):
    """The single most important property of this backend."""

    def explode(*_args, **_kwargs):
        raise AssertionError("a provider command tried to spawn a host process")

    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "run", explode)

    client = FakeClient(create_error=RuntimeError("connection refused"))
    env = make_env(client)
    try:
        result = env.execute("echo hi")
    finally:
        env.cleanup()
    assert result["returncode"] == 125
    assert "did not run" in result["output"]
    assert "nothing was executed on the host" in result["output"]


def test_commands_reuse_one_session():
    client = FakeClient()
    env = make_env(client)
    try:
        env.execute("true")
        env.execute("true")
    finally:
        env.cleanup()
    assert client.created == ["sbx_001"]


def test_cwd_is_passed_through():
    client = FakeClient()
    env = make_env(client)
    try:
        env.execute("pwd")
    finally:
        env.cleanup()
    assert client.execs[-1]["cwd"] == "/workspace"


def test_host_environment_is_never_forwarded(monkeypatch):
    monkeypatch.setenv("GRAPHN_API_KEY", "gn_secret")
    monkeypatch.setenv("SIGNAL_ACCOUNT", "+15551234567")
    client = FakeClient()
    env = make_env(client)
    try:
        env.execute("env")
    finally:
        env.cleanup()
    for call in client.execs:
        assert call["env"] is None


def test_cleanup_destroys_the_session_it_created():
    client = FakeClient()
    env = make_env(client)
    env.execute("true")
    env.cleanup()
    assert client.destroyed == ["sbx_001"]


def test_cleanup_is_idempotent():
    client = FakeClient()
    env = make_env(client)
    env.execute("true")
    env.cleanup()
    env.cleanup()
    assert client.destroyed == ["sbx_001"]


def test_expired_lease_rolls_over_and_carries_the_workspace():
    from hermes_sandbox.client import ExecResult
    from hermes_sandbox.config import SandboxConfig
    from hermes_sandbox.environment import SandboxEnvironment

    client = FakeClient(results=[ExecResult(ok=True, exit_code=0)])
    config = SandboxConfig.from_env({"SANDBOX_CARRY_WORKSPACE": "true"})
    env = SandboxEnvironment(config=config, cwd="/workspace", timeout=30, task_id="test", client=client)
    env._bootstrapped = True
    env._snapshot_ready = True
    try:
        env.execute("true")
        first = env.session_id
        # Force the rollover the way an expiring lease would.
        env._session.lease_expires_at = "2000-01-01T00:00:00.000Z"
        env.execute("true")
    finally:
        env.cleanup()
    assert client.created == ["sbx_001", "sbx_002"]
    assert first == "sbx_001"
    assert env.generation == 1
    assert client.puts == [("sbx_002", "/workspace", len(b"workspace-archive"))]


def test_rollover_without_carry_does_not_capture():
    from hermes_sandbox.client import ExecResult

    client = FakeClient(results=[ExecResult(ok=True, exit_code=0)], archive=None)
    env = make_env(client)  # carry disabled
    try:
        env.execute("true")
        env._session.lease_expires_at = "2000-01-01T00:00:00.000Z"
        env.execute("true")
    finally:
        env.cleanup()
    assert client.puts == []
    assert len(client.created) == 2


def test_expiry_mid_command_retries_once_in_a_new_session():
    from hermes_sandbox.client import ExecResult

    client = FakeClient(
        results=[ExecResult(ok=True, exit_code=0, stdout="second\n")],
        fail_on_exec=1,
    )
    env = make_env(client)
    try:
        result = env.execute("long-job")
    finally:
        env.cleanup()
    assert result["returncode"] == 0
    assert "second" in result["output"]
    assert len(client.created) == 2


def test_describe_exposes_diagnostics():
    client = FakeClient()
    env = make_env(client)
    try:
        env.execute("true")
        info = env.describe()
    finally:
        env.cleanup()
    assert info["session_id"] == "sbx_001"
    assert info["tier"] == "standard"
    assert info["generation"] == 0


def test_bootstrap_runs_once_per_environment():
    from hermes_sandbox.client import ExecResult

    client = FakeClient(results=[ExecResult(ok=True, exit_code=0)])
    env = make_env(client)
    env._bootstrapped = False
    env._snapshot_ready = False
    try:
        env.execute("true")
        env.execute("true")
    finally:
        env.cleanup()
    # One bootstrap command plus the two real ones, and no repeat on the second.
    assert len(client.execs) == 3
    assert env._bootstrapped is True
