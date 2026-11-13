"""Provider registration, flags and diagnostics."""

from __future__ import annotations

import pytest

from conftest import requires_hermes

pytestmark = requires_hermes


class FakeContext:
    def __init__(self) -> None:
        self.terminal_providers = []

    def register_terminal_environment_provider(self, provider) -> None:
        self.terminal_providers.append(provider)


def make_provider():
    from hermes_sandbox.provider import SandboxProvider

    return SandboxProvider()


def test_register_exposes_one_sandbox_backend():
    import hermes_sandbox

    ctx = FakeContext()
    hermes_sandbox.register(ctx)
    assert len(ctx.terminal_providers) == 1
    provider = ctx.terminal_providers[0]
    assert provider.name == "sandbox"
    assert provider.display_name == "x402 Sandbox"


def test_registration_is_accepted_by_the_real_registry_rules():
    """The core rejects collisions with built-ins; 'sandbox' is not one."""
    from tools.terminal_tool_backends import _ENV_BUILDERS

    assert "sandbox" not in _ENV_BUILDERS


def test_classification_flags():
    provider = make_provider()
    assert provider.is_remote is True
    assert provider.is_container is True
    # Isolation is not permission. The approval layer stays on.
    assert provider.skip_container_guards is False
    assert provider.default_cwd == "/workspace"
    assert provider.session_isolated_when_nonpersistent is True
    assert provider.supports_streams is False


def test_credentials_are_stripped_from_spawned_subprocesses():
    stripped = make_provider().strip_env_keys
    for name in ("GRAPHN_API_KEY", "OPENAI_API_KEY", "CONTROLLER_ADMIN_TOKEN", "SIGNAL_ACCOUNT"):
        assert name in stripped


def test_is_available_is_offline(monkeypatch):
    """A picker paint must not touch the network."""

    def explode(*_args, **_kwargs):
        raise AssertionError("is_available() made a network call")

    monkeypatch.setattr("hermes_sandbox.provider.SandboxClient", explode)
    monkeypatch.setattr("hermes_sandbox.environment.SandboxClient", explode)
    assert make_provider().is_available() is True


def test_is_available_false_when_misconfigured(monkeypatch):
    monkeypatch.setenv("SANDBOX_TIER", "enormous")
    provider = make_provider()
    assert provider.is_available() is False
    status, detail = provider.probe()
    assert status == "needs_setup"
    assert "SANDBOX_TIER" in detail


def test_create_environment_rejects_bad_config(monkeypatch):
    from hermes_sandbox.errors import SandboxError

    monkeypatch.setenv("SANDBOX_API_URL", "not-a-url")
    with pytest.raises(SandboxError):
        make_provider().create_environment(cwd="/workspace", timeout=30, task_id="t")


def test_create_environment_honours_a_per_task_image(monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGE", "guest")
    env = make_provider().create_environment(cwd="/", timeout=30, task_id="t", image="developer")
    # The API has no image field, so this is a label the provider validates
    # separately; it must not be silently dropped or silently substituted.
    assert env.requested_image == "developer"
    assert env.default_cwd if hasattr(env, "default_cwd") else True


def test_doctor_checks_report_an_unknown_image(monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGE", "math-lean")
    provider = make_provider()

    class StubClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def list_images(self):
            return [{"id": "guest", "digest": "sha256:abc", "current": True}]

        def close(self):
            pass

    monkeypatch.setattr("hermes_sandbox.provider.SandboxClient", StubClient)
    rows = provider.doctor_checks()
    image_rows = [row for row in rows if row[1] == "sandbox image"]
    assert image_rows and image_rows[0][0] is False
    assert "guest" in image_rows[0][2]
